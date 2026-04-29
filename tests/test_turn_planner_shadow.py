from __future__ import annotations

from app.domain.turns.llm_analyzer import LLMTurnAnalysis, LLMTurnAnalyzer
from app.domain.turns.planner import TurnPlanner


class FakeAnalyzer(LLMTurnAnalyzer):
    def __init__(self, analysis: LLMTurnAnalysis) -> None:
        self._analysis = analysis

    def analyze(self, **_: object) -> LLMTurnAnalysis | None:
        return self._analysis


def test_shadow_mode_keeps_heuristic_actions_but_records_diff(test_user):
    user_id, _ = test_user
    analyzer = FakeAnalyzer(
        LLMTurnAnalysis(
            sentiment_priority="high",
            emotion_label="sadness",
            allow_reminder_action=True,
            scheduled_event=True,
            timing_question_ok=True,
            reasoning=["grief_event_detected"],
            model_name="mistral-large-3:675b-cloud",
        )
    )
    planner = TurnPlanner(
        analyzer=analyzer,
        shadow_enabled=True,
        llm_primary_enabled=False,
    )

    plan = planner.build_plan(
        user_id=user_id,
        session_id=None,
        message_text="We're burying my father tomorrow.",
    )

    assert plan.planner_source == "heuristic+llm_shadow"
    assert plan.allow_reminder_action is False
    assert plan.sentiment_priority == "high"
    assert plan.shadow_comparison is not None
    assert plan.shadow_comparison["llm_model"] == "mistral-large-3:675b-cloud"
    assert "scheduled_event" in plan.shadow_comparison["mismatch_fields"]
    assert plan.shadow_comparison["heuristic_summary"]["allow_reminder_action"] is False
    assert plan.shadow_comparison["llm_summary"]["scheduled_event"] is True
    assert plan.planner_latency_ms is not None


def test_primary_mode_uses_llm_event_judgment_for_reminder_action(test_user):
    user_id, _ = test_user
    analyzer = FakeAnalyzer(
        LLMTurnAnalysis(
            primary_intent="conversation",
            sentiment_priority="high",
            emotion_label="sadness",
            allow_reminder_action=True,
            scheduled_event=True,
            timing_question_ok=True,
            reasoning=["grief_event_detected"],
            model_name="mistral-large-3:675b-cloud",
        )
    )
    planner = TurnPlanner(
        analyzer=analyzer,
        shadow_enabled=False,
        llm_primary_enabled=True,
    )

    plan = planner.build_plan(
        user_id=user_id,
        session_id=None,
        message_text="We're burying my father tomorrow.",
    )

    assert plan.planner_source == "llm_primary"
    assert plan.allow_reminder_action is True
    assert plan.scheduled_event is True
    assert plan.timing_question_ok is True
    assert plan.shadow_comparison is not None
    assert "allow_reminder_action" in plan.shadow_comparison["mismatch_fields"]
