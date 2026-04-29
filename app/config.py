"""Configuration management using Pydantic."""

from functools import lru_cache
from typing import Dict

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Telegram
    telegram_bot_token: str

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
    vision_model: str = "llava-llama3"
    # Worker model for background jobs (sentiments, nightly, embeddings prompt-fix).
    # Defaults to chat_model when unset.  Set this to a local model to avoid
    # burning cloud API quota on automated background processing.
    worker_model: str | None = None

    # HuggingFace (optional - diffusers also auto-reads ~/.cache/huggingface/token)
    hf_token: str | None = None

    # External image backends
    flux2_klein_url: str = "http://127.0.0.1:7860"
    flux2_klein_host: str = "0.0.0.0"
    flux2_klein_port: int = 7860
    flux2_klein_timeout_seconds: float = 900.0
    flux2_klein_gguf_path: str = ""
    flux2_klein_base_repo: str = "black-forest-labs/FLUX.2-klein-9B"
    flux2_klein_use_torch_compile: bool = False
    media_use_torch_compile: bool = False
    easy_diffusion_url: str = "http://127.0.0.1:9000"
    easy_diffusion_model: str | None = None
    easy_diffusion_vae_model: str | None = None
    easy_diffusion_sampler: str | None = None
    easy_diffusion_vram_usage_level: str = "balanced"
    easy_diffusion_timeout_seconds: float = 900.0
    perchance_timeout_seconds: float = 180.0
    perchance_use_persistent_profile: bool = True
    perchance_other_url: str = "https://perchance.org/imageapi"
    perchance_other_timeout_seconds: float = 90.0

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
