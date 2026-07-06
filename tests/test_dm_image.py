"""Tests for the DungeonMaster image client."""

from __future__ import annotations

import httpx
import pytest

from app.services import dm_image
from app.services.dm_image import ImageUnavailable, generate_image


class _FakeResp:
    def __init__(self, status_code=200, content=b"\x89PNG", content_type="image/png", text=""):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": content_type}
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)  # type: ignore[arg-type]


class _FakeClient:
    resp: object = None
    raise_exc: Exception | None = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        if _FakeClient.raise_exc:
            raise _FakeClient.raise_exc
        return _FakeClient.resp

    async def get(self, url):
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
