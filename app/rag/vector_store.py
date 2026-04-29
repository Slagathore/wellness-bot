"""
Wellness Vector Store

Mission Statement:
This module manages the vector database for wellness resources.
It uses sqlite-vec for efficient semantic search of mental health,
wellness, and self-care information.

Features:
- Document storage with embeddings
- Semantic similarity search
- Metadata filtering (category, source, etc.)
- Automatic deduplication

#todo: Add versioning for updated documents
#todo: Implement document expiry/refresh logic
#todo: Add usage analytics (which resources are most retrieved)
#todo: Evaluate migrating per-user document embeddings to sqlite-vector for faster deletes
"""

import json
import logging
import math
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from app.utils.ollama import get_embeddings

logger = logging.getLogger(__name__)


class WellnessVectorStore:
    """Vector database for wellness resources"""

    def __init__(self, db_path: str = "wellness_data/wellness_resources.db"):
        """
        Initialize vector store

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Create tables
        self._init_db()

        logger.info(f"[RAG] Wellness Vector Store initialized at {db_path}")

    def _init_db(self):
        """Create database tables if they don't exist"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        try:
            # Documents table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wellness_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    category TEXT,
                    source TEXT,
                    url TEXT,
                    metadata TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            # Chunks table (split documents for better retrieval)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS document_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    chunk_text TEXT NOT NULL,
                    embedding_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (doc_id) REFERENCES wellness_documents(doc_id),
                    UNIQUE(doc_id, chunk_index)
                )
            """
            )

            # Embeddings table for vector storage
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chunk_embeddings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    embedding_id TEXT UNIQUE NOT NULL,
                    vector BLOB NOT NULL,
                    metadata TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            # Indexes
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_docs_category ON wellness_documents(category)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunks_doc ON document_chunks(doc_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_embeddings_id ON chunk_embeddings(embedding_id)"
            )

            conn.commit()
            logger.info("[RAG] Database tables created")

        except Exception as e:
            logger.error(f"Database initialization error: {e}", exc_info=True)
            raise
        finally:
            conn.close()

    def add_documents(self, documents: List[Dict[str, Any]], chunk_size: int = 500):
        """
        Add wellness documents to vector store

        Args:
            documents: List of document dicts with keys:
                - doc_id: Unique identifier
                - title: Document title
                - content: Full text content
                - category: Optional category (anxiety, depression, sleep, etc.)
                - source: Optional source (CDC, NIMH, etc.)
                - url: Optional reference URL
                - metadata: Optional additional metadata (dict)
            chunk_size: Number of characters per chunk

        Returns:
            Number of documents added
        """
        if not documents:
            return 0

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        added = 0

        try:
            for doc in documents:
                # Insert document
                doc_metadata = doc.get("metadata") or {}
                metadata_json = json.dumps(doc_metadata)

                try:
                    conn.execute(
                        """
                        INSERT INTO wellness_documents
                        (doc_id, title, content, category, source, url, metadata)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            doc["doc_id"],
                            doc["title"],
                            doc["content"],
                            doc.get("category"),
                            doc.get("source"),
                            doc.get("url"),
                            metadata_json,
                        ),
                    )
                except sqlite3.IntegrityError:
                    # Document already exists, skip
                    logger.debug(f"Document {doc['doc_id']} already exists, skipping")
                    continue

                # Chunk the document
                chunks = self._chunk_text(doc["content"], chunk_size)

                # Store chunks and generate embeddings
                for idx, chunk_text in enumerate(chunks):
                    # Generate embedding
                    embedding = get_embeddings([chunk_text])[0]

                    # Store embedding in database
                    embedding_id = f"{doc['doc_id']}_chunk_{idx}"
                    embedding_blob = json.dumps(embedding)  # Store as JSON
                    chunk_metadata = {
                        "doc_id": doc["doc_id"],
                        "chunk_index": idx,
                    }
                    if isinstance(doc_metadata, dict):
                        chunk_metadata.update(doc_metadata)
                    metadata_json = json.dumps(chunk_metadata)

                    conn.execute(
                        """
                        INSERT OR REPLACE INTO chunk_embeddings
                        (embedding_id, vector, metadata)
                        VALUES (?, ?, ?)
                    """,
                        (embedding_id, embedding_blob, metadata_json),
                    )

                    # Store chunk reference
                    conn.execute(
                        """
                        INSERT INTO document_chunks
                        (doc_id, chunk_index, chunk_text, embedding_id)
                        VALUES (?, ?, ?, ?)
                    """,
                        (doc["doc_id"], idx, chunk_text, embedding_id),
                    )

                added += 1
                logger.info(
                    f"[RAG] Added document: {doc['title']} ({len(chunks)} chunks)"
                )

            conn.commit()
            logger.info(f"[RAG] Added {added} documents to vector store")

        except Exception as e:
            logger.error(f"Error adding documents: {e}", exc_info=True)
            conn.rollback()
            raise
        finally:
            conn.close()

        return added

    def search(
        self,
        query: str,
        top_k: int = 5,
        category: Optional[str] = None,
        metadata_filter: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Semantic search for relevant wellness resources

        Args:
            query: Search query
            top_k: Number of results to return
            category: Optional filter by category

        Returns:
            List of result dicts with keys:
                - doc_id: Document ID
                - title: Document title
                - chunk_text: Relevant text chunk
                - score: Similarity score
                - category: Document category
                - source: Document source
                - url: Reference URL
                - metadata: Document-level metadata
                - chunk_metadata: Chunk-level metadata (includes doc metadata)
        """
        # Generate query embedding (list of floats)
        query_vec = get_embeddings([query])[0]

        # Get all embeddings from database
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        all_embeddings = conn.execute(
            """
            SELECT embedding_id, vector, metadata FROM chunk_embeddings
        """
        ).fetchall()

        # Calculate cosine similarity for each
        similarities = []
        for row in all_embeddings:
            chunk_meta = json.loads(row["metadata"]) if row["metadata"] else {}

            if metadata_filter:
                mismatch = False
                for key, value in metadata_filter.items():
                    if chunk_meta.get(key) != value:
                        mismatch = True
                        break
                if mismatch:
                    continue

            embedding_vec = json.loads(row["vector"])
            similarity = _cosine_similarity(query_vec, embedding_vec)
            similarities.append(
                {
                    "embedding_id": row["embedding_id"],
                    "score": float(similarity),
                    "metadata": chunk_meta,
                }
            )

        # Sort by similarity (highest first)
        similarities.sort(key=lambda x: x["score"], reverse=True)
        results = similarities[: top_k * 2]  # Get extra for filtering

        # Fetch document metadata
        formatted_results = []

        try:
            for result in results:
                doc_id = result["metadata"]["doc_id"]
                chunk_index = result["metadata"]["chunk_index"]

                # Get document info
                doc_row = conn.execute(
                    """
                    SELECT d.title, d.category, d.source, d.url, d.metadata, c.chunk_text
                    FROM wellness_documents d
                    JOIN document_chunks c ON d.doc_id = c.doc_id
                    WHERE d.doc_id = ? AND c.chunk_index = ?
                """,
                    (doc_id, chunk_index),
                ).fetchone()

                if not doc_row:
                    continue

                doc_meta = {}
                if doc_row["metadata"]:
                    try:
                        doc_meta = json.loads(doc_row["metadata"])
                    except json.JSONDecodeError:
                        doc_meta = {"raw_metadata": doc_row["metadata"]}

                # Filter by category if specified
                if category and doc_row["category"] != category:
                    continue

                formatted_results.append(
                    {
                        "doc_id": doc_id,
                        "title": doc_row["title"],
                        "chunk_text": doc_row["chunk_text"],
                        "score": result["score"],
                        "category": doc_row["category"],
                        "source": doc_row["source"],
                        "url": doc_row["url"],
                        "metadata": doc_meta,
                        "chunk_metadata": result["metadata"],
                    }
                )

                if len(formatted_results) >= top_k:
                    break

        finally:
            conn.close()

        return formatted_results

    def _chunk_text(self, text: str, chunk_size: int = 500) -> List[str]:
        """
        Split text into chunks for better retrieval

        Args:
            text: Text to chunk
            chunk_size: Approximate characters per chunk

        Returns:
            List of text chunks
        """
        # Split by paragraphs first
        paragraphs = text.split("\n\n")
        chunks = []
        current_chunk = ""

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            # If adding this paragraph exceeds chunk size, save current chunk
            if len(current_chunk) + len(para) > chunk_size and current_chunk:
                chunks.append(current_chunk.strip())
                current_chunk = para
            else:
                current_chunk += "\n\n" + para if current_chunk else para

        # Add final chunk
        if current_chunk:
            chunks.append(current_chunk.strip())

        return chunks

    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about the vector store"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        try:
            doc_count = conn.execute(
                "SELECT COUNT(*) as count FROM wellness_documents"
            ).fetchone()["count"]
            chunk_count = conn.execute(
                "SELECT COUNT(*) as count FROM document_chunks"
            ).fetchone()["count"]

            categories = conn.execute(
                """
                SELECT category, COUNT(*) as count
                FROM wellness_documents
                WHERE category IS NOT NULL
                GROUP BY category
            """
            ).fetchall()

            return {
                "total_documents": doc_count,
                "total_chunks": chunk_count,
                "categories": {row["category"]: row["count"] for row in categories},
            }
        finally:
            conn.close()

    def close(self) -> None:
        """Release resources held by the vector store (compatibility hook)."""
        logger.debug("[RAG] Vector store close invoked")


def _cosine_similarity(vec_a: Sequence[float], vec_b: Sequence[float]) -> float:
    """Compute cosine similarity without requiring optional numpy dependency."""

    if not vec_a or not vec_b:
        return 0.0

    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for a, b in zip(vec_a, vec_b):
        dot += a * b
        norm_a += a * a
        norm_b += b * b

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


# Module-level variables tracking
# - WellnessVectorStore: Main class for vector storage operations
# - add_documents(): Stores wellness resources with embeddings
# - search(): Semantic similarity search
# - _chunk_text(): Text chunking for better retrieval

# #todo: Add caching for frequently accessed documents
# #todo: Implement relevance feedback (track which results users find helpful)
# #todo: Add support for multi-modal embeddings (text + images)
# #todo: Evaluate approximate nearest-neighbor index once chunk_embeddings grows beyond 50k rows
