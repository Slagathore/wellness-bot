"""FastAPI wrapper around the local FLUX.2 Klein GGUF pipeline."""

from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

import torch  # type: ignore[reportMissingImports]
from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.config import settings

log = logging.getLogger("flux2-klein-server")

_pipe: Any | None = None
_generate_lock = threading.Lock()

DEFAULT_WIDTH = 1024
DEFAULT_HEIGHT = 1024
DEFAULT_GUIDANCE_SCALE = 4.0
DEFAULT_NUM_STEPS = 4
MAX_TOTAL_PIXELS = 4_194_304


def _cfg():
    return settings()


def load_pipeline() -> None:
    """Load the transformer and pipeline once at process startup."""
    global _pipe
    if _pipe is not None:
        return

    cfg = _cfg()
    gguf_path = str(cfg.flux2_klein_gguf_path).strip()
    base_repo = str(cfg.flux2_klein_base_repo).strip()

    log.info("Loading FLUX.2 Klein GGUF transformer from %s", gguf_path)
    from diffusers import (  # type: ignore[reportMissingImports]
        Flux2KleinPipeline, Flux2Transformer2DModel, GGUFQuantizationConfig)

    transformer = Flux2Transformer2DModel.from_single_file(
        gguf_path,
        quantization_config=GGUFQuantizationConfig(compute_dtype=torch.bfloat16),
        config=base_repo,
        subfolder="transformer",
        torch_dtype=torch.bfloat16,
    )
    log.info("Transformer loaded. Building FLUX.2 Klein pipeline.")

    pipe = Flux2KleinPipeline.from_pretrained(
        base_repo,
        transformer=transformer,
        torch_dtype=torch.bfloat16,
    )
    if torch.cuda.is_available():
        pipe.enable_model_cpu_offload()
        log.info("Pipeline ready with CPU offload enabled.")
    else:
        pipe.to("cpu")
        log.warning("CUDA unavailable; FLUX.2 Klein is running on CPU.")

    if bool(cfg.flux2_klein_use_torch_compile):
        log.info("Compiling transformer with torch.compile; first generation will be slower.")
        pipe.transformer = torch.compile(
            pipe.transformer,
            mode="reduce-overhead",
            fullgraph=True,
        )

    _pipe = pipe


def unload_pipeline() -> None:
    """Release the loaded pipeline and any cached CUDA state."""
    global _pipe
    _pipe = None
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    log.info("FLUX.2 Klein pipeline unloaded.")


def _gpu_summary() -> tuple[str, float, float, float]:
    if not torch.cuda.is_available():
        return "cpu", 0.0, 0.0, 0.0
    vram_total = torch.cuda.get_device_properties(0).total_memory / (1024**3)
    vram_used = torch.cuda.memory_allocated(0) / (1024**3)
    vram_free = max(vram_total - vram_used, 0.0)
    return (
        torch.cuda.get_device_name(0),
        round(vram_total, 2),
        round(vram_used, 2),
        round(vram_free, 2),
    )


def _snap_dimension(value: int) -> int:
    return max(64, (int(value) // 16) * 16)


def _generate_png_bytes(req: "GenerateRequest") -> tuple[bytes, float, float]:
    if _pipe is None:
        raise RuntimeError("FLUX.2 Klein pipeline is not loaded.")

    width = _snap_dimension(req.width)
    height = _snap_dimension(req.height)
    if width * height > MAX_TOTAL_PIXELS:
        raise ValueError(
            f"Resolution {width}x{height} exceeds the 4MP model limit. Reduce dimensions."
        )

    generator = None
    if req.seed is not None:
        generator = torch.Generator(device="cpu").manual_seed(int(req.seed))

    log.info(
        "Generating image prompt=%r size=%dx%d steps=%d guidance=%.2f seed=%s",
        req.prompt[:120],
        width,
        height,
        req.num_inference_steps,
        req.guidance_scale,
        req.seed if req.seed is not None else "random",
    )

    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()

    started = time.perf_counter()
    with _generate_lock:
        with torch.inference_mode():
            result = _pipe(
                prompt=req.prompt,
                width=width,
                height=height,
                guidance_scale=req.guidance_scale,
                num_inference_steps=req.num_inference_steps,
                generator=generator,
            )
    elapsed = time.perf_counter() - started
    peak_vram_gb = (
        torch.cuda.max_memory_allocated() / (1024**3) if torch.cuda.is_available() else 0.0
    )

    image = result.images[0]
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue(), elapsed, peak_vram_gb


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(load_pipeline)
    try:
        yield
    finally:
        unload_pipeline()


app = FastAPI(
    title="FLUX.2 Klein Image Generation",
    description="Local FLUX.2 Klein 9B GGUF image generation service",
    lifespan=lifespan,
)


class GenerateRequest(BaseModel):
    prompt: str = Field(..., description="The image prompt.")
    width: int = Field(DEFAULT_WIDTH, ge=64, le=2048)
    height: int = Field(DEFAULT_HEIGHT, ge=64, le=2048)
    guidance_scale: float = Field(DEFAULT_GUIDANCE_SCALE, ge=1.0, le=10.0)
    num_inference_steps: int = Field(DEFAULT_NUM_STEPS, ge=1, le=20)
    seed: int | None = Field(default=None)


class HealthResponse(BaseModel):
    status: str
    gpu: str
    vram_total_gb: float
    vram_used_gb: float
    vram_free_gb: float
    model_loaded: bool


@app.post("/unload")
async def unload() -> Response:
    """Unload the pipeline and free VRAM — called by MediaGenerationService before loading a local SDXL model."""
    await asyncio.to_thread(unload_pipeline)
    return Response(content='{"status":"unloaded"}', media_type="application/json")


@app.post("/reload")
async def reload() -> Response:
    """Reload the pipeline after a local SDXL model has finished."""
    try:
        await asyncio.to_thread(load_pipeline)
        return Response(content='{"status":"loaded"}', media_type="application/json")
    except Exception as exc:
        log.exception("Reload failed: %s", exc)
        return Response(content=f'{{"status":"error","detail":"{exc}"}}', media_type="application/json", status_code=500)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    gpu_name, vram_total, vram_used, vram_free = _gpu_summary()
    return HealthResponse(
        status="ok",
        gpu=gpu_name,
        vram_total_gb=vram_total,
        vram_used_gb=vram_used,
        vram_free_gb=vram_free,
        model_loaded=_pipe is not None,
    )


@app.post("/generate")
async def generate(req: GenerateRequest) -> Response:
    if _pipe is None:
        return Response(content="Model not loaded", status_code=503)

    try:
        image_bytes, elapsed, peak_vram_gb = await asyncio.to_thread(_generate_png_bytes, req)
    except ValueError as exc:
        return Response(content=str(exc), status_code=400)
    except Exception as exc:
        log.exception("FLUX.2 Klein generation failed")
        return Response(content=str(exc), status_code=500)

    return Response(
        content=image_bytes,
        media_type="image/png",
        headers={
            "X-Generation-Time": f"{elapsed:.2f}s",
            "X-Peak-VRAM-GB": f"{peak_vram_gb:.2f}",
        },
    )


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
    cfg = _cfg()
    host = str(cfg.flux2_klein_host).strip() or "0.0.0.0"
    port = int(cfg.flux2_klein_port or 7865)
    log.info("Starting FLUX.2 Klein image service on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port)
