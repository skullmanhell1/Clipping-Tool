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

#: Schemes yt-dlp may be handed. Anything else - `file://` most importantly - is refused
#: before yt-dlp sees it, because its generic extractor will happily read a local path.
_ALLOWED_SCHEMES = frozenset({"http", "https"})

ProgressCallback = Callable[[float, str], None]
"""A callback ``fn(fraction, message)`` where fraction is in ``[0, 1]``."""


class DownloadError(RuntimeError):
    """Raised when a URL cannot be fetched."""


class UnsafeURLError(DownloadError):
    """Raised when a URL points somewhere ingest must not reach.

    Deliberately a :class:`DownloadError` subclass: every existing caller already handles that
    type, so adding this cannot turn a rejected URL into an unhandled 500. Callers that want to
    distinguish "you may not fetch that" from "fetching that failed" can catch it specifically -
    ``api.main`` does, to answer 400 rather than 422.
    """


def _is_disallowed_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> Optional[str]:
    """Return why ``ip`` is not a legitimate ingest target, or ``None`` if it is fine.

    ``is_global`` alone is not enough. It is False for the ranges wanted here but also False for
    some that need naming in the error, and on IPv6 it does not see through an IPv4-mapped
    address - ``::ffff:127.0.0.1`` is a loopback wearing a hat. Unwrapping first is what makes
    the IPv6 answers agree with the IPv4 ones.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if ip.is_loopback:
        return "a loopback address"
    if ip.is_link_local:
        # 169.254.0.0/16 is where every major cloud serves instance credentials.
        return "a link-local address (cloud instance metadata lives here)"
    if ip.is_private:
        return "a private address"
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return "a reserved, multicast or unspecified address"
    return None


def validate_public_url(url: str, *, allow_private: Optional[bool] = None) -> str:
    """Return ``url`` unchanged, or raise :class:`UnsafeURLError`.

    yt-dlp fetches whatever it is given, so a URL endpoint is a request forwarder into whatever
    network the deployment sits in. The dangerous target is not an obscure one: every major cloud
    serves instance credentials from ``169.254.169.254`` over plain HTTP with no authentication.

    Checks, in order: the scheme is http/https; a host is present; and no address the host
    resolves to is loopback, link-local, private, reserved, multicast or unspecified. *Every*
    resolved address is checked rather than the first, because a name that returns one public and
    one private address would otherwise pass and then connect to either.

    ``allow_private`` defaults to ``settings.url_ingest_allow_private``. It is a parameter as well
    as a setting so the rules can be tested as a pure function, and so a caller that genuinely
    means to fetch from a LAN host can say so at the call site.

    **Two limits worth stating plainly, because this is not a complete SSRF defence:**

    * *Redirects.* A permitted host may redirect to a forbidden one. yt-dlp performs its own
      requests, so only the submitted URL passes through here.
    * *DNS rebinding.* The name is resolved here and resolved again by yt-dlp; a record with a
      one-second TTL can differ between the two.

    Closing either means owning the socket layer rather than the URL, which is a much larger
    change than this. What this does close is the direct attack - the one an unauthenticated
    endpoint makes trivial - and it refuses non-HTTP schemes outright.
    """
    if allow_private is None:
        from config import settings  # local import: this module must stay import-cheap

        allow_private = settings.url_ingest_allow_private

    candidate = (url or "").strip()
    parts = urlsplit(candidate)
    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(
            f"Only http and https URLs can be ingested, not {scheme or 'a scheme-less path'!r}."
        )

    try:
        host = parts.hostname
    except ValueError as exc:  # malformed IPv6 literal, e.g. http://[::1
        raise UnsafeURLError(f"Could not read the host from that URL: {exc}") from exc
    if not host:
        raise UnsafeURLError("That URL has no host.")

    if allow_private:
        return candidate

    # An IP literal needs no DNS, and must not be given a free pass just because resolution of a
    # bare address could fail on a host with unusual name services.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        reason = _is_disallowed_address(literal)
        if reason:
            raise UnsafeURLError(f"Refusing to fetch {host!r}: it is {reason}.")
        return candidate

    try:
        resolved = socket.getaddrinfo(host, parts.port or None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError):
        # A name that does not resolve reaches nothing, so it is not a route into the network -
        # and rejecting it here would replace yt-dlp's accurate "no such host" with a security
        # error, which sends someone with a typo looking in the wrong place.
        return candidate

    for family, _type, _proto, _canonname, sockaddr in resolved:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError:  # pragma: no cover - getaddrinfo returning a non-address
            continue
        reason = _is_disallowed_address(address)
        if reason:
            raise UnsafeURLError(
                f"Refusing to fetch {host!r}: it resolves to {address}, which is {reason}."
            )
    return candidate


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
        UnsafeURLError: if the URL points somewhere ingest must not reach.
        DownloadError: if info extraction fails.
    """
    import yt_dlp

    # Guarded here rather than only in the API layer. This is the cheaper of the two yt-dlp
    # entry points and therefore the more attractive probe: it makes a real request, returns
    # promptly, and its error text is handed straight back to the caller as a 422 - a blind-SSRF
    # oracle. Validating inside the module covers every caller, including any future ingest
    # path that never passes through `api.main`.
    validate_public_url(url)

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
        UnsafeURLError: if the URL points somewhere ingest must not reach.
        DownloadError: on any download failure.
    """
    import yt_dlp

    # `worker.jobs` calls this with the URL held on the job record, which nothing re-checks
    # between submission and execution - so the guard belongs here, not only at the endpoint.
    validate_public_url(url)

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
