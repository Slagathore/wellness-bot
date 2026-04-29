"""Telegram handlers for adaptive psych assessments."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ContextTypes

from .service import AssessmentResult, AssessmentStep, ProfileAssessmentManager

if TYPE_CHECKING:  # pragma: no cover
    from app.runtime.interfaces import UnifiedWellnessBot


async def start_profile_assessment(
    bot: "UnifiedWellnessBot",
    manager: ProfileAssessmentManager,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Kick off a new profile assessment session."""

    if not update.effective_user or not update.message:
        return

    user_id = bot.ensure_user(
        update.effective_user.id,
        update.effective_user.username,
        update.effective_user.first_name,
    )
    if user_id is None:
        await update.message.reply_text(
            "Sorry, I couldn't access your profile right now. Please try again."
        )
        return

    user_id = int(user_id)
    args = list(getattr(context, "args", []) or [])
    focus = " ".join(args).strip() or None
    session = manager.get_active_session(user_id)
    if session:
        current_index = int(session["current_index"])
        questions = session["question_data"]
        try:
            question_list = [item.get("question", "") for item in json.loads(questions)]
            next_question = question_list[current_index]
        except Exception:  # noqa: BLE001
            next_question = None
        if next_question:
            await update.message.reply_text(
                "You're already in the middle of an assessment.\n"
                "Answer the current question with `/profileanswer <your reply>` or `/profilecancel` to stop.\n\n"
                f"Current question: {next_question}",
                parse_mode="Markdown",
            )
            return

    step = manager.start_session(user_id, focus)
    await update.message.reply_text(
        _format_question(step, focus),
        parse_mode="Markdown",
    )


def _format_question(step: AssessmentStep, focus: str | None) -> str:
    header = "**Adaptive Psych Profile Session**"
    focus_line = f"Focus: {focus}" if focus else "Focus: comprehensive"
    footer = "Reply with `/profileanswer <your reply>` to continue."
    if step.is_final:
        footer += "\n(_This is the final question in this session._)"
    return f"{header}\n{focus_line}\n\nQuestion {step.index + 1}: {step.question}\n\n{footer}"


async def handle_profile_answer(
    bot: "UnifiedWellnessBot",
    manager: ProfileAssessmentManager,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Record an answer and advance to the next adaptive question."""

    if not update.effective_user or not update.message:
        return

    user_id = bot.ensure_user(
        update.effective_user.id,
        update.effective_user.username,
        update.effective_user.first_name,
    )
    if user_id is None:
        await update.message.reply_text(
            "Sorry, I couldn't access your profile right now. Please try again."
        )
        return

    user_id = int(user_id)
    session = manager.get_active_session(user_id)
    if not session:
        await update.message.reply_text(
            "You're not in an active assessment. Start a new one with `/profiletest`.",
            parse_mode="Markdown",
        )
        return

    args = list(getattr(context, "args", []) or [])
    answer = " ".join(args).strip()
    if not answer:
        await update.message.reply_text(
            "Please include your answer after the command, e.g. `/profileanswer I usually...`.",
            parse_mode="Markdown",
        )
        return

    outcome = manager.record_response(session, answer)
    if isinstance(outcome, AssessmentResult):
        bot.invalidate_profile_cache(user_id)
        await update.message.reply_text(
            "Thanks for sharing those insights. Here's what I gathered:\n\n"
            f"{outcome.summary}\n\nI'll tuck this into your profile for future conversations.",
        )
    elif isinstance(outcome, AssessmentStep):
        await update.message.reply_text(
            _format_question(outcome, session.get("focus_area")),
            parse_mode="Markdown",
        )


async def cancel_profile_assessment(
    bot: "UnifiedWellnessBot",
    manager: ProfileAssessmentManager,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Abort the current assessment session."""

    if not update.effective_user or not update.message:
        return

    user_id = bot.ensure_user(
        update.effective_user.id,
        update.effective_user.username,
        update.effective_user.first_name,
    )
    if user_id is None:
        await update.message.reply_text(
            "Sorry, I couldn't access your profile right now. Please try again."
        )
        return

    user_id = int(user_id)
    session = manager.get_active_session(user_id)
    if not session:
        await update.message.reply_text("There's no active assessment to cancel.")
        return

    manager.cancel_session(session["id"])
    await update.message.reply_text(
        "Assessment cancelled. You can restart anytime with `/profiletest`.",
        parse_mode="Markdown",
    )
