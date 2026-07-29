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
FROM node:20-slim AS frontend
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# --- Stage 2: python runtime -------------------------------------------------
FROM python:3.11-slim

# System dependencies:
# - ffmpeg: video/audio processing (probe, cut, reframe, captions burn)
# - libgl1 / libglib2.0-0: runtime libs required by opencv / mediapipe (later)
# - nodejs / npm: runs the @whop/sdk publisher bridge (publisher_bridge/whop.mjs)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        fonts-liberation \
        fontconfig \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/*

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

# Install the Whop publisher bridge (Node @whop/sdk) with its own layer cache.
COPY publisher_bridge/package*.json ./publisher_bridge/
RUN cd publisher_bridge && npm install --omit=dev

# Copy the application source.
COPY . .

# Bring in the built SPA from the frontend stage so FastAPI serves it at "/".
COPY --from=frontend /ui/dist ./frontend/dist

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
