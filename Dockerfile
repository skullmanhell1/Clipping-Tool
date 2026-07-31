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
FROM node:22-slim AS frontend
WORKDIR /ui
COPY frontend/package*.json ./
# `npm ci`, not `npm install`: CI runs `npm ci` against the committed lockfile, so using
# `npm install` here let the image resolve a *different* dependency tree than the one the
# frontend tests passed on. `ci` also fails loudly if package.json and the lockfile have
# drifted apart, which `install` silently reconciles.
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- Stage 2: python runtime -------------------------------------------------
FROM python:3.11-slim

# System dependencies:
# - ffmpeg: video/audio processing (probe, cut, reframe, captions burn)
# - libgl1 / libglib2.0-0: runtime libs required by opencv / mediapipe
#
# ffmpeg is deliberately NOT pinned to an apt version, and that is a considered choice
# rather than an oversight. Debian removes superseded versions from the archive, so
# `ffmpeg=7:5.1.6-0+deb12u1` builds fine today and fails to resolve the moment a security
# update lands - turning a working Dockerfile into a broken one with no code change. For a
# project that will not be actively maintained, a build that keeps working is worth more
# than a byte-identical ffmpeg.
#
# What protects against filter drift instead:
#   * `worker/engines/capabilities.py` probes ffmpeg for the filters it needs and records
#     `unavailable:<capability>` rather than assuming
#   * `worker/video_encoders.py` verifies an encoder by actually encoding a frame, because
#     `-encoders` lists what was compiled in, not what works
#   * the version is asserted at image-build time below, so a wildly different major
#     version is visible in the build log rather than discovered in a render
#
# Tested against: ffmpeg 7.0.2 (static) and Debian bookworm's 5.1.x.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        fonts-liberation \
        fontconfig \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -version | head -1 \
    && ffprobe -version | head -1

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
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

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
# Node itself unless INSTALL_WHOP_BRIDGE=true (I7) - `npm install` needs npm.
COPY publisher_bridge/package*.json ./publisher_bridge/
RUN if [ "$INSTALL_WHOP_BRIDGE" = "true" ]; then \
        cd publisher_bridge && npm install --omit=dev ; \
    fi

# Copy the application source.
COPY . .

# Bring in the built SPA from the frontend stage so FastAPI serves it at "/".
COPY --from=frontend /ui/dist ./frontend/dist

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
