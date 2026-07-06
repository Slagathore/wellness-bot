"""Shared helpers for neutralizing untrusted text before it enters a prompt.

Used by both the Telegram adapter and the Mini App service so player/LLM-authored
content (character cards, adventure lore, retcons) can't hijack the completion
protocol or break out of a delimited block.
"""

from __future__ import annotations

from app.orchestrator.prompt_builder import RESPONSE_COMPLETION_SENTINEL

# Marker used to fence untrusted text inside a system prompt so the model can
# tell narrative data from operator instructions.
UNTRUSTED_FENCE = "==="


def sanitize_untrusted_text(text: str | None, *, limit: int = 4000) -> str:
    """Neutralize player/LLM-authored text before it enters a system prompt.

    Strips the completion sentinel (so a planted copy can't fake completion or
    suppress the sentinel-append safeguard) and the fence marker (so the text
    can't close its delimited block early). Deliberately does NOT filter
    narrative content — retcons still change canon; this only stops the text
    from breaking out of its fence or hijacking the completion protocol.
    """
    cleaned = (text or "").replace(RESPONSE_COMPLETION_SENTINEL, "")
    cleaned = cleaned.replace(UNTRUSTED_FENCE, "-")
    return cleaned.strip()[:limit]
