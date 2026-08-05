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
    # Deliberate: a container must bind all interfaces to be reachable.
    api_host: str = Field(
        default="0.0.0.0",  # noqa: S104
        description="API bind host.",
    )
    api_port: int = Field(default=8000, description="API bind port.")
    # Comma-separated list of allowed CORS origins.
    cors_origins: str = Field(default="*", description="Allowed CORS origins.")

    # ------------------------------------------------------------ security --
    # Every route was unauthenticated, and `render.yaml` deploys this publicly with
    # `autoDeploy: true`. A single shared secret is the whole scheme on purpose: this is a
    # single-tenant self-hosted tool, and per-user accounts are plan item U12 (P2/L), a
    # different and much larger change.
    #
    # Unset means "allow everything", because an existing deployment must not lose access on
    # upgrade - the same reasoning the CORS wildcard default follows. Startup logs a loud
    # warning in that case, and refuses to boot outright under ENVIRONMENT=production.
    api_auth_token: str | None = Field(
        default=None,
        description="Shared secret required on /api/* and /clips/*. Unset = no auth (a "
        "startup warning is logged; refused outright in production).",
    )
    # Rate limiting is in-process on purpose. `redis` and `rq` are declared dependencies that
    # nothing imports, and adding a live Redis requirement to make the app safe would turn an
    # optional dependency into a mandatory one.
    rate_limit_enabled: bool = Field(
        default=True,
        description="Throttle the expensive write routes (jobs, upload, preview, rerender).",
    )
    rate_limit_requests: int = Field(
        default=30,
        description="Requests allowed per client per window on rate-limited routes.",
    )
    rate_limit_window_seconds: float = Field(
        default=60.0,
        description="Length of the rate-limit window in seconds.",
    )
    # SSRF guard. yt-dlp will fetch whatever it is given, so an unauthenticated URL endpoint is
    # a request forwarder into the deployment's own network - including cloud metadata at
    # 169.254.169.254. Self-hosters who genuinely want to ingest from a LAN media server can opt
    # back in; it is off by default because the safe choice must not require a decision.
    url_ingest_allow_private: bool = Field(
        default=False,
        description="Allow URL ingest from loopback/link-local/private address ranges. "
        "Leave false unless you are deliberately ingesting from a LAN host.",
    )
    # Whether to believe X-Forwarded-For when identifying a client for rate limiting. False by
    # default because a client can forge the header when the app is directly exposed, and
    # trusting it then lets one caller present as unlimited distinct clients. Behind a proxy
    # that sets it (Render, nginx) the opposite is true: without this every request looks like
    # the proxy, so one bucket is shared by everyone. Set it to match the deployment.
    trust_forwarded_for: bool = Field(
        default=False,
        description="Trust X-Forwarded-For for client identity. Enable only when running "
        "behind a proxy that sets it.",
    )

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
    openai_api_key: str | None = Field(default=None, description="OpenAI key.")
    openai_model: str = Field(default="gpt-4o-mini", description="OpenAI model.")
    # Optional custom base URL for any OpenAI-compatible endpoint (e.g. a local
    # Ollama / LM Studio server, or a proxy). Leave unset for real OpenAI.
    openai_base_url: str | None = Field(
        default=None, description="Custom OpenAI-compatible base URL."
    )
    anthropic_api_key: str | None = Field(default=None, description="Anthropic key.")
    anthropic_model: str = Field(default="claude-3-5-sonnet-latest", description="Anthropic model.")
    # Google Gemini (used via its OpenAI-compatible endpoint).
    gemini_api_key: str | None = Field(default=None, description="Gemini API key.")
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
    # T4: a prompt prepended to the decode, which is how Whisper is told about words it has
    # no reason to expect - people's names, product names, jargon, brands. Without it a
    # recurring proper noun is mis-transcribed the same way every time it is said, and that
    # mistake is then burned into the captions of every clip.
    #
    # Empty by default because the useful content is per-video, not per-install. Use
    # ProcessingOptions.vocabulary for that; this is the standing list for a channel that
    # always says the same handful of unusual words.
    whisper_initial_prompt: str = Field(
        default="",
        description="Text prepended to the ASR decode to bias it towards expected "
        "vocabulary (T4). Per-video terms belong in the job's `vocabulary`.",
    )
    # ----------------------------------------------------- VAD (T5) --------
    # Voice-activity detection was switched on with every parameter left at the library's
    # defaults, so none of it could be adjusted for difficult audio. The defaults below are
    # faster-whisper's own, so behaviour is unchanged until something is changed on purpose.
    whisper_vad_filter: bool = Field(
        default=True,
        description="Run Silero VAD before decoding (T5). Off passes the whole track to the "
        "model, which is slower and hallucinates more over music and silence.",
    )
    whisper_vad_threshold: float = Field(
        default=0.5,
        description="Speech probability above which VAD calls a frame speech (T5). Lower "
        "keeps quiet or distant speech that the default discards; higher "
        "rejects more noise.",
    )
    whisper_vad_min_silence_ms: int = Field(
        default=2000,
        description="Silence this long (ms) splits speech for VAD (T5).",
    )
    whisper_vad_min_speech_ms: int = Field(
        default=250,
        description="Speech shorter than this (ms) is discarded by VAD (T5). Raising it "
        "drops interjections - 'yeah', a laugh - which may be the punchline.",
    )
    whisper_vad_speech_pad_ms: int = Field(
        default=400,
        description="Padding (ms) kept either side of detected speech (T5). Too little "
        "clips the first and last phoneme of each utterance.",
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
    # T3: Whisper invents text over music, applause and silence, and gets stuck in decode
    # loops. Every threshold here is set so that two independent signals must agree before a
    # segment is dropped, because a false positive deletes real speech and nothing downstream
    # can notice - while a missed hallucination is visible in the clip.
    transcript_filter_enabled: bool = Field(
        default=True,
        description="Drop transcript segments that look hallucinated or looped (T3).",
    )
    transcript_no_speech_threshold: float = Field(
        default=0.6,
        description="Whisper no_speech_prob at or above which a segment is suspect (T3). "
        "Never acted on alone: quiet but real speech scores high here.",
    )
    transcript_logprob_threshold: float = Field(
        default=-1.0,
        description="Mean token log-probability at or below which a segment is suspect (T3). "
        "Must coincide with a high no_speech_prob before anything is dropped.",
    )
    transcript_max_token_run: int = Field(
        default=4,
        description="Identical consecutive tokens that mark a decode loop (T3). No speaker "
        "says the same word four times with nothing in between.",
    )
    transcript_max_segment_repeats: int = Field(
        default=2,
        description="Consecutive segments repeating the same phrase before the repeats are "
        "dropped (T3) - a loop spanning segment boundaries.",
    )
    transcript_min_word_probability: float = Field(
        default=0.35,
        description="Mean word probability below which a repetitive segment is treated as a "
        "loop over non-speech (T3).",
    )
    transcript_filter_keep_floor: float = Field(
        default=0.5,
        description="Minimum share of segments that must survive filtering (T3). Below it "
        "nothing is dropped: if most of a transcript looks invented the "
        "thresholds are wrong for that audio, and emptying it is worse than "
        "keeping a poor transcript.",
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
    retention_days: int = Field(
        default=30, description="Clip retention window (days); 0 = keep forever."
    )
    # Auto-delete a job's scratch/temp files when it finishes (toggleable).
    auto_delete_temp: bool = Field(default=True, description="Delete temp files after each job.")
    # Delete the local clip copy once it has been published (never the source).
    delete_local_after_publish: bool = Field(
        default=False, description="Delete the local clip after a successful publish."
    )
    # How often the background retention sweeper runs.
    retention_sweep_hours: float = Field(
        default=6.0, description="Retention sweep interval (hours)."
    )
    # Low-disk warning thresholds surfaced in the UI.
    disk_warn_free_gb: float = Field(
        default=2.0, description="Warn when free space drops below this (GB)."
    )
    disk_warn_percent: float = Field(
        default=90.0, description="Warn when used space exceeds this (%)."
    )
    # Runtime-mutable settings + saved profiles are persisted here.
    runtime_config_path: Path = Field(default=BASE_DIR / "storage" / "runtime_config.json")
    profiles_path: Path = Field(default=BASE_DIR / "storage" / "profiles.json")

    # --------------------------------------------------------- updates -----
    # GitHub repo (owner/name) used by the "check for updates" feature.
    github_repo: str = Field(
        default="skullmanhell1/Clipping-Tool", description="owner/name for update checks."
    )
    update_check_enabled: bool = Field(
        default=True, description="Enable GitHub release update checks."
    )

    # --------------------------------------------------------------- s3 ----
    s3_bucket: str | None = Field(default=None, description="S3 bucket name.")
    s3_region: str | None = Field(default=None, description="S3 region.")
    s3_access_key_id: str | None = Field(default=None, description="S3 access key.")
    s3_secret_access_key: str | None = Field(default=None, description="S3 secret key.")
    s3_endpoint_url: str | None = Field(
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
    # x264 quality/speed. Previously hard-coded at eight call sites across five modules.
    # Lower CRF is higher quality and a larger file; 18-23 is the sane range.
    x264_crf: int = Field(default=20, description="x264 CRF (quality); lower = better.")
    x264_preset: str = Field(default="veryfast", description="x264 speed/efficiency preset.")

    # S9: snap clip starts to shot boundaries so a clip does not open mid-shot. Detection is
    # ffmpeg's luma-based scene score over a narrow window near each boundary, so it finds most
    # hard cuts and misses equiluminant ones - which is why every snap is capped and optional.
    scene_snap_enabled: bool = Field(
        default=True,
        description="Snap clip starts to a nearby shot boundary (S9).",
    )
    scene_snap_threshold: float = Field(
        default=0.3,
        description="ffmpeg scene score above which a frame is treated as a hard cut (S9).",
    )
    scene_snap_window_s: float = Field(
        default=2.0,
        description="Seconds either side of a clip start to scan for a cut (S9). Narrow on "
        "purpose: detection decodes video, and scanning a whole source to move a "
        "boundary by under a second is disproportionate.",
    )
    scene_snap_max_shift_s: float = Field(
        default=1.0,
        description="Furthest a clip start may be moved to reach a cut (S9). Beyond this the "
        "boundary the selector chose is kept.",
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
    broll_provider_api_key: str | None = Field(
        default=None, description="API key for the external b-roll provider (BYOK)."
    )
    broll_provider_base_url: str | None = Field(
        default=None, description="Base URL for the external b-roll provider."
    )
    # External b-roll downloading is OFF by default (opt-in only).
    broll_allow_download: bool = Field(
        default=False, description="Allow fetching b-roll assets from an external provider."
    )

    # ---------------------------------- visual selection (Tier 1) ---------
    # Cap on keyframes sampled per source for visual/prompt clip finding.
    #
    # S14: raised from 12. Twelve frames across an hour-long source is one every five minutes,
    # which cannot distinguish one clip-length window from its neighbour - every candidate in a
    # five-minute stretch got the same visual score, so the signal was constant where it needed
    # to discriminate. 48 gives roughly one frame per minute on a long source and several per
    # candidate on a short one.
    keyframe_sample_limit: int = Field(
        default=48, description="Max keyframes sampled per source for visual selection."
    )
    # S14: sampling width. Was hard-coded at 160 px, at which the motion proxy is measuring
    # little more than JPEG noise: a 160x90 thumbnail averages away exactly the frame-to-frame
    # difference it is supposed to detect. 480 is still tiny to decode and gives the brightness
    # and motion proxies something to work with.
    keyframe_sample_width: int = Field(
        default=480, description="Pixel width of sampled keyframes for visual selection (S14)."
    )

    # ---------------------------------- selection scoring (S11, S15, S17) --
    # Per-signal weights for the deterministic fallback's ranking. Exposed as settings rather
    # than literals so the blend can be tuned against the S1 benchmark without a code change
    # (S17). They are relative, not required to sum to 1 - the scorer normalises by their total.
    #
    # The defaults are a starting point, not a measured optimum: the honest way to set these is
    # to run scripts/eval_selection.py against labelled footage and move them. What *is*
    # defensible without labels is that all four beat "keep the longest segments", which is what
    # they replace (S11).
    selection_weight_hook: float = Field(
        default=0.40,
        description="Weight of the S6 hook score in fallback ranking. Highest of the four "
        "because retention is decided in the opening seconds.",
    )
    selection_weight_pace: float = Field(
        default=0.20,
        description="Weight of speech-rate deviation from the speaker's own norm (S4).",
    )
    selection_weight_energy: float = Field(
        default=0.20,
        description="Weight of audio energy relative to the source median (S2).",
    )
    selection_weight_length: float = Field(
        default=0.20,
        description="Weight of how closely the clip matches the requested length. Replaces the "
        "old rule that simply kept the longest segments.",
    )
    # S7/S8/S12: what the passage says, not just how it was delivered.
    #
    # Lower than the delivery weights on purpose. These are lexical rules over ASR output, so they
    # are the least certain signals in the set - a mis-transcribed opener can make a complete
    # thought look like a fragment - and until the S1 dataset can measure them, a weight large
    # enough to overturn the acoustic signals would be a guess with consequences.
    selection_weight_structure: float = Field(
        default=0.15,
        description="Weight of question/answer and list structure in fallback ranking (S7).",
    )
    selection_weight_standalone: float = Field(
        default=0.20,
        description="Weight of standalone completeness (S12). The highest of the three text "
        "signals: nothing downstream can supply context a clip is missing, whereas "
        "boundary snapping can still fix an unfinished ending.",
    )
    selection_weight_intensity: float = Field(
        default=0.10,
        description="Weight of lexical emotional intensity (S8). The lowest: it overlaps with "
        "the S2 energy signal, and double-counting emphasis would let one loud, "
        "strongly-worded moment dominate a whole source.",
    )
    # S15: how much two candidates may overlap before the lower-scoring one is dropped.
    # Measured as a fraction of the *shorter* candidate, so a short clip wholly inside a long
    # one reads as 1.0 rather than as the small IoU that would let it through.
    selection_max_overlap: float = Field(
        default=0.5,
        description="Max overlap (fraction of the shorter clip) before a candidate is treated "
        "as a duplicate (S15). 1.0 disables overlap de-duplication.",
    )
    selection_max_text_similarity: float = Field(
        default=0.7,
        description="Max content-word Jaccard similarity before two candidates are treated as "
        "the same moment (S15). 1.0 disables text de-duplication.",
    )
    # S6: relative weights inside the hook score itself.
    hook_weight_promptness: float = Field(
        default=0.40, description="Weight of how promptly speech starts, in the hook score (S6)."
    )
    hook_weight_pace: float = Field(
        default=0.20, description="Weight of hook pace vs the clip's own pace (S6)."
    )
    hook_weight_energy: float = Field(
        default=0.25, description="Weight of hook energy vs the clip's own energy (S6)."
    )
    hook_weight_text: float = Field(
        default=0.15,
        description="Weight of textual opener signals in the hook score (S6). Lowest on "
        "purpose: a keyword list is the component most easily fired by coincidence.",
    )
    # S2: energy envelope resolution. One reading per this many seconds, measured in a single
    # ffmpeg astats pass over the source.
    energy_envelope_window_s: float = Field(
        default=1.0,
        description="Seconds per audio-energy reading (S2). Smaller resolves individual words "
        "and adds noise; larger blurs the laughs and shouts worth detecting.",
    )
    # S10: show the measured per-segment features to the LLM alongside the transcript text.
    selection_features_in_prompt: bool = Field(
        default=True,
        description="Annotate transcript lines with measured pace/energy in the selection "
        "prompt (S10), so the model can see that a moment was loud or animated.",
    )

    # How long the per-area directory sizes reported by /api/storage may be reused.
    # Computing them walks clips/, uploads/ and temp/ in full, and the storage panel
    # polls that endpoint, so every poll used to traverse the whole storage tree. The
    # figures are a rough gauge, so a short cache is free. 0 disables caching.
    disk_usage_cache_seconds: float = Field(
        default=30.0, description="TTL for cached storage area sizes; 0 = always recompute."
    )

    # ---------------------------------------- caption details (C12, C22) ---
    # C12: platform UI safe areas. The vertical caption margins were hard-coded at 220/200 and
    # are not TikTok-aware, so a caption could sit under the username, the platform's own
    # caption text or the action rail - unreadable, and invisible to the creator because the
    # chrome is not in the rendered file. Empty means the generic profile, which reproduces the
    # previous margins exactly.
    caption_safe_area: str = Field(
        default="",
        description="Platform safe-area profile for caption margins (C12): tiktok | instagram "
        "| youtube | none. Empty uses the generic profile, which is identical to "
        "the previous hard-coded margins.",
    )
    caption_offset_px: int = Field(
        default=0,
        description="Extra pixels between the caption and its edge (C13). Positive only; a "
        "negative value would push text into the chrome the safe area avoids.",
    )
    # C22: burned captions are permanent, so masking is a publishing decision rather than a
    # default - a creator whose voice is profane should not be censored by their own tool.
    caption_mask_profanity: bool = Field(
        default=False,
        description="Mask profanity in burned captions (C22), keeping the first letter and the "
        "word's length so the sentence stays readable.",
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
    # V4: restart the reframe smoother at every shot change. The EMA otherwise carries the
    # previous shot's framing across a cut and converges on the new one over the following
    # second, which reads on screen as the crop searching for the subject after every cut.
    reframe_reset_on_cut: bool = Field(
        default=True,
        description="Reset reframe tracking at shot changes (V4). Costs one video-only decode "
        "per reframed clip; off restores smoothing straight through cuts.",
    )

    # ------------------------------------------- output geometry (O5, O9) --
    # Resolution was fixed at the 1080-class values in ffmpeg_utils.ASPECT_PRESETS with no way
    # to ask for anything else, so a 4K source was always downscaled and a low-powered host had
    # no way to trade quality for encode time. Named by the short side; the long side follows
    # from the aspect.
    output_short_side: int = Field(
        default=1080,
        description="Output short side in pixels (O9): 720, 1080, 1440 or 2160. 1080 is the "
        "short-form consensus. An unrecognised value falls back to 1080.",
    )

    # ----------------------------------------------- look details (V11, V13) --
    # V11: what fills the frame around fitted video. Was one hard-coded look (boxblur 40 plus a
    # slight darkening), which suits talking-head footage and actively hurts other things - a
    # blurred screen recording is an unreadable smear.
    background_style: str = Field(
        default="blur",
        description="Letterbox background: blur | mirror | black | color | gradient (V11). "
        "'black' is the honest choice for screen recordings.",
    )
    background_color: str = Field(
        default="0x0F172A",
        description="Fill colour used when background_style is 'color' (V11).",
    )
    # V13: the progress bar was a 12px bar in one cyan, always at the bottom - where on a 9:16
    # clip it sits directly under the captions and competes with them.
    progress_bar_position: str = Field(
        default="bottom", description="Progress bar position: bottom | top (V13)."
    )
    progress_bar_style: str = Field(
        default="bar",
        description="Progress bar style: bar | track (V13). 'track' adds a dim full-width rail "
        "so how much is left is visible, not only how much has passed.",
    )
    progress_bar_color: str = Field(
        default="0x22D3EE", description="Progress bar fill colour (V13)."
    )
    progress_bar_thickness: int = Field(
        default=12, description="Progress bar thickness in pixels (V13)."
    )
    # V9: which opening treatment `transitions` applies.
    transition_style: str = Field(
        default="punch_in",
        description="Opening transition: punch_in | zoom_cut | whip_pan | dissolve (V9).",
    )
    # O8: optional hardware H.264 encoding.
    video_encoder: str = Field(
        default="libx264",
        description="H.264 encoder: libx264 | auto | h264_nvenc | h264_qsv | "
        "h264_videotoolbox | h264_vaapi (O8). 'auto' probes for a working hardware "
        "encoder by actually encoding a frame - a listed encoder is not a usable "
        "one, and this ffmpeg lists h264_v4l2m2m while failing on the first frame. "
        "Anything unavailable falls back to libx264; a *named* request that falls "
        "back records an encoder_unavailable marker, because silently ignoring it is "
        "how someone spends a week believing their GPU is in use. Default is "
        "libx264, not auto: hardware encoders are not comparable with x264 at the "
        "same nominal quality, so 'auto' would change existing output the first time "
        "it landed on a machine with a GPU.",
    )
    # AU9: sound-effect stings on transitions and emoji.
    sfx_dir: Path = Field(
        default=BASE_DIR / "assets" / "sfx",
        description="Directory of your own sting files as <name>.wav (pop, click, whoosh, swipe) "
        "(AU9). A user file always wins over the synthesised version.",
    )
    sfx_mode: str = Field(
        default="off",
        description="Where sound-effect stings are placed: off | emoji | transitions | both "
        "(AU9). Off by default - an audible change to every clip is not something to "
        "acquire by upgrading. 'pop' and 'click' are synthesised honestly, because a "
        "pop IS a band-passed noise burst. 'whoosh' and 'swipe' are NOT synthesised: "
        "a whoosh needs a filter that moves across the sound and ffmpeg cannot express "
        "a time-varying filter frequency in one pass, so a static band-passed noise "
        "swell would be a hiss shipped under a name promising a sweep. Those need a "
        "file in SFX_DIR; without one the sting is skipped and the clip records "
        "sfx_missing:<name>.",
    )
    sfx_volume: float = Field(
        default=0.35,
        ge=0.0,
        le=1.0,
        description="Sting level relative to full scale (AU9). Mixed with amix normalize=0, so a "
        "sting never lowers the speech - with normalisation on, adding one accent "
        "would make the whole clip 1/n quieter.",
    )
    # V15: keep captions off the speaker's mouth.
    caption_avoid_faces: bool = Field(
        default=False,
        description="Move the caption to another of the nine C13 positions when it would cover a "
        "detected face's mouth (V15). Off by default for two reasons: it costs a "
        "face-detection pass over the clip, which a render that never had a collision "
        "would be paying for nothing, and it changes placement on the clips it does "
        "act on. It only ever acts on an actual overlap, keeps the horizontal "
        "alignment the preset chose, and when no position clears the face - a close-up "
        "filling the frame - it changes nothing and records "
        "caption_face_overlap_unavoidable rather than moving the text from the mouth "
        "to the eyes.",
    )
    # A22: motion on b-roll stills, and a dip in the bed under b-roll.
    broll_ken_burns: bool = Field(
        default=False,
        description="Slow zoom-and-drift on b-roll *stills* (A22). A still that sits motionless "
        "over moving footage is the clearest sign a clip was assembled rather than "
        "edited. Off by default because it changes the shipped look: with it on, "
        "stills are cover-cropped into a fixed 16:9 box (zoompan needs an explicit "
        "output size) instead of keeping their own aspect. Video assets already move "
        "and are never affected.",
    )
    broll_ken_burns_zoom: float = Field(
        default=0.12,
        ge=0.0,
        le=1.0,
        description="How far a b-roll still zooms over its window, as a fraction (A22). 0.12 is "
        "12% over the whole window - deliberately small: motion that is noticeable "
        "on a 2-second overlay is distracting rather than cinematic. Zero disables "
        "the motion even with BROLL_KEN_BURNS on.",
    )
    broll_duck: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="How far the music bed dips while a b-roll overlay is on screen, as a "
        "fraction (A22). 0 (default) leaves the audio graph untouched; 0.35 is a "
        "clearly audible accent. Applied to the *bed* only, never the mix - the "
        "b-roll is illustrating what is being said, so ducking the speech would "
        "invert the point. Additional to the AU2 speech duck, not instead of it.",
    )
    # A13: which artwork set the emoji overlay draws from.
    emoji_style: str = Field(
        default="noto",
        description="Emoji artwork set: noto | twemoji | openmoji (A13). Only Noto is vendored; "
        "the others are fetched on demand and so need EMOJI_ALLOW_DOWNLOAD or a "
        "prior 'scripts/fetch_emoji.py --style <name>'. A glyph missing from the "
        "selected style falls back to the vendored Noto file rather than dropping "
        "the overlay. An unknown value resolves to noto rather than failing the job.",
    )
    # C19: where an emoji overlay sits relative to the captions.
    emoji_placement: str = Field(
        default="spread",
        description="Emoji placement: spread (three slots across the frame, the shipped "
        "behaviour) or caption (just clear of the caption block, C19). 'caption' "
        "only makes sense because C19 puts the emoji on the word the caption "
        "highlights - a glyph beside a caption illustrating a different word would "
        "read as a mistake.",
    )
    # C20: pick the caption's outline/box colour from the video behind it.
    caption_auto_contrast: bool = Field(
        default=False,
        description="Sample the region a caption will occupy and choose a dark or light outline "
        "for legibility (C20). Off by default: it costs three seeks per clip and "
        "changes rendered output. Never alters the fill colour, which is a brand "
        "decision.",
    )
    # V17: score a few candidate frames for the thumbnail instead of taking a fixed position,
    # which on a clip opening on a cut or a blink chose exactly the wrong still.
    smart_thumbnail: bool = Field(
        default=True,
        description="Score candidate frames when choosing the thumbnail (V17). Costs a few "
        "small decodes per clip; off restores the fixed midpoint frame.",
    )
    # Default number of regions for split-screen reframe (2-up).
    split_screen_max_regions: int = Field(
        default=2, description="Max regions for split-screen reframe (default 2-up)."
    )
    # V18: a user-supplied 3D LUT, applied after the colour preset. Empty disables it.
    color_lut: str = Field(
        default="",
        description="Path to a .cube/.3dl 3D LUT applied after the colour preset (V18). "
        "Empty means no LUT. A missing or unreadable file is ignored rather "
        "than failing the render.",
    )
    # V19: ease the Ken Burns ramp instead of moving at a constant rate.
    zoom_ease: bool = Field(
        default=False,
        description="Ease the Ken Burns push in and out instead of ramping linearly (V19). "
        "Same start and end zoom; only the curve between them changes. Off by "
        "default because it changes the rendered output, and every visual setting "
        "here defaults to the previously shipped behaviour so the v0.8.0 parity "
        "gate stays meaningful.",
    )
    # V19: bump the zoom on detected audio accents.
    beat_sync_zoom: bool = Field(
        default=False,
        description="Add a short scale bump at detected audio onsets (V19). Off by default: "
        "it suits music-led footage and is a distraction on talking-head clips.",
    )
    beat_sync_rise_db: float = Field(
        default=6.0,
        description="Level rise between envelope readings that counts as an accent (V19).",
    )
    # V16: crop existing letterbox bars before reframing.
    auto_deletterbox: bool = Field(
        default=True,
        description="Detect and crop existing letterbox/pillarbox bars before reframing (V16). "
        "Without this, reframing already-boxed footage centres the crop on the "
        "bars and bakes them into the output.",
    )
    # O7: target one platform's output profile rather than one file for every destination.
    output_platform: str = Field(
        default="",
        description="Target platform output profile: tiktok | instagram | youtube | "
        "youtube_shorts | x | whop (O7). Empty means use the explicit output "
        "settings. Controls resolution, bitrate ceiling and the clip-length cap; "
        "the aspect is advisory so it cannot override a user's choice.",
    )
    # O12: burned-in captions, a selectable soft track, or both.
    caption_mode: str = Field(
        default="burned",
        description="How captions are delivered: burned | soft | both (O12). 'soft' adds a "
        "selectable mov_text track instead of burning pixels; note mov_text is "
        "plain text, so preset animation and highlighting are lost in that track.",
    )
    # T10: an English subtitle track alongside the original-language captions.
    subtitle_translation: bool = Field(
        default=False,
        description="Add a translated (English) subtitle track and sidecar beside the "
        "original-language captions (T10). A bool rather than a target language "
        "because Whisper's translate task only ever produces English, so a "
        "language field would be a control that silently ignores its value. Costs "
        "a second ASR pass over the source (cached separately by T8), so it is off "
        "by default and skipped entirely when the source is already English.",
    )
    # AU4: speech de-noise.
    speech_denoise: str = Field(
        default="off",
        description="Speech de-noise strength: off | light | standard | strong (AU4). Uses "
        "afftdn, or arnndn when SPEECH_DENOISE_MODEL points at a real model file.",
    )
    speech_denoise_model: str = Field(
        default="",
        description="Path to an arnndn .rnnn model (AU4). ffmpeg ships no models, so this is "
        "empty by default and afftdn is used instead. A configured-but-missing "
        "file degrades to afftdn rather than failing the render.",
    )
    # AU5: sibilance reduction.
    deesser: str = Field(
        default="off",
        description="De-esser strength: off | light | standard | strong (AU5). De-reverb is "
        "not included: ffmpeg has no de-reverb filter, and approximating one with "
        "a high-pass would be mislabelling it.",
    )
    # V14: a closing call-to-action over the tail of the clip. Empty disables it.
    end_card_text: str = Field(
        default="",
        description="Call-to-action shown over the last seconds of every clip (V14). Empty "
        "disables it, which is the previous behaviour.",
    )
    end_card_seconds: float = Field(
        default=2.0,
        description="How long the end card is held (V14). Capped at half the clip so a short "
        "clip is not mostly call-to-action.",
    )
    # V8: how often the follow-active crop position is updated.
    reframe_command_fps: float = Field(
        default=24.0,
        description="Crop-position updates per second for follow-active reframe (V8). Was 12, "
        "which is visible as stepping on fast movement. Costs only sendcmd script "
        "size, not decode time.",
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
    # PB5: automatic retry of *transient* publish failures, with exponential backoff. A publish
    # attempt previously had exactly one chance, so a network blip was indistinguishable from a
    # rejected video and both waited for a human to press Retry.
    publish_max_retries: int = Field(
        default=3,
        description="Automatic retries per publish attempt for transient failures (PB5). 0 "
        "disables automatic retry, restoring the previous single-shot behaviour.",
    )
    publish_retry_base_seconds: float = Field(
        default=30.0, description="First retry delay; doubles per retry (PB5)."
    )
    publish_retry_max_seconds: float = Field(
        default=3600.0, description="Ceiling on the retry delay (PB5)."
    )
    # I3: cache the per-source measurements that need a whole-file decode.
    intermediate_cache_enabled: bool = Field(
        default=True,
        description="Cache silence maps, energy envelopes and sampled keyframes by source "
        "content hash (I3). T8 already caches transcripts; these are the other "
        "whole-file decodes that repeated on every run of the same video.",
    )
    intermediate_cache_dir: Path | None = Field(
        default=None,
        description="Where I3 intermediates live. Defaults to <temp_dir>/intermediates.",
    )
    intermediate_cache_max_entries: int = Field(
        default=200,
        description="Cache entries retained before the oldest are pruned (I3). 0 disables "
        "pruning, which on a long-lived instance is a slow disk leak.",
    )
    # PB4: how early an expiring access token is renewed.
    publish_token_refresh_margin_seconds: float = Field(
        default=300.0,
        description="Refresh an OAuth access token this long before it expires (PB4). An upload "
        "takes tens of seconds, so a token expiring mid-request costs the whole file.",
    )
    # PB6: regenerate copy per destination rather than fitting the existing text.
    publish_tailor_with_llm: bool = Field(
        default=False,
        description="Regenerate the description for each destination platform on publish (PB6). "
        "Off by default: it costs one model call per platform per clip. When off, "
        "the existing copy is fitted to the platform's limits at sentence "
        "boundaries instead of being truncated mid-word.",
    )
    public_base_url: str | None = Field(default=None)
    # Whop (@whop/sdk Node bridge)
    whop_api_key: str | None = Field(default=None)
    whop_company_id: str | None = Field(default=None)
    whop_node_binary: str = Field(default="node")
    # YouTube OAuth
    youtube_client_id: str | None = Field(default=None)
    youtube_client_secret: str | None = Field(default=None)
    youtube_refresh_token: str | None = Field(default=None)
    youtube_channel_id: str | None = Field(default=None)
    # TikTok Content Posting API
    tiktok_access_token: str | None = Field(default=None)
    tiktok_open_id: str | None = Field(default=None)
    tiktok_direct_post_approved: bool = Field(default=False)
    # Instagram Graph API (Professional account)
    instagram_access_token: str | None = Field(default=None)
    instagram_account_id: str | None = Field(default=None)
    instagram_api_version: str = Field(default="v25.0")
    instagram_content_publish_approved: bool = Field(default=False)
    # X API v2 OAuth user context
    x_api_key: str | None = Field(default=None)
    x_api_secret: str | None = Field(default=None)
    x_access_token: str | None = Field(default=None)
    x_account_id: str | None = Field(default=None)
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

    @property
    def is_local_environment(self) -> bool:
        """Whether ``environment`` names a developer machine rather than a deployment.

        The same four names were already inlined at the CORS startup check, and the auth and
        SSRF checks need exactly the same question answered. A third and fourth copy of the
        tuple is how they drift apart, so it lives here once.
        """
        return self.environment.strip().lower() in ("development", "dev", "local", "test")

    @property
    def auth_enabled(self) -> bool:
        """Whether a shared secret is configured.

        Treats whitespace as unset: ``API_AUTH_TOKEN=" "`` in an env file is a mistake, and
        reading it as a real secret would mean requiring a token nobody can guess *and* nobody
        intended, which presents as the app being broken rather than as being misconfigured.
        """
        return bool((self.api_auth_token or "").strip())

    @property
    def api_auth_token_value(self) -> str:
        """The configured secret, stripped. Empty string when auth is disabled."""
        return (self.api_auth_token or "").strip()


@lru_cache
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance (loaded once per process)."""
    return Settings()


# Convenience singleton for straightforward imports.
settings = get_settings()
