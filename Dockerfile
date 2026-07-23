# =============================================================================
# Backend + worker image for the AI Video Clipper.
# Bundles FFmpeg (required for all video operations) with the Python app.
# The same image runs both the API and the RQ worker; the command is chosen in
# docker-compose.yml.
# =============================================================================
FROM python:3.11-slim

# System dependencies:
# - ffmpeg: video/audio processing
# - libgl1 / libglib2.0-0: runtime libs required by opencv / mediapipe
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

EXPOSE 8000

# Default command runs the API. The worker service overrides this in compose.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
