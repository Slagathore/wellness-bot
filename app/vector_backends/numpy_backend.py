"""Pure Python vector backend using NumPy for similarity search.

This is a fallback for when sqlite-vec/sqlite-vss don't work (common on Windows).
Vectors are stored as JSON blobs in SQLite and loaded into memory for search.
For production with >10k vectors, use a proper vector DB, but this works fine for wellness tracking.
"""

from __future__ import annotations

import json
from typing import Sequence

try:
    import numpy as np  # type: ignore[reportMissingImports]
except ImportError:  # pragma: no cover - optional dependency
    np = None

from app.config import settings
from app.db import db_ro, db_rw

from .base import VectorBackend


class NumpyVectorBackend(VectorBackend):
    """Vector storage using SQLite JSON + NumPy for in-memory similarity search."""

    def __init__(self) -> None:
        self._dim: int | None = None

    def ensure_ready(self, dim: int) -> None:
        """Create the vector storage table if needed."""
        if np is None:
            raise RuntimeError("numpy is required for NumpyVectorBackend")
        self._dim = dim

        with db_rw() as conn:
            # Simple table: rowid, vector_json, metadata
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS numpy_vectors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vector_json TEXT NOT NULL,
                    message_id INTEGER UNIQUE NOT NULL,
                    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
                )
            """
            )

            # Index for fast message_id lookups
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_numpy_vectors_message
                ON numpy_vectors(message_id)
            """
            )

    def upsert(self, message_id: int, vector: Sequence[float], payload: dict) -> str:
        """Insert or update the vector for the given message."""
        if self._dim is None:
            raise RuntimeError(
                "Vector backend not initialized. Call ensure_ready() first."
            )

        vector_json = json.dumps(list(vector))

        with db_rw() as conn:
            # Check if exists
            existing = conn.execute(
                "SELECT id FROM numpy_vectors WHERE message_id = ?", (message_id,)
            ).fetchone()

            if existing:
                rowid = existing["id"]
                conn.execute(
                    "UPDATE numpy_vectors SET vector_json = ? WHERE id = ?",
                    (vector_json, rowid),
                )
            else:
                cursor = conn.execute(
                    "INSERT INTO numpy_vectors(vector_json, message_id) VALUES (?, ?)",
                    (vector_json, message_id),
                )
                rowid = cursor.lastrowid

            # Update embedding_links
            conn.execute(
                """
                INSERT INTO embedding_links(message_id, backend, backend_key, model)
                VALUES(?, 'numpy', ?, ?)
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
            conn.execute(
                "DELETE FROM numpy_vectors WHERE message_id = ?", (message_id,)
            )
            conn.execute(
                "DELETE FROM embedding_links WHERE message_id = ?", (message_id,)
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
        """Return the nearest messages for the provided embedding using cosine similarity."""
        if np is None:
            raise RuntimeError("numpy is required for NumpyVectorBackend")
        if self._dim is None:
            raise RuntimeError(
                "Vector backend not initialized. Call ensure_ready() first."
            )

        query_vec = np.array(query_vector, dtype=np.float32)
        query_norm = np.linalg.norm(query_vec)

        if query_norm == 0:
            return []  # Zero vector, no meaningful similarity

        # Normalize query vector for cosine similarity
        query_vec = query_vec / query_norm

        # Build SQL to get candidate vectors
        placeholders = ",".join("?" for _ in role_filter)
        ts_clause = "AND m.timestamp >= ?" if ts_cutoff else ""
        scope_clause = "AND m.scope = ?" if scope_filter else ""

        sql = f"""
            SELECT
                nv.id,
                nv.vector_json,
                nv.message_id,
                m.content,
                m.timestamp,
                m.role
            FROM numpy_vectors AS nv
            JOIN messages AS m ON m.id = nv.message_id
            WHERE m.user_id = ?
              AND m.role IN ({placeholders})
              {ts_clause}
              {scope_clause}
        """

        params: list = [user_id, *role_filter]
        if ts_cutoff:
            params.append(ts_cutoff)
        if scope_filter:
            params.append(scope_filter)

        with db_ro() as conn:
            rows = conn.execute(sql, params).fetchall()

        if not rows:
            return []

        # Load vectors and compute cosine similarity
        results = []
        for row in rows:
            vec = np.array(json.loads(row["vector_json"]), dtype=np.float32)
            vec_norm = np.linalg.norm(vec)

            if vec_norm == 0:
                continue

            vec = vec / vec_norm

            # Cosine similarity = dot product of normalized vectors
            similarity = float(np.dot(query_vec, vec))

            # Convert to distance (1 - similarity) so lower is better
            distance = 1.0 - similarity

            results.append(
                {
                    "message_id": row["message_id"],
                    "content": row["content"],
                    "timestamp": row["timestamp"],
                    "role": row["role"],
                    "distance": distance,
                }
            )

        # Sort by distance (ascending = most similar first) and take top k
        results.sort(key=lambda x: x["distance"])
        return results[:k]
