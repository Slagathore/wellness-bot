from __future__ import annotations

from app.domain.conversation.service import ConversationService, UserMessage
from app.infra.llm.client import LLMClient


class DummyRepo:
    def __init__(self):
        self.records = []
        self.session_id = 1

    def append(self, message: UserMessage, reply: str | None = None) -> None:
        self.records.append((message, reply))

    def get_session_id(self, db_user_id: int) -> int:
        return self.session_id


def test_normalize_dict_response():
    repo = DummyRepo()
    llm = LLMClient(chat_fn=lambda *args, **kwargs: {"text": "hi"})
    svc = ConversationService(
        repo,
        llm,
        response_generator=lambda msg, llm=llm: llm.chat([]),
    )
    svc.handle_user_message(
        UserMessage(
            user_id="100", db_user_id=1, text="hey", chat_id=1, correlation_id=None
        )
    )
    assert repo.records[0][1] == "hi"


def test_normalize_empty_response_defaults():
    repo = DummyRepo()
    llm = LLMClient(chat_fn=lambda *args, **kwargs: {})
    svc = ConversationService(
        repo,
        llm,
        response_generator=lambda msg, llm=llm: llm.chat([]),
    )
    svc.handle_user_message(
        UserMessage(
            user_id="100", db_user_id=1, text="hey", chat_id=1, correlation_id=None
        )
    )
    assert repo.records[0][1] == "[No response received from LLM]"


def test_response_filter_applied():
    repo = DummyRepo()
    llm = LLMClient(chat_fn=lambda *args, **kwargs: "line1\n!admin\nline2")
    svc = ConversationService(
        repo,
        llm,
        response_filter=lambda t: "\n".join(
            [ln for ln in t.splitlines() if not ln.strip().startswith("!")]
        ),
        response_generator=lambda msg, llm=llm: llm.chat([]),
    )
    svc.handle_user_message(
        UserMessage(
            user_id="100", db_user_id=1, text="hey", chat_id=1, correlation_id=None
        )
    )
    assert "admin" not in repo.records[0][1]
    assert "line2" in repo.records[0][1]
