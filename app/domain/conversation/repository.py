"""
Minimal conversation repository stub.

Replace with real persistence (messages table) during migration.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.domain.conversation.service import ConversationRepository, UserMessage

logger = logging.getLogger(__name__)


class NullConversationRepository(ConversationRepository):
    """No-op repo that only logs interactions (for scaffolding/testing)."""

    def append(self, message: UserMessage, reply: Optional[str] = None) -> None:
        logger.debug(
            "Conversation log user=%s text=%s reply=%s",
            message.user_id,
            message.text,
            reply,
        )

    def get_session_id(self, db_user_id: int) -> int:
        logger.debug("Conversation get_session_id called for user_id=%s", db_user_id)
        return 0
