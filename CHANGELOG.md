# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- Emoji / overlay effects
- Face-tracking reframe (mediapipe) replacing centre-crop
- Auto-publishing to Whop / YouTube / TikTok / Instagram / X
- S3 storage backend + retention cleanup
- RQ-backed distributed worker (currently in-process)

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
