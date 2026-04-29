# Wellness Vector Store Module Documentation

**Module**: `app/rag/vector_store.py`
**Purpose**: Persist wellness knowledge resources with embeddings, expose semantic search, and support ingestion utilities for the RAG pipeline.
**Status**: Active and used by orchestrator/pipeline flows when retrieval augmentation is enabled.

---

## Mission Statement

Maintain a lightweight, SQLite-backed vector store that unlocks retrieval-augmented prompts for Wellness Bot without forcing heavyweight dependencies. The module chunks resources, stores embeddings, and performs cosine similarity searches using pure Python math so it works on shadow/canary hosts that may lack NumPy.

---

## Module Overview

- Initializes a small SQLite database (documents, chunks, embeddings tables).
- Provides ingestion via `add_documents()` which chunks content, calls `get_embeddings()`, and stores both metadata and embeddings.
- Offers `search()` to perform semantic similarity using cosine similarity.
- Supplies helper utilities (`_chunk_text`, `_cosine_similarity`) plus stats/close helpers.

---

## Classes

### `WellnessVectorStore`

Encapsulates all persistence and retrieval logic for wellness documents.

| Method            | Role                                                            |
| ----------------- | --------------------------------------------------------------- |
| `__init__`        | Ensures database path exists, bootstraps schema.                |
| `_init_db()`      | Creates tables/indexes if they do not exist.                    |
| `add_documents()` | Inserts docs, splits into chunks, stores embeddings + metadata. |
| `search()`        | Generates a query embedding and returns top matching chunks.    |
| `_chunk_text()`   | Splits raw text into manageable segments.                       |
| `get_stats()`     | Returns counts for monitoring.                                  |
| `close()`         | Compatibility hook for future resource cleanup.                 |

---

## Helper Functions

- `_cosine_similarity(vec_a, vec_b)`: Pure-Python cosine similarity (introduced to remove the hard NumPy dependency).

---

## Variables & Data Contracts

- SQLite tables: `wellness_documents`, `document_chunks`, `chunk_embeddings`.
- Embeddings stored as JSON arrays; metadata persisted as JSON for flexible filtering.

---

## Extension Ideas

- Build approximate nearest-neighbor indexes once `chunk_embeddings` exceeds ~50k rows.
- Add per-category cache warmers for frequently accessed documents.
- Track relevance feedback to improve ranking heuristics.
