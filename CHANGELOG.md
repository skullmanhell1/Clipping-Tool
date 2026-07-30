# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed — clips could open mid-shot (S9)

- Clip starts now snap to a nearby shot boundary. A clip beginning two seconds into a shot opens
  on a fragment — half a gesture, the tail of a camera move — and reads as careless before the
  viewer has heard a word. The `S1` harness showed the scale: at IoU 0.7, the threshold that asks
  whether *boundaries* are right rather than whether the right moment was found, the selector
  scored **zero across the board**.
- **ffmpeg's scene score rather than PySceneDetect.** The plan names PySceneDetect and it is the
  standard tool, but this needs one number — "is there a hard cut near here" — and ffmpeg is
  already the dependency every other stage shells out to.
- **Narrow windows, not a full scan.** Detection decodes video, so only a couple of seconds either
  side of each candidate start is examined. Scanning an hour-long source to move a boundary by
  under a second would be wildly disproportionate.
- **Only the start moves, and only within a cap.** The ending is chosen for content reasons — a
  punchline, a completed thought — so a shot change near it is not a reason to truncate. Beyond
  the cap the boundary the selector chose is kept: moving a start several seconds to reach a cut
  is not snapping, it is choosing a different moment.
- **A documented blind spot.** ffmpeg scores scene change on luma, so a cut between two shots of
  similar brightness is invisible to it. A test pins that deliberately so it is not later mistaken
  for a regression. A missed cut leaves the boundary exactly where it was, which is the previous
  behaviour.


### Fixed
- **`.env.example` had drifted from the code.** `config.Settings` points at it for "the full
  list", but it documented 67 of 93 settings and carried one key —
  `PUBLISH_DEFAULT_INTERVAL_SECONDS` — that no longer exists. Since `Settings` uses
  `extra="ignore"`, setting that key was accepted and discarded, so it read as a working
  control that did nothing. All 93 are now documented and
  `tests/test_config_documentation.py` fails on drift in either direction.
- `render.yaml` set `ENVIRONMENT=production` but never `CORS_ORIGINS`, so a Render deploy ran
  the `*` wildcard in production — which also disables credentialed cross-origin requests. The
  blueprint now asks for an explicit origin.
- `test_visual_selection_leaves_no_keyframe_temp_directory` is gated on ffmpeg. It drives the
  real `select_moments_visual`, whose transcript-free path probes the source before sampling;
  without the binary that probe fails, sampling is never reached, and the test's own guard
  correctly reported that it proved nothing.

### Planned
- RQ-backed distributed worker (currently in-process, and **not** yet wired up — see the note
  under 0.9.0). `redis` and `rq` are declared dependencies but no code imports them.
- Adopt ruff's `UP` (pyupgrade, ~450 findings) and `B` (bugbear, ~30) rule sets; each is a
  mechanical sweep of its own.
- Enforce formatting. `black` is a dev dependency but has never run, so adopting it would
  reformat essentially every file.

## [0.10.0] - 2026-07-29

Reliability and tooling hardening. No feature work; every item below is a defect found by
running the application and its tooling for real rather than by reading it.

### Added
- **`POST /api/publish-attempts/{id}/approve` and `/retry`.** `review_required` was a
  reachable dead end: `instagram.py`, `whop.py` and `x.py` all return it — X's own message
  reads "approve review before posting" — but no route could move an attempt out of that
  state, so such posts stopped permanently. `approve` rewrites the stored request to
  `mode="auto"`; `retry` preserves the mode so a review submission is never silently
  escalated into a live post. Approving a platform that lacks direct-publish permission is
  refused with a 409 carrying the platform's own explanation, because re-queueing it would
  simply reproduce `review_required` on the next tick — an invisible infinite bounce.
- **Durable job state** (`worker/job_persistence.py`, `JOBS_DB`). The job store was process
  memory only, so any restart discarded every job while the clips stayed on disk and in the
  publish history — which is why the history view listed clips whose downloads 404'd. Jobs
  are now written through to SQLite on every mutation. A job stored as `queued`/`processing`
  is resolved to `failed` on load, since no worker thread exists to advance it and a
  perpetually spinning progress bar is worse than an honest failure.
- **Upload validation.** `POST /api/upload` accepted any file of any size. There is now an
  extension allow-list (400), a size ceiling enforced while writing rather than trusting
  `Content-Length` (413), an empty-file check, and deletion of partial writes. A rejected
  file in a batch rolls the whole request back, so no orphaned uploads are left behind.
- **`requirements-ml.txt`** plus `--build-arg INSTALL_ML=true`, making real stem separation an
  explicit opt-in. `torch`/`demucs` were absent from `requirements.txt`, the Dockerfile *and*
  `render.yaml`, so every deploy silently took the crude ffmpeg approximation with no
  documented way to enable the real path.
- **`pyproject.toml`** with pytest and ruff configuration; see below.
- **Frontend lint and tests.** `npm run lint` previously failed outright — the script existed
  but eslint was not a dependency and no config file was present. Added a flat eslint config
  and 24 vitest tests covering the API client's URL/error handling and `Dropdown`.
- **Real-binary capability tests** (`tests/test_capabilities_real_binary.py`), which
  cross-check the probe against `ffmpeg -h filter=<name>` — an independent mechanism that
  shares no parsing code with the `-filters` table. Verified to fail on the bug they guard.
- `diarization_handoff_gap`, `visual_selection_weight`, `disk_usage_cache_seconds`,
  `ffmpeg_timeout_seconds`, `ffprobe_timeout_seconds`, `max_upload_bytes`,
  `max_persisted_jobs` settings.

### Fixed
- **No ffmpeg call outside the stem engine had a timeout.** `worker/ffmpeg_utils._run` called
  `subprocess.run` with no `timeout`, so every render, extract, thumbnail and remux was
  unbounded. Because jobs run in a thread pool with a single worker, one hung ffmpeg blocked
  the entire queue forever — silently, since a stalled process yields neither output nor an
  exception. Bounded now, with the ceiling chosen by binary (probe vs encode) and `0` as a
  documented opt-out.
- **Diarisation invented speakers who never spoke.** Label assignment advanced a round-robin
  on every silence longer than `pause_gap` (0.9s). Pauses just over that are routine inside
  one person's speech, so a monologue was reported as two speakers and speaker-aware reframe
  cut back and forth between two "speakers" who were the same person. Ending a turn and
  changing speaker are now separate thresholds, and attribution is biased toward keeping
  words with the current speaker.
- **Per-publisher rate limits were dead code.** The scheduler applied
  `max(publisher.min_interval_seconds, publish_default_interval_seconds)` with the latter
  defaulting to 30s, and every publisher declares 2–18s — so all of them were overridden and
  publishing ran roughly twice as slowly as intended. The setting is now
  `publish_min_interval_floor_seconds`, defaulting to 0.
- **`sample_keyframes` leaked a temp directory per run.** It created its scratch directory
  with `mkdtemp` and nothing ever deleted it, leaving a `kf-*` directory of JPEGs in the
  system temp space on every visual-selection run — unbounded growth, and outside the
  retention sweeper's remit.
- **`/api/storage` walked the whole storage tree on every poll**, and the storage panel polls.
  Area sizes are now cached briefly and computed with `os.scandir` instead of
  `rglob` + `stat`; the cleanup endpoint passes `refresh=True` so it cannot report
  pre-cleanup totals. Volume free/total figures are never cached.
- **CORS advertised credentials it could not deliver.** `allow_credentials=True` was hard-coded
  while `cors_origins` defaulted to `*`; the CORS specification forbids that combination and
  browsers reject the response, so the default configuration broke every credentialed
  cross-origin request while appearing to permit it. Credentials are now derived from the
  origin list, and a wildcard on a non-development environment logs a warning.
- The visual/transcript blend weight was effectively hard-coded: `merge_scores` took a
  `weight` argument that its only call site never passed. Now `visual_selection_weight`,
  clamped to `[0, 1]`.
- Nine unclosed-file leaks in the test suite, surfaced by making warnings errors.
- 14 unused imports and 27 unsorted import blocks.

### Changed
- **CI is now able to fail.** `ruff check . || true` could not, and with no `[tool.ruff]`
  config the enforced rule set was whatever the installed ruff version defaulted to — 857
  findings on 0.16 versus a handful on older releases. The rule set is pinned and the step
  blocks. The suite must also run clean of *skips*: a skipped test is not a passing test, and
  that is how the earlier missing-ffmpeg gap went unnoticed. The frontend job now lints and
  tests as well as building, and uses `npm ci`.
- **The deploy job's secret checks never worked.** `if: ${{ secrets.X != '' }}` does not
  evaluate as intended, because the `secrets` context is not resolvable inside an `if`
  expression. The values are surfaced as job-level `env` and tested via `env.*`.
- `pytest` treats warnings as errors, with `--strict-markers`/`--strict-config`.
- `api/main.py` uses a lifespan handler instead of the deprecated `@app.on_event("startup")`.

## [0.9.0] - 2026-07-29

### Added
- **Stem-aware audio repair (`stem_inpainting` engine, default OFF).** An AUDIO-stage AV engine
  that separates clip audio into a `vocals`/`music`/`other` Stem_Set, applies per-stem gains, and
  repairs the waveform joins that filler-word removal leaves behind.
  - Two separator backends behind one file-based protocol: a local `demucs` checkpoint (`ml`),
    and a dependency-free ffmpeg approximation (`music := clip - vocals`) that is only ever
    reached carrying a `degraded:` marker so it is never mistaken for real separation.
  - Seam repair as an equal-power V-notch, `sin(PI/2*|t-c|/h)`, evaluated **per sample**. Fixed
    at exactly two media passes per clip regardless of seam count: extract (`-vn`) and remux
    (`-c:v copy`).
  - Mix presets `speech_focus` / `music_focus` / `clean_speech`, plus `custom` gains over
    `0.0-4.0`; repair modes `off` / `crossfade` / `spectral`; optional declick and retained
    per-stem WAVs as durable artifacts.
  - Eleven new `ProcessingOptions` fields (`stem_inpainting_enabled` plus ten `stem_*`),
    accepted by `OptionsModel` and `/api/upload`, advertised under
    `/api/info` → `capabilities.stem_inpainting`, and exposed as a "Stem repair" group in the
    settings panel. `spectral` is shown disabled with a "needs local model" hint when
    `model:htdemucs` is unavailable.
- `demucs` and `torch` remain **optional**: they are not in `requirements.txt`, and a stock
  install runs the engine via the ffmpeg approximation.

### Changed
- **`Engine_Host` now adopts replacement media from a `degraded` engine**, not only an `applied`
  one (`_MEDIA_BEARING_STATUSES`). Degradation describes fidelity, not usability — an engine that
  fell back and still produced a usable file has produced usable output. Requirement 8.3 is
  unchanged, because it is carried by `media is None` rather than by status.
- `Engine_Host.run_stage` gained an additive, keyword-only `notes` parameter for caller-supplied
  Engine_Context notes. Existing call sites are unaffected.
- `Dropdown.jsx` supports per-option and whole-control `disabled`, so a mode that exists but is
  unavailable can be shown with its reason instead of hidden.
- CI installs `libgl1`/`libglib2.0-0`. `opencv-python` was already installed but could not be
  imported without them, so the vision code paths were never actually loaded in CI.

### Fixed
- **The `ffmpeg_filter:` capability probe could not see 124 of ffmpeg's 486 filters**, so every
  engine requiring one of them reported `unavailable` on every host regardless of how ffmpeg was
  built. `ffmpeg -filters` prints a three-character flags column per row (`T..`, `..C`), and the
  parser identified it with `not parts[0].isalnum()`. A filter with *every* flag set prints a
  dot-free group (`TSC highpass`), which that test rejects, so the row fell through to a
  bare-name branch and recorded `"TSC"` as the filter name while losing `highpass`. Affected
  `highpass`, `lowpass`, `bass`, `treble`, `equalizer`, `afftdn`, `arnndn` and 117 more. In
  practice this made the `stem_inpainting` ffmpeg backend permanently unreachable, since it
  requires `highpass` and `lowpass`. The flags column is now recognised by its alphabet, and rows
  are only accepted when the pad-spec column (`A->A`) is present. Every canned test listing had
  used dot-bearing flag groups only, which is why the whole suite passed against a feature that
  could not run.
- **Seam repair never applied.** The repair filter was specified and implemented as `volume` with
  `eval=frame` and a time-dependent expression. Against ffmpeg 7.x that is a silent no-op — `t`
  does not take the values a per-frame evaluation implies, so a `between(t,…)`-gated expression
  never fires, and the output is byte-identical to the input with no error. Replaced with
  `aeval`, which evaluates per sample.
- **Separated stems failed their own integrity check.** They were verified against the clip
  container duration, but a lossy audio stream carries encoder padding (2.000 s of AAC decodes to
  ~2.020 s of PCM), so every real separation was rejected. Stems are now checked against the
  decoded audio they were separated from.
- **Repaired clips grew by ~20 ms per pass** as that same padding compounded through
  extract + re-encode. The remux is now bounded with `-t` taken from the original clip's audio
  stream duration. (Deliberately not `-shortest`, which truncates to whichever stream happens to
  be shorter — an input-dependent change rather than a measured one.)
- `tests/test_engine_host.py` no longer leaves the `worker.engines` process globals cleared. It
  cleared the default registry and `MODEL_LOCATORS` for isolation without restoring them, and
  because `loader.py` populates those by import side effect they could not be repopulated —
  making later test files depend on pytest's file ordering.

## [0.8.0] - 2026-07-23

### Added — Speaker Diarisation & Multi-Speaker Reframe
- **Speaker diarisation** (`worker/diarization.py`): segments a source into
  ordered, non-overlapping `Speaker_Turn`s from the offline Whisper word
  timeline — **CPU-only, no GPU, no network**. An optional dependency-injected
  diarisation backend is supported but never required; it degrades to
  word-timeline segmentation on absence/error. Diarisation runs **once per
  source** and is capped at a configurable max-speakers (default 2).
- **Speaker-aware reframe** (`worker/effects/reframe.py`): multi-face detection
  + face-track grouping + a face↔speaker associator drive two output layouts —
  **follow-active** (a single dynamic crop that glides to whoever is speaking)
  and **split-screen** (a 2-up composite of the most-talkative speakers) — with
  **subtle / standard / heavy** smoothing and smooth transitions on speaker
  change. All geometry is applied in the **existing single ffmpeg pass**.
- **Graceful degradation**: an explicit precedence ladder — speaker-aware
  reframe → the existing single-speaker reframe → the static blurred reformat —
  guarantees a clip is always produced, recording the fallback in
  `effects_applied` (`speaker_reframe:<layout>`, `speaker_reframe_degraded`,
  `speaker_reframe_substituted`, `diarization:transcript`/`:model`/`_degraded`).
- **Permissibility-aware**: under permissibility mode diarisation uses only the
  offline word timeline (any external backend is bypassed) and no network call
  occurs.
- **API + Web UI**: `/api/info` advertises `reframe_layouts` and
  `reframe_intensities`; `POST /api/upload` accepts `diarization`,
  `speaker_reframe`, `reframe_layout`, and `reframe_intensity`; the settings
  panel gains Speaker-aware-reframe + Diarisation toggles and Reframe layout /
  intensity dropdowns.

### Changed
- The pipeline geometry stage now routes through the speaker-aware precedence
  ladder; when both new toggles are off it takes the exact v0.7.0 path.

### Notes
- **Every new capability defaults OFF** — an "all-off" run reproduces v0.7.0
  output and `effects_applied` exactly. Transcript-first diarisation is a
  CPU-only heuristic best suited to turn-based interviews/podcasts; an acoustic
  BYOK backend can be injected without other changes. Enabling speaker-aware
  reframe adds roughly 1.0–1.1x the single-speaker reframe render time
  (follow-active) or ~1.1–1.3x (split-screen); disabled it adds zero cost.

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
