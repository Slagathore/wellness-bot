"""User-scoped message queue helper for Telegram updates."""

from __future__ import annotations

import threading
from collections import deque
from typing import Deque, Dict, Iterable

from telegram import Update
from telegram.ext import ContextTypes


class MessageQueue:
    """Thread-safe queue ensuring only one in-flight message per chat."""

    def __init__(self) -> None:
        self._queues: Dict[
            int, Deque[tuple[Update, ContextTypes.DEFAULT_TYPE, str]]
        ] = {}
        self._lock = threading.Lock()

    def enqueue(
        self,
        chat_id: int,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ) -> int:
        """Add message to queue; returns pending count (0 = immediate processing)."""

        with self._lock:
            pending = self._queues.setdefault(chat_id, deque())
            pending.append((update, context, text))
            return len(pending) - 1

    def release(
        self, chat_id: int
    ) -> Iterable[tuple[Update, ContextTypes.DEFAULT_TYPE, str]]:
        """
        Mark the current message as processed and return queued follow-ups.

        The first entry represents the in-flight message; after removing it, remaining messages
        are returned in arrival order.
        """

        with self._lock:
            queue = self._queues.get(chat_id)
            if not queue:
                return []
            if queue:
                queue.popleft()
            if not queue:
                self._queues.pop(chat_id, None)
                return []
            remaining = list(queue)
            self._queues[chat_id] = deque()
            return remaining

    def abandon(self, chat_id: int) -> None:
        """Remove queue for chat (e.g., after errors)."""

        with self._lock:
            self._queues.pop(chat_id, None)
