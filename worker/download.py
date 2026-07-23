"""Video ingest: URL downloads (yt-dlp) and input classification.

Handles the two Phase 1 input sources:

* A remote video **URL** — downloaded with yt-dlp.
* A local **file** — already on disk (uploaded via the API or dropped into the
  watch folder); we just probe it.

:func:`fetch_metadata` performs a cheap, download-free info extraction so the UI
can show a preview card (title, duration, thumbnail, source) before processing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

ProgressCallback = Callable[[float, str], None]
"""A callback ``fn(fraction, message)`` where fraction is in ``[0, 1]``."""


class DownloadError(RuntimeError):
    """Raised when a URL cannot be fetched."""


@dataclass
class VideoMeta:
    """Lightweight metadata about a source video for preview cards."""

    title: str
    duration: Optional[float] = None
    thumbnail: Optional[str] = None
    source: Optional[str] = None      # webpage URL or filename
    uploader: Optional[str] = None


def is_url(value: str) -> bool:
    """Return whether ``value`` looks like an HTTP(S) URL."""
    return bool(_URL_RE.match(value.strip()))


def fetch_metadata(url: str) -> VideoMeta:
    """Fetch metadata for ``url`` without downloading the media.

    Raises:
        DownloadError: if info extraction fails.
    """
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # yt-dlp raises many subclasses
        raise DownloadError(f"Could not read video info: {exc}") from exc

    return VideoMeta(
        title=info.get("title") or "Untitled",
        duration=info.get("duration"),
        thumbnail=info.get("thumbnail"),
        source=info.get("webpage_url") or url,
        uploader=info.get("uploader"),
    )


def download_video(
    url: str,
    dest_dir: str | Path,
    progress_cb: Optional[ProgressCallback] = None,
    max_height: int = 1080,
) -> tuple[Path, VideoMeta]:
    """Download ``url`` into ``dest_dir`` and return ``(path, metadata)``.

    Args:
        url: The video URL.
        dest_dir: Directory to write the downloaded file into.
        progress_cb: Optional progress callback receiving ``(fraction, msg)``.
        max_height: Cap the downloaded resolution (keeps processing fast).

    Raises:
        DownloadError: on any download failure.
    """
    import yt_dlp

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    def _hook(d: dict) -> None:
        if progress_cb is None:
            return
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            frac = (done / total) if total else 0.0
            progress_cb(min(frac, 0.99), "Downloading video")
        elif d.get("status") == "finished":
            progress_cb(1.0, "Download complete")

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
        "format": f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best",
        "merge_output_format": "mp4",
        "progress_hooks": [_hook],
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))
            # merge_output_format may have changed the extension to .mp4
            if not path.exists():
                mp4 = path.with_suffix(".mp4")
                if mp4.exists():
                    path = mp4
    except Exception as exc:
        raise DownloadError(f"Download failed: {exc}") from exc

    if not path.exists():
        raise DownloadError(f"Downloaded file not found for {url}")

    meta = VideoMeta(
        title=info.get("title") or path.stem,
        duration=info.get("duration"),
        thumbnail=info.get("thumbnail"),
        source=info.get("webpage_url") or url,
        uploader=info.get("uploader"),
    )
    return path, meta
