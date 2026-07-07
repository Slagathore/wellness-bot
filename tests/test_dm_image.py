"""Tests for the DungeonMaster image client."""

from __future__ import annotations

import httpx
import pytest

from app.services import dm_image
from app.services.dm_image import ImageUnavailable, generate_image


class _FakeResp:
    def __init__(self, status_code=200, content=b"\x89PNG", content_type="image/png", text="", json_data=None):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": content_type}
        self.text = text
        self._json = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)  # type: ignore[arg-type]

    def json(self):
        return self._json


class _FakeClient:
    resp: object = None
    raise_exc: Exception | None = None
    last_json: object = None
    last_url: str = ""
    last_params: object = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        _FakeClient.last_url = url
        _FakeClient.last_json = json
        if _FakeClient.raise_exc:
            raise _FakeClient.raise_exc
        return _FakeClient.resp

    async def get(self, url, params=None):
        _FakeClient.last_url = url
        _FakeClient.last_params = params
        if _FakeClient.raise_exc:
            raise _FakeClient.raise_exc
        return _FakeClient.resp


@pytest.fixture(autouse=True)
def _patch_httpx(monkeypatch, test_config):
    # dm_image binds `from app.config import settings` at import, so point its
    # reference at the test config (same stale-binding pattern as elsewhere).
    monkeypatch.setattr(dm_image.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(dm_image, "settings", lambda: test_config)
    _FakeClient.resp = None
    _FakeClient.raise_exc = None
    _FakeClient.last_json = None
    _FakeClient.last_url = ""
    _FakeClient.last_params = None
    yield


@pytest.mark.asyncio
async def test_disabled_raises(test_config):
    test_config.dm_image_enabled = False
    with pytest.raises(ImageUnavailable):
        await generate_image("a castle")


@pytest.mark.asyncio
async def test_empty_subject_raises(test_config):
    with pytest.raises(ImageUnavailable):
        await generate_image("   ")


@pytest.mark.asyncio
async def test_success_returns_bytes(test_config):
    _FakeClient.resp = _FakeResp(content=b"\x89PNG-data")
    out = await generate_image("an orc warband at dawn", style="scene")
    assert out == b"\x89PNG-data"


@pytest.mark.asyncio
async def test_server_error_raises(test_config):
    _FakeClient.resp = _FakeResp(status_code=500, content=b"", content_type="text/plain", text="boom")
    with pytest.raises(ImageUnavailable):
        await generate_image("x")


@pytest.mark.asyncio
async def test_non_image_response_raises(test_config):
    _FakeClient.resp = _FakeResp(content=b"{}", content_type="application/json")
    with pytest.raises(ImageUnavailable):
        await generate_image("x")


@pytest.mark.asyncio
async def test_unreachable_raises(test_config):
    _FakeClient.raise_exc = httpx.ConnectError("refused")
    with pytest.raises(ImageUnavailable):
        await generate_image("x")


@pytest.mark.asyncio
async def test_generate_spec_includes_engine_and_loras(test_config):
    _FakeClient.resp = _FakeResp(content=b"\x89PNG")
    await generate_image(
        "a knight", style="scene", engine="wai",
        loras=[{"file": "illustrious/foo.safetensors", "weight": 0.7},
               {"file": "illustrious/bar.safetensors"}],  # default weight
    )
    spec = _FakeClient.last_json
    assert spec["model"] == "wai"
    assert spec["style"] == "scene"
    assert spec["loras"] == [
        {"file": "illustrious/foo.safetensors", "weight": 0.7},
        {"file": "illustrious/bar.safetensors", "weight": 0.8},
    ]


@pytest.mark.asyncio
async def test_generate_omits_empty_loras(test_config):
    _FakeClient.resp = _FakeResp(content=b"\x89PNG")
    await generate_image("a knight", loras=[{"weight": 0.5}])  # no file -> dropped
    assert "loras" not in _FakeClient.last_json


def test_resolve_engine():
    assert dm_image.resolve_engine("realistic", True) == "lustify"
    assert dm_image.resolve_engine("realistic", False) == "jugg"
    assert dm_image.resolve_engine("anime", False) == "wai"
    assert dm_image.resolve_engine("anthro", False) == "pony"
    assert dm_image.resolve_engine("auto", True) is None
    assert dm_image.resolve_engine(None, False) is None
    assert dm_image.resolve_engine("wai", False) == "wai"  # concrete passthrough


def test_apply_shot():
    assert "close-up portrait" in dm_image.apply_shot("a knight", "portrait")
    assert dm_image.apply_shot("a knight", None) == "a knight"
    assert dm_image.apply_shot("", "portrait") == ""  # nothing to frame


@pytest.mark.asyncio
async def test_list_engines_returns_list(test_config):
    _FakeClient.resp = _FakeResp(json_data=[{"key": "wai", "supports_lora": True}])
    out = await dm_image.list_engines()
    assert out == [{"key": "wai", "supports_lora": True}]
    assert _FakeClient.last_url.endswith("/engines")


@pytest.mark.asyncio
async def test_list_loras_passes_engine(test_config):
    _FakeClient.resp = _FakeResp(json_data=[{"file": "pony/x.safetensors"}])
    out = await dm_image.list_loras("pony")
    assert out and out[0]["file"] == "pony/x.safetensors"
    assert _FakeClient.last_params == {"engine": "pony"}


@pytest.mark.asyncio
async def test_list_loras_empty_engine_short_circuits(test_config):
    assert await dm_image.list_loras("") == []


@pytest.mark.asyncio
async def test_discovery_degrades_gracefully_when_down(test_config):
    _FakeClient.raise_exc = httpx.ConnectError("refused")
    assert await dm_image.list_engines() == []
    assert await dm_image.list_loras("wai") == []
    assert (await dm_image.search_loras("q", "wai")).get("results") == []
    assert (await dm_image.download_lora("wai", version_id=1)).get("ok") is False


@pytest.mark.asyncio
async def test_download_lora_builds_body(test_config):
    _FakeClient.resp = _FakeResp(json_data={"ok": True, "file": "pony/x.safetensors"})
    out = await dm_image.download_lora("pony", version_id=42, name="Cool LoRA")
    assert out["ok"] is True
    assert _FakeClient.last_json == {"engine": "pony", "version_id": 42, "name": "Cool LoRA"}
