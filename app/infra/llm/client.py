"""
LLM client wrapper to standardize chat/generation/vision calls with retries/timeouts.

Provides both synchronous (chat, generate, vision) and asynchronous (chat_async)
methods.  The async path uses httpx under the hood through ollama.chat_async(),
which avoids blocking the asyncio event loop — critical for FastAPI endpoints.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, Iterable, Optional

from app.config import settings
from app.utils import ollama

logger = logging.getLogger(__name__)


LLMResponse = str | dict[str, Any]


class LLMClient:
    """Facade over ollama utils with basic retry/backoff support."""

    def __init__(
        self,
        *,
        chat_fn: Callable[..., LLMResponse] | None = None,
        generate_fn: Callable[..., LLMResponse] | None = None,
        vision_fn: Callable[..., LLMResponse] | None = None,
        max_retries: int | None = None,
        backoff_seconds: float | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        cfg = settings()
        self._chat = chat_fn or ollama.chat
        self._generate = generate_fn or ollama.generate
        self._vision = vision_fn or ollama.vision
        self._max_retries = (
            max_retries if max_retries is not None else cfg.llm_max_retries
        )
        self._backoff = (
            backoff_seconds if backoff_seconds is not None else cfg.llm_backoff_seconds
        )
        self._timeout = (
            timeout_seconds if timeout_seconds is not None else cfg.llm_timeout_seconds
        )

    def chat(
        self,
        messages: Iterable[Dict[str, str]],
        *,
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        # Accept convenience kwargs like temperature/top_p and map them to Ollama
        # options if caller didn't provide options explicitly.
        options = kwargs.get("options")
        if not isinstance(options, dict):
            options = {}
        moved = False
        for key in ("temperature", "top_p", "num_ctx", "max_tokens", "request_timeout"):
            if key in kwargs:
                options[key] = kwargs.pop(key)
                moved = True
        if moved:
            kwargs["options"] = options
        return self._call_with_retry(
            self._chat, messages=messages, model=model, **kwargs
        )

    # ------------------------------------------------------------------
    # Async chat — uses httpx under the hood (non-blocking)
    # ------------------------------------------------------------------
    async def chat_async(
        self,
        messages: Iterable[Dict[str, str]],
        *,
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Non-blocking chat for use inside async endpoints (FastAPI, etc.).

        Uses ``ollama.chat_async`` which is backed by ``httpx.AsyncClient``.
        Retry/backoff semantics mirror the synchronous :meth:`chat` path.
        """
        options = kwargs.get("options")
        if not isinstance(options, dict):
            options = {}
        moved = False
        for key in ("temperature", "top_p", "num_ctx", "max_tokens", "request_timeout"):
            if key in kwargs:
                options[key] = kwargs.pop(key)
                moved = True
        if moved:
            kwargs["options"] = options

        return await self._call_with_retry_async(
            ollama.chat_async, messages=messages, model=model, **kwargs
        )

    async def _call_with_retry_async(
        self, fn: Callable[..., Any], **kwargs: Any
    ) -> LLMResponse:
        """Async equivalent of :meth:`_call_with_retry`."""
        attempts = 0
        last_exc: Exception | None = None
        start = time.monotonic()
        while attempts <= self._max_retries:
            try:
                call_kwargs = dict(kwargs)
                if (
                    "timeout" not in call_kwargs
                    and "request_timeout" not in call_kwargs
                ):
                    call_kwargs["timeout"] = self._timeout
                try:
                    return await fn(**call_kwargs)
                except TypeError:
                    call_kwargs.pop("timeout", None)
                    return await fn(**call_kwargs)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                attempts += 1
                if attempts > self._max_retries:
                    break
                delay = self._backoff * attempts
                logger.warning(
                    "Async LLM call failed (attempt %s/%s): %s",
                    attempts,
                    self._max_retries + 1,
                    exc,
                )
                await asyncio.sleep(delay)
                if (time.monotonic() - start) > (
                    self._timeout * (self._max_retries + 1)
                ):
                    break
        if last_exc:
            raise last_exc
        raise RuntimeError("Async LLM call failed without exception")

    def generate(
        self, prompt: str, *, model: Optional[str] = None, **kwargs: Any
    ) -> LLMResponse:
        return self._call_with_retry(
            self._generate, prompt=prompt, model=model, **kwargs
        )

    def vision(
        self,
        prompt: str,
        image: bytes | str,
        *,
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        return self._call_with_retry(
            self._vision, prompt=prompt, image=image, model=model, **kwargs
        )

    def _call_with_retry(
        self, fn: Callable[..., LLMResponse], **kwargs: Any
    ) -> LLMResponse:
        attempts = 0
        last_exc: Exception | None = None
        start = time.monotonic()
        while attempts <= self._max_retries:
            try:
                call_kwargs = dict(kwargs)
                if (
                    "timeout" not in call_kwargs
                    and "request_timeout" not in call_kwargs
                ):
                    call_kwargs["timeout"] = self._timeout
                try:
                    return fn(**call_kwargs)
                except TypeError:
                    # Fallback: remove timeout if backend does not support it
                    call_kwargs.pop("timeout", None)
                    return fn(**call_kwargs)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                attempts += 1
                if attempts > self._max_retries:
                    break
                delay = self._backoff * attempts
                logger.warning(
                    "LLM call failed (attempt %s/%s): %s",
                    attempts,
                    self._max_retries + 1,
                    exc,
                )
                time.sleep(delay)
                if (time.monotonic() - start) > (
                    self._timeout * (self._max_retries + 1)
                ):
                    break
        if last_exc:
            raise last_exc
        raise RuntimeError("LLM call failed without exception")


def default_llm_client() -> LLMClient:
    return LLMClient()
