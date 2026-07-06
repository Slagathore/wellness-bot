"""FastAPI server for the roleplay/adventure Telegram Mini App.

Auth is per-user via Telegram WebApp initData (no shared password), so this is
distinct from the operator admin panel. Runs standalone or can be mounted.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import settings
from app.interfaces.webapp.auth import (InitDataError, parse_webapp_user,
                                        verify_init_data)
from app.interfaces.webapp.service import AdventureNotFound, WebappService

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Wellness Roleplay Mini App")
service = WebappService()

if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


async def current_user_id(request: Request) -> int:
    """Resolve the DB user id from verified Telegram initData."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("tma "):
        init_data = auth[4:]
    else:
        init_data = request.headers.get("X-Telegram-Init-Data", "")
    cfg = settings()
    try:
        fields = verify_init_data(
            init_data,
            cfg.telegram_bot_token,
            max_age_seconds=cfg.webapp_initdata_max_age_seconds,
        )
        webapp_user = parse_webapp_user(fields)
    except InitDataError as exc:
        raise HTTPException(status_code=401, detail=f"invalid initData: {exc}") from exc
    return service.ensure_user(
        webapp_user.telegram_user_id,
        username=webapp_user.username,
        first_name=webapp_user.first_name,
    )


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
