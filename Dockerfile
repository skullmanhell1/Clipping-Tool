# =============================================================================
# Multi-stage build for the AI Video Clipper.
#
#   Stage 1 (frontend): build the React + Tailwind SPA -> frontend/dist
#   Stage 2 (runtime):  Python + FFmpeg app that also serves the built SPA
#
# In Phase 1 the API process runs the clip pipeline in-process (background
# thread pool), so a single container is sufficient.
# =============================================================================

# --- Stage 1: build the frontend --------------------------------------------
# Node 22: vite 8 requires ^20.19.0 || >=22.12.0 (I10). Pinned to the major rather than left on
# node:20-slim, where the requirement is satisfied only by the newest 20.x patch releases - so a
# base-image refresh could quietly stop satisfying it.
# `-bookworm-slim` rather than bare `-slim`: `-slim` follows Debian's *current* stable, so a
# Debian major release would change glibc and every system library under the build without a
# change here. Note the component order differs between these two images -- node publishes
# `22-bookworm-slim` while python publishes `3.11-slim-bookworm`, and the other spelling of
# either does not exist. Both were checked against the registry rather than assumed.
FROM node:22-bookworm-slim AS frontend
WORKDIR /ui
# Both files: the glob matches package-lock.json, which `npm ci` requires.
COPY frontend/package*.json ./
# `npm ci`, not `npm install`. CI already used `npm ci` while the image used `npm install`, so the
# two installed different trees from the same commit: `npm install` is free to resolve a newer
# version that satisfies the range and to rewrite the lockfile, `npm ci` installs the lockfile
# exactly and fails if it disagrees with package.json. The image was the one build nobody could
# reproduce.
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- Stage 2: python runtime -------------------------------------------------
# `-bookworm` for the same reason as the frontend stage, and here it also fixes **ffmpeg's**
# version: ffmpeg is installed from Debian's archive below, and a Debian release is the thing that
# decides which ffmpeg that is. Within bookworm it stays on the 5.1.x series; on `-slim` alone the
# next Debian stable would silently move it to 7.x, which changes filter defaults. `scripts/
# docker_smoke.sh` renders through the image so a jump that alters output is visible.
FROM python:3.11-slim-bookworm

# System dependencies:
# - ffmpeg: video/audio processing (probe, cut, reframe, captions burn)
# - libgl1 / libglib2.0-0: runtime libs required by opencv / mediapipe
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        fonts-liberation \
        fontconfig \
    && rm -rf /var/lib/apt/lists/*

# Optional: Node, needed *only* to run the Whop publisher bridge (publisher_bridge/whop.mjs)
# through @whop/sdk (I7).
#
# Off by default because Debian's nodejs+npm is around 200 MB of image for one optional
# publisher, and the Whop publisher already reports itself unavailable without it rather than
# failing a job - it checks for the node binary as well as the bridge script, so an image built
# without this reports "not_configured" instead of accepting a publish it cannot perform.
#
# Enable with:  docker build --build-arg INSTALL_WHOP_BRIDGE=true .
ARG INSTALL_WHOP_BRIDGE=false
RUN if [ "$INSTALL_WHOP_BRIDGE" = "true" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends nodejs npm \
        && rm -rf /var/lib/apt/lists/* ; \
    fi

# A2: register the bundled caption faces (assets/fonts, described by fonts.json) with
# fontconfig. Copied early and in their own layer so the cache survives source changes.
#
# This is belt-and-braces with the `fontsdir` option that `captions.subtitles_filter`
# passes to libass, and both are wanted: `fontsdir` covers a bare checkout and CI, while a
# system install is what lets fontconfig select *named instances* of a variable font.
# Verified with libass at -loglevel verbose: a request for "Montserrat" resolves to
# `Montserrat_700wght` when installed here, but silently falls back to NotoSans-Bold when
# only reachable through `fontsdir`.
COPY assets/fonts/ /usr/share/fonts/clipping-tool/
RUN fc-cache -f \
    # Fail the build rather than ship an image whose captions silently substitute. The
    # font chain has already been broken once by exactly this going unnoticed (C1).
    && fc-match Anton | grep -q Anton \
    && fc-match "Archivo Black" | grep -q ArchivoBlack \
    && fc-match "Poppins ExtraBold" | grep -q Poppins-ExtraBold

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install Python dependencies first for better layer caching.
#
# From the **lockfile**, with `--require-hashes`. `requirements.txt` states ranges, so installing
# it directly meant every rebuild re-resolved and could pick up a new minor of anything — the
# image was not reproducible from a commit. `--require-hashes` goes further than pinning
# versions: pip verifies the bytes it downloaded match what was resolved, so a re-uploaded or
# tampered index entry for a pinned version is rejected rather than installed.
#
# `requirements.txt` is copied too, only so `scripts/lock_requirements.sh --check` can run inside
# the image if needed; nothing installs from it.
COPY requirements.txt requirements.lock ./
RUN pip install --upgrade pip && pip install --require-hashes -r requirements.lock

# Optional: real source separation for the `stem_inpainting` engine (torch + demucs).
# Off by default because torch adds several hundred megabytes to the image, and the
# engine degrades to an ffmpeg approximation without it rather than failing. Enable
# with:  docker build --build-arg INSTALL_ML=true .
# Note the package is only half of it — a checkpoint must also be present at
# models/stems/htdemucs.th (or CLIPPER_STEM_MODEL_DIR), because the engine treats a
# model that would need downloading as unavailable by design.
ARG INSTALL_ML=false
COPY requirements-ml.txt ./
RUN if [ "$INSTALL_ML" = "true" ]; then \
        pip install -r requirements-ml.txt \
            --extra-index-url https://download.pytorch.org/whl/cpu ; \
    fi

# Install the Whop publisher bridge (Node @whop/sdk) with its own layer cache. Skipped along with
# Node itself unless INSTALL_WHOP_BRIDGE=true (I7) - `npm ci` needs npm.
COPY publisher_bridge/package*.json ./publisher_bridge/
RUN if [ "$INSTALL_WHOP_BRIDGE" = "true" ]; then \
        cd publisher_bridge && npm ci --omit=dev ; \
    fi

# Copy the application source.
COPY . .

# Bring in the built SPA from the frontend stage so FastAPI serves it at "/".
COPY --from=frontend /ui/dist ./frontend/dist

# Run as an unprivileged user. The container downloads arbitrary URLs with yt-dlp, shells out to
# ffmpeg with caller-influenced filter arguments, and writes files under /app/storage — so a
# defect in any of those three reached root, and root in a container is one namespace escape away
# from root on the host.
#
# A fixed UID/GID rather than whatever the base image allocates next: a bind-mounted host
# directory carries the host's ownership, so the number has to be knowable to be granted access.
# `docker-compose.yml` documents the one command that grants it.
#
# `/app/storage` is created and chowned here so the default (unmounted) case works out of the box:
# `settings.ensure_local_dirs()` runs at startup and would otherwise be creating directories under
# a root-owned /app. Only that subtree is chowned — the source stays root-owned and world-readable,
# so the running process cannot rewrite its own code.
# Not `--system`: that flag allocates from the low reserved range and warns when given an explicit
# UID above SYS_UID_MAX (999). The UID has to be an explicit high number so a bind-mounted host
# directory can be chowned to it, so the flag and the requirement are incompatible.
RUN groupadd --gid 10001 clipper \
    && useradd --uid 10001 --gid clipper --no-create-home --shell /usr/sbin/nologin clipper \
    && mkdir -p /app/storage \
    && chown -R clipper:clipper /app/storage

USER clipper

EXPOSE 8000

# Hits /healthz, which api.security exempts from the shared secret for exactly this reason — a
# health check has nowhere to hold a credential, and a probe that 401s reports the container as
# unhealthy forever.
#
# Uses python rather than curl because curl is not installed and adding it for one probe is 5 MB
# for something the interpreter already does. `--start-period` is generous because importing
# mediapipe, opencv and faster-whisper takes real time on a cold start, and a health check that
# fails during a normal boot causes a restart loop that looks like a crash.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
