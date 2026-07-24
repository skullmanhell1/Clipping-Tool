"""Application configuration.

Centralised, type-safe settings for the video clipping tool. Values are loaded
from environment variables and/or a local ``.env`` file using
``pydantic-settings``.

Import the shared singleton wherever configuration is needed::

    from config import settings

    print(settings.app_name)

Nothing in here performs real work yet -- it only declares the configuration
surface the rest of the application (API, worker, publishers, storage) will
consume in later phases.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root -- used to resolve default local storage locations.
BASE_DIR = Path(__file__).resolve().parent


class LLMProvider(str, Enum):
    """Supported large language model providers for the pluggable client."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    # Google Gemini via its OpenAI-compatible endpoint (has a free tier).
    GEMINI = "gemini"


class StorageBackend(str, Enum):
    """Supported storage backends. Local by default, swappable to S3."""

    LOCAL = "local"
    S3 = "s3"


class Settings(BaseSettings):
    """Strongly-typed application settings.

    Every field maps to an environment variable of the same (upper-cased) name.
    See ``.env.example`` for the full list and human-friendly descriptions.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ----------------------------------------------------------------- app --
    app_name: str = Field(default="AI Video Clipper", description="Display name.")
    environment: str = Field(default="development", description="dev/staging/prod.")
    debug: bool = Field(default=True, description="Enable verbose debug behaviour.")
    api_host: str = Field(default="0.0.0.0", description="API bind host.")
    api_port: int = Field(default=8000, description="API bind port.")
    # Comma-separated list of allowed CORS origins.
    cors_origins: str = Field(default="*", description="Allowed CORS origins.")

    # --------------------------------------------------------------- queue --
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL for the RQ task queue.",
    )
    # When Redis is unavailable, tasks fall back to running in-process.
    use_inprocess_fallback: bool = Field(
        default=True,
        description="Run jobs synchronously in-process if Redis is unavailable.",
    )
    rq_queue_name: str = Field(default="clips", description="RQ queue name.")

    # ----------------------------------------------------------------- llm --
    llm_provider: LLMProvider = Field(
        default=LLMProvider.OPENAI,
        description="Which LLM provider the pluggable client should use.",
    )
    openai_api_key: Optional[str] = Field(default=None, description="OpenAI key.")
    openai_model: str = Field(default="gpt-4o-mini", description="OpenAI model.")
    # Optional custom base URL for any OpenAI-compatible endpoint (e.g. a local
    # Ollama / LM Studio server, or a proxy). Leave unset for real OpenAI.
    openai_base_url: Optional[str] = Field(
        default=None, description="Custom OpenAI-compatible base URL."
    )
    anthropic_api_key: Optional[str] = Field(default=None, description="Anthropic key.")
    anthropic_model: str = Field(
        default="claude-3-5-sonnet-latest", description="Anthropic model."
    )
    # Google Gemini (used via its OpenAI-compatible endpoint).
    gemini_api_key: Optional[str] = Field(default=None, description="Gemini API key.")
    gemini_model: str = Field(default="gemini-2.0-flash", description="Gemini model.")
    gemini_base_url: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta/openai/",
        description="Gemini OpenAI-compatible base URL.",
    )

    # ------------------------------------------------------- transcription --
    # faster-whisper model size, e.g. tiny/base/small/medium/large-v3.
    whisper_model: str = Field(default="base", description="faster-whisper model.")
    # "cpu", "cuda", or "auto" to detect a GPU when present.
    whisper_device: str = Field(default="auto", description="Transcription device.")
    whisper_compute_type: str = Field(
        default="int8", description="faster-whisper compute type (e.g. int8, float16)."
    )

    # ------------------------------------------------------------- storage --
    storage_backend: StorageBackend = Field(
        default=StorageBackend.LOCAL,
        description="Active storage backend (local or s3).",
    )
    storage_root: Path = Field(
        default=BASE_DIR / "storage",
        description="Root directory for local storage.",
    )
    uploads_dir: Path = Field(
        default=BASE_DIR / "storage" / "uploads",
        description="Where uploaded/downloaded source videos are stored.",
    )
    temp_dir: Path = Field(
        default=BASE_DIR / "storage" / "temp",
        description="Scratch space for intermediate processing artefacts.",
    )
    clips_dir: Path = Field(
        default=BASE_DIR / "storage" / "clips",
        description="Where finished clips are written.",
    )
    # Number of days finished clips are retained before cleanup.
    retention_days: int = Field(default=7, description="Clip retention window (days).")

    # --------------------------------------------------------------- s3 ----
    s3_bucket: Optional[str] = Field(default=None, description="S3 bucket name.")
    s3_region: Optional[str] = Field(default=None, description="S3 region.")
    s3_access_key_id: Optional[str] = Field(default=None, description="S3 access key.")
    s3_secret_access_key: Optional[str] = Field(
        default=None, description="S3 secret key."
    )
    s3_endpoint_url: Optional[str] = Field(
        default=None, description="Optional custom S3-compatible endpoint."
    )

    # ------------------------------------------------------------- ffmpeg --
    ffmpeg_binary: str = Field(default="ffmpeg", description="Path to ffmpeg binary.")
    ffprobe_binary: str = Field(default="ffprobe", description="Path to ffprobe binary.")

    # ------------------------------------------------------------ assets ---
    emoji_assets_dir: Path = Field(
        default=BASE_DIR / "assets" / "emoji",
        description="Directory containing (and caching) Twemoji PNG assets.",
    )
    music_dir: Path = Field(
        default=BASE_DIR / "assets" / "music",
        description="Optional directory of user-supplied mood music beds "
                    "(e.g. music/upbeat.mp3). Falls back to a synthesised bed.",
    )

    # ------------------------------------------------------- effects (P4) --
    # Base CDN for on-demand Twemoji PNG downloads (cached into emoji_assets_dir).
    twemoji_cdn_base: str = Field(
        default="https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/72x72",
        description="Base URL for Twemoji 72x72 PNG assets.",
    )
    # Allow the emoji overlay to fetch missing PNGs from the CDN at render time.
    emoji_allow_download: bool = Field(
        default=True, description="Fetch missing Twemoji PNGs from the CDN."
    )
    # Default background-music level (0..1) mixed under the original audio.
    music_default_volume: float = Field(
        default=0.12, description="Default background-music volume (0..1)."
    )

    # ---------------------------------------------------------- publishers --
    history_db: Path = Field(default=BASE_DIR / "storage" / "history.db")
    publish_poll_seconds: float = Field(default=2.0)
    publish_default_interval_seconds: float = Field(default=30.0)
    public_base_url: Optional[str] = Field(default=None)
    # Whop (@whop/sdk Node bridge)
    whop_api_key: Optional[str] = Field(default=None)
    whop_company_id: Optional[str] = Field(default=None)
    whop_node_binary: str = Field(default="node")
    # YouTube OAuth
    youtube_client_id: Optional[str] = Field(default=None)
    youtube_client_secret: Optional[str] = Field(default=None)
    youtube_refresh_token: Optional[str] = Field(default=None)
    youtube_channel_id: Optional[str] = Field(default=None)
    # TikTok Content Posting API
    tiktok_access_token: Optional[str] = Field(default=None)
    tiktok_open_id: Optional[str] = Field(default=None)
    tiktok_direct_post_approved: bool = Field(default=False)
    # Instagram Graph API (Professional account)
    instagram_access_token: Optional[str] = Field(default=None)
    instagram_account_id: Optional[str] = Field(default=None)
    instagram_api_version: str = Field(default="v25.0")
    instagram_content_publish_approved: bool = Field(default=False)
    # X API v2 OAuth user context
    x_api_key: Optional[str] = Field(default=None)
    x_api_secret: Optional[str] = Field(default=None)
    x_access_token: Optional[str] = Field(default=None)
    x_account_id: Optional[str] = Field(default=None)
    x_direct_post_approved: bool = Field(default=False)

    def ensure_local_dirs(self) -> None:
        """Create local storage/asset directories if they do not yet exist.

        Safe to call on startup; a no-op when directories already exist.
        """
        for path in (
            self.storage_root,
            self.uploads_dir,
            self.temp_dir,
            self.clips_dir,
            self.emoji_assets_dir,
            self.music_dir,
        ):
            Path(path).mkdir(parents=True, exist_ok=True)

    @property
    def cors_origins_list(self) -> list[str]:
        """Return CORS origins as a list, splitting the comma-separated value."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance (loaded once per process)."""
    return Settings()


# Convenience singleton for straightforward imports.
settings = get_settings()
