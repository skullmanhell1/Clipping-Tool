# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- Video ingest (upload + yt-dlp download)
- Transcription (faster-whisper)
- AI moment selection + metadata generation
- FFmpeg cutting, vertical face-tracking reframe, captions, emoji & overlays
- Auto-publishing to Whop / YouTube / TikTok / Instagram / X
- S3 storage backend + retention cleanup
- React + Tailwind dashboard (upload, job status, clip gallery)

## [0.1.0] - 2026-07-23

### Added
- Initial project scaffold (foundation only; no features implemented).
- `config.py` pydantic settings + `.env.example` covering LLM, transcription,
  storage/S3, and all publisher credentials.
- Backend package skeleton: `api/`, `worker/` (+ `worker/effects/`),
  `publishers/`, `storage_backends/` with documented stubs.
- FastAPI app that boots and serves a dark-themed placeholder page plus
  `/healthz` and `/api/info` endpoints.
- React + Tailwind dark-themed frontend skeleton.
- `docker-compose.yml` bundling the app (with FFmpeg), a worker, and Redis.
- GitHub Actions CI workflow (lint + import/boot smoke check).
