from __future__ import annotations

"""
Repository-level pytest hooks.

Pytest on Windows inspects the magic device name ``nul`` because the path
reports ``exists()`` even though it is not a real file.  That causes the
collector to crash with ``WindowsPath(.../nul) is not a file``.  We ignore the
device so test discovery can proceed normally.
"""

from pathlib import Path

_RESERVED_DEVICES = {"nul"}
_SKIP_DIRS = {".venv", "archive"}


def pytest_ignore_collect(collection_path, config):  # type: ignore[override]
    try:
        p = Path(str(collection_path))
        name = p.name.lower()
    except Exception:  # pragma: no cover - collector errors surface separately
        return False
    if name in _RESERVED_DEVICES:
        return True
    if any(part in _SKIP_DIRS for part in p.parts):
        return True
    return False
