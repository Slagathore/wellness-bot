"""
Tests for Phase 8 cloud model drain fixes.

Covers:
 - ReminderDispatcher._compute_next_run: daily/weekly/hourly/cron/once/invalid
 - ReminderDispatcher.handle_due:  LLM failure → fallback text, always marks sent
 - ReminderDispatcher.handle_due:  missing chat_id → still marks sent
 - worker_model setting is used in _generate_message instead of default chat model

These tests ensure background workers and the reminder loop never silently
drain cloud quota through infinite retry loops or defaulting to CHAT_MODEL.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest


def _aware_now() -> datetime:
    """Return timezone-aware now matching operator_now's timezone (CST / UTC-6)."""
    from app.utils.time_utils import OPERATOR_TZ
    return datetime.now(OPERATOR_TZ)


# ---------------------------------------------------------------------------
#  Helpers / fakes
# ---------------------------------------------------------------------------

class FakeReminderService:
    """Minimal ReminderService stand-in that records calls."""

    def __init__(self) -> None:
        self.mark_calls: list[tuple[str, datetime | None]] = []

    def mark_sent_and_schedule_next(
        self, reminder_id: str, next_send_time: datetime | None
    ) -> None:
        self.mark_calls.append((reminder_id, next_send_time))


class FakeLLMClient:
    """LLMClient stand-in; can be set to raise or return a dict."""

    def __init__(self, response: Any = None, *, should_raise: bool = False) -> None:
        self._response = response or {"message": {"content": "Stay hydrated! 💧"}}
        self._should_raise = should_raise
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        self.calls.append({"messages": messages, **kwargs})
        if self._should_raise:
            raise RuntimeError("Cloud quota exhausted")
        return self._response


class FakeSessionStore:
    """UserSessionStore stand-in."""

    def get_user_id(self, chat_id: Any) -> int | None:
        return int(chat_id) if chat_id else None

    def get_or_create_session(self, uid: int) -> int:
        return 1

    def save_message(self, session_id: int, uid: int, role: str, text: str) -> None:
        pass


def _make_dispatcher(
    llm_response: Any = None,
    llm_should_raise: bool = False,
) -> tuple[Any, FakeReminderService, FakeLLMClient]:
    """Build a (dispatcher, fake_svc, fake_llm) tuple for testing."""
    from app.domain.reminders.dispatcher import ReminderDispatcher

    svc = FakeReminderService()
    llm = FakeLLMClient(response=llm_response, should_raise=llm_should_raise)
    sessions = FakeSessionStore()
    dispatcher = ReminderDispatcher(
        reminders=cast(Any, svc),
        llm=cast(Any, llm),
        sessions=cast(Any, sessions),
    )
    return dispatcher, svc, llm


# ---------------------------------------------------------------------------
#  _compute_next_run tests
# ---------------------------------------------------------------------------

class TestComputeNextRun:
    """Verify _compute_next_run handles all frequency types correctly."""

    @pytest.fixture()
    def dispatcher(self):
        d, _, _ = _make_dispatcher()
        return d

    # -- daily with specific_hour/minute ------------------------------------

    def test_daily_with_specific_hour(self, dispatcher):
        """daily + specific_hour → tomorrow at that hour."""
        meta = {
            "frequency": "daily",
            "specific_hour": 8,
            "specific_minute": 30,
        }
        result = dispatcher._compute_next_run(meta)
        assert result is not None
        assert result.hour == 8
        assert result.minute == 30
        assert result.second == 0
        assert result > _aware_now()

    def test_daily_with_midnight_hour(self, dispatcher):
        """hour=0 (midnight) must not be treated as falsy/skipped."""
        meta = {
            "frequency": "daily",
            "specific_hour": 0,
            "specific_minute": 0,
        }
        result = dispatcher._compute_next_run(meta)
        assert result is not None
        assert result.hour == 0
        assert result.minute == 0

    def test_daily_falls_back_to_base_hour(self, dispatcher):
        """When specific_hour is absent, use base_hour."""
        meta = {
            "frequency": "daily",
            "base_hour": 14,
            "base_minute": 15,
        }
        result = dispatcher._compute_next_run(meta)
        assert result is not None
        assert result.hour == 14
        assert result.minute == 15

    def test_daily_no_hour_offsets_from_now(self, dispatcher):
        """daily with no hour metadata → now + 1 day."""
        meta = {"frequency": "daily"}
        result = dispatcher._compute_next_run(meta)
        assert result is not None
        now = _aware_now()
        assert result > now
        # Should be roughly 24h from now (within 5 minutes tolerance).
        assert abs((result - now).total_seconds() - 86400) < 300

    # -- weekly -------------------------------------------------------------

    def test_weekly_with_hour(self, dispatcher):
        """weekly + specific_hour → 7 days from now at that hour."""
        meta = {
            "frequency": "weekly",
            "specific_hour": 10,
            "specific_minute": 0,
        }
        result = dispatcher._compute_next_run(meta)
        assert result is not None
        assert result.hour == 10
        # Should be within 7 days + a bit of tolerance
        delta = (result - _aware_now()).total_seconds()
        assert 0 < delta <= 7 * 86400 + 300

    # -- hourly -------------------------------------------------------------

    def test_hourly(self, dispatcher):
        """hourly with no hour metadata → now + 1 hour."""
        meta = {"frequency": "hourly"}
        result = dispatcher._compute_next_run(meta)
        assert result is not None
        delta = (result - _aware_now()).total_seconds()
        assert abs(delta - 3600) < 300

    # -- every_other_day ----------------------------------------------------

    def test_every_other_day(self, dispatcher):
        """every_other_day → 2 days out."""
        meta = {
            "frequency": "every_other_day",
            "specific_hour": 9,
            "specific_minute": 0,
        }
        result = dispatcher._compute_next_run(meta)
        assert result is not None
        assert result.hour == 9
        delta = (result - _aware_now()).total_seconds()
        assert 0 < delta <= 2 * 86400 + 300

    # -- once → None (should disable) --------------------------------------

    def test_once_returns_none(self, dispatcher):
        """frequency='once' → None (reminder should be disabled)."""
        meta = {"frequency": "once"}
        assert dispatcher._compute_next_run(meta) is None

    # -- no frequency → None ------------------------------------------------

    def test_no_frequency_returns_none(self, dispatcher):
        meta = {}
        assert dispatcher._compute_next_run(meta) is None

    # -- valid cron expression ----------------------------------------------

    def test_real_cron_expression(self, dispatcher):
        """Valid cron '0 8 * * *' should use croniter."""
        meta = {"frequency": "daily", "cadence_cron": "0 8 * * *"}
        result = dispatcher._compute_next_run(meta)
        assert result is not None
        assert result.hour == 8
        assert result.minute == 0

    # -- cadence_cron overrides frequency -----------------------------------

    def test_cadence_cron_overrides_frequency(self, dispatcher):
        """cadence_cron takes priority over frequency string."""
        meta = {"frequency": "daily", "cadence_cron": "30 14 * * *"}
        result = dispatcher._compute_next_run(meta)
        assert result is not None
        assert result.hour == 14
        assert result.minute == 30

    # -- invalid cron → None ------------------------------------------------

    def test_invalid_cron_returns_none(self, dispatcher):
        """Garbage cron that isn't a recognized frequency → None."""
        meta = {"frequency": "garbage_string_xyz", "cadence_cron": "not-a-cron"}
        assert dispatcher._compute_next_run(meta) is None

    # -- case insensitivity -------------------------------------------------

    def test_daily_case_insensitive(self, dispatcher):
        """'Daily', 'DAILY' etc. all work."""
        for variant in ("Daily", "DAILY", "dAiLy"):
            meta = {"frequency": variant, "specific_hour": 7, "specific_minute": 0}
            result = dispatcher._compute_next_run(meta)
            assert result is not None, f"Failed for {variant!r}"
            assert result.hour == 7


# ---------------------------------------------------------------------------
#  handle_due tests — LLM failure fallback
# ---------------------------------------------------------------------------

class TestHandleDueFallback:
    """Verify handle_due always marks sent even when LLM fails."""

    def test_llm_failure_still_marks_sent(self):
        """When LLM raises, fallback text is used and mark_sent runs."""
        dispatcher, svc, llm = _make_dispatcher(llm_should_raise=True)

        payload = {
            "chat_id": 12345,
            "user_id": "378",
            "reminder_id": "560",
            "text": "Drink water",
            "metadata": {"frequency": "daily", "specific_hour": 9},
        }
        # Patch event_bus.publish to avoid side effects
        with patch("app.domain.reminders.dispatcher.event_bus") as mock_bus:
            dispatcher.handle_due(payload)

        # mark_sent_and_schedule_next MUST have been called
        assert len(svc.mark_calls) == 1
        rid, next_time = svc.mark_calls[0]
        assert rid == "560"
        assert next_time is not None  # daily → rescheduled, not disabled

    def test_llm_failure_uses_fallback_text(self):
        """Fallback message includes the original reminder text."""
        dispatcher, svc, llm = _make_dispatcher(llm_should_raise=True)

        payload = {
            "chat_id": 12345,
            "user_id": "378",
            "reminder_id": "560",
            "text": "Take your vitamins",
            "metadata": {"frequency": "once"},
        }
        sent_texts: list[str] = []

        def capture_publish(event: str, data: dict, **kw: Any) -> None:
            if event == "send_reply":
                sent_texts.append(data.get("text", ""))

        with patch("app.domain.reminders.dispatcher.event_bus") as mock_bus:
            mock_bus.publish = capture_publish
            # Re-import events constants for the patched module
            with patch("app.domain.reminders.dispatcher.events") as mock_events:
                mock_events.EVENT_SEND_REPLY = "send_reply"
                mock_events.EVENT_REMINDER_UPDATE_NEXT = "reminder_update_next"
                dispatcher.handle_due(payload)

        assert any("Take your vitamins" in t for t in sent_texts)

    def test_llm_success_marks_sent(self):
        """Normal case: LLM succeeds, mark_sent still runs."""
        dispatcher, svc, llm = _make_dispatcher()

        payload = {
            "chat_id": 12345,
            "user_id": "378",
            "reminder_id": "560",
            "text": "Eat lunch",
            "metadata": {"frequency": "daily", "specific_hour": 13},
        }
        with patch("app.domain.reminders.dispatcher.event_bus"):
            dispatcher.handle_due(payload)

        assert len(svc.mark_calls) == 1
        assert svc.mark_calls[0][1] is not None  # daily → rescheduled

    def test_once_reminder_disabled_after_dispatch(self):
        """frequency='once' → next_send_time=None → reminder disabled."""
        dispatcher, svc, llm = _make_dispatcher()

        payload = {
            "chat_id": 12345,
            "user_id": "378",
            "reminder_id": "999",
            "text": "One-off reminder",
            "metadata": {"frequency": "once"},
        }
        with patch("app.domain.reminders.dispatcher.event_bus"):
            dispatcher.handle_due(payload)

        assert len(svc.mark_calls) == 1
        assert svc.mark_calls[0][1] is None  # once → disabled


# ---------------------------------------------------------------------------
#  handle_due — missing chat_id
# ---------------------------------------------------------------------------

class TestHandleDueMissingChatId:
    """When chat_id is absent, still mark sent to prevent infinite loop."""

    def test_no_chat_id_marks_sent(self):
        dispatcher, svc, _ = _make_dispatcher()

        payload = {
            "chat_id": None,
            "user_id": "378",
            "reminder_id": "560",
            "text": "Ghost reminder",
            "metadata": {"frequency": "daily", "specific_hour": 8},
        }
        with patch("app.domain.reminders.dispatcher.event_bus"):
            dispatcher.handle_due(payload)

        assert len(svc.mark_calls) == 1
        assert svc.mark_calls[0][0] == "560"

    def test_no_chat_id_no_reminder_id_no_crash(self):
        """Missing both chat_id AND reminder_id → no crash, no mark call."""
        dispatcher, svc, _ = _make_dispatcher()

        payload = {
            "chat_id": None,
            "user_id": "378",
            "text": "Orphan",
            "metadata": {},
        }
        with patch("app.domain.reminders.dispatcher.event_bus"):
            dispatcher.handle_due(payload)

        assert len(svc.mark_calls) == 0  # no reminder_id to mark


# ---------------------------------------------------------------------------
#  worker_model routing
# ---------------------------------------------------------------------------

class TestWorkerModelRouting:
    """Verify _generate_message passes worker_model to LLM, not chat_model."""

    def test_worker_model_passed_to_llm(self):
        """The 'model' kwarg should be the worker_model setting, not None."""
        dispatcher, svc, llm = _make_dispatcher()

        with patch("app.config.settings") as mock_gs:
            mock_settings = MagicMock()
            mock_settings.worker_model = "huihui_ai/gemma3n-abliterated:e2b-fp16"
            mock_gs.return_value = mock_settings

            result = dispatcher._generate_message(
                "Drink water", {"time_of_day": "morning"}
            )

        assert len(llm.calls) == 1
        assert llm.calls[0].get("model") == "huihui_ai/gemma3n-abliterated:e2b-fp16"

    def test_worker_model_none_passes_none(self):
        """When worker_model is None, model=None is passed (falls back to chat_model)."""
        dispatcher, svc, llm = _make_dispatcher()

        with patch("app.config.settings") as mock_gs:
            mock_settings = MagicMock()
            mock_settings.worker_model = None
            mock_gs.return_value = mock_settings

            dispatcher._generate_message("Test", {})

        assert len(llm.calls) == 1
        assert llm.calls[0].get("model") is None


# ---------------------------------------------------------------------------
#  ReminderService.create no longer fires immediate EVENT_REMINDER_DUE
# ---------------------------------------------------------------------------

class TestCreateNoImmediateFire:
    """service.create() must NOT publish EVENT_REMINDER_DUE anymore."""

    def test_create_does_not_fire_due_event(self):
        from app.domain.reminders.commands import CreateReminderCommand
        from app.domain.reminders.service import ReminderService

        class StubRepo:
            def create(self, cmd: CreateReminderCommand) -> str:
                return "100"

            def due_before(self, ts: datetime, limit: int = 100):
                return []

            def mark_sent(self, reminder_id: str, sent_at: datetime) -> None:
                return None

            def delete(self, reminder_id: str) -> None:
                return None

            def list_for_user(self, user_id: str, limit: int = 25):
                return []

            def disable(self, reminder_id: str) -> None:
                return None

            def disable_all_for_user(self, user_id: str) -> int:
                return 0

            def update_sent(
                self, reminder_id: str, next_send_time: datetime | None
            ) -> None:
                return None

            def reschedule(self, reminder_id: str, next_send_time: datetime) -> None:
                return None

        svc = ReminderService(StubRepo())
        cmd = CreateReminderCommand(
            user_id="1",
            kind="wellness",
            text="Drink water",
            next_run_at=datetime.now() + timedelta(hours=1),
            enabled=True,
        )

        with patch("app.domain.reminders.service.event_bus") as mock_bus:
            rid = svc.create(cmd)

        assert rid == "100"
        # Must NOT publish EVENT_REMINDER_DUE (or any event at all)
        mock_bus.publish.assert_not_called()


# ---------------------------------------------------------------------------
#  config.worker_model exists
# ---------------------------------------------------------------------------

class TestWorkerModelConfig:
    """Verify the worker_model setting exists and defaults to None."""

    def test_worker_model_default_none(self, monkeypatch):
        from app.config import Settings

        # Prevent Pydantic from reading the real .env file
        monkeypatch.delenv("WORKER_MODEL", raising=False)
        s = Settings(
            **{
                "_env_file": None,
                "telegram_bot_token": "fake",
                "data_root": "/tmp",
                "database_path": "/tmp/db.sqlite",
            }
        )
        assert s.worker_model is None

    def test_worker_model_can_be_set(self, monkeypatch):
        from app.config import Settings
        monkeypatch.delenv("WORKER_MODEL", raising=False)
        s = Settings(
            **{
                "_env_file": None,
                "telegram_bot_token": "fake",
                "data_root": "/tmp",
                "database_path": "/tmp/db.sqlite",
                "worker_model": "local-model:latest",
            }
        )
        assert s.worker_model == "local-model:latest"
