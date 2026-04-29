"""Thin wrapper around the Ollama HTTP API."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from typing import Any, Sequence

import httpx
import requests

from app.config import settings

logger = logging.getLogger(__name__)

_ORIGINAL_CHAT_FUNC = None
_ORIGINAL_GENERATE_FUNC = None

# Paid cloud model providers that need OpenAI-compatible API
OPENAI_COMPATIBLE_PROVIDERS = {
    "openai/": "https://api.openai.com/v1",
    "anthropic/": "https://api.anthropic.com/v1",
    "google/": "https://generativelanguage.googleapis.com/v1beta",
    "cohere/": "https://api.cohere.ai/v1",
    "mistral/": "https://api.mistral.ai/v1",
}


def _is_cloud_model(model: str) -> tuple[bool, str | None, str]:
    """Check if model needs cloud API and return (is_cloud, base_url, model_name).

    Returns:
        (is_cloud, base_url_or_none, actual_model_name)
    """
    for prefix, base_url in OPENAI_COMPATIBLE_PROVIDERS.items():
        if model.startswith(prefix):
            # Extract actual model name (e.g., "openai/gpt-4" -> "gpt-4")
            actual_model = model[len(prefix) :]
            return True, base_url, actual_model

    # Not a cloud model, use local Ollama
    return False, None, model


def _get_api_key(provider_prefix: str) -> str | None:
    """Get API key for cloud provider from environment."""
    # Map provider prefix to env var name
    env_var_map = {
        "openai/": "OPENAI_API_KEY",
        "anthropic/": "ANTHROPIC_API_KEY",
        "google/": "GOOGLE_API_KEY",
        "cohere/": "COHERE_API_KEY",
        "mistral/": "MISTRAL_API_KEY",
    }

    env_var = env_var_map.get(provider_prefix)
    if env_var:
        return os.getenv(env_var)
    return None


def _call_override(
    func, kwargs_primary: dict[str, Any], *, fallback_keys: tuple[str, ...]
) -> dict:
    """Invoke overrides while tolerating narrower signatures."""

    try:
        return func(**kwargs_primary)
    except TypeError:
        reduced = {
            key: kwargs_primary[key] for key in fallback_keys if key in kwargs_primary
        }
        return func(**reduced)


def _chat_impl(
    messages: list[dict[str, str]],
    model: str | None = None,
    options: dict[str, Any] | None = None,
) -> dict:
    """Invoke Ollama's chat endpoint or cloud provider API."""

    if model is None:
        model = settings().chat_model

    is_cloud, base_url, actual_model = _is_cloud_model(model)

    if is_cloud and base_url:
        # Use OpenAI-compatible API for paid cloud models
        return _cloud_chat(messages, actual_model, base_url, options)

    # Use local Ollama API (handles both local and Ollama cloud models)
    cfg = settings()
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    request_timeout: float | None = None
    if options:
        payload_options = dict(options)
        timeout_value = payload_options.pop("request_timeout", None)
        if timeout_value is not None:
            try:
                request_timeout = float(timeout_value)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid request_timeout %r supplied to chat(); falling back to default.",
                    timeout_value,
                )
        if payload_options:
            payload["options"] = payload_options
    if request_timeout is None:
        request_timeout = 120.0

    # When caller provides an explicit timeout, prefer fast-fail over retries to
    # avoid minute-long waits on user-facing chat paths.
    explicit_timeout = bool(options and "request_timeout" in options)
    max_attempts = 1 if explicit_timeout else 3
    data: dict[str, Any] | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(
                f"{cfg.ollama_host}/api/chat",
                json=payload,
                timeout=request_timeout,
            )
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as exc:
                raise RuntimeError(
                    f"Ollama API error: invalid JSON response ({exc})"
                ) from exc
            break
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            retriable = status_code is not None and (
                status_code == 429 or 500 <= status_code < 600
            )
            if retriable and attempt < max_attempts:
                # Respect Retry-After header when present (429)
                retry_after = None
                if exc.response is not None:
                    retry_after = exc.response.headers.get("Retry-After")
                if retry_after:
                    try:
                        sleep_for = max(float(retry_after), 1.0)
                    except (TypeError, ValueError):
                        sleep_for = 3.0 * attempt
                elif status_code == 429:
                    sleep_for = 3.0 * attempt  # longer backoff for rate-limits
                else:
                    sleep_for = 1.5 * attempt
                logger.warning(
                    "Ollama chat attempt %s/%s failed with %s. Retrying in %.1fs.",
                    attempt,
                    max_attempts,
                    status_code,
                    sleep_for,
                )
                time.sleep(sleep_for)
                continue
            raise RuntimeError(f"Ollama API error: {exc}") from exc
        except requests.RequestException as exc:
            if attempt < max_attempts:
                sleep_for = 1.5 * attempt
                logger.warning(
                    "Ollama chat attempt %s/%s encountered %s. Retrying in %.1fs.",
                    attempt,
                    max_attempts,
                    exc.__class__.__name__,
                    sleep_for,
                )
                time.sleep(sleep_for)
                continue
            raise RuntimeError(f"Ollama API error: {exc}") from exc

    if data is None:
        raise RuntimeError("Ollama API error: no response data")
    return {"text": data["message"]["content"], "raw": data}


async def _chat_impl_async(
    messages: list[dict[str, str]],
    model: str | None = None,
    options: dict[str, Any] | None = None,
) -> dict:
    """Async variant of chat() for low-latency pipeline paths."""

    if model is None:
        model = settings().chat_model

    is_cloud, base_url, actual_model = _is_cloud_model(model)
    if is_cloud and base_url:
        return await _cloud_chat_async(messages, actual_model, base_url, options)

    cfg = settings()
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    request_timeout: float | None = None
    if options:
        payload_options = dict(options)
        timeout_value = payload_options.pop("request_timeout", None)
        if timeout_value is not None:
            try:
                request_timeout = float(timeout_value)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid request_timeout %r supplied to chat_async(); falling back to default.",
                    timeout_value,
                )
        if payload_options:
            payload["options"] = payload_options
    if request_timeout is None:
        request_timeout = 120.0

    explicit_timeout = bool(options and "request_timeout" in options)
    max_attempts = 1 if explicit_timeout else 3
    data: dict[str, Any] | None = None
    last_error: Exception | None = None

    timeout = httpx.Timeout(request_timeout)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(1, max_attempts + 1):
            try:
                response = await client.post(f"{cfg.ollama_host}/api/chat", json=payload)
                response.raise_for_status()
                data = response.json()
                break
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status_code = exc.response.status_code if exc.response is not None else None
                retriable = status_code is not None and (
                    status_code == 429 or 500 <= status_code < 600
                )
                if retriable and attempt < max_attempts:
                    # Respect Retry-After header when present (429)
                    retry_after = exc.response.headers.get("Retry-After") if exc.response is not None else None
                    if retry_after:
                        try:
                            sleep_for = max(float(retry_after), 1.0)
                        except (TypeError, ValueError):
                            sleep_for = 3.0 * attempt
                    elif status_code == 429:
                        sleep_for = 3.0 * attempt  # longer backoff for rate-limits
                    else:
                        sleep_for = 1.5 * attempt
                    logger.warning(
                        "Ollama chat_async attempt %s/%s failed with %s. Retrying in %.1fs.",
                        attempt,
                        max_attempts,
                        status_code,
                        sleep_for,
                    )
                    await asyncio.sleep(sleep_for)
                    continue
                break
            except (httpx.RequestError, ValueError) as exc:
                last_error = exc
                if attempt < max_attempts:
                    sleep_for = 1.5 * attempt
                    logger.warning(
                        "Ollama chat_async attempt %s/%s encountered %s. Retrying in %.1fs.",
                        attempt,
                        max_attempts,
                        exc.__class__.__name__,
                        sleep_for,
                    )
                    await asyncio.sleep(sleep_for)
                    continue
                break

    if data is None:
        raise RuntimeError(f"Ollama API error: {last_error}") from last_error
    return {"text": data["message"]["content"], "raw": data}


def chat(
    messages: list[dict[str, str]],
    model: str | None = None,
    options: dict[str, Any] | None = None,
) -> dict:
    """Invoke the current chat handler (supports test monkeypatching)."""

    override = globals().get("chat")
    if override is not _ORIGINAL_CHAT_FUNC:
        kwargs = {"messages": messages, "model": model, "options": options}
        return _call_override(override, kwargs, fallback_keys=("messages", "options"))
    return _chat_impl(messages=messages, model=model, options=options)


async def chat_async(
    messages: list[dict[str, str]],
    model: str | None = None,
    options: dict[str, Any] | None = None,
) -> dict:
    """Async chat helper for latency-sensitive paths."""

    override = globals().get("chat")
    if override is not _ORIGINAL_CHAT_FUNC:
        kwargs = {"messages": messages, "model": model, "options": options}
        if asyncio.iscoroutinefunction(override):
            return await override(**kwargs)
        return await asyncio.to_thread(
            _call_override,
            override,
            kwargs,
            fallback_keys=("messages", "options"),
        )
    return await _chat_impl_async(messages=messages, model=model, options=options)


def _cloud_chat(
    messages: list[dict[str, str]],
    model: str,
    base_url: str,
    options: dict[str, Any] | None = None,
) -> dict:
    """Call cloud provider API using OpenAI-compatible format."""

    # Get API key from model prefix
    for prefix in OPENAI_COMPATIBLE_PROVIDERS.keys():
        if model.startswith(prefix.rstrip("/")):
            api_key = _get_api_key(prefix)
            break
    else:
        # Try to infer from base_url
        provider = base_url.split("//")[1].split(".")[0]
        api_key = os.getenv(f"{provider.upper()}_API_KEY")

    if not api_key:
        raise RuntimeError(
            "Cloud model requires API key. Set environment variable (e.g., OPENAI_API_KEY)"
        )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    request_timeout: float | None = None

    # Map Ollama options to OpenAI parameters
    if options:
        timeout_value = options.get("request_timeout")
        if timeout_value is not None:
            try:
                request_timeout = float(timeout_value)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid request_timeout %r supplied to chat(); falling back to default.",
                    timeout_value,
                )
        cloud_options = {k: v for k, v in options.items() if k != "request_timeout"}
        if "temperature" in cloud_options:
            payload["temperature"] = cloud_options["temperature"]
        if "top_p" in cloud_options:
            payload["top_p"] = cloud_options["top_p"]
        # Map Ollama's num_predict → OpenAI's max_tokens (output cap)
        if "max_tokens" in cloud_options:
            payload["max_tokens"] = int(cloud_options["max_tokens"])
        elif "num_predict" in cloud_options:
            payload["max_tokens"] = int(cloud_options["num_predict"])
    if request_timeout is None:
        request_timeout = 120.0

    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=request_timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Cloud API error: {exc}") from exc

    data = response.json()
    return {"text": data["choices"][0]["message"]["content"], "raw": data}


async def _cloud_chat_async(
    messages: list[dict[str, str]],
    model: str,
    base_url: str,
    options: dict[str, Any] | None = None,
) -> dict:
    """Async cloud provider chat using OpenAI-compatible API."""

    for prefix in OPENAI_COMPATIBLE_PROVIDERS.keys():
        if model.startswith(prefix.rstrip("/")):
            api_key = _get_api_key(prefix)
            break
    else:
        provider = base_url.split("//")[1].split(".")[0]
        api_key = os.getenv(f"{provider.upper()}_API_KEY")

    if not api_key:
        raise RuntimeError(
            "Cloud model requires API key. Set environment variable (e.g., OPENAI_API_KEY)"
        )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    request_timeout: float | None = None

    if options:
        timeout_value = options.get("request_timeout")
        if timeout_value is not None:
            try:
                request_timeout = float(timeout_value)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid request_timeout %r supplied to chat_async(); falling back to default.",
                    timeout_value,
                )
        cloud_options = {k: v for k, v in options.items() if k != "request_timeout"}
        if "temperature" in cloud_options:
            payload["temperature"] = cloud_options["temperature"]
        if "top_p" in cloud_options:
            payload["top_p"] = cloud_options["top_p"]
        # Map Ollama's num_predict → OpenAI's max_tokens (output cap)
        if "max_tokens" in cloud_options:
            payload["max_tokens"] = int(cloud_options["max_tokens"])
        elif "num_predict" in cloud_options:
            payload["max_tokens"] = int(cloud_options["num_predict"])
    if request_timeout is None:
        request_timeout = 120.0

    timeout = httpx.Timeout(request_timeout)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
        except httpx.RequestError as exc:
            raise RuntimeError(f"Cloud API error: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"Cloud API error: {exc}") from exc

    data = response.json()
    return {"text": data["choices"][0]["message"]["content"], "raw": data}


def _generate_impl(
    prompt: str,
    model: str | None = None,
    format: str | None = None,
    options: dict[str, Any] | None = None,
) -> dict:
    """Invoke Ollama's single-prompt generation endpoint or cloud provider API."""

    if model is None:
        model = settings().chat_model

    is_cloud, base_url, actual_model = _is_cloud_model(model)

    if is_cloud and base_url:
        # Convert to chat format for paid cloud APIs (they don't have a generate endpoint)
        messages = [{"role": "user", "content": prompt}]
        result = _cloud_chat(messages, actual_model, base_url, options)

        # If JSON format requested, try to parse and validate
        if format == "json":
            import json

            try:
                # Verify it's valid JSON
                json.loads(result["text"])
            except json.JSONDecodeError:
                # Ask model to fix it
                fix_prompt = f"Convert this to valid JSON:\n{result['text']}"
                messages = [{"role": "user", "content": fix_prompt}]
                result = _cloud_chat(messages, actual_model, base_url, options)

        return result

    # Use local Ollama API (handles both local and Ollama cloud models)
    cfg = settings()
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
    }
    if format:
        payload["format"] = format
    if options:
        payload["options"] = options

    try:
        response = requests.post(
            f"{cfg.ollama_host}/api/generate",
            json=payload,
            timeout=120,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Ollama API error: {exc}") from exc

    data = response.json()
    return {"text": data["response"], "raw": data}


def generate(
    prompt: str,
    model: str | None = None,
    format: str | None = None,
    options: dict[str, Any] | None = None,
) -> dict:
    """Invoke current generate handler (supports test monkeypatching)."""

    override = globals().get("generate")
    if override is not _ORIGINAL_GENERATE_FUNC:
        kwargs = {
            "prompt": prompt,
            "model": model,
            "format": format,
            "options": options,
        }
        return _call_override(
            override, kwargs, fallback_keys=("prompt", "format", "options")
        )
    return _generate_impl(prompt=prompt, model=model, format=format, options=options)


def vision(
    prompt: str,
    image_path: str,
    model: str | None = None,
    options: dict[str, Any] | None = None,
) -> dict:
    """Analyze an image with vision model.

    Args:
        prompt: Question or instruction about the image
        image_path: Absolute path to image file
        model: Vision model to use (defaults to settings)
        options: Ollama options (temperature, etc.)

    Returns:
        {"text": "description", "raw": {...}}
    """
    import base64

    if model is None:
        model = settings().vision_model

    cfg = settings()

    # Read and encode image
    with open(image_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")

    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "images": [image_data],
        "stream": False,
    }
    if options:
        payload["options"] = options

    try:
        response = requests.post(
            f"{cfg.ollama_host}/api/generate",
            json=payload,
            timeout=180,  # Vision models can be slower
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Ollama vision API error: {exc}") from exc

    data = response.json()
    return {"text": data["response"], "raw": data}


def get_embeddings(
    texts: list[str], model: str | None = None, timeout: float = 10.0
) -> list[list[float]]:
    """Generate embeddings for texts using Ollama.

    Args:
        texts: List of text strings to embed
        model: Embedding model to use (defaults to settings.embed_model)
        timeout: Request timeout (seconds)

    Returns:
        List of embedding vectors (list of floats)
    """
    if model is None:
        model = settings().embed_model

    cfg = settings()
    embeddings = []

    embed_dims = getattr(settings(), "embed_dimensions", 384) or 384

    for text in texts:
        payload = {"model": model, "prompt": text}

        try:
            response = requests.post(
                f"{cfg.ollama_host}/api/embeddings", json=payload, timeout=timeout
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            logging.getLogger(__name__).warning(
                "[Embeddings] Request to %s failed (%s). Using deterministic fallback vector.",
                cfg.ollama_host,
                exc.__class__.__name__,
            )
            embeddings.append(_fallback_embedding(text, embed_dims))
            continue

        data = response.json()
        embeddings.append(data["embedding"])

    return embeddings


async def get_embeddings_async(
    texts: Sequence[str],
    model: str | None = None,
    timeout: float = 10.0,
) -> list[list[float]]:
    """Async embedding generation using HTTPX for non-blocking hot paths."""

    if model is None:
        model = settings().embed_model
    cfg = settings()
    embed_dims = getattr(cfg, "embed_dimensions", 384) or 384
    if not texts:
        return []

    timeout_cfg = httpx.Timeout(timeout)
    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=timeout_cfg) as client:
        for text in texts:
            payload = {"model": model, "prompt": text}
            try:
                response = await client.post(
                    f"{cfg.ollama_host}/api/embeddings",
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                vectors.append(data["embedding"])
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).warning(
                    "[Embeddings] Async request to %s failed (%s). Using deterministic fallback vector.",
                    cfg.ollama_host,
                    exc.__class__.__name__,
                )
                vectors.append(_fallback_embedding(text, embed_dims))
    return vectors


def preload_embedding_model(model: str | None = None, text: str = "warmup") -> None:
    """Warm up the embedding model to avoid cold-start latency."""

    model_to_use = model or settings().embed_model
    try:
        logging.getLogger(__name__).info(
            "[Embeddings] Pre-loading model %s", model_to_use
        )
        get_embeddings([text], model=model_to_use, timeout=15.0)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "[Embeddings] Preload failed for %s: %s", model_to_use, exc
        )


if _ORIGINAL_CHAT_FUNC is None:
    _ORIGINAL_CHAT_FUNC = chat
if _ORIGINAL_GENERATE_FUNC is None:
    _ORIGINAL_GENERATE_FUNC = generate


def _fallback_embedding(text: str, dimensions: int) -> list[float]:
    """
    Generate a deterministic embedding vector for offline/test scenarios.

    This keeps downstream systems functioning (search, reminders, tests) even
    when the Ollama embeddings endpoint is unavailable.
    """

    normalized_text = (text or "").strip()
    if not normalized_text:
        normalized_text = " "
    seed = hashlib.sha256(normalized_text.encode("utf-8")).digest()
    values: list[float] = []
    counter = 0
    # Derive pseudo-random floats in [-1, 1] based on the hash of the text.
    while len(values) < dimensions:
        counter_bytes = counter.to_bytes(4, byteorder="big", signed=False)
        digest = hashlib.sha256(seed + counter_bytes).digest()
        for idx in range(0, len(digest), 4):
            if len(values) >= dimensions:
                break
            chunk = digest[idx : idx + 4]
            integer = int.from_bytes(chunk, byteorder="big", signed=False)
            normalized = (integer / 0xFFFFFFFF) * 2.0 - 1.0
            values.append(normalized)
        counter += 1
    return values
