"""Client for the DungeonMaster SDXL image backend (dm_imagegen).

All image generation is delegated to the standalone DM image server
(`python dm_imagegen.py --serve`, default http://127.0.0.1:8500). That server
keeps a model resident and uses fast baked recipes (jugg = SFW realism,
lustify = NSFW realism, wai = anime, pony = anthro/cartoon; 4-8 steps), so we
never import torch/diffusers here — we POST a prompt and get a PNG back.

If the server is disabled or unreachable, callers get a clear ImageUnavailable
error instead of a crash.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Styles the DM router understands (its STYLE_TO_MODEL map). A curated subset
# for pickers; anything else falls back to the server's default routing.
STYLES = (
    "scene",       # jugg — realistic environments/landscapes
    "portrait",    # jugg — realistic character portrait
    "cinematic",   # jugg — cinematic realism
    "anime",       # wai — illustrated/anime
    "character",   # wai — illustrated character
    "pony",        # pony — anthro/cartoon
)


class ImageUnavailable(Exception):
    """Raised when image generation is disabled, unreachable, or errors."""


def _base_url() -> str:
    return str(getattr(settings(), "dm_image_url", "http://127.0.0.1:8500")).rstrip("/")


def is_enabled() -> bool:
    return bool(getattr(settings(), "dm_image_enabled", False))


async def image_health() -> Optional[Dict[str, Any]]:
    """Return the DM server's /health payload, or None if disabled/unreachable."""
    if not is_enabled():
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{_base_url()}/health")
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("DM image health check failed: %s", exc)
        return None


async def generate_image(
    subject: str,
    *,
    style: Optional[str] = None,
    nsfw: bool = False,
    seed: Optional[int] = None,
) -> bytes:
    """Generate one image via the DM server and return raw PNG bytes.

    Raises ImageUnavailable on any failure (disabled, unreachable, non-image
    response) so callers can degrade gracefully.
    """
    if not is_enabled():
        raise ImageUnavailable("image generation is disabled")
    subject = (subject or "").strip()
    if not subject:
        raise ImageUnavailable("empty image subject")

    spec: Dict[str, Any] = {"subject": subject[:1200], "nsfw": bool(nsfw)}
    if style:
        spec["style"] = style
    if seed is not None:
        spec["seed"] = int(seed)

    timeout = float(getattr(settings(), "dm_image_timeout_seconds", 300.0))
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{_base_url()}/generate", json=spec)
    except httpx.HTTPError as exc:
        raise ImageUnavailable(
            f"image server unreachable at {_base_url()} — is `dm_imagegen.py --serve` running? ({exc})"
        ) from exc

    if resp.status_code != 200:
        raise ImageUnavailable(f"image server error {resp.status_code}: {resp.text[:200]}")
    content = resp.content
    if not content or not resp.headers.get("content-type", "").startswith("image/"):
        raise ImageUnavailable("image server did not return an image")
    return content
