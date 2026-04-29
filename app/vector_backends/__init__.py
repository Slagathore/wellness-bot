"""Mission Statement:
Provide a flexible factory that selects the best-fit vector backend for Wellness Bot
without forcing unused dependencies onto every runtime environment. This module wires
up the configured backend, primes it, and shields the rest of the codebase from
backend-specific imports so we can run shadow/canary flows with minimal friction."""

from __future__ import annotations

from typing import Callable, Dict

from app.config import settings

from .base import VectorBackend
from .sqlite_vec import SqliteVecBackend
from .sqlite_vss import SqliteVssBackend

_BACKEND: VectorBackend | None = None
_FACTORIES: Dict[str, Callable[[], VectorBackend]] = {
    "sqlite-vss": lambda: SqliteVssBackend(),
    "sqlite-vec": lambda: SqliteVecBackend(),
}


def _build_numpy_backend() -> VectorBackend:
    """Instantiate the numpy backend only when explicitly requested."""

    try:
        from .numpy_backend import (
            NumpyVectorBackend,
        )  # local import to avoid hard dependency
    except ModuleNotFoundError as exc:  # pragma: no cover - defensive guard
        raise RuntimeError(
            "VECTOR_BACKEND set to numpy, but numpy is not installed. "
            "Install numpy or switch VECTOR_BACKEND to sqlite-vec/sqlite-vss."
        ) from exc
    return NumpyVectorBackend()


def get_backend() -> VectorBackend:
    """Return a singleton instance of the configured vector backend."""

    global _BACKEND
    if _BACKEND is None:
        cfg = settings()
        backend_factory = _FACTORIES.get(cfg.vector_backend)
        if backend_factory is None:
            backend_factory = _build_numpy_backend
        backend = backend_factory()
        backend.ensure_ready(cfg.embed_dimensions)
        _BACKEND = backend
    return _BACKEND


# todo: auto-register remote vector backends (pgvector, qdrant) once telemetry baselines exist.
