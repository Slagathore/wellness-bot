"""Fallback vector backend using sqlite-vss."""

from __future__ import annotations

import sqlite3
import struct
from typing import Callable, cast, Sequence

from app.config import settings
from app.db import db_ro, db_rw
from .base import VectorBackend


class SqliteVssBackend(VectorBackend):
    """Implementation that relies on the sqlite-vss extension."""

    def __init__(self) -> None:
        self._dim: int | None = None

    def _register_extension(self, conn: sqlite3.Connection) -> None:
        # Lazy import to avoid import error when unused.
        import sqlite_vss  # type: ignore[reportMissingImports]

        conn.enable_load_extension(True)
        register = getattr(sqlite_vss, "vss_register", None)
        if not callable(register):
            raise RuntimeError("sqlite_vss.vss_register is unavailable")
        register_fn = cast(Callable[[sqlite3.Connection], None], register)
        register_fn(conn)
        conn.enable_load_extension(False)

    def ensure_ready(self, dim: int) -> None:
        self._dim = dim
        with db_rw() as conn:
            self._register_extension(conn)
            conn.execute(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS embeddings
                USING vss0(
                    vector({dim})
                )
                """
            )

    def upsert(self, message_id: int, vector: Sequence[float], payload: dict) -> str:
        if self._dim is None:
            raise RuntimeError(
                "Vector backend not initialized. Call ensure_ready() first."
            )

        vector_bytes = struct.pack(f"{len(vector)}f", *vector)

        with db_rw() as conn:
            self._register_extension(conn)
            cursor = conn.execute(
                "INSERT INTO embeddings(vector) VALUES (?)",
                (vector_bytes,),
            )
            rowid = cursor.lastrowid

            conn.execute(
                """
                INSERT INTO embedding_links(message_id, backend, backend_key, model)
                VALUES(?, 'sqlite-vss', ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    backend_key = excluded.backend_key,
                    backend = excluded.backend,
                    model = excluded.model
                """,
                (message_id, str(rowid), settings().embed_model),
            )

        return str(rowid)

    def delete(self, message_id: int) -> None:
        with db_rw() as conn:
            self._register_extension(conn)
            row = conn.execute(
                "SELECT backend_key FROM embedding_links WHERE message_id = ?",
                (message_id,),
            ).fetchone()

            if row:
                conn.execute(
                    "DELETE FROM embeddings WHERE rowid = ?",
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
                vss_distance_l2(e.vector, ?) AS distance
            FROM embeddings AS e
            JOIN embedding_links AS em
                ON em.backend = 'sqlite-vss'
                AND em.backend_key = CAST(e.rowid AS TEXT)
            JOIN messages AS m
                ON m.id = em.message_id
            WHERE m.user_id = ?
              AND m.role IN ({placeholders})
              {ts_clause}
              {scope_clause}
            ORDER BY distance ASC
            LIMIT ?
        """

        params: list = [vector_bytes, user_id, *role_filter]
        if ts_cutoff:
            params.append(ts_cutoff)
        if scope_filter:
            params.append(scope_filter)
        params.append(k)

        with db_ro() as conn:
            self._register_extension(conn)
            rows = conn.execute(sql, params).fetchall()

        return [dict(row) for row in rows]
