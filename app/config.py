"""Configuration management using Pydantic."""

from functools import lru_cache
from typing import Dict

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Telegram
    telegram_bot_token: str

    # Roleplay/adventure Mini App (Telegram WebApp). Disabled by default; set a
    # public HTTPS URL and enable to surface a "Play in App" button.
    webapp_enabled: bool = False
    webapp_url: str | None = None
    webapp_host: str = "127.0.0.1"
    webapp_port: int = 8130
    webapp_initdata_max_age_seconds: int = 86400
    # Optional shared secret for opening the Mini App in a normal desktop browser
    # (outside Telegram). When set, the webapp shows a login box; a correct token
    # signs a session cookie mapped to the admin_username's account. Leave unset
    # to keep the webapp Telegram-only.
    webapp_access_token: str | None = None
    webapp_session_ttl_seconds: int = 604800  # 7 days

    # Discord
    discord_bot_token: str | None = None

    # Infrastructure
    redis_url: str = "redis://localhost:6379/0"
    ollama_host: str = "http://localhost:11434"

    # Timeouts / retries
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 2
    llm_backoff_seconds: float = 0.5
    vector_max_retries: int = 2
    vector_backoff_seconds: float = 0.25

    # Jobs / toggles
    enable_checkin_job: bool = False

    # Embeddings
    embed_model: str = "nomic-embed-text"
    embed_dimensions: int = 384

    # AI Models
    chat_model: str = "huihui_ai/gemma3n-abliterated:e2b-fp16"
    vision_model: str = "mistral-large-3:675b-cloud"
    # Worker model for local background helpers (reminder text, embeddings prompt-fix, etc.).
    # Defaults to chat_model when unset.  Set this to a local model to avoid
    # burning cloud API quota on automated background processing.
    worker_model: str | None = None
    # Dedicated model for the hot-path turn planner / sentiment analyzer.
    planner_model: str | None = "mistral-large-3:675b-cloud"
    # Backward-compatible alias for older env/config keys.
    turn_planner_model: str | None = None
    # Dedicated model for nightly reprocessing and batch profile analysis.
    nightly_model: str | None = "gemini-3-flash-preview:cloud"
    turn_planner_timeout_seconds: float = 8.0

    # HuggingFace (optional - diffusers also auto-reads ~/.cache/huggingface/token)
    hf_token: str | None = None

    # Image generation via the DungeonMaster SDXL backend (standalone server;
    # run `python dm_imagegen.py --serve`). This is the only image path used.
    dm_image_url: str = "http://127.0.0.1:8500"
    dm_image_enabled: bool = True
    dm_image_timeout_seconds: float = 300.0

    # Filesystem
    data_root: str = "/data/telegram_wellness_bot"
    database_path: str = "/data/telegram_wellness_bot/telegram_wellness.db"

    # Admin
    admin_username: str | None = None
    admin_password: str | None = None
    admin_trust_token: str | None = None  # optional long-lived trust cookie
    enable_dangerous_tools: bool = False
    admin_llm_console_enabled: bool = False
    admin_db_edit_enabled: bool = False
    admin_omni_broadcast_enabled: bool = False

    # Vector backend selection
    vector_backend: str = "sqlite-vec"

    # Context management
    ctx_token_budget: int = 120000
    # Feature flags
    enable_async_reminder_parser: bool = False
    feature_flags: Dict[str, bool] = {}
    feature_flags_env_var: str = "APP_FEATURE_FLAGS"

    top_k_retrieval: int = 5
    memory_semantic_join_timeout_seconds: float = 0.45
    memory_classifier_threshold: float = 0.45
    memory_cache_ttl_seconds: float = 60.0
    profile_context_cache_ttl_seconds: float = 300.0
    event_bus_workers: int = 32

    # Shard rotation thresholds
    max_shard_messages: int = 1000
    max_shard_bytes: int = 5_000_000

    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"  # Ignore extra fields like app_feature_flags from .env


@lru_cache()
def settings() -> Settings:
    """Cached settings loader to avoid repeated env parsing."""

    return Settings()  # type: ignore[call-arg]
