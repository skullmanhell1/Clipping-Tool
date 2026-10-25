"""Authenticated Google Drive file metadata and streaming downloads.

The connected Drive app is not shared with this server process. Private Drive links therefore use
an OAuth refresh token supplied through environment variables; no credentials are embedded in the
browser, returned by the API, or written to job records.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

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


class _DocsHTMLParser(HTMLParser):
    """Flatten Google Docs export HTML while preserving headings, lists, and table rows."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.current: list[str] = []
        self.in_cell = False
        self.in_row = False
        self.list_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.current.append("#" * int(tag[1]) + " ")
        elif tag in {"ul", "ol"}:
            self.list_depth += 1
        elif tag == "li":
            self.current.append("- ")
        elif tag in {"td", "th"}:
            self.in_cell = True
            if self.in_row and self.current and not self.current[-1].endswith("|"):
                self.current.append(" | ")
        elif tag == "tr":
            self.in_row = True
            self.current = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"}:
            self.in_cell = False
        elif tag == "tr":
            value = "".join(self.current).strip()
            if value:
                self.lines.append(value)
            self.current = []
            self.in_row = False
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6"} and self.current and not self.in_row:
            value = "".join(self.current).strip()
            if value:
                self.lines.append(value)
            self.current = []
        elif tag in {"ul", "ol"}:
            self.list_depth = max(0, self.list_depth - 1)

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", data or " ")
        if text.strip() or self.in_cell:
            self.current.append(text)

    def text(self) -> str:
        if self.current:
            value = "".join(self.current).strip()
            if value:
                self.lines.append(value)
        return "\n".join(line for line in self.lines if line.strip())


def _docs_export_text(content: str) -> str:
    if not content.lstrip().lower().startswith(("<!doctype", "<html", "<head")):
        return content
    parser = _DocsHTMLParser()
    parser.feed(content)
    return parser.text()


def extract_file_id(url: str) -> str | None:
    """Extract an ID from a Google Docs or Drive direct-file link.

    Only the fixed Google hosts and direct-file URL shapes are accepted. In particular, folder
    links, arbitrary Google pages, user-info URLs, and non-HTTP schemes never reach the OAuth
    reader. The same helper is used by video ingest and campaign-brief import so the browser only
    ever submits an opaque URL; credentials stay on the server.
    """
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"}:
        return None
    if parts.username or parts.password:
        return None
    hostname = (parts.hostname or "").lower()
    if hostname not in {
        "drive.google.com",
        "www.drive.google.com",
        "docs.google.com",
        "www.docs.google.com",
    }:
        return None
    path_parts = [part for part in parts.path.split("/") if part]
    file_id = None
    if len(path_parts) >= 3 and path_parts[0] == "file" and path_parts[1] == "d":
        file_id = path_parts[2]
    elif hostname in {"docs.google.com", "www.docs.google.com"} and path_parts[:2] == [
        "document",
        "d",
    ]:
        file_id = path_parts[2] if len(path_parts) >= 3 else None
    elif (
        hostname in {"docs.google.com", "www.docs.google.com"}
        and len(path_parts) >= 5
        and path_parts[0] == "document"
        and path_parts[1] == "u"
        and path_parts[2].isdigit()
        and path_parts[3] == "d"
    ):
        file_id = path_parts[4]
    elif path_parts and path_parts[0] in {"open", "uc"}:
        file_id = parse_qs(parts.query).get("id", [None])[0]
    return file_id if file_id and _FILE_ID_RE.fullmatch(file_id) else None


def extract_folder_id(url: str) -> str | None:
    """Extract a Google Drive folder id without accepting arbitrary Google URLs."""
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or parts.username or parts.password:
        return None
    if (parts.hostname or "").lower() not in {"drive.google.com", "www.drive.google.com"}:
        return None
    path_parts = [part for part in parts.path.split("/") if part]
    folder_id = None
    if len(path_parts) >= 2 and path_parts[0] == "drive" and path_parts[1] == "folders":
        folder_id = path_parts[2] if len(path_parts) >= 3 else None
    elif len(path_parts) >= 5 and path_parts[0] == "drive" and path_parts[1] == "u" and path_parts[3] == "folders":
        folder_id = path_parts[4]
    elif path_parts and path_parts[0] in {"open", "uc"}:
        folder_id = parse_qs(parts.query).get("id", [None])[0]
    return folder_id if folder_id and _FILE_ID_RE.fullmatch(folder_id) else None


def _file_resource_url(file_id: str, *, export: bool = False) -> str:
    """Build a fixed-host Drive URL with the ID encoded as exactly one path segment."""
    value = str(file_id or "")
    if not _FILE_ID_RE.fullmatch(value):
        raise GoogleDriveError("That Google Drive file ID is not valid.")
    # Keep untrusted IDs out of URL syntax: validate the Drive alphabet and percent-encode the
    # value as a single path component before appending it to the fixed Google API endpoint.
    encoded_id = quote(value, safe="")
    suffix = "/export" if export else ""
    return f"{FILES_URL}/{encoded_id}{suffix}"


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
    ) and bool(configured_scopes())


def configured_scopes() -> list[str]:
    """Return configured OAuth scopes, retaining only read-only Drive scopes."""
    from config import settings

    raw = str(getattr(settings, "google_drive_scopes", "") or "")
    values = [item.strip() for item in re.split(r"[,\s]+", raw) if item.strip()]
    return [item for item in values if item.endswith("/drive.readonly") or item.endswith("/drive.metadata.readonly")]


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
                "scope": " ".join(configured_scopes()),
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


def _get_file_metadata(
    file_id: str, headers: dict[str, str], *, require_video: bool = True
) -> dict:
    """Read basic metadata with an already-refreshed bearer token.

    Video ingest keeps its original allow-list by default.  Brief sync uses the same authenticated
    boundary with ``require_video=False`` so a Google Doc can be inspected without adding a second
    credential path.
    """
    import httpx

    if not _FILE_ID_RE.fullmatch(str(file_id or "")):
        raise GoogleDriveError("That Google Drive file ID is not valid.")
    try:
        response = httpx.get(
            _file_resource_url(file_id),
            params={
                "fields": "id,name,mimeType,size,modifiedTime,version,headRevisionId,md5Checksum",
            },
            headers=headers,
            timeout=20.0,
        )
        if response.status_code in {401, 403}:
            raise GoogleDriveError("Google Drive file access was denied. Confirm Viewer access.")
        if response.status_code == 429:
            raise GoogleDriveError("Google Drive rate limit reached. Retry the import later.")
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
    if require_video and not mime_type.startswith("video/"):
        raise GoogleDriveError(f"Google Drive item is {mime_type or 'not a file'}, not a video.")
    return data


def get_file_metadata(file_id: str, *, require_video: bool = True) -> dict:
    """Read only basic metadata for one authenticated Drive file."""
    return _get_file_metadata(file_id, _headers(), require_video=require_video)


def list_folder_files(folder_id: str, *, page_size: int = 100) -> list[dict]:
    """List files directly in a folder using the authenticated read-only Drive API."""
    import httpx

    if not _FILE_ID_RE.fullmatch(str(folder_id or "")):
        raise GoogleDriveError("That Google Drive folder ID is not valid.")
    headers = _headers()
    rows: list[dict] = []
    token = ""
    try:
        for _ in range(100):
            params = {
                "q": f"'{folder_id}' in parents and trashed = false",
                "pageSize": max(1, min(int(page_size), 1000)),
                "orderBy": "name",
                "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime,version,headRevisionId,md5Checksum)",
            }
            if token:
                params["pageToken"] = token
            response = httpx.get(FILES_URL, params=params, headers=headers, timeout=20.0)
            if response.status_code in {401, 403}:
                raise GoogleDriveError("Google Drive folder access was denied. Confirm Viewer access.")
            if response.status_code == 429:
                raise GoogleDriveError("Google Drive rate limit reached. Retry the import later.")
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise GoogleDriveError("Google Drive returned invalid folder metadata.")
            files = data.get("files", [])
            if not isinstance(files, list):
                raise GoogleDriveError("Google Drive returned invalid folder contents.")
            rows.extend(item for item in files if isinstance(item, dict))
            token = str(data.get("nextPageToken") or "")
            if not token:
                break
    except GoogleDriveError:
        raise
    except Exception as exc:
        raise GoogleDriveError(
            "Could not read that Google Drive folder. Confirm the OAuth account has Viewer access."
        ) from exc
    return rows


def download_public_text_file(file_id: str, *, source_type: str = "google_drive") -> tuple[str, dict]:
    """Read a publicly shared Google Doc/text file without OAuth credentials."""
    import httpx

    if not _FILE_ID_RE.fullmatch(str(file_id or "")):
        raise GoogleDriveError("That Google Drive file ID is not valid.")
    if source_type == "google_docs":
        url = f"https://docs.google.com/document/d/{quote(file_id, safe='')}/export"
        params = {"format": "html"}
    else:
        url = f"https://drive.google.com/uc"
        params = {"export": "download", "id": file_id}
    try:
        response = httpx.get(url, params=params, follow_redirects=True, timeout=30.0)
        if response.status_code in {401, 403}:
            raise GoogleDriveError("The Google link is private or access was denied.")
        if response.status_code == 429:
            raise GoogleDriveError("Google Drive rate limit reached. Retry the import later.")
        response.raise_for_status()
        content = _docs_export_text(response.content.decode("utf-8-sig"))
    except GoogleDriveError:
        raise
    except Exception as exc:
        raise GoogleDriveError(
            "The public Google link could not be read. Confirm link sharing is enabled and the file is a supported text document."
        ) from exc
    if not content.strip():
        raise GoogleDriveError("The public Google document is empty or unreadable.")
    if content.lstrip().lower().startswith(("<!doctype html", "<html", "<head")):
        raise GoogleDriveError("The Google link is private or did not return a supported text document.")
    return content, {"id": file_id, "mimeType": "text/plain", "modifiedTime": "", "version": ""}


def download_text_file(file_id: str, *, mime_type: str = "") -> tuple[str, dict]:
    """Download a Google Doc or text file as UTF-8 text and return its metadata.

    Google Docs are exported through Drive's ``files.export`` endpoint; ordinary text files use the
    media endpoint.  This is intentionally a small seam used by campaign brief sync and is not
    involved in the existing video download path.
    """
    import httpx

    headers = _headers()
    metadata = _get_file_metadata(file_id, headers, require_video=False)
    source_mime = str(mime_type or metadata.get("mimeType") or "")
    if source_mime == "application/vnd.google-apps.document":
        url = _file_resource_url(file_id, export=True)
        params = {"mimeType": "text/html"}
    elif source_mime.startswith("text/") or source_mime == "application/json":
        url = _file_resource_url(file_id)
        params = {"alt": "media"}
    else:
        raise GoogleDriveError(
            f"Google Drive item is {source_mime or 'not a text document'}, not a Google Doc or text file."
        )
    try:
        response = httpx.get(url, params=params, headers=headers, timeout=30.0)
        if response.status_code in {401, 403}:
            raise GoogleDriveError("Google Drive file access was denied. Confirm Viewer access.")
        if response.status_code == 429:
            raise GoogleDriveError("Google Drive rate limit reached. Retry the import later.")
        response.raise_for_status()
        content = _docs_export_text(response.content.decode("utf-8-sig"))
    except GoogleDriveError:
        raise
    except Exception as exc:
        raise GoogleDriveError(
            "Could not export the Google Doc brief. Confirm the OAuth account has Viewer access."
        ) from exc
    return content, metadata


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
            _file_resource_url(file_id),
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


def download_asset(
    file_id: str,
    dest_dir: str | Path,
    *,
    max_bytes: int = 25 * 1024 * 1024,
    suffix: str = "",
) -> Path:
    """Download a non-video Drive asset such as a font, logo, intro, or outro.

    Unlike :func:`download_file`, this deliberately does not require a video MIME type. The same
    OAuth boundary and byte ceiling are reused, and the final filename is derived from the validated
    Drive id rather than from user-controlled path components.
    """
    import httpx

    headers = _headers()
    path: Path | None = None
    if not _FILE_ID_RE.fullmatch(str(file_id or "")):
        raise GoogleDriveError("That Google Drive asset ID is not valid.")
    try:
        response = httpx.get(
            _file_resource_url(file_id),
            params={"fields": "id,name,mimeType,size"},
            headers=headers,
            timeout=20.0,
        )
        response.raise_for_status()
        metadata = response.json()
        name = Path(str(metadata.get("name") or file_id)).name
        extension = suffix or Path(name).suffix
        if not extension:
            extension = ".asset"
        path = Path(dest_dir) / f"{file_id}{extension.lower()}"
        expected = int(metadata.get("size") or 0)
        if max_bytes > 0 and expected > max_bytes:
            raise GoogleDriveError("Google Drive asset exceeds the configured download limit.")
        path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with httpx.stream(
            "GET",
            _file_resource_url(file_id),
            params={"alt": "media"},
            headers=headers,
            follow_redirects=True,
            timeout=httpx.Timeout(60.0, connect=20.0),
        ) as stream:
            stream.raise_for_status()
            with path.open("wb") as output:
                for chunk in stream.iter_bytes(chunk_size=1024 * 1024):
                    written += len(chunk)
                    if max_bytes > 0 and written > max_bytes:
                        raise GoogleDriveError(
                            "Google Drive asset exceeds the configured download limit."
                        )
                    output.write(chunk)
        if written <= 0:
            raise GoogleDriveError("Google Drive returned an empty asset.")
        return path
    except GoogleDriveError:
        try:
            if path is not None:
                path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except Exception as exc:
        if path is not None:
            path.unlink(missing_ok=True)
        raise GoogleDriveError("Google Drive asset download failed.") from exc
