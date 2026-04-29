from __future__ import annotations

import pytest

from app.domain.turns.followups import TurnFollowupService
from app.domain.turns.models import TurnPlan


@pytest.mark.asyncio
async def test_followup_service_sends_search_correction_when_live_info_was_deferred(
    test_user,
    monkeypatch: pytest.MonkeyPatch,
):
    user_id, _ = test_user

    async def fake_search(*args, **kwargs):
        return "Current weather: 72 F and clear."

    monkeypatch.setattr(
        "app.domain.turns.followups.enhance_response_with_search_async",
        fake_search,
    )

    service = TurnFollowupService()
    result = await service.handle_followup_async(
        {
            "user_id": user_id,
            "session_id": None,
            "chat_id": 12345,
            "correlation_id": "corr-followup",
            "user_message_id": None,
            "assistant_message_id": None,
            "audit_id": None,
            "live_search_mode": "followup",
            "user_text": "What's the weather in Dallas today?",
            "assistant_text": "I think it might be warm today.",
            "turn_plan": TurnPlan(
                user_id=user_id,
                session_id=None,
                message_text="What's the weather in Dallas today?",
                primary_intent="question",
                sentiment_priority="normal",
                needs_live_search_now=False,
                needs_live_search_followup=True,
                search_query="weather in Dallas today",
            ).to_dict(),
        }
    )

    assert result["search_followup_sent"] is True
    assert "Current weather" in str(result["followup_message_text"])
    assert result["assistant_reply_review"]["needs_followup"] is True

