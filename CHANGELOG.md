# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- RQ-backed distributed worker (currently in-process)

## [0.7.0] - 2026-07-23

### Added — Tier 1 Creator Output Upgrade
- **Animated caption presets** (`worker/effects/caption_presets.py` +
  `worker/captions.py`): a serializable `CaptionPreset` model and registry
  covering the three legacy templates (karaoke / boxed / minimal) plus new
  animated presets — **pop**, **typewriter**, and **hormozi** — rendered purely
  with **libass ASS tags** (no `drawtext`). Per-word animation is anchored and
  time-bounded to each spoken word.
- **Keyword highlighting**: a deterministic keyword planner (stopword / length /
  ALL-CAPS / numeral / high-confidence rules) with an optional **AI
  (context-aware) mode** that only ever *extends* the deterministic set;
  highlighted words get a distinct colour/scale while their timing is preserved.
  Optional **in-caption emoji** rendered inline (independent of the overlay
  emoji effect).
- **B-roll auto-insertion** (`worker/effects/broll.py`): a pure cue planner
  (`plan_broll_cues`) bounded by an **intensity** cap (off / subtle / standard /
  heavy) on both count and total on-screen time, plus a provider layer —
  **LocalProvider** (from `broll_dir`, no network) and an optional BYOK
  **ExternalProvider** (injectable downloader, records
  provider/source_id/license/attribution). `asset_sourcing_mode`
  (off / local_only / local_then_external) governs sourcing; unknown-license and
  failed assets are dropped. Overlays composite **below captions** in the
  existing single ffmpeg pass, and only composited assets are recorded on
  `ClipResult.broll_assets`.
- **Prompt / visual clip finding** (`worker/visual_selection.py`): an optional
  **selection prompt** plus cheap **CPU-only** keyframe sampling (bounded,
  once-per-source) and brightness/motion proxies merged with the transcript
  ranking; degrades cleanly to transcript-only selection when sampling fails, no
  provider/LLM is configured, or the feature is off.
- **Permissibility mode**: a single toggle that forces `asset_sourcing_mode` to
  **local_only**, disables added music, and blocks any external download — for
  music/sourcing-sensitive workflows.
- **API**: `/api/info` now advertises `caption_presets`, `caption_animations`,
  `asset_sourcing_modes`, `broll_intensities`, `broll_providers`, and
  `broll_available`; `POST /api/upload` accepts the twelve new option fields.
- **Web UI**: the settings panel gains a caption-preset dropdown, keyword /
  AI-highlight / in-caption-emoji toggles, a b-roll section (enable, intensity,
  sourcing mode, provider), a selection-prompt textarea + visual-selection
  toggle, and a permissibility-mode toggle.

### Changed
- Clip selection now routes through `select_moments_visual`, which delegates
  back to the v0.6.0 transcript-only selector whenever visual selection is off
  or degraded.

### Notes
- **Every new capability defaults OFF** — an "all-new-options-off" run
  reproduces v0.6.0 output and `effects_applied` exactly. No external network is
  used unless BYOK external b-roll is explicitly enabled and configured.

## [0.6.0] - 2026-07-23

### Added — Phase 5: storage, settings profiles & updates
- **Storage backends implemented** behind one interface (`storage_backends/`):
  a full `LocalStorage` and `S3Storage` (`save`/`open`/`url`/`delete`/`exists`/
  `list`/`size`, presigned URLs, injectable boto3 client) selected by
  `STORAGE_BACKEND` — **the code path is identical for local and S3**.
- **Retention & cleanup** (`storage_backends/retention.py`): a user-exposed
  retention window — **7 / 14 / 30 / 60 / 90 days** or **Keep forever** (default
  **30**), enforced by a background sweeper that **never touches source video**;
  plus `disk_usage()` (with a low-space warning) and manual "clean up now".
- **Temp auto-delete** toggle (removes a job's scratch files when it finishes)
  and a **delete-local-copy-after-publishing** toggle (guarded to clip files;
  never the source).
- **Sidecar metadata**: a `<clip>.json` capturing title/description/hashtags/
  effects is written next to every clip (and mirrored to the backend).
- **Protected source deletion**: original source video is only ever removed via
  an explicit, confirmed `DELETE /api/jobs/{id}/source?confirm=true`.
- **Runtime-mutable settings** (`runtime_config.py`): retention window and the
  two toggles are editable from the UI and persisted to
  `storage/runtime_config.json`, layered over the `.env` defaults.
- **Saved settings profiles** (`profiles.py`): snapshot the full configuration
  (clip length, aspect, caption style, effects, publishing targets) as a named
  profile; multiple profiles, quick-switch, edit/delete, and a **default**
  profile that pre-fills settings on load.
- **Update checking** (`updates.py`): compares the `VERSION` file to the latest
  GitHub release (cached, failure-tolerant) and drives an **"update available"**
  banner in the UI.
- **API**: `GET/POST /api/storage`, `/api/storage/settings`, `/api/storage/cleanup`,
  `DELETE /api/jobs/{id}/source`, `GET/POST /api/profiles`,
  `POST /api/profiles/{id}/default`, `DELETE /api/profiles/{id}`, `GET /api/updates`;
  the app version now comes from the `VERSION` file and `/api/info` reports the
  storage backend + retention choices.
- **Web UI**: a **Settings** tab with a **Storage** group (disk usage meter +
  low-space warning, retention, toggles, cleanup), a **Settings profiles** bar
  (save/switch/default/delete + prefill), the running **version**, and the
  update banner.

### Changed
- Default clip retention is now **30 days** (was 7); it is adjustable at runtime.
- **CI/CD**: the workflow adds a `deploy` job that auto-deploys from `main` to
  Render/Railway via a deploy-hook secret; a `render.yaml` Blueprint is included.
- Job pipeline mirrors finished clips (+ sidecar + thumbnail) through the storage
  backend on the same code path for local and S3; `README` documents the
  one-command update (`git pull && docker compose up --build`).

## [0.5.0] - 2026-07-23

### Added — Phase 4: visual effects (all individually toggleable)
- **Easy effects** (`worker/effects/overlays.py`), composed into a single
  efficient video pass: **zoom / Ken-Burns**, **punch-in intro**, **fade in/out**
  (video + audio), **colour grade** presets (vivid / warm / cool / cinematic /
  b&w), and a growing **progress bar**.
- **Hook title overlay**: the AI-generated hook text is burned in at the start
  (rendered via libass so it needs no `drawtext`/freetype build of ffmpeg).
- **Background music** (`worker/effects/audio.py`): **mood-selectable** beds
  (upbeat / chill / dramatic / corporate / suspense). Uses your own licensed
  track from `assets/music/<mood>.*` if present, otherwise synthesises a soft,
  copyright-free ambient bed and mixes it under the speech with a configurable
  volume (and matching fades).
- **Face-tracking auto-reframe** (`worker/effects/reframe.py`): detects the main
  speaker (OpenCV Haar cascade, MediaPipe-ready), smooths the crop path
  (EMA + resample) so the "camera" glides, and applies the moving crop in one
  ffmpeg pass via `sendcmd` + `crop`. Replaces the static centre-crop when
  enabled and **degrades gracefully** to the blurred-background reformat if no
  face is found or OpenCV is unavailable.
- **Auto-emoji overlays** (`worker/effects/emoji.py`) synced to spoken words via
  the Whisper word timestamps: a built-in **keyword→emoji map** plus an optional
  **AI (context-aware) mode**, four **intensity** levels (Off / Subtle /
  Standard / Heavy), Twemoji PNGs (fetched + cached from the CDN), and an
  optional alpha **pop** animation.
- **Filler-word / dead-air removal** (`worker/effects/filler.py`): cuts
  "um"/"uh" and long pauses, then **rebases** the word timeline so captions and
  emoji stay in sync.
- **Caption Template & Position**: templates (karaoke / boxed / minimal) and
  placement (bottom / center / top), surfaced in the UI.
- **Compositor** (`worker/effects/compositor.py`): applies all enabled effects
  in a single ffmpeg pass, stream-copying any track it doesn't change and doing
  nothing (fast path) when no effect is enabled. Each clip records which effects
  were applied (shown as badges in the gallery).
- **Pipeline & API**: per-clip flow is now cut → (filler trim) → geometry
  (reframe or reformat) → single-pass compositor → thumbnail; all effect options
  are accepted by the upload / URL / batch endpoints and `/api/info` advertises
  the available moods, colour presets, emoji intensities, and caption templates.
- **UI**: a **Visual effects** settings section with every toggle, the caption
  template/position, colour grade, music mood + volume, and emoji controls.

### Changed
- `Dockerfile` installs `fonts-liberation` + `fontconfig` so burned-in
  captions/hook titles render with a metric-compatible font.

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
