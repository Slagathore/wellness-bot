from __future__ import annotations

from app.db import db_rw
from app.domain.turns.planner import TurnPlanner


def test_planner_detects_reminder_request(test_user):
    user_id, _ = test_user
    planner = TurnPlanner()
    plan = planner.build_plan(
        user_id=user_id,
        session_id=None,
        message_text="Remind me to call mom tomorrow morning",
    )
    assert plan.allow_reminder_action is True
    assert plan.primary_intent == "reminder_request"


def test_planner_allows_distressing_scheduled_event_followup(test_user):
    user_id, _ = test_user
    planner = TurnPlanner()
    plan = planner.build_plan(
        user_id=user_id,
        session_id=None,
        message_text="I'm really nervous about my interview tomorrow.",
    )
    assert plan.allow_reminder_action is True


def test_planner_blocks_generic_future_activity_followup(test_user):
    user_id, _ = test_user
    planner = TurnPlanner()
    plan = planner.build_plan(
        user_id=user_id,
        session_id=None,
        message_text="I have work tomorrow.",
    )
    assert plan.allow_reminder_action is False


def test_planner_detects_media_request(test_user):
    user_id, _ = test_user
    planner = TurnPlanner()
    plan = planner.build_plan(
        user_id=user_id,
        session_id=None,
        message_text="Can you generate an image of a calm ocean at sunset?",
    )
    assert plan.allow_media_action is True
    assert plan.primary_intent == "media_request"


def test_planner_detects_live_search(test_user):
    user_id, _ = test_user
    planner = TurnPlanner()
    plan = planner.build_plan(
        user_id=user_id,
        session_id=None,
        message_text="What's the weather in Dallas today?",
    )
    assert plan.needs_live_search_now is True


def test_planner_defers_recommendation_search(test_user):
    user_id, _ = test_user
    planner = TurnPlanner()
    plan = planner.build_plan(
        user_id=user_id,
        session_id=None,
        message_text="What are some of the best restaurants near me?",
    )
    assert plan.needs_live_search_now is False
    assert plan.needs_live_search_followup is True


def test_planner_flags_profile_contradiction(test_user):
    user_id, _ = test_user
    with db_rw() as conn:
        conn.execute(
            """
            INSERT INTO profile_context (user_id, key, value)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
            """,
            (user_id, "name", "Cole"),
        )
    planner = TurnPlanner()
    plan = planner.build_plan(
        user_id=user_id,
        session_id=None,
        message_text="My name is Alex now.",
    )
    assert plan.contradictions
    assert plan.contradictions[0]["key"] == "name"
