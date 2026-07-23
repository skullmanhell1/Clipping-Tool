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
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install Python dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy the application source.
COPY . .

# Bring in the built SPA from the frontend stage so FastAPI serves it at "/".
COPY --from=frontend /ui/dist ./frontend/dist

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
