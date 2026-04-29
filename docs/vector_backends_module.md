# Vector Backends Module Documentation

**Module**: `app/vector_backends/__init__.py`
**Purpose**: Select and hydrate the configured vector store backend without leaking backend-specific dependencies to the rest of the codebase.
**Status**: Active; invoked by orchestrator, memory, and retrieval flows whenever embeddings are required.

---

## Mission Statement

Keep embedding storage flexible across different deployment footprints by lazily instantiating the backend specified in configuration, ensuring optional dependencies (like NumPy) are only loaded when requested. The module centralizes backend creation, readiness checks, and future extension points for remote providers.

---

## Module Overview

- Exposes `get_backend()` which returns a singleton `VectorBackend` implementation.
- Reads `settings().vector_backend` to determine which backend factory to call.
- Provides `_build_numpy_backend()` guard that raises a clear error if NumPy is missing when the numpy backend is requested.
- Registers factories for SQLite-based backends (`sqlite-vec`, `sqlite-vss`) that have no external binary dependencies.

---

## Classes & Collaborators

- `VectorBackend` (protocol defined in `app/vector_backends/base.py`): common interface with `ensure_ready()` and vector ops consumed downstream.
- `SqliteVecBackend`: lightweight local backend using sqlite-vec extension.
- `SqliteVssBackend`: alternative SQLite backend leveraging the vss extension.
- `NumpyVectorBackend`: pure-Python fallback that requires NumPy; now imported lazily.
- `settings()` from `app.config`: provides the active `vector_backend` string and embedding dimension.

---

## Functions

| Function                 | Description                                                                                                             |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| `_build_numpy_backend()` | Imports and instantiates `NumpyVectorBackend` only when explicitly needed; raises actionable error if NumPy is missing. |
| `get_backend()`          | Returns the singleton backend instance, ensuring it is ready (via `ensure_ready`).                                      |

---

## Variables

- `_BACKEND`: module-level cache of the singleton backend to avoid repeated instantiation.
- `_FACTORIES`: mapping from backend name to factory callables for SQLite-backed implementations.

---

## Extension Ideas

- Add factories for remote/vector services (e.g., pgvector, Qdrant, Pinecone) once telemetry and configuration contracts are defined.
- Surface backend health metrics (init latency, query timings) for readiness dashboards.
