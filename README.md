# AI Video Clipper

Turn long-form video into short, vertical, captioned clips — and (optionally)
auto-publish them. This repository currently contains the **project scaffold
only**: the folder structure, configuration, Docker setup, a booting API, and a
placeholder dark-themed UI. Features are implemented phase by phase in later
work.

> **Status:** v0.1.0 — foundation / stubs only. No processing features are
> implemented yet.

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
├── publishers/{base,whop,youtube,tiktok,instagram,x}.py
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
#    API + placeholder UI : http://localhost:8000
#    API docs (Swagger)   : http://localhost:8000/docs
#    Health check         : http://localhost:8000/healthz
```

Generated clips appear under `./storage/clips` on your host (the `storage/`
directory is bind-mounted).

## Local development (without Docker)

**Backend:**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # or requirements-dev.txt for tooling
cp .env.example .env
uvicorn api.main:app --reload          # http://localhost:8000
```

> FFmpeg must be installed and on your `PATH` for video features (installed
> automatically inside the Docker image).

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
  `Dockerfile` for the API and worker and provisions a managed Redis add-on.
  Set the environment variables from `.env.example` in the platform dashboard.
- **AWS / GCP / Azure:** run the image on ECS/Fargate, Cloud Run, or Container
  Apps with a managed Redis (ElastiCache / Memorystore) and switch
  `STORAGE_BACKEND=s3` for durable clip storage.

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
