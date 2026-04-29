"""Telegram command handlers for the feedback feature."""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ContextTypes

from app.db import db_ro, db_rw

if TYPE_CHECKING:  # pragma: no cover
    from app.runtime.interfaces import UnifiedWellnessBot

MAX_FEEDBACK_LENGTH = 1500

STATUS_LABELS = {
    "new": "New",
    "reviewing": "Reviewing",
    "resolved": "Resolved",
    "wont_fix": "Won't fix",
}


async def report_bug(
    bot: "UnifiedWellnessBot", update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /reportbug command."""
    if not update.effective_user or not update.message:
        return

    telegram_user = update.effective_user
    user_id = bot.ensure_user(
        telegram_user.id, telegram_user.username, telegram_user.first_name
    )
    content = _extract_payload(update.message.text)

    if not content:
        await update.message.reply_text(
            "Tell me what went wrong so I can log it.\n\n"
            "Usage: /reportbug <description>",
        )
        return

    truncated_content = content[:MAX_FEEDBACK_LENGTH].strip()

    with db_rw() as conn:
        conn.execute(
            """INSERT INTO user_feedback (user_id, feedback_type, content)
            VALUES (?, 'bug', ?)""",
            (user_id, truncated_content),
        )

    await update.message.reply_text(
        "Thanks for the heads-up. I've recorded the bug and will review it soon."
    )
    bot.log(f"Bug report stored for tg_id={telegram_user.id}")


async def submit_suggestion(
    bot: "UnifiedWellnessBot", update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /suggestion command."""
    if not update.effective_user or not update.message:
        return

    telegram_user = update.effective_user
    user_id = bot.ensure_user(
        telegram_user.id, telegram_user.username, telegram_user.first_name
    )
    content = _extract_payload(update.message.text)

    if not content:
        await update.message.reply_text(
            "Share your idea right after the command.\n\n"
            "Usage: /suggestion <what would help you?>",
        )
        return

    truncated_content = content[:MAX_FEEDBACK_LENGTH].strip()

    with db_rw() as conn:
        conn.execute(
            """INSERT INTO user_feedback (user_id, feedback_type, content)
            VALUES (?, 'suggestion', ?)""",
            (user_id, truncated_content),
        )

    await update.message.reply_text(
        "Appreciate the suggestion! It'll show up in the feedback queue."
    )
    bot.log(f"Suggestion stored for tg_id={telegram_user.id}")


async def list_my_feedback(
    bot: "UnifiedWellnessBot", update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /myfeedback command."""
    if not update.effective_user or not update.message:
        return

    telegram_user = update.effective_user
    user_id = bot.ensure_user(
        telegram_user.id, telegram_user.username, telegram_user.first_name
    )

    with db_ro() as conn:
        rows = conn.execute(
            """SELECT feedback_type, content, status, created_at
            FROM user_feedback
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 20""",
            (user_id,),
        ).fetchall()

    if not rows:
        await update.message.reply_text(
            "You have not submitted any bugs or suggestions yet."
        )
        return

    lines = ["Here is the latest feedback you sent:\n"]
    for row in rows:
        entry_type = "Bug" if row["feedback_type"] == "bug" else "Suggestion"
        status_label = STATUS_LABELS.get(row["status"], "Open")
        created_at = (row["created_at"] or "")[:16]
        snippet = row["content"].strip()
        if len(snippet) > 160:
            snippet = f"{snippet[:157]}..."

        lines.append(f"{entry_type} · {status_label} · {created_at}")
        lines.append(f"  {snippet}")
        lines.append("")

    await update.message.reply_text("\n".join(lines).strip())


def _extract_payload(message_text: str | None) -> str:
    """Strip the command prefix and return free-form text."""
    if not message_text:
        return ""

    parts = message_text.split(maxsplit=1)
    if len(parts) == 1:
        return ""
    return parts[1].strip()
