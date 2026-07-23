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
    anthropic_api_key: Optional[str] = Field(default=None, description="Anthropic key.")
    anthropic_model: str = Field(
        default="claude-3-5-sonnet-latest", description="Anthropic model."
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
        description="Directory containing Twemoji PNG assets.",
    )

    # ---------------------------------------------------------- publishers --
    # Whop
    whop_api_key: Optional[str] = Field(default=None, description="Whop API key.")
    # YouTube OAuth
    youtube_client_id: Optional[str] = Field(default=None, description="YT client id.")
    youtube_client_secret: Optional[str] = Field(
        default=None, description="YT client secret."
    )
    youtube_refresh_token: Optional[str] = Field(
        default=None, description="YT OAuth refresh token."
    )
    # TikTok
    tiktok_access_token: Optional[str] = Field(
        default=None, description="TikTok access token."
    )
    # Instagram
    instagram_access_token: Optional[str] = Field(
        default=None, description="Instagram Graph API access token."
    )
    # X / Twitter
    x_api_key: Optional[str] = Field(default=None, description="X (Twitter) API key.")
    x_api_secret: Optional[str] = Field(default=None, description="X API secret.")

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
