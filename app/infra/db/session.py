"""
DB session helpers compatible with new modular runtime.

Wraps existing app.db functions to keep a single place for acquisition and
future migration to another backend.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from app.db import close_pool, db_ro as _db_ro, db_rw as _db_rw


@contextmanager
def db_ro() -> Iterator[sqlite3.Connection]:
    """Read-only connection context."""
    with _db_ro() as conn:
        yield conn


@contextmanager
def db_rw() -> Iterator[sqlite3.Connection]:
    """Read-write connection context."""
    with _db_rw() as conn:
        yield conn


def shutdown_pool() -> None:
    """Close pooled connections."""
    close_pool()
