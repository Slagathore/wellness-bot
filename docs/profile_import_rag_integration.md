"""
Mission Statement:
Document the RAG integration path for uploaded profile documents so operators and contributors understand how imported files become searchable context.
"""

## Module-Level Summary

- **app/features/profile_import/handlers.py**: `_process_import_documents` now calls `UnifiedWellnessBot.ingest_profile_documents` after persisting import payloads. This pushes combined text into the vector store so later queries can retrieve it. The file also logs ingestion failures and sets a `rag_ingestion` field on the stored payload.
- **app/rag/vector_store.py**: `WellnessVectorStore.add_documents` merges document-level metadata into chunk embeddings; `search` accepts a `metadata_filter` so callers can scope results (e.g., to a specific user). Result dictionaries now include `metadata` and `chunk_metadata` for downstream consumers.
- **unified_bot.py**: The `UnifiedWellnessBot` class gained helpers to ingest documents, register ownership, seed the registry from disk, and conditionally retrieve uploaded content during message handling.

## Function & Class Details

- `ImportDocument` (handlers.py): Unchanged data class representing uploads; its filenames feed the new ingestion metadata.
- `_process_import_documents` (handlers.py): After combining text and persisting to `profile_context`, it awaits `bot.ingest_profile_documents(...)` and records the returned metadata under `rag_ingestion`.
- `WellnessVectorStore.add_documents` (vector_store.py): Stores merged metadata (`doc_type`, `owner_user_id`, etc.) beside chunk embeddings so `search` can filter on them.
- `WellnessVectorStore.search` (vector_store.py): Signature adds `metadata_filter`; results now supply `metadata` (document-level) and `chunk_metadata` (chunk-level). Filters short-circuit before cosine similarity when metadata does not match.
- `UnifiedWellnessBot.ingest_profile_documents`: Hashes combined text to build a deterministic `doc_id`, writes document metadata, runs `vector_store.add_documents` in a background thread, tracks chunk counts, and registers the doc for the owning user.
- `UnifiedWellnessBot._register_profile_document`: Maintains an in-memory registry mapping user IDs to their document IDs.
- `UnifiedWellnessBot._prime_profile_doc_registry`: On startup, reads existing vector store rows to repopulate the registry so previously imported docs remain discoverable.
- `UnifiedWellnessBot._estimate_chunk_count`: Mirrors the vector store chunking heuristic so ingestion metadata reports approximate chunk totals.
- `UnifiedWellnessBot._should_query_profile_docs`: Keyword heuristic that checks whether the user query appears to reference uploaded material before running an embedding search.
- `UnifiedWellnessBot._retrieve_profile_documents`: Uses the metadata filter to request only the calling user’s documents, caps snippet length, and returns context plus human-readable citations.
- `UnifiedWellnessBot.handle_message`: Extends the RAG block to call `_retrieve_profile_documents` when `_should_query_profile_docs` is true, appending the imported context and citations to the system prompt.

## Key Variables Introduced

- `metadata` (handlers.py & vector_store.py): Carries `doc_type`, `owner_user_id`, and other ingestion details for each imported document.
- `self._profile_doc_registry` (unified_bot.py): `defaultdict(set)` mapping user IDs to known profile-import document IDs.
- `metadata_filter` (vector_store.py): Optional `dict[str, Any]` used during searches to scope results to user-specific documents.

## Called Modules & Classes

- `UnifiedWellnessBot` methods rely on `WellnessVectorStore` for storage, `db_ro` for profile summaries, and `ReminderIntentScheduler` for reminder updates (unchanged but referenced here for completeness).
- `WellnessVectorStore` pulls `get_embeddings` from `app.utils.ollama` to produce vectors and uses `numpy` for similarity calculations.
- The ingestion flow depends on `ImportDocument` (handlers.py) and the Telegram file handling pipelines defined elsewhere in the bot.

## Follow-Up Ideas

- #todo: Implement per-user TTL or archival policy for uploaded documents so stale instructions can be retired automatically.
- #todo: Add unit tests that simulate a PDF import followed by a targeted question to ensure the new retrieval path stays healthy.
