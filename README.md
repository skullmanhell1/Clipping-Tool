# AI Video Clipper

Turn long-form video into short, vertical, captioned clips — and (optionally)
auto-publish them.

> **Status:** v0.12.4 — **Phases 1–5 + the Tier 1 creator-output upgrade + speaker diarisation & multi-speaker reframe are working.** Paste a URL or upload video,
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
  tool synthesises a soft two-tone fallback (not a commercial song) and mixes it
  under the speech.
- **Face-tracking auto-reframe:** detects and follows the main speaker so the
  vertical crop glides with them (replaces the static crop); falls back to the
  blurred-background reformat when no face is found.
- **Auto-emoji:** word-synced Twemoji overlays with a keyword map **or** an AI
  context-aware mode, four intensity levels, and an optional pop animation.
- **Filler-word removal:** cuts "um"/"uh" and long pauses, keeping captions and
  emoji in sync by rebasing the word timeline.
- **Caption Template & Position:** karaoke / boxed / minimal plus animated `pop`,
  `typewriter`, and `hormozi` presets, at bottom / center / top.

> A selected non-vendored emoji style can download PNGs from its configured CDN
> on first use (cached under `assets/emoji`); set `EMOJI_ALLOW_DOWNLOAD=false`
> to use only local assets. Noto is vendored by default.
> Burned-in text uses libass — the Docker image installs `fonts-liberation` so a
> font is always available.

## Meme mode, unified campaign presets, and quality delivery

The creator workflow now includes an opt-in **Meme mode** panel. It stores a clip-relative timeline
of SFX, visual beats, caption emphasis, and image/GIF/video/text overlays. Presets (`Chaotic`,
`Subtle`, and `Gaming Rage`) expand into reviewable events; auto-suggestions can use transcript
keywords, energy peaks, silence boundaries, and scene cuts. Each event has a stable ID, exact start
and duration, volume, and ducking amount, so it can be edited, removed, or undone without changing
neighboring events. Built-in tones are generated locally; uploaded files are stored through
`POST /api/meme-assets/upload` and are marked `user_supplied`/`license: unknown` until the operator
records provenance.

Campaigns use one versioned preset workflow. The Campaign panel imports multiple Google Docs/Drive
links, folders, pasted text, or multiple local requirement files, then exposes one editable preset for
clipping, editing tools, captions/brand rules, source approvals, compliance, and posting. Local TXT,
Markdown, CSV, JSON, and YAML files are read in the browser (2 MB each, 10 per intake); raw file objects
stay local while extracted text goes through the review-only importer. Structured JSON is review-only
until the operator chooses Save & apply. The same review step now includes an optional
creator-studio blueprint for content type, clip goal, audience, hook approach, selection direction,
retention ranking, target clip count, story-first assembly, active-speaker framing, b-roll intent, and
the actual caption, framing, sourcing, and visual-selection recipe used by the clip engine. Editable
quick-start recipes cover podcast growth, education authority, product launches, and clean storytelling;
they fill the review draft but never bypass campaign locks or Save & apply.
Review and Save & apply also show a readiness preflight for identity, output format, distribution,
strategy, source approvals, and policy checks, so blocked requirements are explicit instead of hidden.
The active preset also follows the clip into scheduling: manual-only campaigns disable direct auto
publishing, disallowed platforms are filtered out, and daily public scheduling requires compliance,
approved clips, required checklist completion, and distinct peak slots.
The API enforces the same platform allow-list, review acknowledgements, approved-clip state, valid slots,
and per-platform compliance before it queues any public attempts.
Omitted publish destinations resolve from the saved campaign routes before policy checks, and duplicate
publisher aliases are rejected instead of creating duplicate attempts.
Campaign posting targets can be configured from 1 to 10 clips per day, with matching distinct peak
slots. Compliance preview can check either draft copy or a selected rendered clip.
Clip review also supports status filters and filter-scoped batch actions, while campaign application
selects the first configured platform output profile automatically.
Reviewers can choose and save a precise thumbnail frame for each rendered clip.
Review decisions also support per-clip notes and shared notes for batch actions.
Selection protects against mid-thought openings and dangling endings, and caption rendering uses
measured wrapping plus automatic TikTok, Reels, and Shorts safe-area margins when an output target
is selected.
Visual selection now compares motion and brightness changes against the source baseline instead of
rewarding footage merely because it is globally bright. Review cards show the resulting quality axes.
The compliance preview also checks the current unsaved campaign draft, including source decisions.
The same strategy and normalization rules are applied server-side for API, upload, and watch-folder
submissions, and local footage is confirmed at publish time when a campaign source allow-list is active.
Required fields are warnings until resolved or explicitly dismissed; channels and discovered videos are
always `suggested`, never auto-approved. Saved presets support switch, rename, duplicate, delete, force
re-sync, JSON export/import, original-source links, last-synced timestamps, and changed-since-sync
indicators. See [`docs/UNIFIED_CAMPAIGNS.md`](docs/UNIFIED_CAMPAIGNS.md) for the schema, safe file
intake, source-review rules, and endpoint map.

The canonical API is `/api/campaign-presets`; `/api/campaign-profiles/*` remains a compatibility
adapter for older clients. The stored JSON uses `schema_version: 2` and keeps the source library
separate from the brief's original links. Private sources require server-side read-only Drive OAuth;
public links use a constrained fallback. Credentials never enter the browser bundle or Git.

Minimal shape of a saved preset:

```json
{
  "schema_version": 2,
  "name": "Creator launch",
  "aspect_resolution": {"aspect_ratios": ["9:16"], "resolutions": [{"width": 1080, "height": 1920}]},
  "posting": {"platforms": ["youtube_shorts", "tiktok"], "manual_only": false},
  "source_library": [{"url": "https://youtu.be/example", "type": "youtube_video", "status": "suggested"}],
  "settings": {"clip_length": "30-60s", "caption_preset": "karaoke"},
  "compliance": {"required_checks": []},
  "warnings": []
}
```

Quality delivery is per job and remains opt-in: 720p/1080p/1440p/4K targets, smart crop/fit, denoise,
subtle sharpening, mild correction, 30/60 fps policy, optional interpolation, platform-aware
bitrate/codec selection, hardware-encoder probing with CPU fallback, and a before/after quality
record on each clip. Upscaling only resizes or filters existing pixels; it cannot recreate detail
that was never captured. Enhanced face tracking adds confidence filtering, zero-phase smoothing,
shot-cut resets, active-speaker association, bounded split-screen, and safe static/blur fallbacks.

All new behavior is backward-compatible and opt-in. Use only owned, commissioned, public-domain,
or individually licensed assets; see [`docs/ASSET_PROVENANCE.md`](docs/ASSET_PROVENANCE.md) before
adding sounds, reaction media, logos, or fonts to a campaign.

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
- **YouTube OAuth preflight** — Publishing settings can exchange the configured refresh token,
  confirm the `youtube.upload` scope when Google returns it, and report an actionable error before
  a real upload is queued. The resumable upload sends the bearer token and exact file length on both
  the session-initiation and binary-upload requests.
- **Multi-platform direct scheduler** — the Schedule workspace can push one completed clip now or
  queue it for TikTok, YouTube Shorts, and Instagram Reels together. Saved campaign routes are
  reused, Shorts stays distinct from ordinary YouTube, and the picker surfaces each destination's
  output size and duration ceiling before the request is queued.
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

## Retention-first engagement editing

The selector can now combine hook quality, delivery changes, emotional and
question/answer structure, optional pitch variation, and source-relative
audience reaction events. Choose **Balanced** or **Aggressive** under Advanced
settings to blend those measurements into ranking and generate a reviewable
retention plan containing:

- a first-three-seconds hook score and transcript-faithful hook text;
- bounded zoom accents at energy, pitch or reaction events;
- deterministic caption emphasis indices;
- optional pacing cuts over long dead-air gaps; and
- up to four low-level emphasis sound effects in Aggressive mode;
- review-card quality axes for **Flow**, **Impact**, **Clarity** and creator-topic
  **Relevance**, plus an ending/payoff check to avoid clips that trail off.

Reaction input is optional JSON such as
`[{"time":42.5,"count":18},{"time":43.1,"count":7}]`, where timestamps
are relative to the original source video. Invalid entries are ignored. The
planner is offline and deterministic, and recalculates cues after trimming so
the times shown on a clip describe the delivered file. See
[`docs/RETENTION_EDITING.md`](docs/RETENTION_EDITING.md) for the scoring model,
prompt instructions, edit workflow and review guidance.

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
│   ├── campaign_profile.py         # versioned campaign preset model/importer
│   ├── campaign_preset.py          # deterministic policy parser/checker
│   ├── campaign_profile_store.py   # saved preset persistence + migration
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

## Safe setup checklist

The default install is intentionally local-first. Start with a local upload, keep
external asset downloading disabled, and add credentials only for the feature you
are ready to use.

1. Copy `.env.example` to `.env`. Keep secrets in `.env` or your deployment
   secret manager; never put them in source, a campaign brief, browser fields, or
   a pull request.
2. Leave `BROLL_ALLOW_DOWNLOAD=false`, `EMOJI_ALLOW_DOWNLOAD=false`, and
   `SFX_MODE=off` unless you have reviewed the provider and asset terms.
3. For a fully local run, set `permissibility_mode` in the UI. It suppresses
   added music and forces b-roll sourcing to `local_only`.
4. Add only footage and assets you own, commissioned, or have a documented
   license to use. Record each asset in
   [`assets/asset-manifest.example.json`](assets/asset-manifest.example.json),
   following [`docs/ASSET_PROVENANCE.md`](docs/ASSET_PROVENANCE.md).

The app does not ship or fetch ripped Vine/Bruh clips, TV/movie audio, or
unlicensed meme templates. Meme suggestions are plans for review, not a claim
that media was sourced. If an optional asset, model, provider, or encoder is
unavailable, the clip continues through the documented fallback path and the
sidecar records a degradation marker where applicable.

## Meme mode and licensing-safe assets

Campaign storytelling can produce deterministic `meme_overlays` suggestions
such as a plot-twist or overwhelmed reaction. These suggestions contain a time
window, concept, prompt, and `asset_required` status; they do **not** download,
embed, or imply ownership of a reaction image, screenshot, movie still, or
sound. Review the plan, then supply a user-owned or licensed asset yourself.

For a safe local workflow:

```text
assets/
├── broll/                 # operator-supplied b-roll or meme templates
├── music/                 # operator-supplied licensed tracks
├── sfx/                   # operator-supplied whoosh/swipe/etc.
└── asset-manifest.json    # provenance record (copy the example template)
```

Use `BROLL_DIR=./assets/broll`, `asset_sourcing_mode=local_only`, and a
`tags.json` file when you want deterministic local matching. External b-roll is
opt-in only: it requires a provider, its API key/base URL, and
`BROLL_ALLOW_DOWNLOAD=true`; unknown-license or failed assets are skipped.
The clip record's `broll_assets` list contains provenance only for assets that
were actually composited. See [Asset provenance and safe sourcing](docs/ASSET_PROVENANCE.md)
for the manifest fields, review steps, and compatible-license guidance.

Sound effects follow the same rule. `pop` and `click` are generated locally and
are safe defaults when requested. `whoosh` and `swipe` are not faked: provide a
licensed file in `SFX_DIR` or the sting is skipped with `sfx_missing:<name>`.
Background music uses a user-supplied track first; otherwise the optional
fallback is a synthetic two-tone drone, not a commercial song, and is marked
`music_degraded:synthesised`. `permissibility_mode` removes added audio.

## Campaign brief import and publishing credentials

Campaign import is reviewable and deterministic:

- Paste a brief in **Campaigns → Campaign brief**, or provide one or more Google Drive/
  Google Docs file or folder links. General channel browsing is discovery-only and never
  approves a source.
- A Drive import runs in the server process, not through the assistant's
  connected Drive account. The importer tries OAuth first, then a public text
  export for link-accessible files. Private files need Viewer access for the
  server account; the paste path remains available when OAuth is not configured.
- Review the parsed rules before applying a replacement brief. Exact supported
  duration ranges—including **20–60 seconds**—update the clip-length setting.
  Parsing or applying a brief does not create a publishing campaign or publish
  anything automatically.
- A changed brief keeps the last good snapshot if a later read fails. A successful
  change flags affected clips for re-render and human review; review the new
  render before publishing.

Use the least-privilege credentials needed by the connector:

| Feature | Required configuration | Minimum guidance |
| --- | --- | --- |
| Private Drive video/brief import | `GOOGLE_DRIVE_CLIENT_ID`, `GOOGLE_DRIVE_CLIENT_SECRET`, `GOOGLE_DRIVE_REFRESH_TOKEN` | Use the read-only `https://www.googleapis.com/auth/drive.readonly` scope and grant the OAuth account Viewer access only to required files. |
| YouTube upload/Shorts | `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` | Use offline OAuth with `https://www.googleapis.com/auth/youtube.upload`; an API key cannot upload. A channel ID is optional. |
| TikTok | `TIKTOK_ACCESS_TOKEN`, `TIKTOK_OPEN_ID` | Request only the approved Content Posting/upload permission shown for your app; Direct Post approval is separate and is off until `TIKTOK_DIRECT_POST_APPROVED=true`. |
| Instagram Reels | `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_ACCOUNT_ID` | Use a Professional account and the `instagram_content_publish` permission. App review/approval is separate from configuring the token. |
| X | `X_ACCESS_TOKEN`, `X_ACCOUNT_ID` | Use the narrowest approved user-context media/posting permission for the app's access tier; direct posting remains off until explicitly approved. |

OAuth refresh tokens and access tokens are credentials. Put them only in the
ignored `.env` file or a deployment secret store, rotate exposed credentials,
and use the publisher preflight/status screen before queuing a real upload.
Platforms may accept only draft/private/review submissions until app approval;
the UI reports that state rather than treating it as public success.

## Quality, output resolution, and upscaling limits

The pipeline can target a short side of **720, 1080, 1440, or 2160** with
`OUTPUT_SHORT_SIDE` (default `1080`). Higher output is useful only when the
source contains enough detail; it increases decode/encode time, disk usage,
upload size, and platform re-encoding cost. A 720p source enlarged to 1080p or
2160p is an upscale, not a recovery of lost detail: it cannot restore focus,
compression detail, or a source frame that was never captured. There is no
AI/super-resolution pass in the shipped pipeline.

For the software encoder, lower `X264_CRF` means better quality and larger
files; `18–23` is the practical range. `X264_PRESET` trades render time against
compression efficiency. `SCALER_FLAGS` applies one resampling policy to every
scale operation; `bicubic` is the shipped default, while measured downscale
results did not justify changing it to `lanczos`. `OUTPUT_MAX_BITRATE_KBPS`
caps busy final clips so a quality target does not create an upload-rejected
file. `OUTPUT_PLATFORM` can fill a platform profile's resolution, bitrate, and
duration ceiling, but it renders one variant, not one file per platform.

`VIDEO_ENCODER=auto` may use a working hardware encoder, but availability is
checked with a real frame encode and unusable hardware falls back to
`libx264`. Hardware encoders are not pixel-comparable to x264 at the same
nominal number. Final output remains the compatibility-oriented 8-bit H.264 /
`yuv420p` contract described in `.env.example`; source quality, color, motion,
captions, and platform processing still limit what viewers see.

## Facial tracking, diarisation, and privacy

Face-tracking reframe samples frames locally and follows face geometry; it is
not face recognition and does not identify a person. Haar is the default CPU
detector. `FACE_DETECTOR_BACKEND=mediapipe` uses the vendored BlazeFace model
under `assets/models` and can help with profile or distant faces. The optional
speaker-aware path uses offline transcript timing as a speaker proxy, capped at
two speakers by default; it is not an identity system.

Treat source video and sampled frames as personal data. Process only footage
where you have the necessary consent/authority, restrict access to `storage/`,
and set `INTERMEDIATE_CACHE_ENABLED=false` when persistent sampled-frame caches
are not needed. Temporary files follow `AUTO_DELETE_TEMP`; source videos are
never auto-deleted. Do not publish a crop that reveals a private person or
unexpected bystander merely because a detector found a face.

The fallback ladder is deliberately safe: MediaPipe/model/runtime failure falls
back to Haar, no usable face falls back to the static blurred/background
reformat, and speaker-aware failure falls back to single-speaker reframe and
then the static crop. The clip is still produced and its sidecar/effects
markers identify meaningful degradation. Higher sampling (`REFRAME_SAMPLE_FPS`,
`REFRAME_SAMPLE_CAP`) improves responsiveness at a real CPU/decode cost; lower
sampling is faster but can miss fast motion.

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

Installs ffmpeg/ffprobe, the Liberation fonts and the OpenCV/MediaPipe runtime libraries (`libGL`,
`glib2`, EGL and GLES), and registers the bundled caption faces with fontconfig the way the
Dockerfile does. Idempotent, and it prints what it found so a partial setup is visible immediately.

Each dependency is load-bearing rather than convenience. Without ffmpeg the real-binary capability
tests cannot run, and they exist to catch the probe bugs every mocked test misses. Without
`libGL`/`glib2`, `import cv2` raises and every vision path silently takes its degraded branch —
CI installed opencv and got no coverage from it for a while for exactly this reason. MediaPipe's
Python API can import without `libEGL`/`libGLES`, but task construction cannot; the detector then
silently falls back to Haar. And the capability probe reads the *system* font list, so without the
bundled faces installed `font_available()` disagrees with what will actually render.

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

Campaign preset parsing, storytelling plans, compliance checks, and the exactly-two-public-clips
daily scheduler are documented in [`docs/CAMPAIGN_AUTOMATION.md`](docs/CAMPAIGN_AUTOMATION.md);
the saved preset schema and canonical route map are in
[`docs/UNIFIED_CAMPAIGNS.md`](docs/UNIFIED_CAMPAIGNS.md).
After updating, keep `.env` and `storage/` intact, rebuild the image, and confirm that each
platform reports direct-public approval before using the campaign scheduler.

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

## Legacy campaign migration

On first load of the configured campaign preset store, the previous BOXABL source list and review
rules are seeded once as the ordinary saved preset `BOXABL Casita`. It can then be edited, duplicated,
re-synced, or deleted like any other preset; the renderer and publisher consume the active preset's
generic policy fields rather than a permanent BOXABL runtime branch. See
[`docs/BOXABL_CAMPAIGN_SETUP.md`](docs/BOXABL_CAMPAIGN_SETUP.md) for the original Drive and YouTube
OAuth setup steps. YouTube uploads require OAuth refresh-token credentials; a public API key alone
cannot upload.

---

## License

TBD.
