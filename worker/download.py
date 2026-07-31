"""Video ingest: URL downloads (yt-dlp) and input classification.

Handles the two Phase 1 input sources:

* A remote video **URL** — downloaded with yt-dlp.
* A local **file** — already on disk (uploaded via the API or dropped into the
  watch folder); we just probe it.

:func:`fetch_metadata` performs a cheap, download-free info extraction so the UI
can show a preview card (title, duration, thumbnail, source) before processing.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

ProgressCallback = Callable[[float, str], None]
"""A callback ``fn(fraction, message)`` where fraction is in ``[0, 1]``."""


class DownloadError(RuntimeError):
    """Raised when a URL cannot be fetched."""


class UnsafeURLError(DownloadError):
    """Raised when a URL resolves somewhere ingest is not allowed to reach."""


def _is_blocked_address(ip: ipaddress._BaseAddress) -> bool:
    """Whether ``ip`` is somewhere a user-supplied URL must not reach.

    ``link_local`` is the one worth naming: ``169.254.169.254`` is the cloud instance
    metadata endpoint on AWS, GCP and Azure, and on a hosted deployment it will happily
    hand out credentials to anything that asks. The rest close the obvious neighbours -
    loopback, RFC1918, CGNAT, and IPv6 unique-local.
    """
    return bool(
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def assert_safe_url(url: str, *, allow_private: Optional[bool] = None) -> None:
    """Raise :class:`UnsafeURLError` unless ``url`` is safe to hand to yt-dlp.

    ``/api/jobs/url`` and ``/api/preview`` take their target from the request body and
    pass it to a downloader that will fetch anything. Without this, a request for
    ``http://169.254.169.254/latest/meta-data/iam/security-credentials/`` is a server-side
    request forgery with the host's own credentials as the payload, and
    ``http://192.168.1.1/`` is a port scan of the operator's LAN.

    Every hostname is resolved and **every** address it resolves to is checked, not just
    the first: a name with one public and one private A record would otherwise pass the
    check and then connect to the private one. DNS rebinding between this check and the
    fetch remains possible - closing that needs the connection itself to be pinned to a
    vetted address, which is not reachable through yt-dlp's interface - so this is a guard
    against the accidental and the opportunistic, not against a determined attacker who
    controls DNS.

    ``allow_private`` defaults to ``settings.allow_private_url_ingest``, which exists
    because clipping from a media server on your own LAN is a legitimate thing to want.
    """
    if allow_private is None:
        from config import settings

        allow_private = settings.allow_private_url_ingest

    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https"):
        raise UnsafeURLError(
            f"Only http and https URLs are accepted (got {parts.scheme or 'no'} scheme)."
        )
    host = parts.hostname
    if not host:
        raise UnsafeURLError("URL has no host.")
    if allow_private:
        return

    # A bare IP literal never reaches the resolver, so check it directly first.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_blocked_address(literal):
            raise UnsafeURLError(
                f"Refusing to fetch {host}: private, loopback or link-local address. "
                "Set ALLOW_PRIVATE_URL_INGEST=true to permit this."
            )
        return

    try:
        infos = socket.getaddrinfo(host, parts.port or None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        # A name that does not resolve is NOT an SSRF risk: there is nothing for the
        # downloader to reach, and the fetch that follows will fail on its own with a
        # message about the actual problem. Refusing here instead would replace yt-dlp's
        # accurate "not found" with a security error that misdescribes the situation, and
        # would make this guard require working DNS in order for *any* ingest to proceed -
        # including in tests that mock the downloader entirely.
        return

    for info in infos:
        address = info[4][0]
        try:
            resolved = ipaddress.ip_address(address)
        except ValueError:  # pragma: no cover - getaddrinfo returned something odd
            continue
        if _is_blocked_address(resolved):
            raise UnsafeURLError(
                f"Refusing to fetch {host}: resolves to {address}, which is a private, "
                "loopback or link-local address. Set ALLOW_PRIVATE_URL_INGEST=true to "
                "permit this."
            )


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
        UnsafeURLError: if the URL resolves somewhere ingest may not reach.
        DownloadError: if info extraction fails.
    """
    import yt_dlp

    # Checked here as well as in download_video because this is reachable on its own from
    # /api/preview, and an SSRF that only reports a title is still an SSRF.
    assert_safe_url(url)

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


def resolve_downloaded_path(prepared: Path) -> Path:
    """The file yt-dlp actually wrote, given the name it prepared.

    ``prepare_filename`` reports the name derived from the *selected format*, before any
    post-processing. ``merge_output_format="mp4"`` then remuxes - but **only when a merge actually
    happened**, i.e. when separate video and audio renditions were selected. A progressive
    single-file source is downloaded as-is and keeps its own container.

    So the prepared name is correct in one case and wrong in the other, and the caller cannot tell
    which from the outside: both return a plausible path, and only one of them exists. Checking for
    the ``.mp4`` sibling covers the merged case without assuming it.

    Extracted from :func:`download_video` because it is the one branch a real download exercises
    only half of - a test would need a source offering separate audio and video renditions to reach
    the other half.
    """
    if prepared.exists():
        return prepared
    merged = prepared.with_suffix(".mp4")
    if merged.exists():
        return merged
    return prepared


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
        UnsafeURLError: if the URL resolves somewhere ingest may not reach.
        DownloadError: on any download failure.
    """
    import yt_dlp

    assert_safe_url(url)

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
            path = resolve_downloaded_path(Path(ydl.prepare_filename(info)))
    except Exception as exc:
        raise DownloadError(f"Download failed: {exc}") from exc

    if not path.exists():
        # yt-dlp reporting success and leaving no file is rare but real - a post-processor that
        # failed after the download, or a template that expanded to a path it could not write.
        # Without this the caller gets a non-existent path and the failure surfaces much later,
        # as an ffprobe error on a file nobody can explain the absence of.
        raise DownloadError(f"Downloaded file not found for {url}")

    meta = VideoMeta(
        title=info.get("title") or path.stem,
        duration=info.get("duration"),
        thumbnail=info.get("thumbnail"),
        source=info.get("webpage_url") or url,
        uploader=info.get("uploader"),
    )
    return path, meta
