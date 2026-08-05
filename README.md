# AI Video Clipper

Turn long-form video into short, vertical, captioned clips — and (optionally)
auto-publish them.

> **Status:** v0.11.0 — **Phases 1–5 + the Tier 1 creator-output upgrade + speaker diarisation & multi-speaker reframe are working.** Paste a URL or upload video,
> and the tool transcribes it, uses an **LLM to pick the most engaging moments**
> (with a virality score), reformats to vertical (or 1:1 / 16:9 / 4:5) — with
> optional **face-tracking auto-reframe** — burns in word-timed captions,
> applies **toggleable visual effects** (zoom, hook title, mood music, fades,
> colour grade, progress bar, word-synced auto-emoji, filler-word removal),
> **auto-writes per-platform titles, descriptions & hashtags** you can edit or
> regenerate, can **auto-publish** the finished clips to Whop, YouTube,
> TikTok, Instagram & X with campaign routing, scheduling, and a full history,
> and manages **storage (retention, S3, disk usage), saved settings profiles,
> and in-app update checks**.

## Speaker diarisation & multi-speaker reframe

Multi-speaker awareness for interviews and podcasts, **individually toggleable**
and **OFF by default** (an all-off run behaves exactly like v0.7.0):

- **Speaker diarisation:** figures out *who is speaking when* from the
  transcript — fully **CPU-only and offline**, no GPU or extra model required.
  Capped at 2 speakers by default (configurable).
- **Speaker-aware reframe** (replaces the single-speaker face reframe when on):
  - **Follow active speaker** — the vertical crop glides to whoever is talking.
  - **Split screen** — a stacked 2-up of the two most-talkative speakers.
  - **Intensity** (subtle / standard / heavy) controls how fast the camera moves
    and how smooth the transitions are.
- **Always safe:** if diarisation or face detection can't run, it falls back to
  the single-speaker reframe and then to the static blurred crop — a clip is
  always produced.
- **Permissibility mode** keeps everything local and offline.

> Speaker-aware reframe uses your local OpenCV install for face detection (CPU).
> An optional acoustic diarisation backend can be plugged in (BYOK) for higher
> accuracy, but is never required.

## Tier 1 — creator output upgrade

Advanced output controls, all **individually toggleable** and **OFF by default**
(so an all-off run behaves exactly like v0.6.0):

- **Animated caption presets:** in addition to karaoke / boxed / minimal, choose
  **pop**, **typewriter**, or **hormozi** — word-timed animations rendered with
  libass (no `drawtext`).
- **Keyword highlighting:** automatically emphasise punchy words (deterministic
  rules, with an optional AI context-aware mode that only extends the set), plus
  optional **in-caption emoji**.
- **B-roll auto-insertion:** drop relevant b-roll over key phrases, capped by an
  **intensity** setting. Source from your **local** `BROLL_DIR` (no network) or,
  with your own API key, an **external** provider; unknown-license or failed
  assets are skipped and composited assets are recorded per clip.
- **Prompt / visual clip finding:** give a **selection prompt** to steer which
  moments are chosen, augmented by lightweight CPU-only keyframe analysis; it
  degrades to transcript-only selection when unavailable.
- **Permissibility mode:** one toggle that forces **local-only** asset sourcing,
  disables added music, and blocks any external download.

> B-roll external sourcing is opt-in and BYOK: set `BROLL_PROVIDER`,
> `BROLL_PROVIDER_API_KEY`, `BROLL_PROVIDER_BASE_URL`, and
> `BROLL_ALLOW_DOWNLOAD=true` in `.env`. With those unset (the default) b-roll
> uses only local files in `BROLL_DIR` and makes no network calls.

## Phase 5 — storage, profiles & updates

**Storage** (a *Storage* group under the **Settings** tab):

- **Retention**, exposed in the app: keep clips **7 / 14 / 30 / 60 / 90 days** or
  **Keep forever** (no auto-deletion). Default **30 days**. A background sweeper
  enforces it; you can also **clean up now**.
- **Auto-delete temp files** after each job (toggleable).
- **Delete local clip copy after publishing** (toggleable).
- **Disk usage** meter with a **low-space warning**.
- **Sidecar metadata** — a `<clip>.json` is written next to every clip.
- Original **source video is never auto-deleted**; removing it requires an
  explicit, confirmed action.
- **Optional S3 backend** behind the same storage interface — flip
  `STORAGE_BACKEND=s3` in `.env` (plus the `S3_*` vars). The code path is
  identical for local and S3.

**Saved settings profiles** — snapshot the full current configuration (clip
length, aspect, caption style, effects, publishing targets, …) as a **named
profile**. Keep multiple profiles, quick-switch, edit/delete, and mark one as
the **default** that pre-fills settings on load.

**Live job progress over SSE** — the dashboard follows a render through
`GET /api/jobs/events`, a [Server-Sent
Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events)
stream. It sends every job once on connect, then only the jobs whose state has
actually changed. It replaces a poll loop that refetched the whole job list —
including every clip and each job's full options object — twice a second for as
long as a tab was open, whether or not anything had moved.

Two settings control it, both in `.env.example`:

| Variable | Default | What it does |
| --- | --- | --- |
| `JOB_EVENTS_POLL_INTERVAL_SECONDS` | `0.5` | How often the stream re-reads the job store looking for a change. Cheaper than the old client poll despite being more frequent: it is an in-memory dict read, not an HTTP round trip plus a full JSON re-serialisation. |
| `JOB_EVENTS_HEARTBEAT_SECONDS` | `15.0` | Idle keepalive. A stream that sends nothing looks dead to anything in the middle, and nginx's default `proxy_read_timeout` is 60s. If you raise this, raise `proxy_read_timeout` too — whichever is shorter decides when the stream drops. |

**Polling is kept as a fallback, not removed.** If the stream cannot be opened
twice in a row the UI switches to the old 1.2s/4s poll for the rest of the
session. That is the path you get behind a reverse proxy that buffers responses,
or in a browser without streaming `fetch`, and it is why
`X-Accel-Buffering: no` is set on the stream — without it nginx buffers the
response and the UI receives one lump of progress at the end of the render
instead of a moving progress bar.

The stream authenticates with the same `Authorization`/`X-API-Token` header as
every other route. It deliberately does **not** accept `?token=`: the browser
`EventSource` API cannot set headers, so the client reads it with `fetch`
instead rather than putting the token in the URL of a connection that stays open
for a whole render — where it would sit in access and proxy logs for that
connection's lifetime.

**Job completion webhook** — one `POST` when a job reaches a terminal state, so a
script does not have to poll either. Set `JOB_WEBHOOK_URL` and every
`completed`/`failed`/`cancelled` transition delivers a JSON summary: the job id
and status, the clip filenames and URLs, each clip's `effects_applied` (so an
integration can see that a clip carries `music_degraded:synthesised`), the stage
timings and the LLM token usage.

| Variable | Default | What it does |
| --- | --- | --- |
| `JOB_WEBHOOK_URL` | *(unset)* | Where to POST. Unset means nothing is ever sent. |
| `JOB_WEBHOOK_SECRET` | *(unset)* | Signs the exact body as `X-Clipping-Signature: sha256=<hex>`, the form GitHub and Stripe use. Worth setting — the payload carries clip URLs. |
| `JOB_WEBHOOK_TIMEOUT_SECONDS` | `5.0` | Delivery is synchronous on the worker thread, so this bounds how long an unreachable receiver can delay the *next* job. |
| `JOB_WEBHOOK_EVENTS` | `completed,failed,cancelled` | Which terminal states to send. Set to `failed` to hear only about problems. |

Three things about it are deliberate. It is fired from the single `finally` every
terminal path passes through, so there is exactly one delivery per job and a
future outcome cannot silently skip it. It makes **one attempt** and claims no
delivery guarantee — a non-2xx or a timeout is logged and dropped, because a real
guarantee needs durable queue state and an in-process retry loop would block the
worker for longer and still lose everything on restart. And the URL is **not**
SSRF-checked, unlike URL ingest: it comes from your own environment, and the
usual target is a service on the same host or compose network.

**Updates & maintenance** — the running **version** is shown in the UI; an
**"update available" banner** appears when a newer GitHub release exists;
[semantic versioning](https://semver.org) via the `VERSION` file; CI builds +
tests on every push and can **auto-deploy from `main`** to Render/Railway; and
`CHANGELOG.md` is kept up to date. See [Updating](#updating) for the one-command
update.

## Phase 4 — visual effects

Every effect is **individually toggleable** in the UI's *Visual effects* panel
and is applied per clip in a **single, efficient ffmpeg pass** (frame-by-frame
work is opt-in and adds render time).

- **Easy effects:** zoom / Ken-Burns, punch-in intro, fade in/out, colour grade
  (vivid / warm / cool / cinematic / b&w), and a progress bar.
- **Hook title:** burns the AI hook text on screen at the start (via libass).
- **Background music:** mood-selectable (upbeat / chill / dramatic / corporate /
  suspense). Bring your own licensed track as `assets/music/<mood>.mp3`, or the
  tool synthesises a soft, **copyright-free** bed and mixes it under the speech.
- **Face-tracking auto-reframe:** detects and follows the main speaker so the
  vertical crop glides with them (replaces the static crop); falls back to the
  blurred-background reformat when no face is found.
- **Auto-emoji:** word-synced Twemoji overlays with a keyword map **or** an AI
  context-aware mode, four intensity levels, and an optional pop animation.
- **Filler-word removal:** cuts "um"/"uh" and long pauses, keeping captions and
  emoji in sync by rebasing the word timeline.
- **Caption Template & Position:** karaoke / boxed / minimal, at bottom / center
  / top.

> Auto-emoji downloads Twemoji PNGs from a CDN on first use (cached under
> `assets/emoji`); set `EMOJI_ALLOW_DOWNLOAD=false` to use only local assets.
> Burned-in text uses libass — the Docker image installs `fonts-liberation` so a
> font is always available.

## Phase 3 — auto-publishing

- **One pluggable publisher interface** (`publishers/base.py`) with a registry,
  so platforms plug in cleanly. Each platform shows a **clear status** in the UI:
  *not configured*, *limited / review*, or *ready*.
- **Platform adapters**, all reading secrets from `.env` and degrading
  gracefully when approval is pending:
  - **Whop** — uploads the clip through the official **`@whop/sdk`** (a small
    Node bridge in `publisher_bridge/`) and attaches it to a **chat**, **forum**,
    or **course** target.
  - **YouTube** — Data API v3 resumable upload via OAuth; vertical clips post as
    **Shorts**.
  - **TikTok / Instagram / X** — built, but **may be limited to draft/private**
    until your developer app is approved. TikTok uploads to the creator inbox as
    a draft until Direct Post is approved; Instagram needs content-publish
    approval; X needs an approved user-context token. Each degrades to a
    `review_required`/draft state and says so in the UI.
- **AI metadata attached automatically** — each upload carries the clip's
  generated title, caption/description, and hashtags.
- **Multi-channel routing** — tag a clip with a **campaign** that maps each
  platform to the right account/target.
- **Throttling + scheduling** — post now or pick a time; a background scheduler
  respects a minimum per-platform interval so you don't hit rate limits.
- **Download bundle** — the plain download is a **ZIP** with the video **plus a
  `.txt`** of that clip's title, caption, and hashtags (a video-only link is
  also available).
- **Logs / history** — every created clip and every publish attempt (platform,
  account, time, success/failure, link) is stored in SQLite and viewable in the
  **History** tab.

> Set the relevant keys in `.env` (see `.env.example`) to enable each platform.
> The Whop bridge needs Node.js and `npm install` inside `publisher_bridge/`
> (handled automatically by the Docker image). Secrets stay in `.env`; the
> history database stores IDs and statuses only — never tokens.

> **Platform docs used while building the adapters** (details rephrased for
> compliance with licensing restrictions):
> [Whop file uploads](https://docs.whop.com/developer/guides/upload-files),
> [TikTok Content Posting API](https://developers.tiktok.com/doc/content-posting-api-reference-upload-video/),
> [Instagram content publishing](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/content-publishing/),
> [X chunked media upload](https://docs.x.com/x-api/media/quickstart/media-upload-chunked).

## Phase 2 — smart selection & metadata

- **LLM highlight selection** (pluggable **OpenAI, Anthropic or Gemini**, key from
  `.env`): finds hooks, punchlines, complete thoughts, and emotional peaks;
  scores each clip's **virality** and snaps cuts to sentence boundaries. Honours
  a **Clip Topic/Keywords** and **Vibe/Tone**, and respects your clip count and
  target length. Falls back to Phase 1's silence/fixed segmentation when no LLM
  key is set.
- **AI metadata per clip, tailored per platform:** title + alternatives,
  description/caption, hashtags (configurable count), on-screen hook text, CTA,
  @mentions, and a thumbnail-text idea — with character/hashtag limits enforced.
- **Editable gallery:** every field is editable and each can be **regenerated**
  individually; clips show their virality score.
- **Advanced settings** in the UI: Clip Topic, Vibe/Tone, Process Range,
  Platform, Hashtag count, and selection method.

> Set `LLM_PROVIDER` and the matching `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` in
> `.env` to enable AI features. Without a key, the tool still produces clips and
> basic metadata via the deterministic fallback.

## Phase 1 — what works today

- **Inputs:** single file upload, paste a video URL (yt-dlp), a **batch** of
  multiple files/links processed in line, and a toggleable **watch-folder** mode.
- **Pipeline:** transcribe (faster-whisper, word-level timestamps) → segment
  (silence-based or fixed-length) → cut → reformat to the chosen aspect ratio
  (blurred-bg fill) → burn karaoke-style captions → thumbnail.
- **UI (dark, Opus-Clip style):** URL/upload/batch input, video preview card,
  settings panel (Language incl. translate, Clip Length, Aspect Ratio, Number
  of Clips), a full-width green **Get Clips** button, live per-video progress,
  and a clip gallery grid with inline preview + download.
- **Runs CPU-only by default** (whisper `small`; set `WHISPER_MODEL=medium` or
  `large-v3` for better quality, or `tiny`/`base` for speed). The default was
  raised from `base` deliberately (`T1`): a mis-transcribed word is burned into
  the video, and captions are the most visible thing the tool produces.

---

## What it will do

Long video → transcribe → AI picks the best moments → cut clips → reformat to
vertical with face-tracking → add captions + effects → auto-generate
titles/hashtags → optionally auto-publish to Whop / YouTube / TikTok /
Instagram / X.

## Tech stack

| Area            | Choice                                             |
| --------------- | -------------------------------------------------- |
| Backend/worker  | Python 3.11+                                        |
| Web API         | FastAPI + uvicorn                                   |
| Video           | FFmpeg (ffmpeg-python / subprocess)                |
| Transcription   | faster-whisper (CPU, GPU when available)           |
| LLM             | Pluggable client (OpenAI, Anthropic, or Gemini via its OpenAI-compatible endpoint), key in env |
| Face tracking   | mediapipe + opencv-python + numpy                  |
| Emoji           | Twemoji PNGs                                        |
| Video download  | yt-dlp                                              |
| Queue           | In-process thread pool (single worker, so a batch runs in order). Redis + RQ is *planned*, not implemented — see the CHANGELOG |
| Frontend        | React + Tailwind (dark theme)                      |
| Storage         | Local folders behind a swappable interface (→ S3)  |
| Config          | pydantic-settings + `.env`                          |
| CI/CD           | GitHub Actions                                      |

## Project layout

Selected files, grouped by what they do. `worker/` holds 41 top-level modules and this lists
about half; the omissions are small helpers, not whole subsystems.

```
.
├── api/
│   ├── main.py                     # FastAPI app — 48 routes across 10 tag groups
│   └── security.py                 # shared-secret auth, /clips mount guard, rate limiter
├── worker/
│   │  # --- orchestration -------------------------------------------------
│   ├── pipeline.py                 # the render pipeline, stage by stage
│   ├── jobs.py                     # in-process job store + manager (get_manager is the RQ seam)
│   ├── job_persistence.py          # SQLite mirror so jobs survive a restart
│   ├── models.py                   # ProcessingOptions, Job, ClipResult — the data model
│   ├── cancellation.py             # cooperative cancel checkpoints
│   ├── observability.py            # per-job log attribution + per-stage timings
│   ├── watch_folder.py             # drop-a-file-in ingest (polling, size-debounced)
│   ├── rerender.py                 # re-render one clip with changed settings
│   │  # --- input ---------------------------------------------------------
│   ├── download.py                 # yt-dlp ingest + the SSRF guard (validate_public_url)
│   ├── ffmpeg_utils.py             # FFmpeg/FFprobe helpers; H264_COMPAT_ARGS lives here
│   ├── video_encoders.py           # hardware encoder probe + fallback (the other -crf site)
│   ├── output_profiles.py          # per-platform encode targets
│   │  # --- transcription -------------------------------------------------
│   ├── transcribe.py               # faster-whisper; Transcript / TranscriptSegment / Word
│   ├── transcript_cache.py         # keyed on file *content*, not path
│   ├── transcript_filter.py        # hallucination filtering
│   ├── transcript_trim.py          # cut-list geometry (cuts in, keeps out); no I/O
│   ├── clip_transcript.py          # a clip's words from cache; never runs ASR
│   ├── language.py                 # language detection (returns nothing for Han script)
│   ├── diarization.py              # speaker turns
│   │  # --- selection -----------------------------------------------------
│   ├── selection.py                # LLM "best moment" selection; ClipCandidate lives here
│   ├── candidate_ranking.py        # scoring, dedup, ordering
│   ├── selection_features.py       # speech rate (S4)
│   ├── audio_features.py           # energy envelope (S2)
│   ├── hook_score.py               # opening-seconds hook strength (S6)
│   ├── discourse.py                # structure / standalone / intensity signals
│   ├── scene_detect.py             # shot boundaries
│   ├── segmentation.py             # silence detection + fallback segmenting
│   ├── visual_selection.py         # frame-level signals
│   │  # --- captions ------------------------------------------------------
│   ├── captions.py                 # cue grouping, ASS build, burn-in
│   ├── caption_placement.py        # position choice, face avoidance
│   ├── caption_contrast.py         # legibility colours measured off the footage (C20)
│   ├── caption_preview.py          # two-second preset sample (C18)
│   ├── script_support.py           # reports caption_script_unsupported rather than substituting
│   ├── text_metrics.py             # measured text fit, for real wrapping
│   ├── subtitle_export.py          # SRT/VTT sidecars
│   ├── branding.py                 # brand kit; ASS &HAABBGGRR colour helpers
│   ├── metadata.py                 # titles / descriptions / hashtags
│   ├── llm_client.py               # pluggable OpenAI/Anthropic/Gemini client
│   ├── thumbnail.py                # scored thumbnail frame choice (V17)
│   ├── intermediate_cache.py       # memoised measurements keyed by source hash
│   ├── effects/
│   │   ├── compositor.py           # single-pass effect composition (the filter graph)
│   │   ├── overlays.py             # zoom/colour/fade/progress filter builders
│   │   ├── audio.py                # music beds, ducking, loudnorm, measure_loudness
│   │   ├── reframe.py              # face-tracking auto-reframe (on by default)
│   │   ├── emoji.py                # word-synced Noto emoji overlays
│   │   ├── broll.py                # b-roll overlays + licensing provenance
│   │   ├── caption_presets.py      # the preset table (drift-pinned)
│   │   ├── sfx.py                  # pop/click only — refuses "whoosh", deliberately
│   │   └── filler.py               # filler-word / pause removal
│   └── engines/                    # the AV engine plugin architecture (~10.8k lines)
│       ├── base.py                 # AV_Engine, Engine_Context/Result — the plugin contract
│       ├── host.py                 # gating ladder, budgets, failure isolation
│       ├── registry.py             # engine registration
│       ├── loader.py               # side-effect import that populates the registry
│       ├── capabilities.py         # ffmpeg capability probes (real-binary tested)
│       ├── artifacts.py            # per-clip workspaces + durable artifacts
│       ├── timebase.py             # one time base per clip
│       ├── kinetic.py              # kinetic typography engine (import-safe by design)
│       └── stems.py                # stem inpainting engine (degrades without demucs)
├── publishers/
│   ├── base.py                     # common publisher interface + PublishState
│   ├── preflight.py                # per-platform pre-upload checks (PB3, labelled O10)
│   ├── manager.py                  # routing + throttled scheduler
│   ├── history.py                  # SQLite clip/publish/campaign history (has a migration)
│   ├── retry.py                    # backoff policy
│   ├── tailoring.py                # per-platform metadata shaping
│   ├── best_times.py               # scheduling suggestions
│   └── {whop,youtube,tiktok,instagram,x}.py
├── publisher_bridge/               # Node @whop/sdk upload bridge
├── storage_backends/               # base + local + s3 + retention/cleanup
├── evaluation/                     # scoring harness (1.6k lines; no labelled data yet — S1/M4)
├── scripts/
│   ├── setup_dev_env.sh            # ffmpeg, Liberation fonts, opencv runtime libs
│   ├── smoke_reel.py               # render one clip with every effect on (M2)
│   ├── mutate.py                   # mutation testing: does a test notice a break?
│   ├── fetch_emoji.py              # --check asserts all 326 emoji are vendored
│   ├── docker_smoke.sh             # build the image and prove it serves
│   └── eval_{selection,transcription}.py
├── assets/
│   ├── fonts/                      # bundled caption faces — font files ONLY (libass reads all)
│   ├── fonts.json                  # the manifest; weight is on fontconfig's scale
│   ├── font-licenses/              # siblings, not children, of fonts/
│   └── emoji/                      # 326 vendored Noto PNGs
├── frontend/                       # React + Tailwind dashboard (Vite, vitest)
├── tests/                          # 2,076 tests; no skips, warnings are errors
├── storage/{uploads,temp,clips}/   # local storage (bind-mounted in Docker)
├── .github/workflows/
│   ├── ci.yml                      # lint, mypy, tests, coverage, smoke reel, docker, deploy
│   └── mutation.yml                # weekly + on-demand mutation run
├── config.py                       # pydantic settings — every field documented in .env.example
├── profiles.py                     # saved settings profiles
├── runtime_config.py               # settings changeable at runtime
├── updates.py                      # in-app update check
├── VERSION · CHANGELOG.md · requirements{,-dev,-ml}.txt · .env.example
├── docker-compose.yml · Dockerfile · render.yaml
├── openapi.json                    # the committed API surface — CI fails if it drifts
└── docs/
    ├── IMPROVEMENT_PLAN.md         # the backlog (an audit of v0.10.0 — read its banner)
    └── BACKUP_AND_RESTORE.md       # what to back up, how, and the restore traps
```

### Backing up your data

Self-hosting means your publish history, job records and rendered clips live on your own
disk, and nothing in this project backs them up for you. **[docs/BACKUP_AND_RESTORE.md](docs/BACKUP_AND_RESTORE.md)**
covers what is durable, what is regenerable, and the two ways a restore silently fails.

The short version, if you read nothing else:

* Two SQLite databases matter — `storage/jobs.db` and `storage/history.db`. `history.db` is
  the irreplaceable one: it is the only record of what was published where.
* **Do not back them up with `cp`.** Both run in WAL mode, so a copy taken while the app is
  running can be missing every recent commit — measured at 0 rows out of 59. Use
  `sqlite3 storage/jobs.db ".backup '/backups/jobs.db'"`, which is safe against a live writer.
* **When restoring, delete the old `-wal` and `-shm` files too.** Leaving them behind makes
  SQLite replay the stale log over your restored data, discarding the entire restore while
  `PRAGMA integrity_check` still reports `ok`.
* A `history.db` backup contains **live OAuth access tokens in plaintext**. Treat it like
  `.env`.

---

## Quick start (Docker)

Docker is the recommended way to run everything (API + worker in one container, with
FFmpeg baked into the image).

```bash
# 1. Configure
cp .env.example .env        # then fill in any keys you need

# 2. Build and run the stack
docker compose up --build

# 3. Open the app
#    Web UI (dashboard)   : http://localhost:8000
#    API docs (Swagger)   : http://localhost:8000/docs
#    Health check         : http://localhost:8000/healthz
```

The Docker image builds the React UI and serves it together with the API, so
`http://localhost:8000` is the full dashboard. Generated clips appear under
`./storage/clips` on your host (the `storage/` directory is bind-mounted), and
dropping a video into `./storage/watch` (a fixed path, not configurable) triggers watch-folder processing when
that mode is enabled.

## Testing

```bash
ruff check .                       # lint (blocking in CI)
pytest                             # warnings are errors; skips fail CI
python scripts/fetch_emoji.py --check   # the emoji the keyword map can emit are vendored
scripts/docker_smoke.sh            # build the image and check it serves the app
cd frontend && npm run lint && npm run test:run && npm run build
```

Two things about the suite are deliberate and worth knowing before you change it.

**Warnings are errors** (`filterwarnings = error` in `pyproject.toml`). A capability bug that made
one engine unreachable was invisible partly because nothing in the suite objected to anything.

**A skipped test fails CI.** ffmpeg, the fonts and the opencv runtime libraries are all installed by
the workflow, so a skip means a dependency silently went missing and the tests it gates have quietly
stopped running — which is exactly how an earlier ffmpeg gap went unnoticed for several releases.

### Mutation testing

A passing suite proves the tests agree with the code. It does not prove they would *disagree* with
the wrong code, and for most of what this project does that is the distinction that matters: a
ranking change produces a plausible ordering, an emoji on the wrong word still renders, a caption in
a substituted font still encodes. None of those raise, so a test that merely exercises the path
passes either way.

`scripts/mutate.py` breaks the code on purpose and checks that something notices:

```bash
# one inline mutation
python scripts/mutate.py --file worker/captions.py \
    --old 'if not cue.words:' --new 'if False:' \
    -- pytest tests/test_captions.py -q -x

# a batch, defined next to the tests it belongs to
python scripts/mutate.py --spec tests/mutations/example.json
python scripts/mutate.py --spec tests/mutations/example.json --list
```

- **CAUGHT** — a test failed. The behaviour is genuinely pinned.
- **ESCAPED** — everything passed. Either the tests do not cover the behaviour, or the mutation was
  *equivalent* and changed nothing observable.

Those two need different fixes, and the difference matters. A missing test wants a test. An
equivalent mutant usually means the same fact is stated in two places, so changing one has no
effect — and the right response is to remove the redundancy, not to mark the mutation as expected.
Both happened while building the current batches: `SFX_NAMES` and `synth_filter` each independently
decided whether a sound effect could be synthesised, and `plan_hits` interpreted its mode in three
places, which made its own guard provably dead code.

Every escape found so far has pointed at something real — a leaked file descriptor on each
unreadable font file, an overlay box that was only even-sided at one frame width, a compositor
wiring that could be replaced with an empty list while every unit test still passed.

Keep each mutation small. One flag, one comparison, one constant: a large mutation that gets caught
tells you nothing about which part of it was noticed. Anchors must match exactly once — the tool
refuses an ambiguous or stale one rather than guessing, and it restores the working tree on every
exit path including a signal.

### System dependencies for the test suite

```bash
bash scripts/setup_dev_env.sh
```

Installs ffmpeg/ffprobe, the Liberation fonts and the opencv runtime libraries, and registers the
bundled caption faces with fontconfig the way the Dockerfile does. Idempotent, and it prints what it
found so a partial setup is visible immediately.

Each of those is load-bearing rather than convenience. Without ffmpeg the real-binary capability
tests cannot run, and they exist to catch the probe bugs every mocked test misses. Without
`libGL`/`glib2`, `import cv2` raises and every vision path silently takes its degraded branch —
CI installed opencv and got no coverage from it for a while for exactly this reason. And the
capability probe reads the *system* font list, so without the bundled faces installed
`font_available()` disagrees with what will actually render.

## Local development (without Docker)

**Backend:**

```bash
python -m venv .venv && source .venv/bin/activate
# The locks are what CI and the Docker image install, so this gives you the same
# versions they test against. Use requirements.txt / requirements-dev.txt instead
# only when you are deliberately resolving newer versions.
pip install --require-hashes -r requirements-dev.lock   # or requirements.lock, runtime only
cp .env.example .env
uvicorn api.main:app --reload          # http://localhost:8000
```

> FFmpeg must be installed and on your `PATH` for video features (installed
> automatically inside the Docker image). The first run downloads the whisper
> model, which defaults to `small`; set `WHISPER_MODEL=tiny` or `base` for a
> faster first start, or `medium`/`large-v3` for higher transcription quality.

**Optional: real stem separation (`stem_inpainting`).** `torch` and `demucs` are
deliberately *not* in `requirements.txt` — torch alone is several hundred megabytes, and
the engine is built to work without it. With them absent it falls back to an ffmpeg
approximation (`music := clip − vocals`) and records a `degraded:python_pkg:demucs`
marker, so a stock install still works, just with cruder separation. To enable the real
thing you need **both** halves:

```bash
# 1. the packages (CPU-only wheels keep the download small)
pip install -r requirements-ml.txt --extra-index-url https://download.pytorch.org/whl/cpu

# 2. the checkpoint, on disk — one of:
#      models/stems/htdemucs.th          (single file)
#      models/stems/htdemucs/model.th    (directory)
#    override the location with CLIPPER_STEM_MODEL_DIR
```

The checkpoint is a separate step on purpose: the engine treats a model that would have
to be downloaded as *unavailable*, so that checking a capability can never turn into a
silent network fetch. Confirm both are in place via `capabilities.stem_inpainting` in
`GET /api/info`; until the checkpoint exists, the `spectral` repair mode stays disabled
in the UI with a "needs local model" hint. In Docker, build with
`--build-arg INSTALL_ML=true` to include the packages.

**Frontend:**

```bash
cd frontend
npm ci                                 # exactly the lockfile; see Dependencies & supply chain
npm run dev                            # http://localhost:5173 (proxies /api)
```

---

## One-click / cloud deploy

The stack is a standard Docker app, so it deploys to any container host:

- **Render (Blueprint):** this repo ships a [`render.yaml`](./render.yaml)
  blueprint. In Render choose **New + → Blueprint**, point at the repo, and set
  the secret env vars (LLM/publisher keys, and `S3_*` if `STORAGE_BACKEND=s3`).
  A persistent disk is mounted at `/app/storage`.
- **Railway / Fly.io:** point the platform at this repo; it builds the
  `Dockerfile` (single container serving the UI + API). Set the environment
  variables from `.env.example`. Give the instance 2 GB+ for whisper and FFmpeg.
- **AWS / GCP / Azure:** run the image on ECS/Fargate, Cloud Run, or Container
  Apps and switch `STORAGE_BACKEND=s3` for durable clip storage.

**Auto-deploy from `main`:** the CI workflow has a `deploy` job that fires on
pushes to `main` after tests pass. Add a repository secret with your provider's
deploy hook to enable it (the step is skipped when unset):

- `RENDER_DEPLOY_HOOK_URL` — Render service **Settings → Deploy Hook**
- `RAILWAY_DEPLOY_HOOK_URL` — Railway deploy webhook

---

## Updating

Running with Docker (recommended) — one command pulls the latest code and
rebuilds:

```bash
git pull && docker compose up --build
```

The app checks GitHub for newer releases and shows an **"update available"**
banner in the UI (and on the **Settings** tab) when your `VERSION` is behind the
latest release. Set `UPDATE_CHECK_ENABLED=false` to disable the check, or
`GITHUB_REPO=owner/name` to point it at a fork.

### How a release happens

Releases are cut by the [`release`](./.github/workflows/release.yml) workflow
when **`VERSION` changes on `main`**. It tags `v<version>`, then publishes a
GitHub release whose notes are the matching `CHANGELOG.md` section. So the
process for a release is: update `VERSION`, add the changelog section, merge.

This exists because the update check above could not previously work. The
repository had **no git tags and no GitHub releases**, while `updates.py` asks
GitHub for `releases/latest` — which 404s when none exist. The banner, its API
route and its tests were all in place and permanently inert, and `CHANGELOG.md`
documented eleven released versions, so the project looked like it had been
releasing all along.

`VERSION` drives it rather than a hand-pushed tag because `VERSION` is already
the value the app reports about itself. Two sources for one number is how the
running version and the version it compares against drift apart, which would
make the banner lie rather than merely stay quiet. The workflow refuses a
`VERSION` that is not a bare semver triple, and re-running it on an
already-tagged version does nothing rather than failing.

---

## Dependencies & supply chain

Every dependency set is pinned, hash-verified, and kept moving by automation.

**Python** — `requirements.txt` declares *intent* (version ranges);
`requirements.lock` decides what is *installed*. The lock pins all 73 packages of
the transitive closure to an exact version and hash, and both the Dockerfile and
CI install it with `pip install --require-hashes`, so a substituted artifact on
the index fails the build instead of being installed. Regenerate after editing
either requirements file:

```bash
uv pip compile --universal --generate-hashes --python-version 3.11 \
  --output-file requirements.lock requirements.txt
uv pip compile --universal --generate-hashes --python-version 3.11 \
  --output-file requirements-dev.lock requirements-dev.txt
```

`uv` is in `requirements-dev.txt`, so an existing dev environment has it. CI
regenerates both and fails if the result differs from what is committed, so a
stale lock cannot merge.

**Node** — both `package.json` files have committed lockfiles and both the
Dockerfile and CI use `npm ci`, never `npm install`. `npm install` may resolve
past the lockfile and rewrite it, which meant an image could ship a dependency
tree that no test run had ever seen.

**`pip-audit` blocks the build**, and audits the lock rather than the ranges —
"is the version we ship safe?" rather than "could a safe version satisfy this?".
Turning it from advisory to blocking immediately found something: **17
vulnerabilities in `pillow` 10.4.0**, every one fixed in 12.x, and therefore
unreachable while the `<11.0` ceiling stood. An upper bound meant to prevent an
unreviewed upgrade was instead holding the project on known heap-corruption and
out-of-bounds-write defects in the image and font parsers that this app feeds
arbitrary downloaded media through. That floor is now `12.3`.

**One deliberate exception:** `yt-dlp` has no upper bound. It exists to track
sites that change their players without notice, so a ceiling would turn "someone
must review an upgrade" into "URL ingest silently stops working". The lock still
pins its exact version and hash, so builds stay reproducible; the range only
governs what a deliberate upgrade may pick.

**[Dependabot](./.github/dependabot.yml)** covers all five surfaces — pip, the
frontend, the publisher bridge, the workflow actions, and the Docker base images.
Pinning without a way to move the pins is how `pillow` accumulated 17 advisories.
Note that a Dependabot PR which moves a Python range **will fail** the
"Locks match their requirements files" check until the locks are regenerated with
the commands above; that failure is the reminder.

**[CodeQL](./.github/workflows/codeql.yml)** analyses both languages weekly and
on every PR. It looks for something no other check here can see: a taint path
through this repo's own code. `ruff` and `mypy` reason about one module at a time,
and the audit tools only know about other people's published vulnerabilities —
but this app takes a caller's URL and hands it to yt-dlp, and builds ffmpeg
filter arguments influenced by request data.

The image also **asserts what it got** rather than pinning an apt version:
ffmpeg's major version and the presence of the `subtitles` filter (libass) are
checked at build time, and again in CI alongside `import cv2`. The Debian version
is deliberately not pinned — Debian rotates point releases out of the archive, so
an exact pin trades a silent-change risk for a certain-breakage one. Verified by
building with `--build-arg FFMPEG_MIN_MAJOR=99`, which fails as intended.

---

## ⚠️ Content & copyright — please read

**You are solely responsible for having the rights to any source footage you
process with this tool.** Only clip content you own or are licensed/authorized
to use. When in doubt, don't.

- **YouTube "reused / repurposed content":** YouTube's monetization policies
  restrict content that is repetitious or reused from others without significant
  original commentary, editing, or transformation. Simply re-cutting someone
  else's video can make a channel ineligible for the YouTube Partner Program or
  cause clips to be demonetized. Add genuine original value and review the
  current YouTube monetization / reused-content policies before publishing.
- **Background music:** Most commercial and popular music is copyrighted. Using
  it — even briefly in the background — can trigger Content ID claims, muting,
  takedowns, demonetization, or strikes across platforms. Use royalty-free,
  properly licensed, or original music, and keep records of your licenses.
- **Platform rules vary:** Each destination (Whop, YouTube, TikTok, Instagram,
  X) has its own terms of service, copyright, and monetization rules. It is your
  responsibility to comply with them for every clip you publish.

This tool does not grant you any rights to third-party content and does not
provide legal advice. If you're unsure about the rights to specific footage or
music, consult a qualified professional.

---

## License

TBD.
