# AI Video Clipper

Turn long-form video into short, vertical, captioned clips — and (optionally)
auto-publish them.

> **Status:** v0.8.0 — **Phases 1–5 + the Tier 1 creator-output upgrade + speaker diarisation & multi-speaker reframe are working.** Paste a URL or upload video,
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
- **Runs CPU-only by default** (whisper `base`; set `WHISPER_MODEL=small` for
  better quality, or `tiny` for speed).

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

```
.
├── api/main.py                     # FastAPI app (health, info, placeholder page)
├── worker/
│   ├── tasks.py                    # re-export shim; currently unused (jobs.py is imported directly)
│   ├── ffmpeg_utils.py             # FFmpeg/FFprobe helpers
│   ├── transcribe.py               # faster-whisper transcription
│   ├── selection.py                # AI "best moment" selection
│   ├── metadata.py                 # titles / descriptions / hashtags
│   ├── captions.py                 # subtitle build + burn-in
│   ├── llm_client.py               # pluggable OpenAI/Anthropic client
│   └── effects/                    # Phase 4 visual effects
│       ├── overlays.py             # zoom/color/fade/progress filter builders
│       ├── audio.py                # mood music beds + mixing
│       ├── reframe.py              # face-tracking auto-reframe
│       ├── emoji.py                # word-synced Twemoji overlays
│       ├── filler.py               # filler-word / pause removal
│       └── compositor.py           # single-pass effect composition
├── publishers/
│   ├── base.py                     # common publisher interface + status
│   ├── history.py                  # SQLite clip/publish/campaign history
│   ├── manager.py                  # routing + throttled scheduler
│   └── {whop,youtube,tiktok,instagram,x}.py
├── publisher_bridge/               # Node @whop/sdk upload bridge
├── storage_backends/               # local + S3 + retention/cleanup
├── assets/emoji/                   # Twemoji PNGs
├── frontend/                       # React + Tailwind dashboard
├── storage/{uploads,temp,clips}/   # local storage (bind-mounted in Docker)
├── .github/workflows/ci.yml
├── config.py                       # pydantic settings
├── VERSION
├── CHANGELOG.md
├── requirements.txt
├── .env.example
├── docker-compose.yml
└── Dockerfile
```

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

## Local development (without Docker)

**Backend:**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # or requirements-dev.txt for tooling
cp .env.example .env
uvicorn api.main:app --reload          # http://localhost:8000
```

> FFmpeg must be installed and on your `PATH` for video features (installed
> automatically inside the Docker image). The first run downloads the whisper
> model; set `WHISPER_MODEL=tiny` for a fast first start or `small` for higher
> transcription quality.

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
npm install
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
