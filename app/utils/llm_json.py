"""Helpers for parsing structured JSON returned by LLMs."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_llm_json(text: object) -> Any:
    raw = str(text or "").strip()
    if not raw:
        raise json.JSONDecodeError("empty payload", "", 0)

    candidates = [raw]
    if raw.startswith("```"):
        candidates.append(_FENCE_RE.sub("", raw).strip())

    first_brace = raw.find("{")
    last_brace = raw.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidates.append(raw[first_brace : last_brace + 1].strip())

    first_bracket = raw.find("[")
    last_bracket = raw.rfind("]")
    if first_bracket != -1 and last_bracket > first_bracket:
        candidates.append(raw[first_bracket : last_bracket + 1].strip())

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    raise json.JSONDecodeError("unable to parse llm json", raw, 0)
