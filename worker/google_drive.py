"""Authenticated Google Drive file metadata and streaming downloads.

The connected Drive app is not shared with this server process. Private Drive links therefore use
an OAuth refresh token supplied through environment variables; no credentials are embedded in the
browser, returned by the API, or written to job records.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

OAUTH_EXCHANGE_ENDPOINT = "https://oauth2.googleapis.com/token"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{5,200}$")

MIME_EXTENSIONS = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "video/x-msvideo": ".avi",
    "video/mpeg": ".mpeg",
    "video/3gpp": ".3gp",
    "video/x-flv": ".flv",
}


class GoogleDriveError(RuntimeError):
    """A safe, user-actionable Google Drive authentication or transfer failure."""


def extract_file_id(url: str) -> str | None:
    """Extract an ID from a Google Drive direct-file link; folders are not accepted."""
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return None
    if (parts.hostname or "").lower() not in {"drive.google.com", "www.drive.google.com"}:
        return None
    path_parts = [part for part in parts.path.split("/") if part]
    file_id = None
    if len(path_parts) >= 3 and path_parts[0] == "file" and path_parts[1] == "d":
        file_id = path_parts[2]
    elif path_parts and path_parts[0] in {"open", "uc"}:
        file_id = parse_qs(parts.query).get("id", [None])[0]
    return file_id if file_id and _FILE_ID_RE.fullmatch(file_id) else None


def configured() -> bool:
    """Whether all server-side OAuth refresh credentials are present; never return values."""
    from config import settings

    return all(
        str(getattr(settings, key, "") or "").strip()
        for key in (
            "google_drive_client_id",
            "google_drive_client_secret",
            "google_drive_refresh_token",
        )
    )


def _access_token() -> str:
    """Exchange the configured refresh token for an access token without exposing secrets."""
    import httpx

    from config import settings

    client_id = str(getattr(settings, "google_drive_client_id", "") or "").strip()
    client_secret = str(getattr(settings, "google_drive_client_secret", "") or "").strip()
    refresh_token = str(getattr(settings, "google_drive_refresh_token", "") or "").strip()
    if not client_id or not client_secret or not refresh_token:
        raise GoogleDriveError(
            "Google Drive import is not configured. Set GOOGLE_DRIVE_CLIENT_ID, "
            "GOOGLE_DRIVE_CLIENT_SECRET and GOOGLE_DRIVE_REFRESH_TOKEN on the server."
        )
    try:
        response = httpx.post(
            OAUTH_EXCHANGE_ENDPOINT,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=20.0,
        )
        response.raise_for_status()
        token = str(response.json().get("access_token") or "").strip()
    except Exception as exc:
        # Do not surface the request object or response body: this is an authentication boundary.
        raise GoogleDriveError(
            "Google Drive OAuth refresh failed. Check the server credentials and refresh-token "
            "scope; the Drive account must be allowed to read the source file."
        ) from exc
    if not token:
        raise GoogleDriveError("Google Drive OAuth refresh returned no access token.")
    return token


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_access_token()}"}


def _get_file_metadata(file_id: str, headers: dict[str, str]) -> dict:
    """Read basic metadata with an already-refreshed bearer token."""
    import httpx

    if not _FILE_ID_RE.fullmatch(str(file_id or "")):
        raise GoogleDriveError("That Google Drive file ID is not valid.")
    try:
        response = httpx.get(
            f"{FILES_URL}/{file_id}",
            params={"fields": "id,name,mimeType,size"},
            headers=headers,
            timeout=20.0,
        )
        response.raise_for_status()
        data = response.json()
    except GoogleDriveError:
        raise
    except Exception as exc:
        raise GoogleDriveError(
            "Could not read that Google Drive file. Confirm the OAuth account has Viewer access "
            "and that the link points to a video file."
        ) from exc
    if not isinstance(data, dict):
        raise GoogleDriveError("Google Drive returned invalid file metadata.")
    mime_type = str(data.get("mimeType") or "")
    if not mime_type.startswith("video/"):
        raise GoogleDriveError(f"Google Drive item is {mime_type or 'not a file'}, not a video.")
    return data


def get_file_metadata(file_id: str) -> dict:
    """Read only basic metadata for one authenticated Drive file."""
    return _get_file_metadata(file_id, _headers())


def download_file(
    file_id: str,
    dest_dir: str | Path,
    *,
    max_bytes: int,
    progress_cb: Callable[[float, str], None] | None = None,
) -> tuple[Path, dict]:
    """Stream a Google Drive video to disk with the same size ceiling as local uploads."""
    import httpx

    headers = _headers()
    metadata = _get_file_metadata(file_id, headers)
    name = Path(str(metadata.get("name") or "drive-video")).name
    mime_type = str(metadata.get("mimeType") or "video/mp4")
    suffix = Path(name).suffix.lower()
    if suffix not in set(MIME_EXTENSIONS.values()):
        suffix = MIME_EXTENSIONS.get(mime_type, ".mp4")
    path = Path(dest_dir) / f"{file_id}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        expected = int(metadata.get("size") or 0)
    except (TypeError, ValueError):
        expected = 0
    if max_bytes > 0 and expected > max_bytes:
        raise GoogleDriveError(
            f"Google Drive video is {expected} bytes, above the configured {max_bytes}-byte download limit."
        )

    written = 0
    try:
        with httpx.stream(
            "GET",
            f"{FILES_URL}/{file_id}",
            params={"alt": "media"},
            headers=headers,
            follow_redirects=True,
            timeout=httpx.Timeout(60.0, connect=20.0),
        ) as response:
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            try:
                stream_size = int(content_length or 0)
            except (TypeError, ValueError):
                stream_size = 0
            if max_bytes > 0 and stream_size > max_bytes:
                raise GoogleDriveError(
                    f"Google Drive video is {stream_size} bytes, above the configured "
                    f"{max_bytes}-byte download limit."
                )
            with path.open("wb") as output:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if max_bytes > 0 and written > max_bytes:
                        raise GoogleDriveError(
                            f"Google Drive video exceeded the configured {max_bytes}-byte download limit."
                        )
                    output.write(chunk)
                    if progress_cb:
                        fraction = (
                            min(written / (stream_size or expected), 0.99)
                            if (stream_size or expected)
                            else 0.0
                        )
                        progress_cb(fraction, "Downloading from Google Drive")
    except GoogleDriveError:
        path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        path.unlink(missing_ok=True)
        raise GoogleDriveError(
            "Google Drive video download failed. Confirm the account can download this file and "
            "try the link again."
        ) from exc

    if written <= 0:
        path.unlink(missing_ok=True)
        raise GoogleDriveError("Google Drive returned an empty video file.")
    if progress_cb:
        progress_cb(1.0, "Google Drive download complete")
    return path, metadata
