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


# "Shot" framing hints appended to the distilled prompt so the control does real
# compositional work (the DM dialect doesn't use `style` for framing). Shared by
# the Mini App and Telegram so both surfaces behave identically.
SHOT_FRAMING = {
    "scene": "wide establishing shot, full environment, cinematic composition",
    "portrait": "close-up portrait, head and shoulders, shallow depth of field",
    "cinematic": "cinematic film still, dramatic lighting, anamorphic, detailed",
}
# Friendly engine aliases -> concrete DM model keys.
_ENGINE_ALIAS = {"anime": "wai", "anthro": "pony"}
ENGINE_CHOICES = ("auto", "realistic", "anime", "anthro")


def resolve_engine(engine: Optional[str], nsfw: bool) -> Optional[str]:
    """Map a friendly engine choice to a DM model key. 'realistic' splits by
    rating; None/'auto' returns None so the server routes from style+nsfw."""
    if not engine or engine == "auto":
        return None
    if engine == "realistic":
        return "lustify" if nsfw else "jugg"
    return _ENGINE_ALIAS.get(engine, engine)  # concrete key passthrough


def apply_shot(prompt: str, shot: Optional[str]) -> str:
    hint = SHOT_FRAMING.get((shot or "").lower())
    return f"{prompt}, {hint}" if hint and prompt else prompt


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
    engine: Optional[str] = None,
    loras: Optional[list] = None,
) -> bytes:
    """Generate one image via the DM server and return raw PNG bytes.

    `engine` pins a specific model (jugg/lustify/wai/pony); when None the server
    routes from `style` + `nsfw`. `loras` is a list of {"file": str, "weight":
    float}; the server hard-gates each to the engine's ecosystem.

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
    if engine:
        spec["model"] = engine
    clean_loras = [
        {"file": lo["file"], "weight": float(lo.get("weight", 0.8))}
        for lo in (loras or []) if isinstance(lo, dict) and lo.get("file")
    ]
    if clean_loras:
        spec["loras"] = clean_loras
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


async def _get_json(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    if not is_enabled():
        raise ImageUnavailable("image generation is disabled")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{_base_url()}{path}", params=params)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        raise ImageUnavailable(f"image server unreachable ({exc})") from exc


async def _post_json(path: str, body: Dict[str, Any], *, timeout: float = 180.0) -> Any:
    if not is_enabled():
        raise ImageUnavailable("image generation is disabled")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{_base_url()}{path}", json=body)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        raise ImageUnavailable(f"image server unreachable ({exc})") from exc


async def list_engines() -> list:
    """Available engines with ecosystem + LoRA-support metadata (or [] if down)."""
    try:
        data = await _get_json("/engines")
        return data if isinstance(data, list) else []
    except ImageUnavailable:
        return []


async def list_loras(engine: str) -> list:
    """LoRAs matching the given engine's ecosystem, scanned fresh (hot-loads)."""
    if not engine:
        return []
    try:
        data = await _get_json("/loras", params={"engine": engine})
        return data if isinstance(data, list) else []
    except ImageUnavailable:
        return []


async def search_loras(query: str, engine: str, *, nsfw: bool = True, limit: int = 20) -> Dict[str, Any]:
    """Search Civitai for engine-ecosystem LoRAs. Returns {results, ...} or {error}."""
    try:
        return await _post_json(
            "/loras/search",
            {"query": query or "", "engine": engine, "nsfw": bool(nsfw), "limit": int(limit)},
        )
    except ImageUnavailable as exc:
        return {"results": [], "error": str(exc)}


async def download_lora(
    engine: str,
    *,
    version_id: Optional[int] = None,
    url: Optional[str] = None,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """Download a Civitai LoRA into its ecosystem bucket. Returns {ok, file, ...}."""
    body: Dict[str, Any] = {"engine": engine}
    if version_id is not None:
        body["version_id"] = int(version_id)
    if url:
        body["url"] = url
    if name:
        body["name"] = name
    try:
        return await _post_json("/loras/download", body, timeout=300.0)
    except ImageUnavailable as exc:
        return {"ok": False, "error": str(exc)}
