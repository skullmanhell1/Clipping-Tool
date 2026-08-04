"""Clip download routes (ZIP package and video-only)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from config import settings
from worker.jobs import get_manager

router = APIRouter()


# ---------------------------------------------------------------------------
# Clip downloads. The primary download is a ZIP containing video + metadata TXT.
# ---------------------------------------------------------------------------
def _clip_metadata_text(clip) -> str:
    return (f"Title\n{clip.title}\n\nCaption / Description\n{clip.description}\n\n"
            f"Hashtags\n{' '.join(clip.hashtags)}\n\nHook\n{clip.hook_text}\n\n"
            f"CTA\n{clip.cta}\n\nMentions\n{' '.join(clip.mentions)}\n")


@router.get("/api/clips/{job_id}/{filename}/download", tags=["clips"])
def download_clip(job_id: str, filename: str) -> StreamingResponse:
    safe_name = Path(filename).name
    path = Path(settings.clips_dir) / Path(job_id).name / safe_name
    job=get_manager().store.get(job_id)
    clip=next((c for c in job.clips if c.filename==safe_name),None) if job else None
    if not path.exists() or not path.is_file() or clip is None:
        raise HTTPException(status_code=404, detail="Clip not found")
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,"w",zipfile.ZIP_DEFLATED) as archive:
        archive.write(path,arcname=safe_name)
        archive.writestr(f"{Path(safe_name).stem}_metadata.txt",_clip_metadata_text(clip))
    buf.seek(0)
    return StreamingResponse(buf,media_type="application/zip",headers={
      "Content-Disposition":f'attachment; filename="{Path(safe_name).stem}_package.zip"'})


@router.get("/api/clips/{job_id}/{filename}/video", tags=["clips"])
def download_video_only(job_id: str, filename: str) -> FileResponse:
    safe_name = Path(filename).name
    path = Path(settings.clips_dir) / Path(job_id).name / safe_name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Clip not found")
    return FileResponse(path,filename=safe_name,media_type="video/mp4")
