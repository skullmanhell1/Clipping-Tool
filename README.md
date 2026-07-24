# AI Video Clipper

Turn long-form video into short, vertical, captioned clips — and (optionally)
auto-publish them.

> **Status:** v0.4.0 — **Phases 1, 2 & 3 are working.** Paste a URL or upload
> video, and the tool transcribes it, uses an **LLM to pick the most engaging
> moments** (with a virality score), reformats them to vertical (or 1:1 / 16:9 /
> 4:5) with a blurred-background fill, burns in word-timed captions,
> **auto-writes per-platform titles, descriptions & hashtags** you can edit or
> regenerate, and can **auto-publish** the finished clips to Whop, YouTube,
> TikTok, Instagram & X with campaign routing, scheduling, and a full history.

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

- **LLM highlight selection** (pluggable **OpenAI or Anthropic**, key from
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
| LLM             | Pluggable client (OpenAI or Anthropic), key in env |
| Face tracking   | mediapipe + opencv-python + numpy                  |
| Emoji           | Twemoji PNGs                                        |
| Video download  | yt-dlp                                              |
| Queue           | Redis + RQ (in-process fallback)                    |
| Frontend        | React + Tailwind (dark theme)                      |
| Storage         | Local folders behind a swappable interface (→ S3)  |
| Config          | pydantic-settings + `.env`                          |
| CI/CD           | GitHub Actions                                      |

## Project layout

```
.
├── api/main.py                     # FastAPI app (health, info, placeholder page)
├── worker/
│   ├── tasks.py                    # pipeline orchestration (RQ + fallback)
│   ├── ffmpeg_utils.py             # FFmpeg/FFprobe helpers
│   ├── transcribe.py               # faster-whisper transcription
│   ├── selection.py                # AI "best moment" selection
│   ├── metadata.py                 # titles / descriptions / hashtags
│   ├── captions.py                 # subtitle build + burn-in
│   ├── llm_client.py               # pluggable OpenAI/Anthropic client
│   └── effects/{reframe,emoji,overlays}.py
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

Docker is the recommended way to run everything (API + worker + Redis, with
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
dropping a video into `./storage/watch` triggers watch-folder processing when
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

**Frontend:**

```bash
cd frontend
npm install
npm run dev                            # http://localhost:5173 (proxies /api)
```

---

## One-click / cloud deploy

The stack is a standard Docker Compose app, so it deploys to any container host:

- **Render / Railway / Fly.io:** point the platform at this repo; it builds the
  `Dockerfile` (single container serving the UI + API) and runs it. Set the
  environment variables from `.env.example` in the platform dashboard. Give the
  instance enough memory/CPU for whisper and FFmpeg (2 GB+ recommended).
- **AWS / GCP / Azure:** run the image on ECS/Fargate, Cloud Run, or Container
  Apps and switch `STORAGE_BACKEND=s3` for durable clip storage.

> Phase 1 processes jobs in-process (a background thread pool). The
> distributed RQ + Redis worker — and a one-click deploy button — arrive in a
> later phase.

> A one-click deploy button (e.g. Render Blueprint / Railway template) will be
> added once the pipeline services are finalised.

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
