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
    # T1: "small", not "base". Captions are the most visible artefact in the product and
    # "base" is a noticeable accuracy step down - a mis-transcribed word is burned into the
    # video. "small" is the cheapest model that does not make that trade. Larger models
    # ("medium", "large-v3") are better still and cost proportionally more, so the choice is
    # left to the operator rather than assumed.
    whisper_model: str = Field(default="small", description="faster-whisper model.")
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
    # Upload guard rails. Without a ceiling any client can fill the disk, and without an
    # extension allow-list arbitrary files land in a directory whose contents are later
    # handed to ffmpeg. 0 disables the size ceiling.
    max_upload_bytes: int = Field(
        default=2 * 1024 * 1024 * 1024,
        description="Largest accepted upload in bytes (default 2 GiB); 0 = unlimited.",
    )
    upload_chunk_bytes: int = Field(
        default=1024 * 1024,
        description="Chunk size used to stream uploads to disk.",
    )
    allowed_upload_extensions: str = Field(
        default=".mp4,.mov,.mkv,.webm,.avi,.m4v,.mpg,.mpeg,.wmv,.flv,.ts,.m2ts,.3gp,.mp3,.wav,.m4a,.aac,.flac,.ogg",
        description="Comma-separated list of accepted upload file extensions.",
    )
    # T8: ASR is the most expensive stage and the most repeated - re-running a source to try
    # a different caption preset or aspect ratio re-transcribes audio that has not changed.
    transcript_cache_dir: Path = Field(
        default=BASE_DIR / "storage" / "transcripts",
        description="Directory of cached transcripts (T8), keyed by source content hash and "
                    "the ASR parameters that produced them.",
    )
    transcript_cache_enabled: bool = Field(
        default=True,
        description="Reuse a cached transcript when the source content and ASR settings "
                    "match (T8). Turn off to force re-transcription.",
    )
    temp_dir: Path = Field(
        default=BASE_DIR / "storage" / "temp",
        description="Scratch space for intermediate processing artefacts.",
    )
    clips_dir: Path = Field(
        default=BASE_DIR / "storage" / "clips",
        description="Where finished clips are written.",
    )
    # Default number of days finished clips are retained before cleanup.
    # 0 means "keep forever". This is the *default*; the effective value is
    # user-tunable at runtime (see runtime_config.py / the Storage settings UI).
    retention_days: int = Field(default=30, description="Clip retention window (days); 0 = keep forever.")
    # Auto-delete a job's scratch/temp files when it finishes (toggleable).
    auto_delete_temp: bool = Field(default=True, description="Delete temp files after each job.")
    # Delete the local clip copy once it has been published (never the source).
    delete_local_after_publish: bool = Field(
        default=False, description="Delete the local clip after a successful publish."
    )
    # How often the background retention sweeper runs.
    retention_sweep_hours: float = Field(default=6.0, description="Retention sweep interval (hours).")
    # Low-disk warning thresholds surfaced in the UI.
    disk_warn_free_gb: float = Field(default=2.0, description="Warn when free space drops below this (GB).")
    disk_warn_percent: float = Field(default=90.0, description="Warn when used space exceeds this (%).")
    # Runtime-mutable settings + saved profiles are persisted here.
    runtime_config_path: Path = Field(default=BASE_DIR / "storage" / "runtime_config.json")
    profiles_path: Path = Field(default=BASE_DIR / "storage" / "profiles.json")

    # --------------------------------------------------------- updates -----
    # GitHub repo (owner/name) used by the "check for updates" feature.
    github_repo: str = Field(default="skullmanhell1/Clipping-Tool", description="owner/name for update checks.")
    update_check_enabled: bool = Field(default=True, description="Enable GitHub release update checks.")

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
    # Wall-clock ceilings for media subprocesses. Jobs run in a thread pool with a
    # single worker, so an ffmpeg that hangs (an unreachable network input, a
    # malformed file that stalls a demuxer, a filter waiting on a pad that never
    # fills) would otherwise block the entire queue indefinitely with no error and
    # no recovery path. Encoding is given a generous ceiling because a long source
    # legitimately takes minutes; probing is metadata-only and should be quick.
    # Set either to 0 to opt out of the ceiling entirely.
    ffmpeg_timeout_seconds: float = Field(
        default=3600.0, description="Max wall-clock seconds for one ffmpeg run; 0 = unbounded."
    )
    ffprobe_timeout_seconds: float = Field(
        default=60.0, description="Max wall-clock seconds for one ffprobe run; 0 = unbounded."
    )
    # O3: deliverables are encoded at a constant frame rate. A variable-frame-rate source -
    # every screen recording and most phone footage - has no single frame duration, so
    # burned captions drift against speech as the effective rate wanders.
    output_fps: int = Field(
        default=30,
        description="Constant frame rate for delivered clips (O3). Sources with variable "
                    "frame rate are resampled to this, which is what keeps burned captions "
                    "in sync.",
    )
    # O4: a VBV ceiling for delivered clips. -crf sets a quality target with no bitrate
    # limit, so a busy clip can balloon past a platform's file-size cap and be rejected.
    output_max_bitrate_kbps: int = Field(
        default=12000,
        description="Peak video bitrate for delivered clips in kbit/s (O4); -bufsize is "
                    "twice this. Generous for 1080x1920 at 30 fps, so it only engages on "
                    "genuinely complex footage.",
    )
    # AU8: neither was set anywhere, so output sample rate and channel count were whatever
    # the source happened to be - 44.1 kHz mono from a phone, 48 kHz 5.1 from a camera.
    output_sample_rate: int = Field(
        default=48000, description="Output audio sample rate in Hz (AU8)."
    )
    output_channels: int = Field(
        default=2, description="Output audio channel count (AU8); 2 = stereo."
    )
    # AU1: loudness targets. A clip quieter than the platform's target gets turned *up* on
    # playback, which amplifies its noise floor along with the speech; one louder is turned
    # down, losing the headroom it was mastered with.
    loudness_target_lufs: float = Field(
        default=-14.0,
        description="Integrated loudness target in LUFS for platforms without a specific "
                    "target (AU1). YouTube is about -14; TikTok and Instagram sit nearer "
                    "-11, and are set per platform in worker.effects.audio.",
    )
    loudness_true_peak_db: float = Field(
        default=-1.0,
        description="True-peak ceiling in dBTP for loudness normalisation (AU1). -1 leaves "
                    "headroom for the lossy encoder, which can overshoot the sample peak.",
    )
    # AU3: a true-peak limiter at the end of the audio chain, using
    # loudness_true_peak_db as its ceiling so there is one source of truth for it.
    true_peak_limit_enabled: bool = Field(
        default=True,
        description="Apply a true-peak limiter at the end of the audio chain (AU3), at "
                    "loudness_true_peak_db. Guards the paths where loudness normalisation "
                    "does not run, where a hot source plus a music bed can exceed full scale.",
    )
    # AU2: how hard the music bed is pushed down while someone is speaking.
    # V10: filler removal joins the kept segments sample-exactly, so every seam was a step
    # discontinuity in the waveform - the click you hear at each removed "um".
    filler_seam_fade_ms: int = Field(
        default=12,
        description="Audio fade length in milliseconds at each filler-removal seam (V10). "
                    "Long enough to remove the click, short enough not to be audible as a "
                    "fade; 0 disables it and restores the hard cut.",
    )
    music_duck_ratio: float = Field(
        default=8.0,
        description="Compression ratio for ducking music under speech (AU2). Higher ducks "
                    "harder; 1.0 disables ducking and restores the flat mix.",
    )

    # ------------------------------------------------------------ assets ---
    emoji_assets_dir: Path = Field(
        default=BASE_DIR / "assets" / "emoji",
        description="Directory containing (and caching) Twemoji PNG assets.",
    )
    font_assets_dir: Path = Field(
        default=BASE_DIR / "assets" / "fonts",
        description="Directory of bundled caption fonts (see assets/fonts.json). "
                    "Passed to libass as 'fontsdir' so appearance does not depend on "
                    "which fonts the host has installed.",
    )
    music_dir: Path = Field(
        default=BASE_DIR / "assets" / "music",
        description="Optional directory of user-supplied mood music beds "
                    "(e.g. music/upbeat.mp3). Falls back to a synthesised bed.",
    )

    # ------------------------------------------------------- effects (P4) --
    # Fallback source for an emoji PNG that is not vendored (A6, A7). The set the built-in
    # keyword map can produce is committed under assets/emoji, fetched by
    # scripts/fetch_emoji.py, so rendering never needs this.
    emoji_cdn_base: str = Field(
        default="https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/512",
        description="Fallback base URL for emoji PNGs (Noto Emoji 512px, OFL-1.1). "
                    "Only consulted for a glyph missing from emoji_assets_dir.",
    )
    # Allow the emoji overlay to fetch a missing PNG at render time.
    #
    # Defaults off now that the assets are vendored: a render is not the place to discover
    # the network is down, and the previous default meant a missing glyph turned into a
    # silent per-clip HTTP request. Turn it on when using an emoji outside the built-in map.
    emoji_allow_download: bool = Field(
        default=False,
        description="Fetch a missing emoji PNG from emoji_cdn_base at render time. Off by "
                    "default: the built-in set is vendored under assets/emoji.",
    )
    # Default background-music level (0..1) mixed under the original audio.
    music_default_volume: float = Field(
        default=0.12, description="Default background-music volume (0..1)."
    )
    # A15: whether the synthesised fallback bed may be used at all.
    #
    # It is not music - it is two sine tones with tremolo, identical for every clip of a
    # given mood. Left on so that asking for music still produces something, but a caller
    # who would rather have silence than a drone can turn it off, and every clip that uses
    # it is marked music_degraded:synthesised.
    music_allow_synthesis: bool = Field(
        default=True,
        description="Allow the synthesised two-tone fallback bed when no user track "
                    "exists in music_dir. Clips using it are marked "
                    "music_degraded:synthesised.",
    )

    # ------------------------------------------- b-roll (Tier 1) ----------
    # Local b-roll library used by the LocalProvider (no network required).
    broll_dir: Path = Field(
        default=BASE_DIR / "assets" / "broll",
        description="Directory of user-supplied b-roll clips/images (LocalProvider).",
    )
    # Cache directory for assets fetched from an external provider (BYOK).
    broll_cache_dir: Path = Field(
        default=BASE_DIR / "assets" / "broll_cache",
        description="Cache directory for downloaded external b-roll assets.",
    )
    # Default external b-roll provider name ("" = none configured).
    broll_provider: str = Field(
        default="", description="Default external b-roll provider name ('' = none)."
    )
    # Bring-your-own-key credentials for the external b-roll provider.
    broll_provider_api_key: Optional[str] = Field(
        default=None, description="API key for the external b-roll provider (BYOK)."
    )
    broll_provider_base_url: Optional[str] = Field(
        default=None, description="Base URL for the external b-roll provider."
    )
    # External b-roll downloading is OFF by default (opt-in only).
    broll_allow_download: bool = Field(
        default=False, description="Allow fetching b-roll assets from an external provider."
    )

    # ---------------------------------- visual selection (Tier 1) ---------
    # Cap on keyframes sampled per source for visual/prompt clip finding.
    keyframe_sample_limit: int = Field(
        default=12, description="Max keyframes sampled per source for visual selection."
    )

    # How long the per-area directory sizes reported by /api/storage may be reused.
    # Computing them walks clips/, uploads/ and temp/ in full, and the storage panel
    # polls that endpoint, so every poll used to traverse the whole storage tree. The
    # figures are a rough gauge, so a short cache is free. 0 disables caching.
    disk_usage_cache_seconds: float = Field(
        default=30.0, description="TTL for cached storage area sizes; 0 = always recompute."
    )

    # ------------- speaker diarisation & multi-speaker reframe (v0.8.0) ----
    # Cap on distinct speakers produced by diarisation (least-represented
    # speakers are merged beyond this cap).
    diarization_max_speakers: int = Field(
        default=2, description="Max distinct speakers produced by diarisation."
    )
    # Silence gap (seconds) that ends a speaker turn during segmentation.
    diarization_pause_gap: float = Field(
        default=0.9, description="Silence gap (s) that ends a speaker turn."
    )
    # Silence gap (seconds) after which the offline heuristic attributes the next turn
    # to a *different* speaker. Deliberately much larger than diarization_pause_gap:
    # ending a turn and changing speaker are different claims. Pauses just over 0.9s are
    # routine inside one person's speech (a breath, a sentence boundary), so treating
    # every one as a hand-off invented speakers who never spoke — and speaker-aware
    # reframe then cut back and forth between two "speakers" who were the same person.
    diarization_handoff_gap: float = Field(
        default=2.5,
        description="Silence gap (s) after which the offline heuristic changes speaker.",
    )
    # Face-sampling rate for speaker-aware reframe (matches v0.7.0 reframe).
    reframe_sample_fps: float = Field(
        default=5.0, description="Face-sampling rate (fps) for speaker-aware reframe."
    )
    # How much visual cues (brightness + motion) count when blended with transcript
    # scores during visual selection. 0 = transcript only, 1 = visuals only. This was
    # effectively hard-coded at 0.5: merge_scores took a `weight` argument that its only
    # call site never passed, so a 50/50 blend could not be tuned — and brightness and
    # motion are weak signals for talking-head footage, where the transcript is the
    # better guide.
    visual_selection_weight: float = Field(
        default=0.5,
        description="Weight of visual cues vs transcript score in selection (0..1).",
    )
    # Cap on frames sampled per clip for face detection.
    reframe_sample_cap: int = Field(
        default=120, description="Max frames sampled per clip for face detection."
    )
    # Default number of regions for split-screen reframe (2-up).
    split_screen_max_regions: int = Field(
        default=2, description="Max regions for split-screen reframe (default 2-up)."
    )

    # ---------------------------------------------------------- publishers --
    history_db: Path = Field(default=BASE_DIR / "storage" / "history.db")
    # Durable job/clip records. Without this the job store is process memory only, so a
    # restart loses every job and the history UI lists clips whose download 404s.
    jobs_db: Path = Field(default=BASE_DIR / "storage" / "jobs.db")
    # Upper bound on retained job records, so a long-lived instance does not grow the
    # jobs table without limit. 0 disables pruning.
    max_persisted_jobs: int = Field(default=500)
    publish_poll_seconds: float = Field(default=2.0)
    # A *floor* under every publisher's own rate limit, for operators who want to be
    # more conservative than the platforms require. 0 means "trust each publisher".
    #
    # This was `publish_default_interval_seconds`, defaulting to 30.0, and the scheduler
    # applied `max(publisher.min_interval_seconds, <this>)`. Since every publisher
    # declares 2-18s (whop 2, x 5, tiktok 10, youtube 15, instagram 18), the 30s floor
    # overrode all of them — so `min_interval_seconds` was dead code on every publisher,
    # and publishing ran roughly twice as slowly as intended with no way to tell why.
    publish_min_interval_floor_seconds: float = Field(default=0.0)
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
            self.transcript_cache_dir,
            self.emoji_assets_dir,
            self.music_dir,
            self.broll_dir,
            self.broll_cache_dir,
        ):
            Path(path).mkdir(parents=True, exist_ok=True)

    @property
    def cors_origins_list(self) -> list[str]:
        """Return CORS origins as a list, splitting the comma-separated value."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def allowed_upload_extensions_set(self) -> set[str]:
        """Accepted upload extensions, lower-cased and dot-prefixed."""
        out: set[str] = set()
        for raw in self.allowed_upload_extensions.split(","):
            ext = raw.strip().lower()
            if not ext:
                continue
            out.add(ext if ext.startswith(".") else f".{ext}")
        return out

    @property
    def cors_allow_wildcard(self) -> bool:
        """Whether the configured origins are the ``*`` wildcard."""
        return "*" in self.cors_origins_list

    @property
    def cors_allow_credentials(self) -> bool:
        """Whether to send ``Access-Control-Allow-Credentials``.

        Never true alongside a wildcard origin. The CORS specification forbids
        combining ``Access-Control-Allow-Origin: *`` with credentials, and browsers
        reject such responses outright — so pairing the two does not loosen security,
        it simply breaks every credentialed cross-origin request while *looking* like
        it permits them. Worse, the wildcard is the default here, so the broken
        combination was the out-of-the-box behaviour.

        Setting an explicit ``CORS_ORIGINS`` list re-enables credentials.
        """
        return not self.cors_allow_wildcard


@lru_cache
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance (loaded once per process)."""
    return Settings()


# Convenience singleton for straightforward imports.
settings = get_settings()
