from __future__ import annotations

import itertools

import pytest

from app.infra.llm.client import LLMClient


def _flaky_fn_factory(failures: int, result: str):
    attempts = itertools.count()

    def _fn(*args, **kwargs):
        idx = next(attempts)
        if idx < failures:
            raise RuntimeError("boom")
        return result

    return _fn


def test_llm_client_retries_then_succeeds():
    client = LLMClient(
        chat_fn=_flaky_fn_factory(2, "ok"), max_retries=3, backoff_seconds=0
    )
    assert client.chat(messages=[{"role": "user", "content": "hi"}]) == "ok"


def test_llm_client_raises_after_retries():
    client = LLMClient(
        chat_fn=_flaky_fn_factory(5, "ok"), max_retries=1, backoff_seconds=0
    )
    with pytest.raises(RuntimeError):
        client.chat(messages=[{"role": "user", "content": "hi"}])
