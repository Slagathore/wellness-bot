"""
Config bootstrapper and validation wrapper around app.config.Settings.
"""

from pathlib import Path
from typing import Any, Dict

from app.config import Settings
from app.config import settings as _legacy_settings


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def load_config(*, env_file: str | None = ".env") -> Settings:
    """
    Load and validate settings.

    Returns Settings and ensures filesystem paths exist.
    """

    # app.config.Settings already reads env_file by default; env_file kept for clarity.
    cfg = _legacy_settings()
    _ensure_paths(cfg)
    _validate(cfg)
    return cfg


def redact(cfg: Settings) -> Dict[str, Any]:
    """Return a redacted dict for logging/debug output."""
    data = cfg.model_dump()
    for key in list(data.keys()):
        if "token" in key or "key" in key or "secret" in key or "password" in key:
            data[key] = "***"
    return data


def _ensure_paths(cfg: Settings) -> None:
    """Create necessary directories."""
    Path(cfg.data_root).mkdir(parents=True, exist_ok=True)
    db_path = Path(cfg.database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)


def _validate(cfg: Settings) -> None:
    missing = []
    if not cfg.telegram_bot_token:
        missing.append("telegram_bot_token")
    allowed = {
        "sqlite-vec",
        "sqlite-vss",
        "pinecone",
        "chroma",
        "weaviate",
        "qdrant",
        "numpy",
    }
    if cfg.vector_backend not in allowed:
        raise ConfigError(f"Unsupported vector_backend: {cfg.vector_backend}")
    if missing:
        raise ConfigError(f"Missing required config: {', '.join(missing)}")


__all__ = ["load_config", "ConfigError", "redact"]
