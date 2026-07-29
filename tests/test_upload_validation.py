"""Tests for upload validation and streaming.

``/api/upload`` accepted any file of any size and wrote it with a synchronous
``shutil.copyfileobj`` inside an ``async def``. Two distinct problems:

* no ceiling and no type check, so a client could fill the disk or drop arbitrary files
  into a directory whose contents are later fed to ffmpeg;
* the synchronous copy blocked the event loop for the entire transfer, so during a large
  upload the server answered nothing — including the progress polls the UI relies on.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from config import settings as app_settings


@pytest.fixture
def client():
    return TestClient(api_main.app)


@pytest.fixture
def uploads_dir(tmp_path: Path, monkeypatch):
    """Point uploads at a per-test directory so leftovers are observable."""
    target = tmp_path / "uploads"
    target.mkdir()
    monkeypatch.setattr(app_settings, "uploads_dir", target)
    return target


@pytest.fixture(autouse=True)
def no_processing(monkeypatch):
    """Stop submitted jobs from actually running a pipeline.

    These tests are about the HTTP boundary; letting the thread pool start real work
    would make them slow and dependent on ffmpeg.
    """
    from worker.jobs import JobManager

    monkeypatch.setattr(JobManager, "_run", lambda self, job_id: None)


def _post(client, *, name: str, data: bytes):
    return client.post(
        "/api/upload",
        files={"files": (name, io.BytesIO(data), "application/octet-stream")},
    )


# ---------------------------------------------------------------------------
# Type validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["clip.mp4", "clip.MOV", "audio.wav", "movie.mkv"])
def test_accepted_media_extensions_are_saved(client, uploads_dir, name):
    """The allow-list is case-insensitive and covers video and audio sources."""
    resp = _post(client, name=name, data=b"x" * 2048)
    assert resp.status_code == 200, resp.text
    assert len(list(uploads_dir.iterdir())) == 1


@pytest.mark.parametrize("name", ["payload.exe", "notes.txt", "archive.zip", "noext"])
def test_disallowed_extensions_are_rejected(client, uploads_dir, name):
    """A non-media upload is refused, and nothing is written."""
    resp = _post(client, name=name, data=b"x" * 2048)
    assert resp.status_code == 400
    assert "unsupported file type" in resp.json()["detail"].lower()
    assert list(uploads_dir.iterdir()) == []


def test_a_path_traversal_filename_cannot_escape_the_uploads_dir(client, uploads_dir):
    """``../`` in a filename is stripped to its basename.

    The name arrives from the client, so it is untrusted; this pins the existing
    ``Path(...).name`` defence rather than assuming it.
    """
    resp = _post(client, name="../../etc/passwd.mp4", data=b"x" * 512)
    assert resp.status_code == 200, resp.text

    written = list(uploads_dir.iterdir())
    assert len(written) == 1
    assert written[0].name.endswith("passwd.mp4")
    assert written[0].parent == uploads_dir


# ---------------------------------------------------------------------------
# Size validation
# ---------------------------------------------------------------------------


def test_an_oversized_upload_is_rejected_with_413(client, uploads_dir, monkeypatch):
    """The ceiling is enforced, and the partial file is not left behind.

    A truncated leftover would later fail inside ffmpeg with an unrelated-looking
    decode error, so deleting it is part of the fix rather than tidiness.
    """
    monkeypatch.setattr(app_settings, "max_upload_bytes", 1024)
    resp = _post(client, name="big.mp4", data=b"x" * 5000)

    assert resp.status_code == 413
    assert "maximum upload size" in resp.json()["detail"].lower()
    assert list(uploads_dir.iterdir()) == [], "a partial file was left on disk"


def test_an_upload_exactly_at_the_ceiling_is_accepted(client, uploads_dir, monkeypatch):
    """The bound is inclusive, so a file of exactly the limit is not a false reject."""
    monkeypatch.setattr(app_settings, "max_upload_bytes", 1024)
    resp = _post(client, name="exact.mp4", data=b"x" * 1024)
    assert resp.status_code == 200, resp.text


def test_a_zero_ceiling_means_unlimited(client, uploads_dir, monkeypatch):
    """0 is the documented opt-out for operators who bound uploads at a proxy."""
    monkeypatch.setattr(app_settings, "max_upload_bytes", 0)
    resp = _post(client, name="huge.mp4", data=b"x" * 100_000)
    assert resp.status_code == 200, resp.text


def test_an_empty_upload_is_rejected(client, uploads_dir):
    """An empty file cannot be processed, so it is refused up front."""
    resp = _post(client, name="empty.mp4", data=b"")
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"].lower()
    assert list(uploads_dir.iterdir()) == []


def test_the_file_is_written_whole_and_unmodified(client, uploads_dir, monkeypatch):
    """Chunked streaming must reassemble the file byte-for-byte.

    A small chunk size forces many iterations, which is where an off-by-one in the
    read/write loop would show up.
    """
    monkeypatch.setattr(app_settings, "upload_chunk_bytes", 7)
    payload = bytes(range(256)) * 40  # 10240 bytes, not a multiple of the chunk size

    resp = _post(client, name="stream.mp4", data=payload)
    assert resp.status_code == 200, resp.text

    written = list(uploads_dir.iterdir())
    assert len(written) == 1
    assert written[0].read_bytes() == payload


# ---------------------------------------------------------------------------
# Batch behaviour
# ---------------------------------------------------------------------------


def test_a_rejected_file_rolls_back_the_whole_batch(client, uploads_dir):
    """One bad file in a batch leaves no orphaned files from the good ones.

    Without the rollback the accepted files would sit in the uploads directory with no
    job referencing them: litter that nothing later cleans up.
    """
    resp = client.post(
        "/api/upload",
        files=[
            ("files", ("good.mp4", io.BytesIO(b"x" * 1024), "video/mp4")),
            ("files", ("bad.exe", io.BytesIO(b"x" * 1024), "application/octet-stream")),
        ],
    )

    assert resp.status_code == 400
    assert list(uploads_dir.iterdir()) == [], "the accepted file was not rolled back"


def test_a_valid_batch_creates_one_job_per_file(client, uploads_dir):
    """The happy path still behaves: a batch id plus a job for each file."""
    resp = client.post(
        "/api/upload",
        files=[
            ("files", ("one.mp4", io.BytesIO(b"x" * 512), "video/mp4")),
            ("files", ("two.mp4", io.BytesIO(b"y" * 512), "video/mp4")),
        ],
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["batch_id"]
    assert len(body["jobs"]) == 2
    assert len(list(uploads_dir.iterdir())) == 2


# ---------------------------------------------------------------------------
# The event loop stays responsive
# ---------------------------------------------------------------------------


def test_the_upload_path_uses_async_reads_not_a_blocking_copy():
    """The handler must ``await`` its reads rather than calling ``copyfileobj``.

    Behavioural proof would need a concurrently-served request against a live server,
    which is more machinery than this earns. Instead this pins the mechanism: the source
    performs an awaited chunked read, and the blocking helper is gone from the module.
    """
    import inspect

    source = inspect.getsource(api_main._save_upload)
    assert "await upload_file.read(" in source
    assert "copyfileobj" not in inspect.getsource(api_main.upload)
