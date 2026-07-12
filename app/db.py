"""SQLite database helpers with read/write separation."""

import os
import sqlite3
from collections.abc import Iterator
from queue import Empty, Queue
import threading
from contextlib import contextmanager, suppress

from app.config import settings


_WRITE_LOCK = threading.RLock()


class ConnectionPool:
    """Simple SQLite connection pool with a fixed maximum size."""

    def __init__(self, size: int):
        self._pool: Queue[sqlite3.Connection] = Queue(maxsize=size)
        self._size = size
        self._created = 0
        self._lock = threading.Lock()

    def acquire(self) -> sqlite3.Connection:
        _ensure_dirs()
        try:
            return self._pool.get_nowait()
        except Empty:
            with self._lock:
                if self._created < self._size:
                    conn = _make_connection()
                    self._created += 1
                    return conn
            # Pool at capacity; block until a connection is returned
            return self._pool.get()

    def release(self, conn: sqlite3.Connection) -> None:
        if conn:
            self._pool.put(conn)

    def close_all(self) -> None:
        while True:
            try:
                conn = self._pool.get_nowait()
            except Empty:
                break
            with suppress(Exception):
                conn.close()
        self._created = 0


_pool_size = getattr(settings(), "db_pool_size", 20) or 20
_POOL = ConnectionPool(size=_pool_size)


def _ensure_dirs() -> None:
    """Ensure the database directory exists."""

    os.makedirs(os.path.dirname(settings().database_path), exist_ok=True)


def _maybe_load_vector_extension(conn: sqlite3.Connection) -> None:
    """Load the sqlite-vec extension on a freshly created connection.

    The extension registers ~50 SQL functions on load. SQLite refuses to
    create/modify functions while any statement is active on the connection
    ("unable to delete/modify user-function due to active statements"), which is
    enforced strictly on Linux. Callers legitimately invoke the vector backend
    from inside an open transaction with active cursors, so loading lazily at
    that point fails. Loading here, when the connection is pristine (only the
    already-completed PRAGMAs have run), avoids that. Best-effort: any failure
    leaves the backend's own lazy loader as a fallback.
    """

    if getattr(settings(), "vector_backend", None) != "sqlite-vec":
        return
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        ext_path = sqlite_vec.loadable_path()
        if not os.path.exists(ext_path) and os.path.exists(ext_path + ".dll"):
            ext_path = ext_path + ".dll"
        conn.load_extension(ext_path)
        conn.enable_load_extension(False)
        conn._sqlite_vec_loaded = True  # type: ignore[attr-defined]
    except Exception:
        with suppress(Exception):
            conn.enable_load_extension(False)


def _make_connection() -> sqlite3.Connection:
    """Create a configured SQLite connection."""

    conn = sqlite3.connect(
        settings().database_path,
        timeout=30.0,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    _maybe_load_vector_extension(conn)
    return conn


@contextmanager
def db_rw() -> Iterator[sqlite3.Connection]:
    """Provide a write-enabled SQLite connection guarded by a lock."""

    conn = _POOL.acquire()
    try:
        with _WRITE_LOCK:
            try:
                yield conn
                conn.commit()
            except Exception:
                if conn.in_transaction:
                    with suppress(Exception):
                        conn.rollback()
                raise
    finally:
        _POOL.release(conn)


@contextmanager
def db_ro() -> Iterator[sqlite3.Connection]:
    """Provide a read-only SQLite connection (no lock required)."""

    conn = _POOL.acquire()
    try:
        yield conn
    finally:
        if conn.in_transaction:
            with suppress(Exception):
                conn.rollback()
        _POOL.release(conn)


def close_pool() -> None:
    """Close all connections in the global pool."""
    _POOL.close_all()
