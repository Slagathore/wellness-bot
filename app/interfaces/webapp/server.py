"""FastAPI server for the roleplay/adventure Telegram Mini App.

Auth is per-user via Telegram WebApp initData (no shared password), so this is
distinct from the operator admin panel. Runs standalone or can be mounted.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import settings
from app.interfaces.webapp.auth import (InitDataError, parse_webapp_user,
                                        session_secret, sign_session,
                                        verify_init_data, verify_session)
from app.interfaces.webapp.service import AdventureNotFound, WebappService
from app.services.dm_image import (
    ImageUnavailable,
    download_lora,
    image_health,
    is_enabled,
    list_engines,
    list_loras,
    search_loras,
)

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Wellness Roleplay Mini App")
service = WebappService()

if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


_SESSION_COOKIE = "mira_session"


def _session_secret() -> bytes:
    return session_secret(settings().telegram_bot_token or "")


def _set_session_cookie(request: Request, response: Response, uid: int) -> None:
    """Issue a signed session cookie. Secure when the edge connection is HTTPS
    (the Cloudflare tunnel sets X-Forwarded-Proto: https)."""
    cfg = settings()
    ttl = int(getattr(cfg, "webapp_session_ttl_seconds", 604800))
    token = sign_session(uid, _session_secret(), ttl_seconds=ttl)
    secure = (request.headers.get("x-forwarded-proto", "").lower() == "https"
              or request.url.scheme == "https")
    response.set_cookie(
        _SESSION_COOKIE, token, max_age=ttl,
        httponly=True, secure=secure, samesite="lax", path="/",
    )


async def current_user_id(request: Request) -> int:
    """Resolve the DB user id from verified Telegram initData, or (for browser
    access outside Telegram) a signed session cookie."""
    cfg = settings()
    auth = request.headers.get("Authorization", "")
    init_data = auth[4:] if auth.startswith("tma ") else request.headers.get("X-Telegram-Init-Data", "")
    if init_data.strip():
        try:
            fields = verify_init_data(
                init_data,
                cfg.telegram_bot_token,
                max_age_seconds=cfg.webapp_initdata_max_age_seconds,
            )
            webapp_user = parse_webapp_user(fields)
            return service.ensure_user(
                webapp_user.telegram_user_id,
                username=webapp_user.username,
                first_name=webapp_user.first_name,
            )
        except InitDataError:
            pass  # fall through to the browser session cookie
    uid = verify_session(request.cookies.get(_SESSION_COOKIE, ""), _session_secret())
    if uid is not None:
        return uid
    raise HTTPException(status_code=401, detail="not authenticated")


class TurnRequest(BaseModel):
    text: str
    mode: str = "do"  # do | say | story


class CreateAdventureRequest(BaseModel):
    title: str
    premise: str = ""
    player_role: str = ""
    character_ids: list[int] = []


class CreateCharacterRequest(BaseModel):
    name: str = ""
    description: str = ""


class AttachCharacterRequest(BaseModel):
    character_id: int
    role: str = "npc"


class MemoryUpdateRequest(BaseModel):
    lore: Optional[str] = None
    player_role: Optional[str] = None
    objective: Optional[str] = None


class ImageRequest(BaseModel):
    subject: Optional[str] = None
    style: Optional[str] = None
    shot: Optional[str] = None
    engine: Optional[str] = None
    nsfw: Optional[bool] = None
    loras: Optional[List[Dict[str, Any]]] = None


class LoraSearchRequest(BaseModel):
    query: Optional[str] = None
    engine: str
    nsfw: bool = True
    limit: int = 20


class LoraDownloadRequest(BaseModel):
    engine: str
    version_id: Optional[int] = None
    url: Optional[str] = None
    name: Optional[str] = None


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    index_path = _STATIC_DIR / "index.html"
    if not index_path.is_file():
        raise HTTPException(status_code=500, detail="Mini App frontend missing")
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


@app.get("/api/me", response_class=JSONResponse)
async def api_me(user_id: int = Depends(current_user_id)) -> Dict[str, Any]:
    return {"user_id": user_id}


class LoginRequest(BaseModel):
    token: str


@app.get("/api/auth/config", response_class=JSONResponse)
async def api_auth_config() -> Dict[str, Any]:
    """Public: whether browser password login is available (no auth required)."""
    return {"password_enabled": bool(getattr(settings(), "webapp_access_token", None))}


@app.post("/api/login", response_class=JSONResponse)
async def api_login(payload: LoginRequest, request: Request, response: Response) -> Dict[str, Any]:
    """Browser password login -> session cookie for the ADMIN_USERNAME account."""
    secret = getattr(settings(), "webapp_access_token", None)
    if not secret:
        raise HTTPException(status_code=404, detail="browser login is disabled")
    if not hmac.compare_digest((payload.token or "").encode(), str(secret).encode()):
        await asyncio.sleep(0.5)  # small constant delay to slow brute force
        raise HTTPException(status_code=401, detail="wrong password")
    uid = service.owner_user_id()
    if uid is None:
        raise HTTPException(status_code=500, detail="owner account not found (check ADMIN_USERNAME)")
    _set_session_cookie(request, response, uid)
    return {"ok": True}


@app.get("/login")
async def login_magic(request: Request, t: str = "") -> Response:
    """Per-user magic link from the Telegram bot: verify the signed token, set a
    session cookie, and redirect into the app."""
    uid = verify_session(t, _session_secret())
    if uid is None:
        return HTMLResponse(
            "<h3>This link is invalid or has expired.</h3>"
            "<p>Open the bot in Telegram and use <b>/webapp</b> for a fresh link.</p>",
            status_code=401,
        )
    resp = RedirectResponse(url="/", status_code=302)
    _set_session_cookie(request, resp, uid)
    return resp


@app.post("/api/logout", response_class=JSONResponse)
async def api_logout(response: Response) -> Dict[str, Any]:
    response.delete_cookie(_SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/image/health", response_class=JSONResponse)
async def api_image_health(user_id: int = Depends(current_user_id)) -> Dict[str, Any]:
    """Image availability + this user's NSFW permission + engine list (one round-trip)."""
    available = bool(is_enabled() and (await image_health()) is not None)
    out: Dict[str, Any] = {"available": available, "nsfw_allowed": service.nsfw_opt_in(user_id)}
    if available:
        out["engines"] = await list_engines()
    return out


@app.get("/api/image/engines", response_class=JSONResponse)
async def api_image_engines(user_id: int = Depends(current_user_id)) -> Dict[str, Any]:
    """Engines available on the DM server (with ecosystem + LoRA-support flags)."""
    return {"engines": await list_engines()}


@app.get("/api/image/loras", response_class=JSONResponse)
async def api_image_loras(
    engine: str, user_id: int = Depends(current_user_id)
) -> Dict[str, Any]:
    """LoRAs matching the given engine's ecosystem (fresh scan — hot-loads new files)."""
    return {"loras": await list_loras(engine)}


@app.post("/api/image/loras/search", response_class=JSONResponse)
async def api_image_loras_search(
    payload: LoraSearchRequest, user_id: int = Depends(current_user_id)
) -> Dict[str, Any]:
    return await search_loras(
        payload.query or "", payload.engine, nsfw=payload.nsfw, limit=payload.limit
    )


@app.post("/api/image/loras/download", response_class=JSONResponse)
async def api_image_loras_download(
    payload: LoraDownloadRequest, user_id: int = Depends(current_user_id)
) -> Dict[str, Any]:
    return await download_lora(
        payload.engine, version_id=payload.version_id, url=payload.url, name=payload.name
    )


@app.post("/api/adventures/{adventure_id}/image")
async def api_scene_image(
    adventure_id: int,
    payload: ImageRequest = ImageRequest(),
    user_id: int = Depends(current_user_id),
) -> Response:
    try:
        png = await service.illustrate_scene(
            user_id, adventure_id, subject=payload.subject, style=payload.style or "scene",
            shot=payload.shot, engine=payload.engine, loras=payload.loras, nsfw=payload.nsfw,
        )
    except AdventureNotFound:
        raise HTTPException(status_code=404, detail="adventure not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ImageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return Response(content=png, media_type="image/png")


@app.post("/api/characters/{character_id}/image")
async def api_character_image(
    character_id: int,
    payload: ImageRequest = ImageRequest(),
    user_id: int = Depends(current_user_id),
) -> Response:
    try:
        png = await service.character_portrait(
            user_id, character_id, shot=payload.shot, engine=payload.engine,
            loras=payload.loras, nsfw=payload.nsfw,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ImageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return Response(content=png, media_type="image/png")


@app.get("/api/characters", response_class=JSONResponse)
async def api_characters(user_id: int = Depends(current_user_id)) -> Dict[str, Any]:
    return {"characters": service.list_characters(user_id)}


@app.post("/api/characters", response_class=JSONResponse)
async def api_create_character(
    payload: CreateCharacterRequest, user_id: int = Depends(current_user_id)
) -> Dict[str, Any]:
    try:
        return await service.create_character(
            user_id, name=payload.name, description=payload.description
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/adventures/{adventure_id}/characters", response_class=JSONResponse)
async def api_adventure_characters(
    adventure_id: int, user_id: int = Depends(current_user_id)
) -> Dict[str, Any]:
    try:
        return {"characters": service.list_adventure_characters(user_id, adventure_id)}
    except AdventureNotFound:
        raise HTTPException(status_code=404, detail="adventure not found")


@app.post("/api/adventures/{adventure_id}/characters", response_class=JSONResponse)
async def api_attach_character(
    adventure_id: int,
    payload: AttachCharacterRequest,
    user_id: int = Depends(current_user_id),
) -> Dict[str, Any]:
    try:
        return service.attach_character(
            user_id, adventure_id, payload.character_id, payload.role
        )
    except AdventureNotFound:
        raise HTTPException(status_code=404, detail="adventure not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/adventures/{adventure_id}/characters/{character_id}", response_class=JSONResponse)
async def api_detach_character(
    adventure_id: int, character_id: int, user_id: int = Depends(current_user_id)
) -> Dict[str, Any]:
    try:
        return service.detach_character(user_id, adventure_id, character_id)
    except AdventureNotFound:
        raise HTTPException(status_code=404, detail="adventure not found")


@app.get("/api/adventures/{adventure_id}/memory", response_class=JSONResponse)
async def api_get_memory(
    adventure_id: int, user_id: int = Depends(current_user_id)
) -> Dict[str, Any]:
    try:
        return service.get_memory(user_id, adventure_id)
    except AdventureNotFound:
        raise HTTPException(status_code=404, detail="adventure not found")


@app.put("/api/adventures/{adventure_id}/memory", response_class=JSONResponse)
async def api_update_memory(
    adventure_id: int,
    payload: MemoryUpdateRequest,
    user_id: int = Depends(current_user_id),
) -> Dict[str, Any]:
    try:
        return service.update_memory(
            user_id,
            adventure_id,
            lore=payload.lore,
            player_role=payload.player_role,
            objective=payload.objective,
        )
    except AdventureNotFound:
        raise HTTPException(status_code=404, detail="adventure not found")


@app.get("/api/adventures", response_class=JSONResponse)
async def api_adventures(
    offset: int = 0, limit: int = 20, user_id: int = Depends(current_user_id)
) -> Dict[str, Any]:
    return service.list_adventures(user_id, offset=offset, limit=limit)


@app.post("/api/adventures", response_class=JSONResponse)
async def api_create_adventure(
    payload: CreateAdventureRequest, user_id: int = Depends(current_user_id)
) -> Dict[str, Any]:
    if not payload.title.strip() and not payload.premise.strip():
        raise HTTPException(status_code=400, detail="title or premise required")
    return await service.create_adventure(
        user_id,
        title=payload.title,
        premise=payload.premise,
        player_role=payload.player_role,
        character_ids=payload.character_ids,
    )


@app.get("/api/adventures/{adventure_id}", response_class=JSONResponse)
async def api_adventure(
    adventure_id: int, user_id: int = Depends(current_user_id)
) -> Dict[str, Any]:
    try:
        return service.get_adventure(user_id, adventure_id)
    except AdventureNotFound:
        raise HTTPException(status_code=404, detail="adventure not found")


@app.get("/api/adventures/{adventure_id}/messages", response_class=JSONResponse)
async def api_messages(
    adventure_id: int, limit: int = 50, user_id: int = Depends(current_user_id)
) -> Dict[str, Any]:
    try:
        return {"messages": service.list_messages(user_id, adventure_id, limit=limit)}
    except AdventureNotFound:
        raise HTTPException(status_code=404, detail="adventure not found")


@app.post("/api/adventures/{adventure_id}/turn", response_class=JSONResponse)
async def api_turn(
    adventure_id: int,
    payload: TurnRequest,
    user_id: int = Depends(current_user_id),
) -> Dict[str, Any]:
    try:
        return await service.generate_turn(
            user_id, adventure_id, payload.text, mode=payload.mode
        )
    except AdventureNotFound:
        raise HTTPException(status_code=404, detail="adventure not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/adventures/{adventure_id}/continue", response_class=JSONResponse)
async def api_continue(
    adventure_id: int, user_id: int = Depends(current_user_id)
) -> Dict[str, Any]:
    try:
        return await service.continue_story(user_id, adventure_id)
    except AdventureNotFound:
        raise HTTPException(status_code=404, detail="adventure not found")


@app.post("/api/adventures/{adventure_id}/retry", response_class=JSONResponse)
async def api_retry(
    adventure_id: int, user_id: int = Depends(current_user_id)
) -> Dict[str, Any]:
    try:
        return await service.retry_last(user_id, adventure_id)
    except AdventureNotFound:
        raise HTTPException(status_code=404, detail="adventure not found")


@app.post("/api/adventures/{adventure_id}/erase", response_class=JSONResponse)
async def api_erase(
    adventure_id: int, user_id: int = Depends(current_user_id)
) -> Dict[str, Any]:
    try:
        return service.erase_last(user_id, adventure_id)
    except AdventureNotFound:
        raise HTTPException(status_code=404, detail="adventure not found")


def run(host: str | None = None, port: int | None = None) -> None:
    import uvicorn

    cfg = settings()
    uvicorn.run(
        app,
        host=host or cfg.webapp_host,
        port=port or cfg.webapp_port,
    )


if __name__ == "__main__":  # pragma: no cover
    run()
