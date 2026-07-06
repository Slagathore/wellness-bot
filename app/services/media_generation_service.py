"""
Media Generation Service for Local AI Image/Video Generation

Supports multiple models with VRAM optimization for RTX 4070 12GB:
- Stable Diffusion XL
- FLUX.1-dev
- SDXL Turbo
- Playground v2.5
- PixArt-Σ
- z-image (FP8 distilled FLUX)
- Z-Image Q8 GGUF
- LTX2 Rapid Merges (text-to-video)
- Wan 1.3B NSFW (text-to-video, 10 epochs)

Features:
- Lazy model loading (only when needed)
- VRAM management (fp16, attention slicing, VAE slicing)
- Database tracking (generated_media table)
- Async generation (doesn't block other requests)
- Model unloading on demand
- Text-to-video generation with epoch selection
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import subprocess
import tempfile
import time
import uuid
from contextlib import ExitStack
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from app.config import settings
from app.infra.db.session import db_rw

logger = logging.getLogger(__name__)

# HuggingFace cache root for local model discovery
_HF_CACHE = Path.home() / ".cache" / "huggingface" / "hub"
# Local SDXL model set - share a common loader path, each has its own scheduler/VAE config
_LOCAL_SDXL_MODELS: frozenset[str] = frozenset({"pony-xl-v6", "wai-illustrious-xl", "unholy-desire-v7"})

# LoRA storage root.  Sub-dirs: sdxl/, pony/, illustrious/
# All three local SDXL models share "sdxl/" LoRAs; pony and illustrious also
# have their own family dirs for character/style LoRAs specific to those bases.
LORA_BASE_DIR: Path = Path.home() / ".cache" / "imagegen" / "loras"

# AnimateDiff SDXL motion module — auto-downloaded from HF on first use if absent
MOTION_MODULE_DIR: Path = Path.home() / ".cache" / "imagegen" / "motion_modules"
MOTION_MODULE_FILENAME: str = "mm_sdxl_v10_beta.ckpt"

# Klein server URL used for cross-process VRAM coordination
_KLEIN_SERVER_URL: str = "http://127.0.0.1:7865"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_LEGACY_REPO_TEMP_ROOT = _REPO_ROOT / "wellness_data" / "tmp"


def _default_temp_root() -> Path:
    """Return a user-level temp/cache root outside the repository."""

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "wellness-bot" / "tmp"

    xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache_home:
        return Path(xdg_cache_home) / "wellness-bot" / "tmp"

    return Path.home() / ".cache" / "wellness-bot" / "tmp"


def _is_legacy_repo_temp_root(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        resolved = path.expanduser().resolve(strict=False)
        legacy = _LEGACY_REPO_TEMP_ROOT.resolve(strict=False)
    except Exception:
        return False
    return resolved == legacy or legacy in resolved.parents


def _configure_process_tempdir() -> str:
    """Pin Python temp usage to a user-owned directory outside the repo.

    Some Windows environments have a broken system TEMP/TMP configuration where
    Python can see the directory but cannot use it for tempfile resolution.
    Media backends like diffusers/transformers/tensorflow touch tempfile during
    import, so we override the temp root before importing them.
    """

    fallback_root = _default_temp_root()
    configured = os.environ.get("WELLNESS_TEMP_DIR")
    configured_path = Path(configured).expanduser() if configured else None
    if _is_legacy_repo_temp_root(configured_path):
        logger.warning(
            "Ignoring legacy repo-local WELLNESS_TEMP_DIR=%s; using %s instead",
            configured_path,
            fallback_root,
        )
        configured_path = None
    temp_root = configured_path if configured_path is not None else fallback_root
    temp_root = temp_root.expanduser()
    try:
        temp_root.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("Unable to create temp directory %s: %s", temp_root, exc)

    resolved = str(temp_root.resolve(strict=False))
    os.environ["WELLNESS_TEMP_DIR"] = resolved
    os.environ["TEMP"] = resolved
    os.environ["TMP"] = resolved
    os.environ["TMPDIR"] = resolved
    tempfile.tempdir = resolved
    return resolved


_MEDIA_TEMP_DIR = _configure_process_tempdir()

# Keep transformers/diffusers on the PyTorch path only.
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
os.environ.setdefault("USE_TF", "0")

# Try importing torch/diffusers, gracefully handle optional-backend failures.
# These imports can fail with RuntimeError/OSError as well, not just ImportError.
TORCH_IMPORT_ERROR: str | None = None
try:
    import torch  # type: ignore[reportMissingImports]
    from diffusers import (  # type: ignore[reportMissingImports]
        DiffusionPipeline, DPMSolverMultistepScheduler,
        StableDiffusionXLPipeline)
    try:
        from diffusers import (  # type: ignore[reportMissingImports]
            GGUFQuantizationConfig, ZImagePipeline, ZImageTransformer2DModel)
    except Exception:
        GGUFQuantizationConfig = None
        ZImagePipeline = None
        ZImageTransformer2DModel = None

    TORCH_AVAILABLE = True
except Exception as exc:
    torch = None
    StableDiffusionXLPipeline = None
    DiffusionPipeline = None
    DPMSolverMultistepScheduler = None
    GGUFQuantizationConfig = None
    ZImagePipeline = None
    ZImageTransformer2DModel = None
    TORCH_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    logger.warning(
        "torch/diffusers unavailable - media generation disabled: %s",
        TORCH_IMPORT_ERROR,
    )
    TORCH_AVAILABLE = False
else:
    logger.info("Media backend temp directory set to %s", _MEDIA_TEMP_DIR)


def _media_backend_error() -> str:
    return TORCH_IMPORT_ERROR or "torch/diffusers not installed"


class MediaGenerationService:
    """Handles AI image/video generation with VRAM management"""

    # Supported models and their configurations
    SUPPORTED_MODELS: Dict[str, Dict[str, Any]] = {
        "flux2-klein": {
            "name": "FLUX.2 Klein 9B GGUF (Local API)",
            "model_id": "black-forest-labs/FLUX.2-klein-9B",
            "media_type": "image",
            "provider": "flux2_klein",
            "vram_gb": 10.0,
            "generation_time_s": 60,
            "max_resolution": 2048,
            "default_steps": 4,
            "default_guidance": 4.0,
        },
        "sdxl": {
            "name": "Stable Diffusion XL",
            "model_id": "stabilityai/stable-diffusion-xl-base-1.0",
            "media_type": "image",
            "vram_gb": 8.0,
            "generation_time_s": 45,
            "max_resolution": 1024,
        },
        "sdxl-turbo": {
            "name": "SDXL Turbo",
            "model_id": "stabilityai/sdxl-turbo",
            "media_type": "image",
            "vram_gb": 6.0,
            "generation_time_s": 20,
            "max_resolution": 1024,
        },
        "flux": {
            "name": "FLUX.1-dev",
            "model_id": "black-forest-labs/FLUX.1-dev",
            "media_type": "image",
            "vram_gb": 10.0,
            "generation_time_s": 90,
            "max_resolution": 1024,
        },
        "z-image-fp8": {
            "name": "z-image FP8 Distilled",
            "model_id": "black-forest-labs/FLUX.1-dev",
            "media_type": "image",
            "local_weights": "models--z-image-fp8-e4m3fn/RedZDX-ZIB-Distilled-nocfg-10steps-fp8-e4m3fn-Diffusion-models.safetensors",
            "vram_gb": 8.0,
            "generation_time_s": 25,
            "max_resolution": 1024,
            "default_steps": 10,
            "default_guidance": 1.0,
        },
        "z-image-q8-gguf": {
            "name": "Z-Image Q8 GGUF",
            "model_id": "Tongyi-MAI/Z-Image",
            "media_type": "image",
            "local_weights": "models--z-image-q8-gguf/z-image-Q8_0.gguf",
            "vram_gb": 8.0,
            "generation_time_s": 420,
            "max_resolution": 2048,
            "default_steps": 32,
            "default_guidance": 4.0,
            "pipeline_call_kwargs": {"cfg_normalization": False},
        },
        "easydiffusion": {
            "name": "EasyDiffusion (Local API)",
            "model_id": "local/easydiffusion",
            "media_type": "image",
            "provider": "easydiffusion",
            "vram_gb": 0.0,
            "generation_time_s": 40,
            "max_resolution": 2048,
            "default_steps": 28,
            "default_guidance": 7.0,
        },
        "perchance": {
            "name": "Perchance",
            "model_id": "perchance/api",
            "media_type": "image",
            "provider": "perchance",
            "vram_gb": 0.0,
            "generation_time_s": 45,
            "max_resolution": 1536,
            "default_steps": 20,
            "default_guidance": 7.0,
        },
        "perchance_other": {
            "name": "Perchance Other (URL API)",
            "model_id": "perchance/imageapi",
            "media_type": "image",
            "provider": "perchance_other",
            "vram_gb": 0.0,
            "generation_time_s": 20,
            "max_resolution": 1536,
            "default_steps": 20,
            "default_guidance": 7.0,
        },
        "playground": {
            "name": "Playground v2.5",
            "model_id": "playgroundai/playground-v2.5-1024px-aesthetic",
            "media_type": "image",
            "vram_gb": 8.0,
            "generation_time_s": 50,
            "max_resolution": 1024,
        },
        "pixart": {
            "name": "PixArt-Σ",
            "model_id": "PixArt-alpha/PixArt-Sigma-XL-2-1024-MS",
            "media_type": "image",
            "vram_gb": 7.0,
            "generation_time_s": 35,
            "max_resolution": 2048,
        },
        "ltx2": {
            "name": "LTX2 Rapid Merges",
            "model_id": "Phr00t/LTX2-Rapid-Merges",
            "media_type": "video",
            "vram_gb": 10.0,
            "generation_time_s": 120,
            "max_resolution": 768,
            "default_width": 768,
            "default_height": 512,
            "default_steps": 30,
            "default_guidance": 7.5,
            "default_frames": 49,
            "default_fps": 24,
        },
        "wan-t2v": {
            "name": "Wan 1.3B NSFW (T2V)",
            "model_id": "wan1.3NSFW-t2v",
            "media_type": "video",
            "local_dir": "models--wan1.3NSFW-t2v",
            "vram_gb": 8.0,
            "generation_time_s": 180,
            "max_resolution": 480,
            "default_width": 480,
            "default_height": 320,
            "default_steps": 30,
            "default_guidance": 7.5,
            "default_frames": 33,
            "default_fps": 16,
            "epochs": list(range(1, 11)),
            "default_epoch": 10,
        },
        # ── Local SDXL safetensors models ──────────────────────────────────────
        # All three share _load_local_sdxl() — config differences live here.
        # "local_safetensors" / "local_vae" are absolute paths on this machine.
        # "scheduler": "euler_a" | "dpm++2m_karras" | "euler"
        # "clip_skip": applied via cross_attention_kwargs at call time
        # "auto_prompt_prefix/suffix" / "auto_negative": injected by _format_prompt()
        # "lora_families": which sub-dirs under LORA_BASE_DIR this model accepts
        # "supports_animatediff": whether AnimateDiffSDXLPipeline wrapping is valid
        # "hires_upscale_default": auto-apply hires upscale pass (wai only)
        "pony-xl-v6": {
            "name": "Pony Diffusion XL v6",
            "model_id": "local/pony-xl-v6",
            "local_safetensors": str(
                Path.home() / ".cache" / "huggingface" / "hub"
                / "ponyDiffusionV6XL_v6StartWithThisOne.safetensors"
            ),
            "local_vae": str(
                Path.home() / ".cache" / "huggingface" / "hub"
                / "sdxl_vae.safetensors"
            ),
            "scheduler": "euler_a",
            "clip_skip": 2,
            "media_type": "image",
            "vram_gb": 7.0,
            "generation_time_s": 45,
            "max_resolution": 1024,
            "default_steps": 25,
            "default_guidance": 7.0,
            "lora_families": ["sdxl", "pony"],
            "supports_animatediff": True,
            # Pony prefix: quality score tags required at the START of the prompt.
            # Suffix: always append rating_explicit (user-selected rating still respected).
            "auto_prompt_prefix": "score_9, score_8_up, score_7_up, score_6_up, score_5_up, score_4_up,",
            "auto_prompt_suffix": "rating_explicit",
            "auto_negative": "",
        },
        "wai-illustrious-xl": {
            "name": "WAI Illustrious SDXL v1.60",
            "model_id": "local/wai-illustrious-xl",
            "local_safetensors": str(
                Path.home() / ".cache" / "huggingface" / "hub"
                / "waiIllustriousSDXL_v160.safetensors"
            ),
            # VAE is baked into the checkpoint — no local_vae key here.
            "scheduler": "euler_a",
            "clip_skip": 1,
            "media_type": "image",
            "vram_gb": 7.0,
            "generation_time_s": 60,
            "max_resolution": 1536,
            "default_steps": 20,
            "default_guidance": 6.0,
            "lora_families": ["sdxl", "illustrious"],
            "supports_animatediff": True,
            # Quality prefix always prepended; suffix forces nsfw+explicit as intended.
            "auto_prompt_prefix": "masterpiece, best quality, amazing quality,",
            "auto_prompt_suffix": "nsfw, explicit",
            "auto_negative": "bad quality,worst quality,worst detail,sketch,censor,",
            # Auto hires-upscale pass at 1.5× (1024→1536) after initial generation.
            "hires_upscale_default": True,
        },
        "unholy-desire-v7": {
            "name": "Unholy Desire Mix Sinister v7.0",
            "model_id": "local/unholy-desire-v7",
            "local_safetensors": str(
                Path.home() / ".cache" / "huggingface" / "hub"
                / "unholyDesireMixSinister_v70.safetensors"
            ),
            "scheduler": "dpm++2m_karras",
            "clip_skip": 1,
            "media_type": "image",
            "vram_gb": 7.0,
            "generation_time_s": 30,
            "max_resolution": 1024,
            "default_steps": 20,
            "default_guidance": 3.5,
            "lora_families": ["sdxl"],
            "supports_animatediff": False,
            "auto_prompt_prefix": (
                "masterpiece, best quality, amazing quality, very aesthetic, absurdres,"
                " ultra detailed face, ultra detailed eyes,"
            ),
            "auto_prompt_suffix": "",
            "auto_negative": (
                "bad quality,worst quality,worst detail,sketch,censor,"
                "extra limbs,deformed fingers,bad anatomy,mutated body,lowres,"
                "worst quality,low quality,low score,bad score,blurry,text,ugly,"
                "hooded eyes,watermark,pale,bad hands,bad anatomy,bad proportions,"
                "poorly drawn face,poorly drawn hand,missing finger,extra limbs,"
                "blurry,pixelated,distorted,lowres,jpeg artifacts,watermark,signature,"
                "text,(deformed:1.5),(bad hand:1.3),overexposed,underexposed,censored,"
                "mutated,extra finger,cloned face,bad eyes"
            ),
        },
    }

    def __init__(self, output_dir: Optional[Path] = None):
        self.output_dir = output_dir or Path("./wellness_data/generated_media")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._status_path = Path(settings().data_root) / "media_runtime_status.json"

        self.current_model: Optional[str] = None
        self.pipeline: Optional[Any] = None
        self.last_load_error: Optional[str] = None
        self.last_load_error_kind: Optional[str] = None

        # Check CUDA availability
        if TORCH_AVAILABLE:
            assert torch is not None
            self.cuda_available = torch.cuda.is_available()
            self.device = "cuda" if self.cuda_available else "cpu"
            if self.cuda_available:
                self.vram_total_gb = torch.cuda.get_device_properties(
                    0
                ).total_memory / (1024**3)
                logger.info(
                    f"CUDA available: {self.device}, VRAM: {self.vram_total_gb:.1f}GB"
                )
            else:
                logger.warning("CUDA not available - falling back to CPU (very slow)")
                self.vram_total_gb = 0
        else:
            self.cuda_available = False
            self.device = "cpu"
            self.vram_total_gb = 0
            logger.warning("torch not available - media generation disabled")

    def _write_runtime_status(self, **fields: Any) -> None:
        """Persist cross-process media runtime status for the admin server."""
        payload: Dict[str, Any] = {}
        if self._status_path.exists():
            try:
                payload = json.loads(self._status_path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
        for key, value in fields.items():
            if value is None:
                payload.pop(key, None)
            else:
                payload[key] = value
        payload["updated_at"] = datetime.utcnow().isoformat() + "Z"
        payload["pid"] = os.getpid()
        try:
            self._status_path.parent.mkdir(parents=True, exist_ok=True)
            self._status_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.debug("Failed to write media runtime status: %s", exc)

    def _read_runtime_status(self) -> Dict[str, Any]:
        if not self._status_path.exists():
            return {}
        try:
            raw = json.loads(self._status_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _query_gpu_device_stats() -> Dict[str, Any]:
        """Read device-wide GPU stats using nvidia-smi when available."""
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            first_line = (result.stdout or "").strip().splitlines()[0]
            util, mem_used, mem_total, temp = [part.strip() for part in first_line.split(",")]
            total_gb = round(float(mem_total) / 1024, 2)
            used_gb = round(float(mem_used) / 1024, 2)
            return {
                "device_util_percent": float(util),
                "device_used_gb": used_gb,
                "device_total_gb": total_gb,
                "device_free_gb": round(max(total_gb - used_gb, 0.0), 2),
                "device_percent_used": round((used_gb / total_gb) * 100, 1) if total_gb else 0.0,
                "device_temp_c": float(temp),
            }
        except Exception:
            return {}

    @staticmethod
    def _repo_snapshot_has_required_files(model_id: str, required_globs: List[str]) -> bool:
        """Return True only when a local HF snapshot includes the requested files."""
        snapshot_root = _HF_CACHE / f"models--{model_id.replace('/', '--')}" / "snapshots"
        if not snapshot_root.exists():
            return False
        try:
            snapshots = [path for path in snapshot_root.iterdir() if path.is_dir()]
        except Exception:
            return False
        for snapshot in snapshots:
            if all(any(snapshot.glob(pattern)) for pattern in required_globs):
                return True
        return False

    def _free_vram_gb(self) -> float:
        """Return currently-free VRAM in GB.  Returns 0 when CUDA is unavailable."""
        if not self.cuda_available or torch is None:
            return 0.0
        total = torch.cuda.get_device_properties(0).total_memory
        used = torch.cuda.memory_allocated(0)
        return max((total - used) / (1024 ** 3), 0.0)

    def _needs_cpu_offload(self, model_vram_gb: float) -> bool:
        """Return True only when the model's VRAM requirement exceeds what is currently free.

        On a 12 GB card with nothing else loaded this should nearly always be False,
        meaning models run fully on GPU.  It becomes True when Klein (or another model)
        occupies VRAM and has not yet been unloaded.
        """
        if not self.cuda_available:
            return False
        return self._free_vram_gb() < float(model_vram_gb)

    def _signal_klein_unload(self) -> None:
        """Tell the Klein FastAPI process to drop its pipeline and free VRAM.

        Called synchronously before loading any local SDXL model.  Silently
        no-ops when Klein is not running (e.g. in tests or when the service
        was never started).
        """
        try:
            import requests as _req
            health_resp = _req.get(f"{_KLEIN_SERVER_URL}/health", timeout=3)
            if health_resp.status_code == 200 and health_resp.json().get("model_loaded"):
                _req.post(f"{_KLEIN_SERVER_URL}/unload", timeout=10)
                logger.info("Requested FLUX.2 Klein unload for VRAM headroom before local SDXL load")
        except Exception:
            pass  # Klein not running — normal during testing or standalone use

    def _set_scheduler(self, pipeline: Any, scheduler_key: str) -> None:
        """Swap the pipeline scheduler in-place based on a string key.

        Supported keys:
          "euler_a"        → EulerAncestralDiscreteScheduler  (default for Pony/Wai)
          "euler"          → EulerDiscreteScheduler
          "dpm++2m_karras" → DPMSolverMultistepScheduler with Karras sigmas + dpmsolver++
        """
        from diffusers.schedulers import (  # type: ignore[reportMissingImports]
            DPMSolverMultistepScheduler, EulerAncestralDiscreteScheduler,
            EulerDiscreteScheduler)
        sched_map: Dict[str, Any] = {
            "euler_a": EulerAncestralDiscreteScheduler,
            "euler": EulerDiscreteScheduler,
            "dpm++2m_karras": DPMSolverMultistepScheduler,
        }
        sched_cls = sched_map.get(scheduler_key, EulerAncestralDiscreteScheduler)
        extra_kwargs: Dict[str, Any] = {}
        if scheduler_key == "dpm++2m_karras":
            extra_kwargs = {"use_karras_sigmas": True, "algorithm_type": "dpmsolver++"}
        pipeline.scheduler = sched_cls.from_config(pipeline.scheduler.config, **extra_kwargs)
        logger.info(
            "Scheduler set to %s (%s)", scheduler_key, type(pipeline.scheduler).__name__
        )

    def get_available_models(self) -> List[Dict[str, Any]]:
        """Get list of available models with their specs"""
        models = []
        for key, info in self.SUPPORTED_MODELS.items():
            entry: Dict[str, Any] = {
                "model_key": key,
                "name": info["name"],
                "media_type": info.get("media_type", "image"),
                "vram_gb": info["vram_gb"],
                "generation_time_s": info["generation_time_s"],
                "max_resolution": info["max_resolution"],
                "available": info["vram_gb"] <= self.vram_total_gb
                or not self.cuda_available,
                "defaults": self.get_generation_defaults(key),
            }
            if info.get("epochs"):
                entry["epochs"] = info["epochs"]
                entry["default_epoch"] = info.get("default_epoch", info["epochs"][-1])
            models.append(entry)
        return models

    def get_generation_defaults(self, model_key: str = "flux2-klein") -> Dict[str, Any]:
        """Return UI defaults for the given model, regardless of media type."""
        model_info = self.SUPPORTED_MODELS.get(model_key, {})
        if model_info.get("media_type") == "video":
            defaults: Dict[str, Any] = {
                "width": int(model_info.get("default_width", model_info.get("max_resolution", 768)) or 768),
                "height": int(model_info.get("default_height", model_info.get("max_resolution", 768)) or 768),
                "num_inference_steps": int(model_info.get("default_steps", 30) or 30),
                "guidance_scale": float(model_info.get("default_guidance", 7.5) or 7.5),
                "num_frames": int(model_info.get("default_frames", 33) or 33),
                "fps": int(model_info.get("default_fps", 16) or 16),
            }
            if model_info.get("default_epoch") is not None:
                defaults["epoch"] = int(model_info["default_epoch"])
            return defaults
        return self.get_image_defaults(model_key)

    def get_image_defaults(self, model_key: str = "flux2-klein") -> Dict[str, Any]:
        """Return interactive defaults tuned for chat-driven image generation."""
        model_info = self.SUPPORTED_MODELS.get(model_key, {})
        max_resolution = int(model_info.get("max_resolution", 1024) or 1024)
        square_size = min(max_resolution, 768)
        defaults: Dict[str, Any] = {
            "width": square_size,
            "height": square_size,
            "num_inference_steps": int(model_info.get("default_steps", 20) or 20),
            "guidance_scale": float(model_info.get("default_guidance", 5.5) or 5.5),
        }
        if model_key == "flux2-klein":
            defaults.update({
                "width": 1024,
                "height": 1024,
                "num_inference_steps": 4,
                "guidance_scale": 4.0,
            })
        elif model_key == "sdxl":
            defaults.update({
                "width": 768,
                "height": 768,
                "num_inference_steps": 18,
                "guidance_scale": 6.5,
            })
        elif model_key == "sdxl-turbo":
            defaults.update({
                "width": 768,
                "height": 768,
                "num_inference_steps": 6,
                "guidance_scale": 0.0,
            })
        elif model_key == "z-image-fp8":
            defaults.update({
                "width": 768,
                "height": 768,
                "num_inference_steps": 10,
                "guidance_scale": 1.0,
            })
        elif model_key == "z-image-q8-gguf":
            defaults.update({
                "width": 768,
                "height": 768,
                "num_inference_steps": 32,
                "guidance_scale": 4.0,
            })
        elif model_key == "easydiffusion":
            defaults.update({
                "width": 768,
                "height": 768,
                "num_inference_steps": 28,
                "guidance_scale": 7.0,
            })
        elif model_key in {"perchance", "perchance_other"}:
            defaults.update({
                "width": 768,
                "height": 768,
                "num_inference_steps": 20,
                "guidance_scale": 7.0,
            })
        elif model_key == "pony-xl-v6":
            defaults.update({
                "width": 1024,
                "height": 1024,
                "num_inference_steps": 25,
                "guidance_scale": 7.0,
            })
        elif model_key == "wai-illustrious-xl":
            defaults.update({
                "width": 1024,
                "height": 1024,
                "num_inference_steps": 20,
                "guidance_scale": 6.0,
            })
        elif model_key == "unholy-desire-v7":
            defaults.update({
                "width": 1024,
                "height": 1024,
                "num_inference_steps": 20,
                "guidance_scale": 3.5,
            })
        return defaults

    def _should_use_memory_slicing(self, model_key: str, media_type: str) -> bool:
        """Trade memory for throughput only when we actually need it."""
        if not self.cuda_available:
            return False
        if media_type != "image":
            return True
        # On a 12 GB card, slicing tends to hurt interactive image latency
        # much more than it helps, especially on FLUX/z-image decode.
        if self.vram_total_gb >= 11.0 and model_key in {"sdxl", "sdxl-turbo", "z-image-fp8", "flux"}:
            return False
        return self.vram_total_gb < 10.0

    def _attach_image_smoke_trace(
        self,
        pipeline: Any,
        *,
        model_key: str,
        user_id: int,
        prompt_excerpt: str,
        width: int,
        height: int,
        num_inference_steps: int,
    ) -> tuple[ExitStack, Dict[str, Any], Dict[str, Any]]:
        """Attach temporary stage tracing around the diffusers image pipeline."""
        stack = ExitStack()
        trace: Dict[str, Any] = {
            "model": model_key,
            "user_id": user_id,
            "width": width,
            "height": height,
            "num_inference_steps": num_inference_steps,
            "hf_cache_root": str(_HF_CACHE),
            "trace_started_at": datetime.utcnow().isoformat() + "Z",
        }
        extra_call_kwargs: Dict[str, Any] = {}
        trace_started = time.perf_counter()
        milestone_steps = sorted({1, max(1, num_inference_steps // 2), num_inference_steps})
        logged_steps: set[int] = set()

        def _elapsed_ms() -> int:
            return int((time.perf_counter() - trace_started) * 1000)

        def _write_phase(phase: str) -> None:
            self._write_runtime_status(
                busy=True,
                phase=phase,
                current_model=model_key,
                last_prompt=prompt_excerpt,
                media_type="image",
                user_id=user_id,
                smoke_trace=trace,
            )

        def _log_event(event: str, **fields: Any) -> None:
            details = " ".join(f"{key}={value}" for key, value in fields.items())
            suffix = f" {details}" if details else ""
            logger.info(
                "[MEDIA-SMOKE] model=%s user=%s event=%s elapsed_ms=%d%s",
                model_key,
                user_id,
                event,
                _elapsed_ms(),
                suffix,
            )

        def _wrap_method(owner: Any, attr_name: str, phase_name: str, runtime_phase: str) -> None:
            if owner is None or not hasattr(owner, attr_name):
                return
            original = getattr(owner, attr_name, None)
            if not callable(original):
                return

            def wrapped(*args: Any, **kwargs: Any) -> Any:
                start_ms = _elapsed_ms()
                trace[f"{phase_name}_started_ms"] = start_ms
                if phase_name == "vae_decode":
                    last_step_ms = trace.get("last_step_seen_ms")
                    if isinstance(last_step_ms, int):
                        trace["gap_after_last_step_ms"] = max(start_ms - last_step_ms, 0)
                _write_phase(runtime_phase)
                _log_event(f"{phase_name}.start")
                try:
                    return original(*args, **kwargs)
                finally:
                    duration_ms = _elapsed_ms() - start_ms
                    trace[f"{phase_name}_ended_ms"] = _elapsed_ms()
                    trace[f"{phase_name}_ms"] = duration_ms
                    _log_event(f"{phase_name}.end", duration_ms=duration_ms)

            setattr(owner, attr_name, wrapped)
            stack.callback(setattr, owner, attr_name, original)

        try:
            call_params = inspect.signature(pipeline.__call__).parameters
        except (TypeError, ValueError):
            call_params = {}
        trace["step_callback_supported"] = "callback_on_step_end" in call_params

        if "callback_on_step_end" in call_params:

            def _on_step_end(
                _pipeline: Any,
                step_index: int,
                _timestep: Any,
                callback_kwargs: Dict[str, Any],
            ) -> Dict[str, Any]:
                step_number = int(step_index) + 1
                elapsed_ms = _elapsed_ms()
                if "first_step_seen_ms" not in trace:
                    trace["first_step_seen_ms"] = elapsed_ms
                    _write_phase("denoising_image")
                    _log_event(
                        "denoise.first_step",
                        step=step_number,
                        total_steps=num_inference_steps,
                    )
                if step_number in milestone_steps and step_number not in logged_steps:
                    logged_steps.add(step_number)
                    trace[f"step_{step_number}_seen_ms"] = elapsed_ms
                    _log_event(
                        "denoise.milestone",
                        step=step_number,
                        total_steps=num_inference_steps,
                    )
                if step_number >= num_inference_steps and "last_step_seen_ms" not in trace:
                    trace["last_step_seen_ms"] = elapsed_ms
                    _log_event(
                        "denoise.last_step",
                        step=step_number,
                        total_steps=num_inference_steps,
                    )
                return callback_kwargs

            extra_call_kwargs["callback_on_step_end"] = _on_step_end
            if "callback_on_step_end_tensor_inputs" in call_params:
                extra_call_kwargs["callback_on_step_end_tensor_inputs"] = []

        _wrap_method(getattr(pipeline, "vae", None), "decode", "vae_decode", "vae_decode")
        _wrap_method(
            getattr(pipeline, "image_processor", None),
            "postprocess",
            "image_postprocess",
            "postprocess_image",
        )
        _wrap_method(pipeline, "maybe_free_model_hooks", "free_model_hooks", "offloading_image")
        return stack, trace, extra_call_kwargs

    @staticmethod
    def _shape_from_dimensions(width: int, height: int) -> str:
        if width <= 0 or height <= 0:
            return "square"
        aspect_ratio = width / height
        if aspect_ratio >= 1.15:
            return "landscape"
        if aspect_ratio <= 0.85:
            return "portrait"
        return "square"

    def _create_image_db_record(
        self,
        *,
        user_id: int,
        prompt: str,
        negative_prompt: Optional[str],
        model_key: str,
        generation_params: Dict[str, Any],
    ) -> int:
        with db_rw() as conn:
            cursor = conn.execute(
                """
                INSERT INTO generated_media (
                    user_id, media_type, prompt, negative_prompt, model_used,
                    generation_params, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    "image",
                    prompt,
                    negative_prompt,
                    model_key,
                    str(generation_params),
                    "generating",
                ),
            )
            lastrowid = cursor.lastrowid
            if lastrowid is None:
                raise RuntimeError("generated_media insert did not return a row id")
            return int(lastrowid)

    def _mark_image_db_record_completed(
        self,
        *,
        db_id: int,
        file_path: Path,
        file_size: int,
        generation_time_ms: int,
    ) -> None:
        with db_rw() as conn:
            conn.execute(
                """
                UPDATE generated_media
                SET file_path = ?, file_size = ?, generation_time_ms = ?,
                    status = 'completed', completed_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (str(file_path), file_size, generation_time_ms, db_id),
            )

    def _mark_image_db_record_failed(self, *, db_id: int, error: str) -> None:
        with db_rw() as conn:
            conn.execute(
                """
                UPDATE generated_media
                SET status = 'failed', error_message = ?
                WHERE id = ?
                """,
                (error, db_id),
            )

    def _save_external_image_bytes(
        self,
        *,
        image_bytes: bytes,
        user_id: int,
        db_id: int,
        suffix: str,
    ) -> tuple[Path, int, int]:
        save_started = time.time()
        normalized_suffix = (suffix or ".jpg").strip().lower()
        if not normalized_suffix.startswith("."):
            normalized_suffix = f".{normalized_suffix}"
        if normalized_suffix == ".jpeg":
            normalized_suffix = ".jpg"
        if normalized_suffix not in {".jpg", ".png", ".webp"}:
            normalized_suffix = ".jpg"

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"user{user_id}_{timestamp}_{db_id}{normalized_suffix}"
        file_path = self.output_dir / filename
        file_path.write_bytes(image_bytes)
        file_size = file_path.stat().st_size
        save_time_ms = int((time.time() - save_started) * 1000)
        return file_path, file_size, save_time_ms

    @staticmethod
    def _infer_external_image_suffix(
        *, content_type: Optional[str], image_bytes: bytes | None = None
    ) -> str:
        normalized_type = str(content_type or "").split(";", 1)[0].strip().lower()
        if normalized_type in {"image/jpeg", "image/jpg"}:
            return ".jpg"
        if normalized_type == "image/png":
            return ".png"
        if normalized_type == "image/webp":
            return ".webp"

        if image_bytes:
            if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                return ".png"
            if image_bytes.startswith(b"\xff\xd8\xff"):
                return ".jpg"
            if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
                return ".webp"

        return ".jpg"

    @staticmethod
    def _guess_image_extension(image_bytes: bytes | None = None) -> str:
        """Backward-compatible helper for external backends that only have bytes."""
        return MediaGenerationService._infer_external_image_suffix(
            content_type=None,
            image_bytes=image_bytes,
        )

    def _resolve_easydiffusion_models(self, base_url: str) -> tuple[Optional[str], Optional[str]]:
        def _first_nonempty(*values: Any) -> Optional[str]:
            for value in values:
                text = str(value or "").strip()
                if text:
                    return text
            return None

        try:
            import requests
        except Exception as exc:
            raise RuntimeError(
                "EasyDiffusion backend requires the `requests` package."
            ) from exc

        try:
            response = requests.get(f"{base_url}/get/app_config", timeout=15)
            response.raise_for_status()
            payload = response.json() or {}
            model_cfg = payload.get("model") if isinstance(payload, dict) else {}
            if not isinstance(model_cfg, dict):
                model_cfg = {}
            sd_model = _first_nonempty(
                model_cfg.get("stable-diffusion"),
                model_cfg.get("stable_diffusion"),
                payload.get("active_model") if isinstance(payload, dict) else None,
                payload.get("activeModel") if isinstance(payload, dict) else None,
                payload.get("selected_model") if isinstance(payload, dict) else None,
                payload.get("selectedModel") if isinstance(payload, dict) else None,
            )
            vae_model = _first_nonempty(
                model_cfg.get("vae"),
                payload.get("active_vae") if isinstance(payload, dict) else None,
                payload.get("activeVae") if isinstance(payload, dict) else None,
                payload.get("selected_vae") if isinstance(payload, dict) else None,
                payload.get("selectedVae") if isinstance(payload, dict) else None,
            )
            return sd_model, vae_model
        except Exception as exc:
            logger.warning("Could not resolve EasyDiffusion active model: %s", exc)
            return None, None

    def _generate_image_via_easydiffusion(
        self,
        *,
        prompt: str,
        negative_prompt: Optional[str],
        width: int,
        height: int,
        num_inference_steps: int,
        guidance_scale: float,
        seed: Optional[int],
        user_id: int,
        db_id: int,
    ) -> Dict[str, Any]:
        try:
            import requests
        except Exception as exc:
            raise RuntimeError(
                "EasyDiffusion backend requires the `requests` package."
            ) from exc

        cfg = settings()
        base_url = str(getattr(cfg, "easy_diffusion_url", "http://127.0.0.1:9000")).rstrip("/")
        timeout_seconds = max(float(getattr(cfg, "easy_diffusion_timeout_seconds", 900.0) or 900.0), 60.0)
        poll_interval_s = 2.0
        session_id = f"wellness-{user_id}-{db_id}-{uuid.uuid4().hex[:8]}"
        request_id = f"media-{db_id}"

        configured_model, _configured_vae = self._resolve_easydiffusion_models(base_url)
        configured_vae = None
        if getattr(cfg, "easy_diffusion_model", None):
            configured_model = str(cfg.easy_diffusion_model).strip() or configured_model
        if getattr(cfg, "easy_diffusion_vae_model", None):
            configured_vae = str(cfg.easy_diffusion_vae_model).strip() or configured_vae
        if not configured_model:
            raise RuntimeError(
                "EasyDiffusion active model could not be resolved. "
                "Set EASY_DIFFUSION_MODEL in .env or choose an active model in EasyDiffusion first."
            )

        payload: Dict[str, Any] = {
            "prompt": prompt,
            "seed": int(seed) if seed is not None else -1,
            "width": int(width),
            "height": int(height),
            "num_outputs": 1,
            "num_inference_steps": int(num_inference_steps),
            "guidance_scale": float(guidance_scale),
            "request_id": request_id,
            "session_id": session_id,
            "stream_image_progress": False,
            "output_format": "jpeg",
            "output_quality": 90,
            "vram_usage_level": getattr(cfg, "easy_diffusion_vram_usage_level", None) or "balanced",
        }
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        if configured_model:
            payload["use_stable_diffusion_model"] = configured_model
        if configured_vae:
            payload["use_vae_model"] = configured_vae
        if getattr(cfg, "easy_diffusion_sampler", None):
            payload["sampler_name"] = str(cfg.easy_diffusion_sampler).strip()

        self._write_runtime_status(
            busy=True,
            phase="queueing_external_image",
            current_model="easydiffusion",
            last_prompt=prompt[:240],
            media_type="image",
            user_id=user_id,
            external_backend="easydiffusion",
        )
        submit_started = time.time()
        render_response = requests.post(f"{base_url}/render", json=payload, timeout=30)
        render_response.raise_for_status()
        render_payload = render_response.json()
        task_id = int(render_payload["task"])
        submit_time_ms = int((time.time() - submit_started) * 1000)

        deadline = time.time() + timeout_seconds
        task_status = "pending"
        while time.time() < deadline:
            ping_response = requests.get(
                f"{base_url}/ping",
                params={"session_id": session_id},
                timeout=15,
            )
            ping_response.raise_for_status()
            ping_payload = ping_response.json()
            task_status = str((ping_payload.get("tasks") or {}).get(str(task_id)) or "")
            if task_status in {"completed", "error", "stopped"}:
                break
            self._write_runtime_status(
                busy=True,
                phase="generating_external_image",
                current_model="easydiffusion",
                last_prompt=prompt[:240],
                media_type="image",
                user_id=user_id,
                external_backend="easydiffusion",
                external_task_id=task_id,
                external_task_status=task_status or "running",
            )
            time.sleep(poll_interval_s)
        else:
            raise TimeoutError(
                f"EasyDiffusion did not finish within {int(timeout_seconds)} seconds."
            )

        final_payload: Dict[str, Any] | None = None
        for _ in range(10):
            final_response = requests.get(f"{base_url}/image/stream/{task_id}", timeout=30)
            if final_response.status_code == 425:
                time.sleep(0.5)
                continue
            final_response.raise_for_status()
            final_payload = final_response.json()
            break
        if not isinstance(final_payload, dict):
            raise RuntimeError("EasyDiffusion completed without a final response payload.")
        if final_payload.get("status") != "succeeded":
            detail = str(final_payload.get("detail") or final_payload)
            if "NoneType" in detail and "startswith" in detail:
                detail = (
                    "EasyDiffusion returned an internal null-path error. "
                    "Check the active checkpoint / VAE selection in EasyDiffusion, "
                    "or set EASY_DIFFUSION_MODEL explicitly."
                )
            raise RuntimeError(f"EasyDiffusion render failed: {detail}")

        image_response = None
        for _ in range(10):
            image_response = requests.get(f"{base_url}/image/tmp/{task_id}/0", timeout=30)
            if image_response.status_code == 425:
                time.sleep(0.5)
                continue
            if image_response.ok:
                break
        if image_response is None or not image_response.ok:
            output = (final_payload.get("output") or [{}])[0]
            output_path = str(output.get("path") or "").strip()
            if output_path.startswith("/"):
                image_response = requests.get(f"{base_url}{output_path}", timeout=30)
            if image_response is None or not image_response.ok:
                raise RuntimeError("EasyDiffusion did not expose a downloadable image result.")

        inference_time_ms = int((time.time() - submit_started) * 1000)
        return {
            "image_bytes": image_response.content,
            "suffix": ".jpg",
            "load_time_ms": submit_time_ms,
            "inference_time_ms": inference_time_ms,
            "external_trace": {
                "backend": "easydiffusion",
                "base_url": base_url,
                "task_id": task_id,
                "session_id": session_id,
                "selected_model": configured_model,
                "selected_vae": configured_vae,
                "task_status": task_status or "completed",
                "queue_depth": render_payload.get("queue"),
            },
        }

    def _generate_image_via_flux2_klein(
        self,
        *,
        prompt: str,
        negative_prompt: Optional[str],
        width: int,
        height: int,
        num_inference_steps: int,
        guidance_scale: float,
        seed: Optional[int],
        user_id: int,
        db_id: int,
    ) -> Dict[str, Any]:
        try:
            import requests
        except Exception as exc:
            raise RuntimeError(
                "FLUX.2 Klein backend requires the `requests` package."
            ) from exc

        cfg = settings()
        base_url = str(getattr(cfg, "flux2_klein_url", "http://127.0.0.1:7865")).rstrip("/")
        timeout_seconds = max(
            float(getattr(cfg, "flux2_klein_timeout_seconds", 900.0) or 900.0),
            60.0,
        )
        payload: Dict[str, Any] = {
            "prompt": prompt,
            "width": int(width),
            "height": int(height),
            "num_inference_steps": int(num_inference_steps),
            "guidance_scale": float(guidance_scale),
            "seed": int(seed) if seed is not None else None,
        }

        self._write_runtime_status(
            busy=True,
            phase="queueing_external_image",
            current_model="flux2-klein",
            last_prompt=prompt[:240],
            media_type="image",
            user_id=user_id,
            external_backend="flux2_klein",
        )
        started = time.time()
        response = requests.post(f"{base_url}/generate", json=payload, timeout=timeout_seconds)
        elapsed_ms = int((time.time() - started) * 1000)
        if not response.ok:
            detail = response.text.strip() or f"HTTP {response.status_code}"
            raise RuntimeError(f"FLUX.2 Klein render failed: {detail}")

        generation_header = str(response.headers.get("X-Generation-Time") or "").strip()
        server_generation_ms = 0
        if generation_header.endswith("s"):
            try:
                server_generation_ms = int(float(generation_header[:-1]) * 1000)
            except ValueError:
                server_generation_ms = 0

        return {
            "image_bytes": response.content,
            "suffix": self._guess_image_extension(response.content),
            "load_time_ms": max(elapsed_ms - server_generation_ms, 0),
            "inference_time_ms": server_generation_ms or elapsed_ms,
            "external_trace": {
                "backend": "flux2_klein",
                "base_url": base_url,
                "requested_model": "flux2-klein",
                "negative_prompt_ignored": bool(negative_prompt),
                "generation_time_header": generation_header or None,
                "peak_vram_gb": response.headers.get("X-Peak-VRAM-GB"),
                "request_id": f"media-{db_id}",
            },
        }

    def _generate_image_via_perchance(
        self,
        *,
        prompt: str,
        negative_prompt: Optional[str],
        width: int,
        height: int,
        guidance_scale: float,
        seed: Optional[int],
        user_id: int,
    ) -> Dict[str, Any]:
        cfg = settings()
        timeout_seconds = max(float(getattr(cfg, "perchance_timeout_seconds", 180.0) or 180.0), 30.0)
        prefer_persistent_profile = bool(
            getattr(cfg, "perchance_use_persistent_profile", True)
        )
        shape = self._shape_from_dimensions(width, height)

        async def _run(*, use_persistent_profile: bool) -> Dict[str, Any]:
            try:
                perchance_module = import_module("perchance")
                ImageGenerator = getattr(perchance_module, "ImageGenerator")
            except Exception as exc:
                raise RuntimeError(
                    "Perchance backend requires `pip install perchance` and `python -m playwright install chromium`."
                ) from exc

            # Guard against library versions that dropped the kwarg.
            try:
                generator_ctx = ImageGenerator(use_persistent_profile=use_persistent_profile)
            except TypeError:
                logger.warning(
                    "perchance.ImageGenerator does not accept use_persistent_profile; "
                    "falling back to default constructor."
                )
                generator_ctx = ImageGenerator()
            async with generator_ctx as generator:
                result = await generator.image(
                    prompt,
                    negative_prompt=negative_prompt,
                    seed=int(seed) if seed is not None else -1,
                    shape=shape,
                    guidance_scale=float(guidance_scale),
                )
                blob = await result.download()
                return {
                    "image_bytes": blob.getvalue(),
                    "suffix": f".{result.file_extension}",
                    "trace": {
                        "backend": "perchance",
                        "shape": shape,
                        "persistent_profile": use_persistent_profile,
                        "seed": result.seed,
                        "width": result.width,
                        "height": result.height,
                        "maybe_nsfw": result.maybe_nsfw,
                    },
                }

        self._write_runtime_status(
            busy=True,
            phase="generating_external_image",
            current_model="perchance",
            last_prompt=prompt[:240],
            media_type="image",
            user_id=user_id,
            external_backend="perchance",
        )
        started = time.time()
        last_exc: Exception | None = None
        attempt_modes: list[bool] = [prefer_persistent_profile]
        if True not in attempt_modes:
            attempt_modes.append(True)
        result = None
        for use_persistent_profile in attempt_modes:
            try:
                result = asyncio.run(
                    asyncio.wait_for(
                        _run(use_persistent_profile=use_persistent_profile),
                        timeout=timeout_seconds,
                    )
                )
                break
            except Exception as exc:
                last_exc = exc
                if "Failed to retrieve user key" in str(exc):
                    logger.warning(
                        "Perchance auth challenge hit (persistent_profile=%s): %s",
                        use_persistent_profile,
                        exc,
                    )
                    continue
                raise
        if result is None:
            if last_exc is not None and "Failed to retrieve user key" in str(last_exc):
                raise RuntimeError(
                    "Perchance is currently returning a Cloudflare challenge instead of a usable user key, "
                    "even after retrying with a persistent browser profile. "
                    "This backend is blocked from this machine right now."
                ) from last_exc
            raise last_exc or RuntimeError("Perchance render failed without a result.")
        inference_time_ms = int((time.time() - started) * 1000)
        return {
            "image_bytes": result["image_bytes"],
            "suffix": result["suffix"],
            "load_time_ms": 0,
            "inference_time_ms": inference_time_ms,
            "external_trace": result["trace"],
        }

    def _generate_image_via_perchance_other(
        self,
        *,
        prompt: str,
        negative_prompt: Optional[str],
        width: int,
        height: int,
        guidance_scale: float,
        seed: Optional[int],
        user_id: int,
    ) -> Dict[str, Any]:
        """Generate an image via the Perchance /imageapi page.

        The target URL (https://perchance.org/imageapi?prompt=...) is a
        JavaScript-rendered page that calls the Perchance text-to-image plugin
        internally and writes <img> elements into #imageCtn once generation
        completes.  A plain HTTP request therefore always gets HTML back, never
        image bytes.  We drive a real headless Chromium browser via Playwright
        (with playwright-stealth to handle Cloudflare) so the JS actually runs,
        then extract the first produced image URL and download it within the
        same browser session to stay inside the established Cloudflare context.
        """
        import urllib.parse

        cfg = settings()
        timeout_seconds = max(
            float(getattr(cfg, "perchance_other_timeout_seconds", 90.0) or 90.0),
            30.0,
        )
        base_url = str(
            getattr(cfg, "perchance_other_url", "https://perchance.org/imageapi")
            or "https://perchance.org/imageapi"
        ).strip()

        # Build the full page URL with the prompt encoded into the query string.
        # The page JS calls decodeURIComponent() on the value, so standard
        # percent-encoding is correct.
        full_url = f"{base_url}?prompt={urllib.parse.quote(prompt.strip(), safe='')}"

        self._write_runtime_status(
            busy=True,
            phase="generating_external_image",
            current_model="perchance_other",
            last_prompt=prompt[:240],
            media_type="image",
            user_id=user_id,
            external_backend="perchance_other",
        )

        async def _run() -> Dict[str, Any]:
            # Prefer rebrowser_playwright (patched binary, best CF bypass) — the
            # same backend that the perchance library uses internally.
            try:
                from rebrowser_playwright.async_api import \
                    async_playwright  # type: ignore[import]
                _playwright_backend = "rebrowser"
            except Exception:
                try:
                    from playwright.async_api import \
                        async_playwright  # type: ignore[assignment]
                    _playwright_backend = "playwright"
                except Exception as exc:
                    raise RuntimeError(
                        "Perchance Other backend requires `pip install playwright` "
                        "and `python -m playwright install chromium`."
                    ) from exc

            # Stealth patch on top of whichever backend is available.
            _stealth_obj = None
            try:
                from playwright_stealth import \
                    Stealth as _Stealth  # type: ignore[import]
                _stealth_obj = _Stealth(navigator_webdriver=True, chrome_runtime=True)
            except Exception:
                try:
                    from playwright_stealth import \
                        stealth_async as _stealth_async  # type: ignore[import]
                    _stealth_obj = _stealth_async  # callable, applied differently below
                except Exception:
                    logger.warning(
                        "playwright-stealth not available; Cloudflare bypass may fail "
                        "for perchance_other."
                    )

            _CHROME_UA = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            )

            # Accumulated console errors from the page for diagnostic logging.
            _console_errors: list[str] = []

            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                try:
                    context = await browser.new_context(
                        user_agent=_CHROME_UA,
                        viewport={"width": 1280, "height": 800},
                        locale="en-US",
                    )
                    page = await context.new_page()

                    # Apply stealth.
                    if _stealth_obj is not None:
                        apply_stealth_async = getattr(_stealth_obj, "apply_stealth_async", None)
                        if callable(apply_stealth_async):
                            await cast(Any, apply_stealth_async)(page)
                        elif callable(_stealth_obj):
                            await cast(Any, _stealth_obj)(page)

                    # Capture console errors so we can include them in our error
                    # messages when the page's generateImage() fails silently.
                    page.on(
                        "console",
                        lambda msg: _console_errors.append(msg.text)
                        if msg.type == "error"
                        else None,
                    )

                    logger.info(
                        "perchance_other: navigating to %s (backend=%s, timeout=%.0fs)",
                        full_url,
                        _playwright_backend,
                        timeout_seconds,
                    )
                    # Use "networkidle" so that the page's deferred plugin JS
                    # (imported via {import:text-to-image-plugin}) has time to
                    # load before DOMContentLoaded fires on the outer page.
                    await page.goto(
                        full_url,
                        wait_until="networkidle",
                        timeout=min(timeout_seconds * 1000, 60_000),
                    )

                    # Poll for one of two terminal states:
                    #   - success: at least one <img> with a populated src
                    #   - failure: loadingMsg shows the error text the page
                    #              sets when Promise.all rejects
                    logger.info("perchance_other: waiting for image or failure signal …")
                    poll_timeout_ms = max((timeout_seconds - 10) * 1000, 30_000)
                    terminal_state_handle = await page.wait_for_function(
                        """() => {
                            const imgs = document.querySelectorAll('#imageCtn img');
                            if (imgs.length > 0 && imgs[0].src && imgs[0].src.startsWith('http')) {
                                return 'success';
                            }
                            const msg = document.getElementById('loadingMsg');
                            if (msg && !msg.hidden &&
                                    msg.textContent &&
                                    msg.textContent.toLowerCase().includes('failed')) {
                                return 'failed:' + msg.textContent.trim();
                            }
                            return false;
                        }""",
                        timeout=poll_timeout_ms,
                    )
                    terminal_state_value = await terminal_state_handle.json_value()
                    terminal_state = str(terminal_state_value or "")

                    if terminal_state.startswith("failed:"):
                        failure_text = terminal_state[len("failed:"):]
                        console_summary = "; ".join(_console_errors[:5]) if _console_errors else "none"
                        raise RuntimeError(
                            f"perchance_other: the page's generateImage() call failed — "
                            f"'{failure_text}'. This usually means the Perchance AI API "
                            f"is blocking headless access (Cloudflare or rate-limit). "
                            f"Console errors: {console_summary}"
                        )

                    # Extract the src of the first generated image.
                    img_src_value = await page.eval_on_selector(
                        "#imageCtn img",
                        "el => el.src",
                    )
                    img_src = str(img_src_value or "").strip()
                    if not img_src or not img_src.startswith("http"):
                        raise RuntimeError(
                            f"perchance_other: unexpected image src from DOM: {img_src!r}"
                        )

                    logger.info("perchance_other: downloading image from %s", img_src)

                    # Download the image via Playwright's own request context so
                    # that we reuse the same cookies / CF session token.
                    api_response = await context.request.get(
                        img_src,
                        timeout=30_000,  # ms
                    )
                    if api_response.status < 200 or api_response.status >= 300:
                        raise RuntimeError(
                            f"perchance_other: image download failed "
                            f"(status={api_response.status}, url={img_src})"
                        )
                    image_bytes = await api_response.body()
                    content_type = str(
                        api_response.headers.get("content-type", "image/jpeg")
                    ).strip()

                    return {
                        "image_bytes": image_bytes,
                        "content_type": content_type,
                        "img_src": img_src,
                        "page_url": full_url,
                        "playwright_backend": _playwright_backend,
                    }
                except Exception:
                    if _console_errors:
                        logger.debug(
                            "perchance_other page console errors: %s",
                            "; ".join(_console_errors[:10]),
                        )
                    raise
                finally:
                    await browser.close()

        started = time.time()
        try:
            result = asyncio.run(
                asyncio.wait_for(
                    _run(),
                    timeout=timeout_seconds,
                )
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"perchance_other: generation did not complete within {timeout_seconds:.0f}s."
            ) from exc

        inference_time_ms = int((time.time() - started) * 1000)
        content_type = result["content_type"]
        image_bytes = result["image_bytes"]

        return {
            "image_bytes": image_bytes,
            "suffix": self._infer_external_image_suffix(
                content_type=content_type,
                image_bytes=image_bytes,
            ),
            "load_time_ms": 0,
            "inference_time_ms": inference_time_ms,
            "external_trace": {
                "backend": "perchance_other",
                "method": "playwright_dom_extraction",
                "playwright_backend": result.get("playwright_backend", "unknown"),
                "base_url": base_url,
                "page_url": result["page_url"],
                "img_src": result["img_src"],
                "content_type": content_type,
                "stealth_applied": True,
                "negative_prompt_ignored": bool(negative_prompt),
                "seed_ignored": seed is not None,
                "requested_width": width,
                "requested_height": height,
                "requested_guidance_scale": guidance_scale,
            },
            # TODO: add option to grab all 4 generated images instead of just
            #       the first, and pick by quality/size or let the user choose
        }

    def _generate_external_image(
        self,
        *,
        prompt: str,
        user_id: int,
        model_key: str,
        negative_prompt: Optional[str],
        width: int,
        height: int,
        num_inference_steps: int,
        guidance_scale: float,
        seed: Optional[int],
    ) -> Dict[str, Any]:
        model_info = self.SUPPORTED_MODELS.get(model_key, {})
        generation_params = {
            "width": width,
            "height": height,
            "num_inference_steps": num_inference_steps,
            "guidance_scale": guidance_scale,
            "seed": seed,
            "provider": model_info.get("provider"),
        }
        db_id = self._create_image_db_record(
            user_id=user_id,
            prompt=prompt,
            negative_prompt=negative_prompt,
            model_key=model_key,
            generation_params=generation_params,
        )

        try:
            provider = str(model_info.get("provider") or "").strip()
            if provider == "flux2_klein":
                provider_result = self._generate_image_via_flux2_klein(
                    prompt=prompt,
                    negative_prompt=negative_prompt,
                    width=width,
                    height=height,
                    num_inference_steps=num_inference_steps,
                    guidance_scale=guidance_scale,
                    seed=seed,
                    user_id=user_id,
                    db_id=db_id,
                )
            elif provider == "easydiffusion":
                provider_result = self._generate_image_via_easydiffusion(
                    prompt=prompt,
                    negative_prompt=negative_prompt,
                    width=width,
                    height=height,
                    num_inference_steps=num_inference_steps,
                    guidance_scale=guidance_scale,
                    seed=seed,
                    user_id=user_id,
                    db_id=db_id,
                )
            elif provider == "perchance":
                provider_result = self._generate_image_via_perchance(
                    prompt=prompt,
                    negative_prompt=negative_prompt,
                    width=width,
                    height=height,
                    guidance_scale=guidance_scale,
                    seed=seed,
                    user_id=user_id,
                )
            elif provider == "perchance_other":
                provider_result = self._generate_image_via_perchance_other(
                    prompt=prompt,
                    negative_prompt=negative_prompt,
                    width=width,
                    height=height,
                    guidance_scale=guidance_scale,
                    seed=seed,
                    user_id=user_id,
                )
            else:
                raise RuntimeError(f"Unknown external image provider: {provider}")

            self._write_runtime_status(
                busy=True,
                phase="saving_image",
                current_model=model_key,
                last_prompt=prompt[:240],
                media_type="image",
                user_id=user_id,
                external_backend=provider,
                external_trace=provider_result.get("external_trace"),
            )
            file_path, file_size, save_time_ms = self._save_external_image_bytes(
                image_bytes=provider_result["image_bytes"],
                user_id=user_id,
                db_id=db_id,
                suffix=provider_result.get("suffix", ".jpg"),
            )
            generation_time_ms = int(provider_result.get("load_time_ms", 0)) + int(
                provider_result.get("inference_time_ms", 0)
            ) + save_time_ms
            self._mark_image_db_record_completed(
                db_id=db_id,
                file_path=file_path,
                file_size=file_size,
                generation_time_ms=generation_time_ms,
            )
            self._write_runtime_status(
                busy=False,
                phase="idle",
                current_model=model_key,
                last_prompt=prompt[:240],
                media_type="image",
                last_result="success",
                last_generation_time_ms=generation_time_ms,
                external_backend=provider,
                external_trace=provider_result.get("external_trace"),
            )
            return {
                "status": "success",
                "image_path": str(file_path),
                "generation_time_ms": generation_time_ms,
                "load_time_ms": int(provider_result.get("load_time_ms", 0)),
                "inference_time_ms": int(provider_result.get("inference_time_ms", 0)),
                "save_time_ms": save_time_ms,
                "file_size": file_size,
                "db_id": db_id,
                "model": model_key,
                "external_trace": provider_result.get("external_trace"),
            }
        except Exception as exc:
            logger.error("External image generation failed: %s", exc, exc_info=True)
            self._mark_image_db_record_failed(db_id=db_id, error=str(exc))
            self._write_runtime_status(
                busy=False,
                phase="image_failed",
                current_model=model_key,
                last_prompt=prompt[:240],
                media_type="image",
                last_result="failed",
                error=str(exc),
                external_backend=model_info.get("provider"),
            )
            return {
                "status": "failed",
                "error": str(exc),
                "db_id": db_id,
            }

    # ── Prompt formatting ──────────────────────────────────────────────────────

    @staticmethod
    def _format_prompt(
        model_key: str,
        model_info: Dict[str, Any],
        raw_prompt: str,
        source_tags: Optional[List[str]] = None,
    ) -> tuple[str, str]:
        """Apply model-specific prompt wrapping; return (positive, auto_negative).

        Pony:   score tags prefix → raw prompt → optional source_tags → suffix (rating_explicit)
        Wai:    quality prefix → raw prompt → suffix (nsfw, explicit)
        Unholy: quality prefix → raw prompt          (no suffix defined)
        Other:  prefix → raw prompt → suffix (passthrough)

        The auto_negative is taken from SUPPORTED_MODELS but callers may extend it.
        """
        prefix = str(model_info.get("auto_prompt_prefix") or "").strip().rstrip(",")
        suffix = str(model_info.get("auto_prompt_suffix") or "").strip()
        auto_neg = str(model_info.get("auto_negative") or "").strip()

        parts: List[str] = []
        if prefix:
            parts.append(prefix)
        parts.append(raw_prompt.strip())
        # Source tags (e.g. source_anime, source_pony) go between the prompt and the
        # quality/rating suffix — this matches Pony's expected tag ordering.
        if source_tags and model_key in _LOCAL_SDXL_MODELS:
            parts.extend(t.strip() for t in source_tags if t.strip())
        if suffix:
            parts.append(suffix)

        positive = ", ".join(p for p in parts if p)
        return positive, auto_neg

    # ── LoRA management ────────────────────────────────────────────────────────

    def list_loras(self, model_key: str) -> Dict[str, List[str]]:
        """Return a dict of {family_name: [filename, ...]} for model_key.

        Only families listed in SUPPORTED_MODELS["lora_families"] are included.
        Creates the family directories if they don't exist yet.
        Returns an empty dict for models that don't accept LoRAs (e.g. Klein).
        """
        families = self.SUPPORTED_MODELS.get(model_key, {}).get("lora_families") or []
        result: Dict[str, List[str]] = {}
        for family in families:
            family_dir = LORA_BASE_DIR / family
            try:
                family_dir.mkdir(parents=True, exist_ok=True)
                result[family] = sorted(
                    f.name for f in family_dir.iterdir()
                    if f.is_file() and f.suffix.lower() == ".safetensors"
                )
            except Exception as exc:
                logger.warning("Could not scan LoRA dir %s: %s", family_dir, exc)
                result[family] = []
        return result

    def _apply_loras(
        self,
        pipeline: Any,
        loras: List[Dict[str, Any]],
    ) -> List[str]:
        """Load and fuse LoRA weights onto the pipeline.

        Each entry in loras: {"name": "file.safetensors", "family": "pony", "weight": 0.8}
        Returns the list of adapter names that were successfully loaded (for cleanup).
        Skips silently if the file doesn't exist.
        """
        loaded_adapters: List[str] = []
        adapter_weights: List[float] = []

        for entry in loras:
            name = str(entry.get("name") or "")
            family = str(entry.get("family") or "sdxl")
            weight = float(entry.get("weight") or 1.0)

            lora_path = LORA_BASE_DIR / family / name
            if not lora_path.exists():
                logger.warning("LoRA not found, skipping: %s", lora_path)
                continue

            # Sanitize name for use as an adapter_name (no whitespace/special chars)
            adapter_name = lora_path.stem.replace(" ", "_")[:64]
            try:
                pipeline.load_lora_weights(
                    str(lora_path.parent),
                    weight_name=lora_path.name,
                    adapter_name=adapter_name,
                )
                loaded_adapters.append(adapter_name)
                adapter_weights.append(weight)
                logger.info("Loaded LoRA: %s @ weight %.2f", lora_path.name, weight)
            except Exception as exc:
                logger.warning("Failed to load LoRA %s: %s", lora_path.name, exc)

        if loaded_adapters:
            pipeline.set_adapters(loaded_adapters, adapter_weights=adapter_weights)

        return loaded_adapters

    def _remove_loras(self, pipeline: Any) -> None:
        """Unload all LoRA adapters from the pipeline after generation."""
        try:
            pipeline.unload_lora_weights()
        except Exception as exc:
            logger.debug("LoRA unload skipped: %s", exc)

    # ── AnimateDiff SDXL ──────────────────────────────────────────────────────

    def _get_animatediff_pipe(self, base_pipeline: Any) -> Any:
        """Wrap a loaded SDXL pipeline with AnimateDiff motion adapter.

        Prefers a local .ckpt file at MOTION_MODULE_DIR/MOTION_MODULE_FILENAME.
        Falls back to auto-downloading from guoyww/animatediff-motion-adapter-sdxl-beta
        (stored in the HF cache — only downloads once).
        """
        from diffusers import (  # type: ignore[reportMissingImports]
            AnimateDiffSDXLPipeline, MotionAdapter)

        local_ckpt = MOTION_MODULE_DIR / MOTION_MODULE_FILENAME
        MOTION_MODULE_DIR.mkdir(parents=True, exist_ok=True)

        if local_ckpt.exists():
            # Load from a single .ckpt file via torch.load + weights injection
            assert torch is not None
            logger.info("Loading AnimateDiff motion module from local ckpt: %s", local_ckpt.name)
            state_dict = torch.load(str(local_ckpt), map_location="cpu", weights_only=True)
            # Some checkpoints wrap the weights in a "state_dict" sub-key
            if "state_dict" in state_dict:
                state_dict = state_dict["state_dict"]
            adapter = MotionAdapter()
            missing, unexpected = adapter.load_state_dict(state_dict, strict=False)
            if missing:
                logger.debug("Motion adapter missing keys: %d", len(missing))
            if unexpected:
                logger.debug("Motion adapter unexpected keys: %d", len(unexpected))
        else:
            logger.info(
                "Motion module .ckpt not found at %s — downloading from HuggingFace "
                "(guoyww/animatediff-motion-adapter-sdxl-beta); this only happens once.",
                local_ckpt,
            )
            adapter = MotionAdapter.from_pretrained(
                "guoyww/animatediff-motion-adapter-sdxl-beta",
                torch_dtype=base_pipeline.dtype if hasattr(base_pipeline, "dtype") else None,
            )

        anim_pipe = AnimateDiffSDXLPipeline.from_pipe(
            base_pipeline,
            motion_adapter=adapter,
        )
        return anim_pipe

    # ── Hires upscale ─────────────────────────────────────────────────────────

    def _hires_upscale(
        self,
        pipeline: Any,
        image: Any,
        prompt: str,
        negative_prompt: str,
        *,
        scale: float = 1.5,
        steps: int = 20,
        denoise_strength: float = 0.4,
        guidance_scale: float = 6.0,
        seed: Optional[int] = None,
    ) -> Any:
        """Run an img2img pass on the generated image to upscale it.

        Uses StableDiffusionXLImg2ImgPipeline.from_pipe() which reuses the
        already-loaded UNet/VAE weights — no extra VRAM load.

        Args:
            pipeline:          Loaded SDXL pipeline (must match img2img interface).
            image:             PIL Image from the initial generation pass.
            prompt:            Same prompt used for initial generation.
            negative_prompt:   Same negative prompt.
            scale:             Resize multiplier (default 1.5 → 1024→1536).
            steps:             Denoising steps for the img2img pass.
            denoise_strength:  How much to re-denoise (0.35–0.5 is the sweet spot).
            guidance_scale:    CFG scale for the img2img pass.
            seed:              RNG seed; None for random.
        Returns:
            Upscaled PIL Image.
        """
        from diffusers import (  # type: ignore[reportMissingImports]
            StableDiffusionXLImg2ImgPipeline,
        )
        from PIL import Image as PILImage  # type: ignore[reportMissingImports]

        orig_w, orig_h = image.size
        new_w = int(orig_w * scale)
        new_h = int(orig_h * scale)
        # Snap to multiples of 8 for VAE compatibility
        new_w = (new_w // 8) * 8
        new_h = (new_h // 8) * 8

        logger.info(
            "Hires upscale: %dx%d → %dx%d (strength=%.2f, steps=%d)",
            orig_w, orig_h, new_w, new_h, denoise_strength, steps,
        )

        upscaled_init = image.resize(
            (new_w, new_h),
            getattr(getattr(PILImage, "Resampling", PILImage), "LANCZOS"),
        )

        # Reuse loaded weights — from_pipe() does NOT copy weights, just reuses them
        img2img_pipe = StableDiffusionXLImg2ImgPipeline.from_pipe(pipeline)

        generator = None
        if seed is not None and torch is not None:
            generator = torch.Generator(
                device="cpu" if not self.cuda_available else "cuda"
            ).manual_seed(seed)

        result = img2img_pipe(
            prompt=prompt,
            negative_prompt=negative_prompt or "",
            image=upscaled_init,
            strength=denoise_strength,
            num_inference_steps=steps,
            guidance_scale=guidance_scale,
            generator=generator,
        )
        return result.images[0]

    # ── Local safetensors loader ───────────────────────────────────────────────

    def _load_local_sdxl(self, model_key: str) -> tuple[Any, bool]:
        """Load a local SDXL safetensors checkpoint economically.

        Shared by pony-xl-v6, wai-illustrious-xl, and unholy-desire-v7.
        Differences (VAE, scheduler, clip_skip) are driven entirely by the
        SUPPORTED_MODELS config so no per-model branching is needed here.

        Returns the loaded pipeline plus a boolean indicating whether CPU
        offload had to be enabled as a fallback.
        """
        assert torch is not None
        model_info = self.SUPPORTED_MODELS[model_key]
        local_path = str(model_info["local_safetensors"])

        if not Path(local_path).exists():
            raise FileNotFoundError(
                f"Local safetensors checkpoint not found: {local_path}"
            )

        from diffusers import (  # type: ignore[reportMissingImports]
            StableDiffusionXLPipeline,
        )

        # Use bfloat16 on Ampere+ GPUs (better precision than float16 at the same width),
        # fall back to float16 on older cards, and float32 on CPU.
        if self.cuda_available and hasattr(torch.cuda, "is_bf16_supported") and torch.cuda.is_bf16_supported():
            dtype = torch.bfloat16
        elif self.cuda_available:
            dtype = torch.float16
        else:
            dtype = torch.float32

        logger.info("Loading local SDXL checkpoint: %s", Path(local_path).name)
        pipeline = StableDiffusionXLPipeline.from_single_file(
            local_path,
            torch_dtype=dtype,
            use_safetensors=True,
        )

        # Optional external VAE swap (Pony needs one; the others have it baked in)
        vae_path = model_info.get("local_vae")
        if vae_path:
            vae_path_obj = Path(str(vae_path))
            if vae_path_obj.exists():
                from diffusers import (  # type: ignore[reportMissingImports]
                    AutoencoderKL,
                )
                logger.info("Swapping VAE from: %s", vae_path_obj.name)
                pipeline.vae = AutoencoderKL.from_single_file(
                    str(vae_path_obj), torch_dtype=dtype
                )
                if self.cuda_available:
                    pipeline.vae = pipeline.vae.to("cuda")
            else:
                logger.warning(
                    "VAE file not found at %s — using checkpoint-embedded VAE", vae_path
                )

        # Apply scheduler from config
        scheduler_key = str(model_info.get("scheduler") or "euler_a")
        self._set_scheduler(pipeline, scheduler_key)

        used_cpu_offload = False
        if self.cuda_available:
            try:
                pipeline = pipeline.to("cuda")
                logger.info("Loaded %s fully to GPU", model_key)
            except RuntimeError as exc:
                err_msg = str(exc).lower()
                oom_markers = {
                    "out of memory",
                    "cuda out of memory",
                    "cublas_status_alloc_failed",
                    "cuda error: out of memory",
                }
                if hasattr(pipeline, "enable_model_cpu_offload") and any(
                    marker in err_msg for marker in oom_markers
                ):
                    logger.warning(
                        "Full GPU load failed for %s; falling back to CPU offload: %s",
                        model_key,
                        exc,
                    )
                    pipeline.enable_model_cpu_offload()
                    used_cpu_offload = True
                else:
                    raise
        else:
            pipeline = pipeline.to("cpu")

        return pipeline, used_cpu_offload

    def load_model(self, model_key: str = "sdxl") -> bool:
        """
        Load a diffusion model with VRAM optimizations.

        Args:
            model_key: Model identifier (sdxl, sdxl-turbo, flux, etc.)

        Returns:
            True if loaded successfully, False otherwise
        """
        if not TORCH_AVAILABLE:
            self.last_load_error_kind = "dependency"
            self.last_load_error = _media_backend_error()
            logger.error(self.last_load_error)
            return False
        assert torch is not None

        if model_key not in self.SUPPORTED_MODELS:
            logger.error(f"Unknown model: {model_key}")
            return False
        self.last_load_error = None
        self.last_load_error_kind = None

        # If model already loaded, skip
        if self.current_model == model_key and self.pipeline is not None:
            logger.info(f"Model {model_key} already loaded")
            return True

        # Before loading any model, ensure any other loaded model (including the
        # Klein subprocess) is released so VRAM is available.
        # Klein runs in a separate process — signal it to unload first.
        self._signal_klein_unload()
        # Unload the in-process pipeline (sdxl, flux, etc.)
        self.unload_model()

        try:
            model_info = self.SUPPORTED_MODELS[model_key]
            model_id = model_info["model_id"]
            media_type = model_info.get("media_type", "image")
            use_model_cpu_offload = False
            pipeline_device_managed = False

            logger.info(f"Loading {model_info['name']} from {model_id}...")
            start_time = time.time()
            self._write_runtime_status(
                busy=True,
                phase="loading_model",
                current_model=model_key,
                current_model_name=model_info["name"],
                last_prompt=None,
            )

            dtype = torch.float16 if self.cuda_available else torch.float32

            # HF auth token (from config or auto-detected by diffusers)
            cfg = settings()
            hf_token = getattr(cfg, "hf_token", None) or None

            # Check if the HF model is already cached to avoid network calls
            cached_dir = _HF_CACHE / f"models--{model_id.replace('/', '--')}"
            is_cached = cached_dir.exists() and (cached_dir / "snapshots").exists()

            if model_key in _LOCAL_SDXL_MODELS:
                # ── Local safetensors SDXL models (Pony, WaiIllustrous, Unholy) ──
                # _load_local_sdxl handles VAE swap, scheduler, and device placement.
                pipeline, use_model_cpu_offload = self._load_local_sdxl(model_key)
                pipeline_device_managed = True

            elif model_key == "sdxl":
                assert StableDiffusionXLPipeline is not None
                pipeline = StableDiffusionXLPipeline.from_pretrained(
                    model_id,
                    torch_dtype=dtype,
                    use_safetensors=True,
                    variant="fp16" if self.cuda_available else None,
                    local_files_only=is_cached,
                    token=hf_token,
                )
            elif model_key == "z-image-fp8":
                # Load base FLUX pipeline from local cache, then swap in FP8 weights
                assert DiffusionPipeline is not None
                flux_cached = (_HF_CACHE / "models--black-forest-labs--FLUX.1-dev" / "snapshots").exists()
                pipeline = DiffusionPipeline.from_pretrained(
                    model_id,
                    torch_dtype=dtype,
                    use_safetensors=True,
                    local_files_only=flux_cached,
                    token=hf_token,
                )
                local_weights = _HF_CACHE / model_info["local_weights"]
                if local_weights.exists():
                    from safetensors.torch import load_file  # type: ignore
                    state_dict = load_file(str(local_weights))
                    pipeline.transformer.load_state_dict(state_dict, strict=False)
                    logger.info(f"Loaded FP8 weights from {local_weights}")
                    # Re-apply float8 quantization via torchao — load_state_dict upcasts
                    # FP8 tensors to float16 because the model parameters are already float16.
                    # torchao restores actual FP8 compute paths for meaningful speedup.
                    try:
                        from torchao.quantization import (  # type: ignore
                            float8_dynamic_activation_float8_weight, quantize_)
                        quantize_(
                            pipeline.transformer,
                            float8_dynamic_activation_float8_weight(),
                        )
                        logger.info("z-image-fp8: torchao float8 quantization applied")
                    except ImportError:
                        logger.warning(
                            "torchao not installed — z-image-fp8 running in float16 "
                            "(install torchao for true FP8 speed: pip install torchao)"
                        )
                    except Exception as _fp8_err:
                        logger.warning(
                            "torchao FP8 quantization failed (%s) — running in float16",
                            _fp8_err,
                        )
                else:
                    logger.warning(f"FP8 weights not found at {local_weights}, using base model")

                # FLUX pipelines must use FlowMatchEulerDiscreteScheduler — it
                # supports the sigmas argument that FluxPipeline.__call__ passes to
                # retrieve_timesteps. Explicitly enforce it in case the cached config
                # or a prior session left the wrong scheduler on the pipeline.
                try:
                    from diffusers.schedulers import (  # type: ignore[reportMissingImports]
                        FlowMatchEulerDiscreteScheduler,
                    )
                    if not isinstance(pipeline.scheduler, FlowMatchEulerDiscreteScheduler):
                        logger.warning(
                            "z-image-fp8: unexpected scheduler %s — forcing FlowMatchEulerDiscreteScheduler",
                            type(pipeline.scheduler).__name__,
                        )
                        pipeline.scheduler = FlowMatchEulerDiscreteScheduler.from_config(
                            pipeline.scheduler.config
                        )
                    else:
                        logger.info("z-image-fp8: scheduler OK (%s)", type(pipeline.scheduler).__name__)
                except Exception as _sched_err:
                    logger.warning("z-image-fp8: could not verify scheduler: %s", _sched_err)
            elif model_key == "z-image-q8-gguf":
                if (
                    ZImagePipeline is None
                    or ZImageTransformer2DModel is None
                    or GGUFQuantizationConfig is None
                ):
                    raise RuntimeError(
                        "Z-Image GGUF requires diffusers>=0.37 with GGUF support installed."
                    )
                local_weights = _HF_CACHE / model_info["local_weights"]
                if not local_weights.exists():
                    raise FileNotFoundError(
                        f"Local GGUF weights not found at {local_weights}"
                    )
                zimage_dtype = (
                    torch.bfloat16
                    if self.cuda_available
                    and hasattr(torch.cuda, "is_bf16_supported")
                    and torch.cuda.is_bf16_supported()
                    else dtype
                )
                base_cached = self._repo_snapshot_has_required_files(
                    model_id,
                    [
                        "model_index.json",
                        "scheduler/scheduler_config.json",
                        "tokenizer/tokenizer.json",
                        "text_encoder/*.safetensors",
                        "vae/*.safetensors",
                    ],
                )
                if not base_cached:
                    logger.info(
                        "Z-Image base files are incomplete locally; allowing diffusers to download missing text encoder/VAE assets."
                    )
                transformer = ZImageTransformer2DModel.from_single_file(
                    str(local_weights),
                    quantization_config=GGUFQuantizationConfig(
                        compute_dtype=zimage_dtype
                    ),
                    torch_dtype=zimage_dtype,
                )
                pipeline = ZImagePipeline.from_pretrained(
                    model_id,
                    transformer=transformer,
                    torch_dtype=zimage_dtype,
                    use_safetensors=True,
                    low_cpu_mem_usage=False,
                    local_files_only=base_cached,
                    token=hf_token,
                )
                use_model_cpu_offload = self.cuda_available

            elif media_type == "video" and model_key == "wan-t2v":
                # Wan T2V — loaded separately via generate_video with epoch param
                pipeline = None
                self.current_model = model_key
                load_time = time.time() - start_time
                logger.info(f"Wan T2V registered in {load_time:.1f}s (loaded per-epoch at generation)")
                return True
            elif media_type == "video" and model_key == "ltx2":
                # LTX2 Rapid Merges — these are safetensors merge weights, not
                # a standard diffusers pipeline. Load base LTX-Video then apply.
                assert DiffusionPipeline is not None
                try:
                    from diffusers import LTXPipeline  # type: ignore
                    pipeline = LTXPipeline.from_pretrained(
                        "Lightricks/LTX-Video-0.9.5",
                        torch_dtype=dtype,
                        token=hf_token,
                    )
                except (ImportError, Exception):
                    pipeline = DiffusionPipeline.from_pretrained(
                        "Lightricks/LTX-Video-0.9.5",
                        torch_dtype=dtype,
                        token=hf_token,
                    )
                # Apply NSFW merge weights if available
                ltx_snap = _HF_CACHE / "models--Phr00t--LTX2-Rapid-Merges" / "snapshots"
                if ltx_snap.exists():
                    merge_files = list(ltx_snap.rglob("nsfw/*.safetensors"))
                    if merge_files:
                        from safetensors.torch import load_file  # type: ignore

                        # Use the latest version
                        merge_file = sorted(merge_files)[-1]
                        state_dict = load_file(str(merge_file))
                        target = getattr(pipeline, "transformer", None) or getattr(pipeline, "unet", None)
                        if target:
                            target.load_state_dict(state_dict, strict=False)
                            logger.info(f"Loaded LTX2 merge weights from {merge_file.name}")
            elif media_type == "video":
                # Other video models
                assert DiffusionPipeline is not None
                pipeline = DiffusionPipeline.from_pretrained(
                    model_id,
                    torch_dtype=dtype,
                    use_safetensors=True,
                    local_files_only=is_cached,
                    token=hf_token,
                )
            else:
                # Generic DiffusionPipeline for other image models
                assert DiffusionPipeline is not None
                pipeline = DiffusionPipeline.from_pretrained(
                    model_id,
                    torch_dtype=dtype,
                    use_safetensors=True,
                    local_files_only=is_cached,
                    token=hf_token,
                )

            # Move to device
            if not pipeline_device_managed:
                if use_model_cpu_offload and hasattr(pipeline, "enable_model_cpu_offload"):
                    pipeline.enable_model_cpu_offload()
                    logger.info("Enabled model CPU offload for %s", model_key)
                else:
                    pipeline = pipeline.to(self.device)

            # torch.compile can help some pipelines, but local SDXL checkpoints on
            # Windows have shown multi-minute TorchInductor warmups and noisy temp
            # artifacts. Keep it opt-in and never apply it to the local SDXL family.
            compile_enabled = bool(getattr(cfg, "media_use_torch_compile", False))
            if (
                self.cuda_available
                and media_type == "image"
                and not use_model_cpu_offload
                and compile_enabled
                and model_key not in _LOCAL_SDXL_MODELS
            ):
                try:
                    assert torch is not None
                    _compile_target = getattr(pipeline, "transformer", None) or getattr(
                        pipeline, "unet", None
                    )
                    if _compile_target is not None:
                        _attr = "transformer" if hasattr(pipeline, "transformer") and pipeline.transformer is not None else "unet"
                        setattr(
                            pipeline,
                            _attr,
                            torch.compile(
                                _compile_target,
                                mode="reduce-overhead",
                                fullgraph=False,
                            ),
                        )
                        logger.info(
                            "torch.compile applied to %s.%s (first run will be ~30s slower)",
                            model_key,
                            _attr,
                        )
                except Exception as _compile_err:
                    logger.info(
                        "torch.compile skipped for %s: %s", model_key, _compile_err
                    )
            elif self.cuda_available and media_type == "image":
                if model_key in _LOCAL_SDXL_MODELS:
                    logger.info(
                        "Skipping torch.compile for %s to avoid multi-minute first-run latency on local SDXL checkpoints",
                        model_key,
                    )
                elif not compile_enabled:
                    logger.info(
                        "torch.compile disabled for %s (set MEDIA_USE_TORCH_COMPILE=true to opt in)",
                        model_key,
                    )

            self.pipeline = pipeline

            # VRAM optimizations
            if self.cuda_available and torch is not None:
                use_slicing = self._should_use_memory_slicing(model_key, media_type)
                if use_slicing and hasattr(pipeline, "enable_attention_slicing"):
                    pipeline.enable_attention_slicing()
                if use_slicing and hasattr(pipeline, "enable_vae_slicing"):
                    pipeline.enable_vae_slicing()
                if not use_slicing:
                    logger.info(
                        "Skipping attention/VAE slicing for model %s on %.1fGB GPU to improve latency",
                        model_key,
                        self.vram_total_gb,
                    )
                if model_key == "z-image-q8-gguf":
                    logger.info(
                        "Skipping xformers for %s because Z-Image attention uses custom kwargs",
                        model_key,
                    )
                else:
                    try:
                        pipeline.enable_xformers_memory_efficient_attention()
                        logger.info("xformers memory-efficient attention enabled")
                    except Exception:
                        logger.info("xformers not available - using default attention")

                # Use DPM++ only for SD-style image models. FLUX/z-image keep their
                # native scheduler.
                if (
                    media_type == "image"
                    and model_key in {"sdxl", "sdxl-turbo", "playground", "pixart"}
                    and hasattr(pipeline, "scheduler")
                ):
                    try:
                        assert DPMSolverMultistepScheduler is not None
                        pipeline.scheduler = DPMSolverMultistepScheduler.from_config(
                            pipeline.scheduler.config
                        )
                    except Exception:
                        logger.info("Could not set DPM scheduler, using default")

            self.current_model = model_key
            load_time = time.time() - start_time
            logger.info(f"Model loaded in {load_time:.1f}s")
            self.last_load_error = None
            self.last_load_error_kind = None
            self._write_runtime_status(
                busy=False,
                phase="idle",
                current_model=model_key,
                current_model_name=model_info["name"],
                last_load_seconds=round(load_time, 2),
            )

            return True

        except Exception as exc:
            err_msg = str(exc).lower()
            mid = self.SUPPORTED_MODELS.get(model_key, {}).get("model_id", model_key)
            if "gated" in err_msg or "401" in err_msg or "authorization" in err_msg or "token" in err_msg:
                self.last_load_error_kind = "auth"
                self.last_load_error = (
                    "This model requires Hugging Face authentication. "
                    f"Run `huggingface-cli login` and accept the model license at https://huggingface.co/{mid}"
                )
                logger.error(
                    f"Failed to load model {model_key}: This model requires HuggingFace authentication. "
                    f"Run 'huggingface-cli login' and accept the model license at https://huggingface.co/{mid}"
                )
            elif "paging file is too small" in err_msg or "os error 1455" in err_msg or isinstance(exc, MemoryError):
                self.last_load_error_kind = "system_memory"
                self.last_load_error = (
                    f"{self.SUPPORTED_MODELS.get(model_key, {}).get('name', model_key)} could not be loaded "
                    "because Windows ran out of paging-file backed memory. Increase the Windows page file "
                    "or use a lighter/local-default model instead."
                )
                logger.error(
                    "Failed to load model %s: Windows paging file too small or process memory exhausted",
                    model_key,
                    exc_info=True,
                )
            elif "disk" in err_msg or "space" in err_msg or "no space" in err_msg:
                self.last_load_error_kind = "disk"
                self.last_load_error = "Not enough disk space to download model weights."
                logger.error(f"Failed to load model {model_key}: Not enough disk space to download model weights")
            else:
                self.last_load_error_kind = "unknown"
                self.last_load_error = f"Failed to load model {model_key}: {exc}"
                logger.error(f"Failed to load model {model_key}: {exc}", exc_info=True)
            self.pipeline = None
            self.current_model = None
            self._write_runtime_status(
                busy=False,
                phase="load_failed",
                current_model=None,
                error=self.last_load_error or str(exc),
            )
            return False

    def unload_model(self) -> None:
        """Unload current model to free VRAM"""
        if self.pipeline is not None:
            logger.info(f"Unloading model: {self.current_model}")
            del self.pipeline
            self.pipeline = None
            self.current_model = None

            # Clear CUDA cache
            if self.cuda_available and torch is not None:
                torch.cuda.empty_cache()
                logger.info("CUDA cache cleared")
        self._write_runtime_status(
            busy=False,
            phase="idle",
            current_model=None,
            current_model_name=None,
        )

    def generate_image(
        self,
        prompt: str,
        user_id: int,
        model_key: str = "flux2-klein",
        negative_prompt: Optional[str] = None,
        width: int = 1024,
        height: int = 1024,
        num_inference_steps: int = 4,
        guidance_scale: float = 4.0,
        seed: Optional[int] = None,
        # ── Local SDXL extras ──────────────────────────────────────────────────
        source_tags: Optional[List[str]] = None,
        # List of LoRA dicts: [{"name": "char.safetensors", "family": "pony", "weight": 0.8}]
        loras: Optional[List[Dict[str, Any]]] = None,
        # AnimateDiff: produce a GIF instead of a still image
        animated: bool = False,
        num_frames: int = 16,
        fps: int = 8,
        # Hires upscale pass (auto-applied for wai-illustrious-xl by default)
        hires_upscale: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Generate an image using AI model.

        Args:
            prompt:               Text description of desired image.
            user_id:              User ID for tracking.
            model_key:            Model to use (default: flux2-klein).
            negative_prompt:      Things to avoid in image.
            width/height:         Image dimensions.
            num_inference_steps:  Quality/speed tradeoff.
            guidance_scale:       How closely to follow prompt.
            seed:                 Random seed for reproducibility.
            source_tags:          Pony-style source tags, e.g. ["source_anime"].
            loras:                LoRA files to hot-load.  Local SDXL models only.
            animated:             Produce a GIF via AnimateDiff (SDXL models only).
            num_frames:           Frames for animated output (default 16).
            fps:                  Frame rate for GIF (default 8).
            hires_upscale:        Run a 1.5× img2img upscale pass.  Defaults to the
                                  model's "hires_upscale_default" setting if None.

        Returns:
            Dict with image_path, generation_time_ms, db_id, or error.
        """
        model_info = self.SUPPORTED_MODELS.get(model_key)
        if not model_info or model_info.get("media_type") != "image":
            return {
                "error": f"Unknown or non-image model: {model_key}",
                "status": "failed",
            }

        provider = str(model_info.get("provider") or "diffusers")
        if provider != "diffusers":
            return self._generate_external_image(
                prompt=prompt,
                user_id=user_id,
                model_key=model_key,
                negative_prompt=negative_prompt,
                width=width,
                height=height,
                num_inference_steps=num_inference_steps,
                guidance_scale=guidance_scale,
                seed=seed,
            )

        if not TORCH_AVAILABLE:
            return {
                "error": _media_backend_error(),
                "error_kind": "dependency",
                "status": "failed",
            }

        # ── Prompt auto-formatting for local SDXL models ──────────────────────
        if model_key in _LOCAL_SDXL_MODELS:
            formatted_prompt, auto_neg = self._format_prompt(
                model_key, model_info, prompt, source_tags=source_tags
            )
            # Merge auto-negative with any caller-supplied negative prompt
            if auto_neg:
                negative_prompt = (
                    f"{auto_neg}, {negative_prompt}" if negative_prompt else auto_neg
                )
            prompt = formatted_prompt
            logger.debug("Formatted prompt for %s: %s", model_key, prompt[:120])

        # ── Hires upscale default resolution for wai-illustrious-xl ──────────
        # Default 1024 base; the 1.5× upscale pass will bring it to ~1536.
        if model_key == "wai-illustrious-xl" and width == 1024 and height == 1024:
            width = 1024
            height = 1024

        # Resolve hires_upscale toggle (caller wins; else respect model default)
        do_hires = (
            hires_upscale
            if hires_upscale is not None
            else bool(model_info.get("hires_upscale_default", False))
        )
        # AnimateDiff is only available for models that support it
        do_animated = animated and bool(model_info.get("supports_animatediff", False))
        if animated and not model_info.get("supports_animatediff"):
            logger.warning(
                "Model %s does not support AnimateDiff; ignoring animated=True", model_key
            )

        # Load model if needed
        load_started = time.time()
        if not self.load_model(model_key):
            return {
                "error": self.last_load_error or f"Failed to load model: {model_key}",
                "error_kind": self.last_load_error_kind or "unknown",
                "status": "failed",
            }
        if self.pipeline is None:
            return {"error": "Model pipeline not initialized", "status": "failed"}
        load_time_ms = int((time.time() - load_started) * 1000)

        # Create database entry
        generation_params = {
            "width": width,
            "height": height,
            "num_inference_steps": num_inference_steps,
            "guidance_scale": guidance_scale,
            "seed": seed,
        }
        db_id = self._create_image_db_record(
            user_id=user_id,
            prompt=prompt,
            negative_prompt=negative_prompt,
            model_key=model_key,
            generation_params=generation_params,
        )

        smoke_trace: Dict[str, Any] = {}
        inference_started = time.time()
        try:
            # Set random seed
            generator = None
            if seed is not None and torch is not None:
                generator_device = "cpu" if model_key == "z-image-q8-gguf" else self.device
                generator = torch.Generator(device=generator_device).manual_seed(seed)

            # Generate image
            logger.info(
                "generate_image start: model=%s device=%s cuda=%s vram_total=%.1fGB "
                "steps=%d size=%dx%d",
                model_key, self.device, self.cuda_available, self.vram_total_gb,
                num_inference_steps, width, height,
            )
            logger.info(f"Generating image for user {user_id}: {prompt[:50]}...")
            self._write_runtime_status(
                busy=True,
                phase="generating_image",
                current_model=model_key,
                last_prompt=prompt[:240],
                media_type="image",
                user_id=user_id,
                last_result=None,
                error=None,
                external_backend=None,
                external_task_id=None,
                external_task_status=None,
                external_trace=None,
                last_inference_time_ms=None,
                last_generation_time_ms=None,
            )
            inference_started = time.time()

            # FLUX-based models are guidance-distilled (no CFG); passing a negative
            # prompt triggers a second text-encoding pass that wastes time and can
            # confuse the pipeline. Skip it entirely for these models.
            _flux_models = {"z-image-fp8", "flux"}
            _call_kwargs: Dict[str, Any] = {
                "prompt": prompt,
                "width": width,
                "height": height,
                "num_inference_steps": num_inference_steps,
                "guidance_scale": guidance_scale,
                "generator": generator,
            }
            extra_pipeline_call_kwargs = model_info.get("pipeline_call_kwargs")
            if isinstance(extra_pipeline_call_kwargs, dict):
                _call_kwargs.update(extra_pipeline_call_kwargs)
            if model_key not in _flux_models:
                _call_kwargs["negative_prompt"] = (
                    negative_prompt or "ugly, blurry, low quality, distorted"
                )

            # clip_skip: inject via cross_attention_kwargs for SDXL local models
            clip_skip = int(model_info.get("clip_skip") or 0)
            if clip_skip and clip_skip > 1 and model_key in _LOCAL_SDXL_MODELS:
                _call_kwargs["clip_skip"] = clip_skip

            # ── Apply LoRAs (local SDXL models only) ──────────────────────────
            loaded_adapters: List[str] = []
            active_pipeline = self.pipeline
            if loras and model_key in _LOCAL_SDXL_MODELS:
                try:
                    loaded_adapters = self._apply_loras(active_pipeline, loras)
                except Exception as lora_exc:
                    logger.warning("LoRA application failed, continuing without: %s", lora_exc)

            # ── Wrap with AnimateDiff if requested ────────────────────────────
            if do_animated:
                try:
                    active_pipeline = self._get_animatediff_pipe(active_pipeline)
                    _call_kwargs["num_frames"] = num_frames
                    # AnimateDiffSDXLPipeline does not use width/height the same way;
                    # keep width as the video width, height as frame height
                except Exception as anim_exc:
                    logger.warning("AnimateDiff wrap failed, generating still: %s", anim_exc)
                    do_animated = False

            smoke_stack, smoke_trace, smoke_call_kwargs = self._attach_image_smoke_trace(
                active_pipeline,
                model_key=model_key,
                user_id=user_id,
                prompt_excerpt=prompt[:240],
                width=width,
                height=height,
                num_inference_steps=num_inference_steps,
            )
            _call_kwargs.update(smoke_call_kwargs)
            with smoke_stack:
                result = active_pipeline(**_call_kwargs)
            inference_time_ms = int((time.time() - inference_started) * 1000)
            smoke_trace["pipeline_returned_ms"] = inference_time_ms
            last_step_ms = smoke_trace.get("last_step_seen_ms")
            if isinstance(last_step_ms, int):
                smoke_trace["tail_after_last_step_ms"] = max(inference_time_ms - last_step_ms, 0)
            logger.info(
                "Image pipeline returned: model=%s inference_ms=%d",
                model_key,
                inference_time_ms,
            )
            logger.info(
                "[MEDIA-SMOKE] model=%s user=%s summary=%s",
                model_key,
                user_id,
                json.dumps(smoke_trace, sort_keys=True),
            )
            self._write_runtime_status(
                busy=True,
                phase="saving_image",
                current_model=model_key,
                last_prompt=prompt[:240],
                media_type="image",
                user_id=user_id,
                last_inference_time_ms=inference_time_ms,
                smoke_trace=smoke_trace,
            )

            # ── Unload LoRAs immediately after generation ─────────────────────
            if loaded_adapters:
                self._remove_loras(self.pipeline)

            save_started = time.time()
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

            if do_animated:
                # AnimateDiff returns a list of frames in result.frames[0]
                frames = result.frames[0] if hasattr(result, "frames") else result.images
                filename = f"user{user_id}_{timestamp}_{db_id}.gif"
                file_path = self.output_dir / filename
                frames[0].save(
                    file_path,
                    save_all=True,
                    append_images=frames[1:],
                    duration=int(1000 / max(fps, 1)),
                    loop=0,
                    optimize=False,
                )
                output_media_type = "gif"
            else:
                image = result.images[0]

                # ── Hires upscale pass ────────────────────────────────────────
                if do_hires and model_key in _LOCAL_SDXL_MODELS:
                    try:
                        image = self._hires_upscale(
                            self.pipeline,
                            image,
                            prompt=prompt,
                            negative_prompt=negative_prompt or "",
                            seed=seed,
                        )
                        logger.info("Hires upscale complete: final size %s", image.size)
                    except Exception as upscale_exc:
                        logger.warning("Hires upscale failed, using base image: %s", upscale_exc)

                filename = f"user{user_id}_{timestamp}_{db_id}.jpg"
                file_path = self.output_dir / filename
                if getattr(image, "mode", "RGB") != "RGB":
                    image = image.convert("RGB")
                image.save(file_path)
                output_media_type = "image"
            file_size = file_path.stat().st_size
            save_time_ms = int((time.time() - save_started) * 1000)
            generation_time_ms = load_time_ms + inference_time_ms + save_time_ms

            # Update database
            self._mark_image_db_record_completed(
                db_id=db_id,
                file_path=file_path,
                file_size=file_size,
                generation_time_ms=generation_time_ms,
            )

            logger.info(
                "Image generated: model=%s load_ms=%d inference_ms=%d save_ms=%d total_ms=%d path=%s",
                model_key,
                load_time_ms,
                inference_time_ms,
                save_time_ms,
                generation_time_ms,
                file_path,
            )
            self._write_runtime_status(
                busy=False,
                phase="idle",
                current_model=model_key,
                last_prompt=prompt[:240],
                media_type="image",
                last_result="success",
                last_generation_time_ms=generation_time_ms,
                smoke_trace=smoke_trace,
                error=None,
                external_backend=None,
                external_task_id=None,
                external_task_status=None,
                external_trace=None,
            )

            return {
                "status": "success",
                "image_path": str(file_path),
                "media_type": output_media_type,
                "generation_time_ms": generation_time_ms,
                "load_time_ms": load_time_ms,
                "inference_time_ms": inference_time_ms,
                "save_time_ms": save_time_ms,
                "file_size": file_size,
                "db_id": db_id,
                "model": model_key,
                "smoke_trace": smoke_trace,
            }

        except Exception as exc:
            if smoke_trace:
                smoke_trace["failed_at_ms"] = smoke_trace.get("failed_at_ms") or int(
                    (time.time() - inference_started) * 1000
                )
            logger.error(f"Image generation failed: {exc}", exc_info=True)
            self._write_runtime_status(
                busy=False,
                phase="image_failed",
                current_model=model_key,
                last_prompt=prompt[:240],
                media_type="image",
                last_result="failed",
                error=str(exc),
                smoke_trace=smoke_trace or None,
                external_backend=None,
                external_task_id=None,
                external_task_status=None,
                external_trace=None,
            )

            # Update database with error
            self._mark_image_db_record_failed(db_id=db_id, error=str(exc))

            return {
                "status": "failed",
                "error": str(exc),
                "db_id": db_id,
                "smoke_trace": smoke_trace,
            }

    def generate_video(
        self,
        prompt: str,
        user_id: int,
        model_key: str = "wan-t2v",
        negative_prompt: Optional[str] = None,
        width: int = 480,
        height: int = 320,
        num_frames: int = 33,
        num_inference_steps: int = 30,
        guidance_scale: float = 7.5,
        fps: int = 16,
        seed: Optional[int] = None,
        epoch: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Generate a video using a text-to-video model.

        Args:
            prompt: Text description of desired video.
            user_id: User ID for tracking.
            model_key: Model to use (wan-t2v, ltx2).
            negative_prompt: Things to avoid.
            width/height: Resolution.
            num_frames: Number of frames to generate.
            num_inference_steps: Quality/speed tradeoff.
            guidance_scale: How closely to follow prompt.
            fps: Frames per second for output.
            seed: Random seed for reproducibility.
            epoch: Epoch checkpoint (wan-t2v only, 1-10).

        Returns:
            Dict with video_path, generation_time_ms, db_id, or error.
        """
        if not TORCH_AVAILABLE:
            return {
                "error": _media_backend_error(),
                "error_kind": "dependency",
                "status": "failed",
            }
        assert torch is not None

        model_info = self.SUPPORTED_MODELS.get(model_key)
        if not model_info or model_info.get("media_type") != "video":
            return {"error": f"Unknown or non-video model: {model_key}", "status": "failed"}

        generation_params = {
            "width": width, "height": height, "num_frames": num_frames,
            "num_inference_steps": num_inference_steps, "guidance_scale": guidance_scale,
            "fps": fps, "seed": seed, "epoch": epoch,
        }

        with db_rw() as conn:
            cursor = conn.execute(
                """
                INSERT INTO generated_media (
                    user_id, media_type, prompt, negative_prompt, model_used,
                    generation_params, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, "video", prompt, negative_prompt, model_key,
                 str(generation_params), "generating"),
            )
            db_id = cursor.lastrowid

        try:
            logger.info(f"Generating video for user {user_id}: {prompt[:50]}...")
            self._write_runtime_status(
                busy=True,
                phase="generating_video",
                current_model=model_key,
                last_prompt=prompt[:240],
                media_type="video",
                user_id=user_id,
            )
            start_time = time.time()

            generator = None
            if seed is not None and self.cuda_available:
                generator = torch.Generator(device=self.device).manual_seed(seed)

            if model_key == "wan-t2v":
                video_frames = self._generate_wan_video(
                    prompt, negative_prompt, width, height,
                    num_frames, num_inference_steps, guidance_scale,
                    generator, epoch or model_info.get("default_epoch", 10),
                )
            else:
                # LTX2 or other video models loaded via standard pipeline
                if not self.load_model(model_key):
                    return {"error": f"Failed to load model: {model_key}", "status": "failed"}
                if self.pipeline is None:
                    return {"error": "Pipeline not initialized", "status": "failed"}
                result = self.pipeline(
                    prompt=prompt,
                    negative_prompt=negative_prompt or "ugly, blurry, low quality",
                    width=width, height=height,
                    num_frames=num_frames,
                    num_inference_steps=num_inference_steps,
                    guidance_scale=guidance_scale,
                    generator=generator,
                )
                video_frames = result.frames[0] if hasattr(result, "frames") else result.images

            generation_time_ms = int((time.time() - start_time) * 1000)

            # Save as MP4
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            filename = f"user{user_id}_{timestamp}_{db_id}.mp4"
            file_path = self.output_dir / filename
            self._save_video(video_frames, file_path, fps)
            file_size = file_path.stat().st_size

            with db_rw() as conn:
                conn.execute(
                    """
                    UPDATE generated_media
                    SET file_path = ?, file_size = ?, generation_time_ms = ?,
                        status = 'completed', completed_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (str(file_path), file_size, generation_time_ms, db_id),
                )

            logger.info(f"Video generated in {generation_time_ms}ms: {file_path}")
            self._write_runtime_status(
                busy=False,
                phase="idle",
                current_model=model_key,
                last_prompt=prompt[:240],
                media_type="video",
                last_result="success",
                last_generation_time_ms=generation_time_ms,
            )
            return {
                "status": "success",
                "video_path": str(file_path),
                "generation_time_ms": generation_time_ms,
                "file_size": file_size,
                "db_id": db_id,
                "model": model_key,
                "num_frames": num_frames,
                "fps": fps,
            }

        except Exception as exc:
            logger.error(f"Video generation failed: {exc}", exc_info=True)
            self._write_runtime_status(
                busy=False,
                phase="video_failed",
                current_model=model_key,
                last_prompt=prompt[:240],
                media_type="video",
                last_result="failed",
                error=str(exc),
            )
            with db_rw() as conn:
                conn.execute(
                    "UPDATE generated_media SET status = 'failed', error_message = ? WHERE id = ?",
                    (str(exc), db_id),
                )
            return {"status": "failed", "error": str(exc), "db_id": db_id}

    def _generate_wan_video(
        self,
        prompt: str,
        negative_prompt: Optional[str],
        width: int,
        height: int,
        num_frames: int,
        num_inference_steps: int,
        guidance_scale: float,
        generator: Any,
        epoch: int,
    ) -> Any:
        """Load Wan T2V epoch checkpoint and generate video frames."""
        assert torch is not None
        assert DiffusionPipeline is not None

        wan_dir = _HF_CACHE / "models--wan1.3NSFW-t2v"
        weights_path = wan_dir / f"wan_1.3B_e{epoch}.safetensors"
        if not weights_path.exists():
            raise FileNotFoundError(f"Wan epoch {epoch} not found: {weights_path}")

        logger.info(f"Loading Wan T2V epoch {epoch} from {weights_path}")

        # Unload previous model first
        self.unload_model()

        # Load base Wan pipeline then apply LoRA/epoch weights
        cfg = settings()
        hf_token = getattr(cfg, "hf_token", None) or None
        try:
            from diffusers import WanPipeline  # type: ignore
            pipeline = WanPipeline.from_pretrained(
                "Wan-AI/Wan2.1-T2V-1.3B",
                torch_dtype=torch.float16 if self.cuda_available else torch.float32,
                token=hf_token,
            )
        except (ImportError, Exception):
            # Fallback: generic DiffusionPipeline
            pipeline = DiffusionPipeline.from_pretrained(
                "Wan-AI/Wan2.1-T2V-1.3B",
                torch_dtype=torch.float16 if self.cuda_available else torch.float32,
                token=hf_token,
            )

        # Load the NSFW epoch weights
        from safetensors.torch import load_file  # type: ignore
        state_dict = load_file(str(weights_path))
        if hasattr(pipeline, "transformer"):
            pipeline.transformer.load_state_dict(state_dict, strict=False)
        elif hasattr(pipeline, "unet"):
            pipeline.unet.load_state_dict(state_dict, strict=False)
        logger.info(f"Loaded Wan epoch {epoch} weights")

        pipeline = pipeline.to(self.device)
        if self.cuda_available:
            if hasattr(pipeline, "enable_attention_slicing"):
                pipeline.enable_attention_slicing()
            if hasattr(pipeline, "enable_vae_slicing"):
                pipeline.enable_vae_slicing()

        self.pipeline = pipeline
        self.current_model = f"wan-t2v-e{epoch}"

        result = pipeline(
            prompt=prompt,
            negative_prompt=negative_prompt or "ugly, blurry, low quality",
            width=width, height=height,
            num_frames=num_frames,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            generator=generator,
        )
        return result.frames[0] if hasattr(result, "frames") else result.images

    @staticmethod
    def _save_video(frames: Any, output_path: Path, fps: int = 16) -> None:
        """Save video frames to MP4 using imageio or PIL fallback."""
        try:
            import imageio  # type: ignore
            writer = imageio.get_writer(str(output_path), fps=fps, codec="libx264")
            import numpy as np  # type: ignore
            for frame in frames:
                if hasattr(frame, "numpy"):
                    arr = frame.numpy()
                elif hasattr(frame, "__array__"):
                    arr = np.asarray(frame)
                else:
                    arr = np.array(frame)
                if arr.dtype != np.uint8:
                    arr = (arr * 255).clip(0, 255).astype(np.uint8)
                writer.append_data(arr)
            writer.close()
            logger.info(f"Video saved to {output_path}")
        except ImportError:
            # Fallback: save individual frames as images
            frames_dir = output_path.parent / f"{output_path.stem}_frames"
            frames_dir.mkdir(exist_ok=True)
            import numpy as np  # type: ignore
            for i, frame in enumerate(frames):
                from PIL import Image  # type: ignore
                if hasattr(frame, "numpy"):
                    arr = frame.numpy()
                elif hasattr(frame, "__array__"):
                    arr = np.asarray(frame)
                else:
                    arr = np.array(frame)
                if arr.dtype != np.uint8:
                    arr = (arr * 255).clip(0, 255).astype(np.uint8)
                Image.fromarray(arr).save(frames_dir / f"frame_{i:04d}.png")
            logger.warning(f"imageio not available - saved {len(frames)} frames to {frames_dir}")

    def get_generation_history(
        self, user_id: Optional[int] = None, limit: int = 50, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Get generation history from database"""
        with db_rw() as conn:
            if user_id is not None:
                rows = conn.execute(
                    """
                    SELECT id, user_id, media_type, prompt, model_used, file_path,
                           file_size, generation_time_ms, status, error_message,
                           created_at, completed_at
                    FROM generated_media
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (user_id, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, user_id, media_type, prompt, model_used, file_path,
                           file_size, generation_time_ms, status, error_message,
                           created_at, completed_at
                    FROM generated_media
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()

        return [
            {
                "id": row[0],
                "user_id": row[1],
                "media_type": row[2],
                "prompt": row[3],
                "model_used": row[4],
                "file_path": row[5],
                "file_size": row[6],
                "generation_time_ms": row[7],
                "status": row[8],
                "error_message": row[9],
                "created_at": row[10],
                "completed_at": row[11],
            }
            for row in rows
        ]

    def get_vram_usage(self) -> Dict[str, Any]:
        """Get current VRAM usage statistics"""
        runtime_status = self._read_runtime_status()
        gpu_stats = self._query_gpu_device_stats()

        if not self.cuda_available or torch is None:
            if gpu_stats:
                return {
                    "available": True,
                    **gpu_stats,
                    "model_loaded": runtime_status.get("current_model"),
                    "runtime_status": runtime_status,
                    "process_visible": False,
                }
            return {"available": False, "message": "CUDA not available"}

        try:
            allocated_gb = torch.cuda.memory_allocated(0) / (1024**3)
            reserved_gb = torch.cuda.memory_reserved(0) / (1024**3)
            free_gb = self.vram_total_gb - reserved_gb

            payload = {
                "available": True,
                "total_gb": round(self.vram_total_gb, 2),
                "allocated_gb": round(allocated_gb, 2),
                "reserved_gb": round(reserved_gb, 2),
                "free_gb": round(free_gb, 2),
                "percent_used": round((allocated_gb / self.vram_total_gb) * 100, 1) if self.vram_total_gb else 0.0,
                "model_loaded": runtime_status.get("current_model") or self.current_model,
                "runtime_status": runtime_status,
                "process_visible": True,
            }
            payload.update(gpu_stats)
            return payload
        except Exception as exc:
            logger.error(f"Failed to get VRAM usage: {exc}")
            return {"available": False, "error": str(exc)}


# Global service instance (lazy initialization)
_media_service: Optional[MediaGenerationService] = None


def get_media_service() -> MediaGenerationService:
    """Get or create the global media generation service"""
    global _media_service
    if _media_service is None:
        cfg = settings()
        media_output_path = getattr(cfg, "media_output_path", None)
        output_dir = Path(media_output_path) if media_output_path else None
        _media_service = MediaGenerationService(output_dir=output_dir)
    return _media_service
