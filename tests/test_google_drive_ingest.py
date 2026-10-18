from __future__ import annotations

from contextlib import contextmanager

import httpx
import pytest

from config import settings
from worker import download, google_drive

VIDEO_METADATA = {
    "id": "1OnH2B5N-TaGyHqPdmT0uxdCeA-38FXry",
    "name": "Elon Musk Clips",
    "mimeType": "video/mp4",
    "size": "8",
}


def _configure_drive(monkeypatch):
    monkeypatch.setattr(settings, "google_drive_client_id", "client-id")
    monkeypatch.setattr(settings, "google_drive_client_secret", "not-a-real-secret")
    monkeypatch.setattr(settings, "google_drive_refresh_token", "not-a-real-refresh-token")


def _json_response(url: str, body: dict):
    return httpx.Response(200, json=body, request=httpx.Request("GET", url))


def test_drive_file_url_parser_only_accepts_direct_files():
    assert (
        google_drive.extract_file_id(
            "https://drive.google.com/file/d/1OnH2B5N-TaGyHqPdmT0uxdCeA-38FXry/view?usp=sharing"
        )
        == "1OnH2B5N-TaGyHqPdmT0uxdCeA-38FXry"
    )
    assert (
        google_drive.extract_file_id(
            "https://drive.google.com/open?id=1OnH2B5N-TaGyHqPdmT0uxdCeA-38FXry"
        )
        == "1OnH2B5N-TaGyHqPdmT0uxdCeA-38FXry"
    )
    assert google_drive.extract_file_id("https://drive.google.com/drive/folders/abcd") is None
    assert google_drive.extract_file_id("https://example.com/file/d/abcd") is None


def test_drive_api_url_keeps_the_id_in_one_validated_path_segment():
    file_id = "1OnH2B5N-TaGyHqPdmT0uxdCeA-38FXry"
    assert google_drive._file_resource_url(file_id) == f"{google_drive.FILES_URL}/{file_id}"
    assert (
        google_drive._file_resource_url(file_id, export=True)
        == f"{google_drive.FILES_URL}/{file_id}/export"
    )
    for invalid in ("https://attacker.test", "abc/../../attacker", "abc?x=1"):
        with pytest.raises(google_drive.GoogleDriveError):
            google_drive._file_resource_url(invalid)


def test_drive_refresh_configuration_is_reported_without_returning_credentials(monkeypatch):
    monkeypatch.setattr(settings, "google_drive_client_id", "")
    monkeypatch.setattr(settings, "google_drive_client_secret", "")
    monkeypatch.setattr(settings, "google_drive_refresh_token", "")
    assert google_drive.configured() is False
    with pytest.raises(google_drive.GoogleDriveError, match="GOOGLE_DRIVE_CLIENT_ID"):
        google_drive._access_token()


def test_metadata_refreshes_oauth_and_reads_video_metadata(monkeypatch):
    _configure_drive(monkeypatch)
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(
            200,
            json={"access_token": "temporary-access-token"},
            request=httpx.Request("POST", url),
        )

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return _json_response(url, VIDEO_METADATA)

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", get)
    metadata = google_drive.get_file_metadata(VIDEO_METADATA["id"])

    assert metadata["name"] == "Elon Musk Clips"
    assert len(calls) == 2
    assert calls[0][0] == google_drive.OAUTH_EXCHANGE_ENDPOINT
    assert calls[1][1]["headers"]["Authorization"] == "Bearer temporary-access-token"
    assert "not-a-real-secret" not in repr(metadata)
    assert "not-a-real-refresh-token" not in repr(metadata)


def test_download_streams_to_disk_and_reports_progress(monkeypatch, tmp_path):
    _configure_drive(monkeypatch)
    monkeypatch.setattr(google_drive, "_headers", lambda: {"Authorization": "Bearer token"})
    monkeypatch.setattr(
        google_drive, "_get_file_metadata", lambda file_id, headers: dict(VIDEO_METADATA)
    )
    requested = {}

    class StreamResponse:
        headers = {"content-length": "8"}

        def raise_for_status(self):
            return None

        def iter_bytes(self, chunk_size):
            assert chunk_size == 1024 * 1024
            yield b"BOXA"
            yield b"BL!!"

    @contextmanager
    def stream(method, url, **kwargs):
        requested.update(method=method, url=url, kwargs=kwargs)
        yield StreamResponse()

    monkeypatch.setattr(httpx, "stream", stream)
    progress = []
    path, metadata = google_drive.download_file(
        VIDEO_METADATA["id"],
        tmp_path,
        max_bytes=100,
        progress_cb=lambda fraction, message: progress.append((fraction, message)),
    )

    assert path.name.endswith(".mp4")
    assert path.read_bytes() == b"BOXABL!!"
    assert metadata["mimeType"] == "video/mp4"
    assert requested["method"] == "GET"
    assert requested["kwargs"]["params"] == {"alt": "media"}
    assert progress[-1] == (1.0, "Google Drive download complete")


def test_drive_download_enforces_size_limit_before_streaming(monkeypatch, tmp_path):
    monkeypatch.setattr(google_drive, "_headers", lambda: {"Authorization": "Bearer token"})
    metadata = {**VIDEO_METADATA, "size": "1024"}
    monkeypatch.setattr(google_drive, "_get_file_metadata", lambda file_id, headers: metadata)
    with pytest.raises(google_drive.GoogleDriveError, match="above the configured"):
        google_drive.download_file(VIDEO_METADATA["id"], tmp_path, max_bytes=100)


def test_download_module_routes_drive_preview_and_download_without_ytdlp(monkeypatch, tmp_path):
    drive_url = "https://drive.google.com/file/d/1OnH2B5N-TaGyHqPdmT0uxdCeA-38FXry/view"
    monkeypatch.setattr(download, "validate_public_url", lambda url: url)
    monkeypatch.setattr(download, "get_drive_file_metadata", lambda file_id: dict(VIDEO_METADATA))
    preview = download.fetch_metadata(drive_url)
    assert preview.title == "Elon Musk Clips"
    assert preview.uploader == "Google Drive"

    expected_path = tmp_path / "1OnH2B5N-TaGyHqPdmT0uxdCeA-38FXry.mp4"
    expected_path.write_bytes(b"video")
    monkeypatch.setattr(
        download,
        "download_drive_file",
        lambda file_id, directory, **kwargs: (expected_path, dict(VIDEO_METADATA)),
    )
    path, metadata = download.download_video(drive_url, tmp_path)
    assert path == expected_path
    assert metadata.uploader == "Google Drive"
    assert metadata.title == "Elon Musk Clips"
