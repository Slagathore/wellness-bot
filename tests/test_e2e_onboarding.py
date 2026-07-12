import json

from app.onboarding.flow import OnboardingFlow
from app.db import db_ro


def _require_response(response: str | None) -> str:
    assert response is not None
    return response


def test_onboarding_flow_progression(test_config, test_user, mock_ollama):
    user_id, telegram_user_id = test_user
    flow = OnboardingFlow()

    # Step 0 -> welcome + check-in prompt
    response = _require_response(
        flow.handle_user_message(telegram_user_id, user_id, "Hi there")
    )
    assert "how often" in response.lower()

    # Step 1 -> reminder types
    response = _require_response(
        flow.handle_user_message(telegram_user_id, user_id, "1")
    )
    assert "reminders for" in response.lower()

    # Step 2 -> feature activation
    response = _require_response(
        flow.handle_user_message(telegram_user_id, user_id, "1,3")
    )
    assert "which features" in response.lower()

    # Step 3 -> personality prompt
    response = _require_response(
        flow.handle_user_message(telegram_user_id, user_id, "mood, sleep")
    )
    assert "how would you like me" in response.lower()

    # Step 4 -> nsfw preference (if enabled) or name prompt
    response = _require_response(
        flow.handle_user_message(telegram_user_id, user_id, "1")
    )
    if "nsfw" in response.lower():
        response = _require_response(
            flow.handle_user_message(telegram_user_id, user_id, "2")
        )
    assert "what name" in response.lower()

    # Step 5 -> pronouns prompt
    response = _require_response(
        flow.handle_user_message(telegram_user_id, user_id, "Alex")
    )
    assert "pronoun" in response.lower()

    # Step 6 -> timezone prompt
    response = _require_response(
        flow.handle_user_message(telegram_user_id, user_id, "he/him")
    )
    assert "timezone" in response.lower()

    # Step 7 -> sleep schedule prompt
    response = _require_response(
        flow.handle_user_message(telegram_user_id, user_id, "UTC-5")
    )
    assert "sleep and wake up" in response.lower()

    # Step 8 -> support preferences prompt
    response = _require_response(
        flow.handle_user_message(telegram_user_id, user_id, "11pm and 7am")
    )
    assert "support you best" in response.lower()

    # Step 9 -> wellness goals prompt
    response = _require_response(
        flow.handle_user_message(
            telegram_user_id,
            user_id,
            "Gentle encouragement and celebrating wins",
        )
    )
    assert "improve or focus" in response.lower()

    # Step 10 -> reminder times for meals
    response = _require_response(
        flow.handle_user_message(
            telegram_user_id, user_id, "Improve my sleep and energy"
        )
    )
    assert "meals" in response.lower()

    # Provide meal reminder times
    response = _require_response(
        flow.handle_user_message(telegram_user_id, user_id, "8am, 1pm, 7pm")
    )
    assert "hydration" in response.lower()

    # Provide hydration reminder times -> should finalize
    response = _require_response(
        flow.handle_user_message(telegram_user_id, user_id, "9am, 2pm, 8pm")
    )
    assert "perfect!" in response.lower()

    with db_ro() as conn:
        user = conn.execute(
            "SELECT onboarding_completed, onboarding_data, feature_flags FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        assert user["onboarding_completed"] == 1
        onboarding_data = user["onboarding_data"]
        feature_flags_json = user["feature_flags"]

    onboarding_summary = json.loads(onboarding_data)
    feature_flags = json.loads(feature_flags_json)

    assert onboarding_summary["check_in_frequency"] == "daily"
    assert onboarding_summary["pronouns"] == "he/him"
    assert (
        onboarding_summary["support_preferences"]
        == "Gentle encouragement and celebrating wins"
    )
    assert onboarding_summary["nsfw_opt_in"] is False
    assert "meals" in onboarding_summary["focus_areas"]
    assert "hydration" in onboarding_summary["focus_areas"]

    assert feature_flags["mood_journaling"] is True
    assert feature_flags["sleep_tracking"] is True
    assert feature_flags["hydration_tracking"] is True

    with db_ro() as conn:
        reminders = conn.execute(
            "SELECT payload, cadence_cron FROM reminders WHERE user_id = ?", (user_id,)
        ).fetchall()
    assert len(reminders) >= 6  # 3 meals + 3 hydration

    with db_ro() as conn:
        profile_entries = conn.execute(
            "SELECT key, value FROM profile_context WHERE user_id = ?", (user_id,)
        ).fetchall()
    keys = {row["key"] for row in profile_entries}
    assert "preferred_name" in keys
    assert "timezone" in keys
    assert "check_in_frequency" in keys
    assert "pronouns" in keys
    assert "support_preferences" in keys
    assert "nsfw_opt_in" in keys
