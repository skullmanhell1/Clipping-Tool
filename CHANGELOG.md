# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- Emoji / overlay effects
- Face-tracking reframe (mediapipe) replacing centre-crop
- S3 storage backend + retention cleanup
- RQ-backed distributed worker (currently in-process)

## [0.4.0] - 2026-07-23

### Added — Phase 3: auto-publishing
- **Common publisher interface** (`publishers/base.py`): a platform-neutral
  `PublishRequest` / `PublishResult` / `PublisherStatus` contract with a shared
  `BasePublisher`, so platforms plug in through one registry
  (`publishers/__init__.py`).
- **Platform adapters**, each reporting a clear configured/limited/ready status
  and degrading gracefully:
  - **Whop** (`publishers/whop.py` + `publisher_bridge/`): uploads via the
    official **`@whop/sdk`** through a Node bridge, then attaches the file to a
    **chat**, **forum**, or **course** target; uploads with no supported target
    return `review_required` for manual placement.
  - **YouTube** (`publishers/youtube.py`): Data API v3 resumable upload over the
    OAuth refresh-token flow; vertical clips publish as **Shorts** (review mode
    uploads privately).
  - **TikTok** (`publishers/tiktok.py`): Content Posting API. Uploads to the
    creator's **inbox as a draft** until Direct Post is approved
    (`TIKTOK_DIRECT_POST_APPROVED`).
  - **Instagram** (`publishers/instagram.py`): Graph API resumable **Reels**
    upload/publish; runs in review mode unless content-publish is approved.
  - **X** (`publishers/x.py`): chunked media upload + post; returns
    `review_required` unless an approved user-context token is present.
- **AI metadata on upload**: each adapter attaches the clip's generated title,
  description/caption, and hashtags automatically (per-platform limits applied).
- **Multi-channel routing** (`publishers/history.py` campaigns): tag a clip with
  a **campaign** that maps each platform to an account/target; clips route to
  the right destination.
- **Throttled scheduling** (`publishers/manager.py`): a persistent background
  scheduler posts **now or at a chosen time**, enforcing a minimum per-platform
  interval to respect rate limits.
- **Metadata download bundle**: the primary clip download now returns a **ZIP**
  containing the MP4 plus a `_metadata.txt` file with the title, caption, and
  hashtags; a raw video-only download remains available.
- **Persistent history** (SQLite): every created clip and every publish attempt
  (platform, account, time, state, link, error) is recorded and survives
  restarts.
- **API**: `GET /api/publishers`, `GET|POST /api/campaigns`,
  `POST /api/jobs/{job}/clips/{clip}/publish`, `GET /api/history`,
  `GET /api/publish-attempts/{id}`, ZIP + video-only clip downloads; upload/URL
  jobs accept `publish_to`, `campaign_id`, `publish_mode`, and `schedule_at` for
  auto-publishing on completion.
- **Web UI**: a **Publishing settings** panel (Publish To multi-select with live
  per-platform status, Campaign, Mode auto/review, Schedule, and campaign
  saving), **per-clip publish/schedule buttons** with live attempt status, the
  metadata-bundle download, and a dedicated **History** view.

### Changed
- `Dockerfile` now installs Node.js and the `publisher_bridge` dependencies so
  the Whop `@whop/sdk` bridge runs inside the container.
- All publisher secrets and scheduler tuning are read from `.env`
  (see `.env.example`); the history store persists IDs/status only — never
  tokens.

## [0.3.0] - 2026-07-23

### Added — Phase 2: smart selection & metadata
- **Pluggable LLM client** (`worker/llm_client.py`): OpenAI or Anthropic
  (key from `.env`), unified `complete` / `complete_json` interface with lenient
  JSON parsing, a `MockLLMClient`, dependency-injection override
  (`set_llm_client`), and an availability check.
- **LLM highlight selection** (`worker/selection.py`): replaces fixed-length
  cutting. Sends the transcript to the LLM to find hooks, punchlines, complete
  thoughts, and emotional peaks; returns candidates with a **virality score**
  and rationale. Honours *Clip Topic/Keywords* and *Vibe/Tone*, respects clip
  count + target length, snaps start/end to sentence boundaries, and falls back
  to deterministic segmentation when no LLM is configured.
- **AI metadata generation** (`worker/metadata.py`): per-clip title (+ 2-3
  alternatives), description/caption, hashtags (configurable count), on-screen
  hook text, CTA, @mentions, and thumbnail text idea — tone tailored **per
  platform** (YouTube / TikTok / Instagram / X / Whop / generic) with character
  and hashtag limits enforced. Individual fields can be regenerated.
- **Pipeline integration** (`worker/pipeline.py`): AI selection + per-clip
  metadata, an optional **Process Range**, and graceful fallback throughout.
- **API** (v0.3.0): extended options (topic, vibe, platform, hashtag count,
  process range, selection strategy); `PATCH /api/jobs/{job}/clips/{clip}` to
  edit clip metadata and `POST .../regenerate` to regenerate a single field;
  `/api/info` now reports platforms, strategies, and LLM availability.
- **Web UI**: an **Advanced settings** section (Clip Topic, Vibe/Tone, Process
  Range, Platform, Hashtag count, selection method) and an editable clip gallery
  showing the **virality score**, editable title (with alternative chips),
  description, hashtags, hook, CTA, and thumbnail text — each with a per-field
  **regenerate** action.

## [0.2.0] - 2026-07-23

### Added — Phase 1: core clip-generating engine
- **FFmpeg utilities** (`worker/ffmpeg_utils.py`): probe, frame-accurate
  segment cut, aspect reformat (9:16 / 1:1 / 16:9 / 4:5) with blurred-background
  fill or padding, audio extraction, and thumbnail generation.
- **Transcription** (`worker/transcribe.py`): faster-whisper with word-level
  timestamps, lazy cached model, auto CPU/GPU device selection, and translate
  mode.
- **Captions** (`worker/captions.py`): word-grouped cues rendered to styled ASS
  with karaoke-style highlighting, burned in via libass.
- **Segmentation** (`worker/segmentation.py`): fixed-length and silence-based
  chunking, with UI Clip Length / Number of Clips option mapping.
- **Ingest** (`worker/download.py`): yt-dlp URL download with progress + cheap
  metadata fetch for preview cards; URL/file classification.
- **Pipeline & jobs** (`worker/pipeline.py`, `worker/jobs.py`): end-to-end
  orchestration with live progress; in-process background job manager with a
  thread-safe store; batches processed in line.
- **Watch-folder mode** (`worker/watch_folder.py`): toggleable folder monitor
  that auto-processes dropped videos with the current settings.
- **API** (`api/main.py`): preview, single-URL, batch, and multi-file upload
  submission; job status/progress; clip listing, static preview, and download;
  watch-folder toggle. Serves the built React SPA.
- **Web UI** (`frontend/`): dark, Opus-Clip-style dashboard — URL/upload/batch
  input, preview card, settings panel (Language, Clip Length, Aspect Ratio,
  Number of Clips), a full-width green "Get Clips" button, per-video progress,
  and a clip gallery with inline preview + download.

### Changed
- Multi-stage `Dockerfile` (builds the SPA, then the Python runtime with FFmpeg).
- `docker-compose.yml` simplified to a single app service for Phase 1
  (in-process jobs); RQ + Redis reserved for a later phase.

## [0.1.0] - 2026-07-23

### Added
- Initial project scaffold (foundation only; no features implemented).
- `config.py` pydantic settings + `.env.example` covering LLM, transcription,
  storage/S3, and all publisher credentials.
- Backend package skeleton: `api/`, `worker/` (+ `worker/effects/`),
  `publishers/`, `storage_backends/` with documented stubs.
- FastAPI app that boots and serves a dark-themed placeholder page plus
  `/healthz` and `/api/info` endpoints.
- React + Tailwind dark-themed frontend skeleton.
- `docker-compose.yml` bundling the app (with FFmpeg), a worker, and Redis.
- GitHub Actions CI workflow (lint + import/boot smoke check).
