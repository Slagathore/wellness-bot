"""Lightweight text normalization helpers before sending content to the LLM."""

from __future__ import annotations

import re

_ELLIPSIS_RE = re.compile(r"\.{4,}")
_MULTI_PUNCT_RE = re.compile(r"([!?])\1{2,}")
_WHITESPACE_RE = re.compile(r"[ \t]{2,}")


def clean_for_llm(text: str | None) -> str:
    """Normalize user/assistant text while preserving emotion."""

    if not text:
        return ""

    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _ELLIPSIS_RE.sub("...", cleaned)
    cleaned = _MULTI_PUNCT_RE.sub(lambda m: m.group(1) * 3, cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()
