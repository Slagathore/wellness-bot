"""
Mira Image Generation Service
===============================
FastAPI server wrapping FLUX.2 Klein 9B GGUF.
Your Telegram bot hits this with a POST and gets an image back.

Install deps:
    pip install fastapi uvicorn python-multipart

Start:
    python mira_imagegen.py

Or with uvicorn directly (more control):
    uvicorn mira_imagegen:app --host 0.0.0.0 --port 7860

Mira calls it like:
    POST http://localhost:7860/generate
    Body: {"prompt": "a dragon fighting a mech in a neon city"}

Returns: PNG image bytes (content-type: image/png)
"""

import io
import os
import time
import torch
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel, Field

# ============================================================
# CONFIG — Tweak these to taste
# ============================================================
GGUF_PATH = r"C:\path\to\your\flux-2-klein-9b-Q4_K_M.gguf"  # set FLUX2_KLEIN_GGUF_PATH in .env or override here
BASE_REPO = "black-forest-labs/FLUX.2-klein-9B"

# Server settings
HOST = "0.0.0.0"    # Bind to all interfaces (so Mira can reach it from localhost or LAN)
PORT = 7860          # Change if this conflicts with something

# Default generation settings (overridable per-request)
DEFAULT_WIDTH = 1024
DEFAULT_HEIGHT = 1024
DEFAULT_GUIDANCE_SCALE = 4.0
DEFAULT_NUM_STEPS = 4
DEFAULT_SEED = None  # None = random each time

# Performance: compile the transformer for faster inference after first run.
# First generation will be slow (~60s) while it compiles, then subsequent
# ones will be noticeably faster. Set False if compilation causes issues.
USE_TORCH_COMPILE = False  # Set True once you've confirmed basic generation works

# ============================================================
# Logging
# ============================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("mira-imagegen")

# ============================================================
# Global pipeline reference — loaded once at startup
# ============================================================
pipe = None


def load_pipeline():
    """Load the GGUF transformer + build the Flux2Klein pipeline once."""
    global pipe

    log.info("Loading GGUF transformer from: %s", GGUF_PATH)
    from diffusers import Flux2Transformer2DModel, Flux2KleinPipeline, GGUFQuantizationConfig

    transformer = Flux2Transformer2DModel.from_single_file(
        GGUF_PATH,
        quantization_config=GGUFQuantizationConfig(compute_dtype=torch.bfloat16),
        config=BASE_REPO,
        subfolder="transformer",
        torch_dtype=torch.bfloat16,
    )
    log.info("Transformer loaded. Building pipeline...")

    pipe = Flux2KleinPipeline.from_pretrained(
        BASE_REPO,
        transformer=transformer,
        torch_dtype=torch.bfloat16,
    )
    pipe.enable_model_cpu_offload()
    log.info("Pipeline ready with CPU offload enabled.")

    # Optional: compile the transformer for ~20-30% faster inference
    # after the first generation (which triggers compilation).
    if USE_TORCH_COMPILE:
        log.info("Compiling transformer with torch.compile (first gen will be slow)...")
        pipe.transformer = torch.compile(
            pipe.transformer,
            mode="reduce-overhead",  # Optimizes for throughput
            fullgraph=True,
        )
        log.info("Compilation queued — will trigger on first generation.")


# ============================================================
# FastAPI app with lifespan (load model at startup)
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the model when the server starts, clean up when it stops."""
    load_pipeline()
    yield
    # Cleanup on shutdown
    global pipe
    pipe = None
    torch.cuda.empty_cache()
    log.info("Pipeline unloaded, VRAM freed.")

app = FastAPI(
    title="Mira Image Generation",
    description="FLUX.2 Klein 9B GGUF image generation service",
    lifespan=lifespan,
)


# ============================================================
# Request / Response models
# ============================================================
class GenerateRequest(BaseModel):
    prompt: str = Field(..., description="The image description / prompt")
    width: int = Field(DEFAULT_WIDTH, ge=64, le=2048, description="Image width (multiple of 16)")
    height: int = Field(DEFAULT_HEIGHT, ge=64, le=2048, description="Image height (multiple of 16)")
    guidance_scale: float = Field(DEFAULT_GUIDANCE_SCALE, ge=1.0, le=10.0, description="Prompt adherence strength")
    num_inference_steps: int = Field(DEFAULT_NUM_STEPS, ge=1, le=20, description="Denoising steps")
    seed: Optional[int] = Field(DEFAULT_SEED, description="RNG seed (null = random)")


class HealthResponse(BaseModel):
    status: str
    gpu: str
    vram_total_gb: float
    vram_used_gb: float
    vram_free_gb: float
    model_loaded: bool


# ============================================================
# Endpoints
# ============================================================
@app.get("/health", response_model=HealthResponse)
async def health():
    """Check if the service is alive and see VRAM usage."""
    vram_total = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
    vram_used = torch.cuda.memory_allocated(0) / (1024 ** 3)
    return HealthResponse(
        status="ok",
        gpu=torch.cuda.get_device_name(0),
        vram_total_gb=round(vram_total, 2),
        vram_used_gb=round(vram_used, 2),
        vram_free_gb=round(vram_total - vram_used, 2),
        model_loaded=pipe is not None,
    )


@app.post("/generate")
async def generate(req: GenerateRequest):
    """
    Generate an image from a text prompt.
    Returns a PNG image directly (content-type: image/png).

    From Mira's bot code, you'd call this like:
        import httpx
        resp = httpx.post("http://localhost:7860/generate", json={"prompt": "..."})
        image_bytes = resp.content  # This is the PNG
    """
    if pipe is None:
        return Response(content="Model not loaded", status_code=503)

    # Snap dimensions to nearest multiple of 16 (required by the model)
    width = (req.width // 16) * 16
    height = (req.height // 16) * 16

    # Check total pixels don't exceed 4 megapixels (model limit)
    if width * height > 4_194_304:
        return Response(
            content=f"Resolution {width}x{height} exceeds 4MP limit. Reduce dimensions.",
            status_code=400,
        )

    # Build the generator for reproducibility (or random if no seed)
    generator = None
    if req.seed is not None:
        generator = torch.Generator(device="cpu").manual_seed(req.seed)

    log.info(
        "Generating: '%s' | %dx%d | steps=%d | cfg=%.1f | seed=%s",
        req.prompt[:80], width, height,
        req.num_inference_steps, req.guidance_scale,
        req.seed or "random",
    )

    start = time.perf_counter()

    # The actual generation — wrapped in inference_mode for speed + lower VRAM
    with torch.inference_mode():
        result = pipe(
            prompt=req.prompt,
            height=height,
            width=width,
            guidance_scale=req.guidance_scale,
            num_inference_steps=req.num_inference_steps,
            generator=generator,
        )

    elapsed = time.perf_counter() - start
    peak_vram = torch.cuda.max_memory_allocated() / (1024 ** 3)
    log.info("Generated in %.1fs | Peak VRAM: %.2f GB", elapsed, peak_vram)

    # Convert PIL image to PNG bytes
    image = result.images[0]
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    buf.seek(0)

    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={
            "X-Generation-Time": f"{elapsed:.2f}s",
            "X-Peak-VRAM-GB": f"{peak_vram:.2f}",
        },
    )


# ============================================================
# Run directly with: python mira_imagegen.py
# ============================================================
if __name__ == "__main__":
    import uvicorn
    log.info("Starting Mira Image Generation Service on %s:%d", HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT)