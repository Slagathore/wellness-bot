"""Tests for the adventure selector / auto-rename / fromchat fixes."""

from __future__ import annotations

import sqlite3

import pytest

from app.interfaces.telegram.adapter import TelegramAdapter


@pytest.mark.parametrize(
    "title,is_placeholder",
    [
        ("Adventure #12", True),
        ("Adventure #", True),
        ("Adventure with Chloe", True),
        ("Converted Adventure", True),
        ("Untitled Adventure", True),
        ("New Adventure", True),
        ("Quick Adventure", True),
        ("", True),
        (None, True),
        # The default `Adventure <timestamp>` form must be recognized so the
        # auto-titler actually replaces it (regression: it used to be missed).
        ("Adventure 2026-05-03 21:07", True),
        ("Adventure 2026-06-19 04:21", True),
        ("Adventure 5", True),
        # Real, user-meaningful titles must be preserved (never auto-overridden),
        # including ones that merely start with the word "Adventure".
        ("Adventure of the Lost Ring", False),
        ("Mansion of Moist Mischief", False),
        ("Old Mother Vane and the Darkness", False),
        ("Sailor Moon at Hogwarts", False),
    ],
)
def test_is_placeholder_adventure_title(title, is_placeholder):
    assert TelegramAdapter._is_placeholder_adventure_title(title) is is_placeholder


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("The Darkness Behind Her Eyes", "The Darkness Behind Her Eyes"),
        ("thinking...\nThe Goose in the Machine", "The Goose in the Machine"),
        ('"Echoes of a Silicon Mind."', "Echoes of a Silicon Mind"),
        ("Dark Obsessions of the", None),   # truncated (trailing stopword)
        ("Darkness", None),                  # single word
        ("a " * 40, None),                   # too long
        ("", None),
        (None, None),
    ],
)
def test_clean_generated_title(raw, expected):
    assert TelegramAdapter._clean_generated_title(raw) == expected


def test_adventure_messages_role_constraint_rejects_assistant():
    """Documents why fromchat must map assistant -> narrator.

    adventure_messages only allows user/character/narrator/system; inserting the
    raw 'assistant' role from the messages table violates the CHECK constraint.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE adventure_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            adventure_id INTEGER NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user', 'character', 'narrator', 'system')),
            content TEXT NOT NULL
        )
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO adventure_messages (adventure_id, role, content) VALUES (1, 'assistant', 'hi')"
        )
    # The mapped role used by the fix is accepted.
    conn.execute(
        "INSERT INTO adventure_messages (adventure_id, role, content) VALUES (1, 'narrator', 'hi')"
    )
    assert conn.execute("SELECT COUNT(*) FROM adventure_messages").fetchone()[0] == 1
