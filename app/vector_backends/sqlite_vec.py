"""Implementation of the vector backend using sqlite-vec."""

from __future__ import annotations

import sqlite3
import struct
from typing import Sequence

from app.config import settings
from app.db import (
    db_ro,
    db_rw,
    mark_vector_extension_loaded,
    vector_extension_is_loaded,
)

from .base import VectorBackend


class SqliteVecBackend(VectorBackend):
    """Vector storage backed by the sqlite-vec extension."""

    def __init__(self) -> None:
        self._dim: int | None = None

    def _register_extension(self, conn: sqlite3.Connection) -> None:
        """Ensure the sqlite-vec extension is available on the connection.

        Connections come from a shared pool and are reused across operations, so
        this may be called many times on the same connection. Loading the
        extension more than once re-registers its SQL functions; on Linux that
        raises "unable to delete/modify user-function due to active statements".
        Load exactly once per connection by tagging it after the first load.
        """

        if vector_extension_is_loaded(conn):
            return

        import os

        import sqlite_vec

        conn.enable_load_extension(True)

        # Fix for Windows: loadable_path() returns path without extension
        # but the actual file is vec0.dll on Windows
        ext_path = sqlite_vec.loadable_path()
        if not os.path.exists(ext_path):
            # Try with .dll extension on Windows
            dll_path = ext_path + ".dll"
            if os.path.exists(dll_path):
                ext_path = dll_path

        conn.load_extension(ext_path)
        conn.enable_load_extension(False)

        mark_vector_extension_loaded(conn)

    def ensure_ready(self, dim: int) -> None:
        """Create the virtual table for vector search if needed."""

        self._dim = dim
        with db_rw() as conn:
            self._register_extension(conn)
            conn.execute(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_messages
                USING vec0(
                    vector float[{dim}]
                )
                """
            )

    def upsert(self, message_id: int, vector: Sequence[float], payload: dict) -> str:
        """Insert or update the vector for the given message."""

        if self._dim is None:
            raise RuntimeError(
                "Vector backend not initialized. Call ensure_ready() first."
            )

        vector_bytes = struct.pack(f"{len(vector)}f", *vector)

        with db_rw() as conn:
            self._register_extension(conn)

            cursor = conn.execute(
                "INSERT INTO vec_messages(vector) VALUES (?)",
                (vector_bytes,),
            )
            rowid = cursor.lastrowid

            conn.execute(
                """
                INSERT INTO embedding_links(message_id, backend, backend_key, model)
                VALUES(?, 'sqlite-vec', ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    backend_key = excluded.backend_key,
                    backend = excluded.backend,
                    model = excluded.model
                """,
                (message_id, str(rowid), settings().embed_model),
            )

        return str(rowid)

    def delete(self, message_id: int) -> None:
        """Remove the stored vector for a message if it exists."""

        with db_rw() as conn:
            self._register_extension(conn)

            row = conn.execute(
                "SELECT backend_key FROM embedding_links WHERE message_id = ?",
                (message_id,),
            ).fetchone()

            if row:
                conn.execute(
                    "DELETE FROM vec_messages WHERE rowid = ?",
                    (int(row["backend_key"]),),
                )
                conn.execute(
                    "DELETE FROM embedding_links WHERE message_id = ?",
                    (message_id,),
                )

    def top_k(
        self,
        user_id: int,
        query_vector: Sequence[float],
        k: int,
        role_filter: tuple[str, ...] = ("user", "assistant"),
        ts_cutoff: str | None = None,
        scope_filter: str | None = None,
    ) -> list[dict]:
        """Return the nearest messages for the provided embedding."""

        if self._dim is None:
            raise RuntimeError(
                "Vector backend not initialized. Call ensure_ready() first."
            )

        vector_bytes = struct.pack(f"{len(query_vector)}f", *query_vector)
        placeholders = ",".join("?" for _ in role_filter)
        ts_clause = "AND m.timestamp >= ?" if ts_cutoff else ""
        scope_clause = "AND m.scope = ?" if scope_filter else ""

        sql = f"""
            SELECT
                em.message_id,
                m.content,
                m.timestamp,
                m.role,
                nn.distance
            FROM (
                SELECT rowid, distance
                FROM vec_messages
                WHERE vec_distance_L2(vector, ?)
                ORDER BY distance ASC
                LIMIT ?
            ) AS nn
            JOIN embedding_links AS em
                ON em.backend = 'sqlite-vec'
                AND em.backend_key = CAST(nn.rowid AS TEXT)
            JOIN messages AS m
                ON m.id = em.message_id
            WHERE m.user_id = ?
              AND m.role IN ({placeholders})
              {ts_clause}
              {scope_clause}
            ORDER BY nn.distance ASC
        """

        params: list = [vector_bytes, k, user_id, *role_filter]
        if ts_cutoff:
            params.append(ts_cutoff)
        if scope_filter:
            params.append(scope_filter)

        with db_ro() as conn:
            self._register_extension(conn)
            rows = conn.execute(sql, params).fetchall()

        return [dict(row) for row in rows]
