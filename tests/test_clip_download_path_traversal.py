"""The clip download endpoints must not serve anything outside ``clips_dir``.

CodeQL flagged ``py/path-injection`` (security-severity 7.5) at five locations in ``api/main.py`` on
the first analysis run after the workflow was unblocked. It was right, and the finding was
exploitable unauthenticated on the default configuration.

``Path(x).name`` was the whole defence. It strips separators, so ``../../etc/passwd`` really does
become ``passwd`` -- but **``Path("..").name`` is ``".."``**: pathlib treats it as an ordinary final
component. So ``job_id=".."`` built ``clips_dir/../<file>``, and
``GET /api/clips/{job_id}/{filename}/video`` checked only ``exists()`` and ``is_file()`` before
streaming the result.

One directory above ``clips_dir`` is ``storage/``, which holds ``jobs.db``, ``history.db``, every
uploaded source video, the cached transcripts -- and, if the operator followed ``.env.example``, the
YouTube cookie jar at ``storage/cookies.txt``, which is a live credential for their Google account.

``download_clip`` happened to be safe because it also looks the job up and ``store.get("..")``
returns ``None``. That is second-order protection that stops holding the moment someone reorders the
checks, so both endpoints now go through one function and both are tested here.

The tests assert on **resolved containment**, not on rejected spellings. Enumerating encodings is a
losing game; the question worth asking is where a path actually points.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import api.main as main
from config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    """An app whose clips_dir is isolated, with a planted secret one level above it."""
    root = tmp_path / "storage" / "clips"
    root.mkdir(parents=True)
    monkeypatch.setattr(settings, "clips_dir", root, raising=False)

    # Exactly where the real databases and the suggested cookie jar live.
    (root.parent / "cookies.txt").write_text("SID=SUPER_SECRET_COOKIE\n")
    (root.parent / "jobs.db").write_bytes(b"SQLite format 3\x00SECRET_DB")

    # A genuine clip, so the negative tests cannot pass merely because nothing is servable.
    job_dir = root / "job123"
    job_dir.mkdir()
    (job_dir / "clip_01.mp4").write_bytes(b"REAL_CLIP_BYTES")

    monkeypatch.setattr(settings, "api_auth_token", None, raising=False)
    return TestClient(main.app)


# --- the exploit, as a regression guard -------------------------------------------------


@pytest.mark.parametrize(
    "job_id",
    [
        "%2E%2E",  # the spelling that actually worked
        "..",
        "%2e%2e",
        "..%2F",
        "....//",
        "%2E%2E%2F%2E%2E",
    ],
)
@pytest.mark.parametrize("target", ["cookies.txt", "jobs.db"])
def test_traversal_cannot_read_above_clips_dir(client, job_id, target):
    """The confirmed exploit: 200 with the file contents, unauthenticated."""
    response = client.get(f"/api/clips/{job_id}/{target}/video")

    assert response.status_code == 404, (
        f"job_id={job_id!r} served {target} with {response.status_code}"
    )
    assert b"SUPER_SECRET_COOKIE" not in response.content
    assert b"SECRET_DB" not in response.content


@pytest.mark.parametrize("job_id", ["%2E%2E", ".."])
def test_the_zip_endpoint_is_covered_too(client, job_id):
    """`download_clip` was only accidentally safe, via a job lookup. Assert it directly."""
    response = client.get(f"/api/clips/{job_id}/cookies.txt/download")

    assert response.status_code == 404
    assert b"SUPER_SECRET_COOKIE" not in response.content


def test_a_filename_of_dotdot_is_refused(client):
    """`filename` is attacker-controlled too, and `Path("..").name` is `".."` there as well."""
    assert client.get("/api/clips/job123/%2E%2E/video").status_code == 404


def test_the_clips_root_itself_is_not_servable(client):
    """`is_relative_to` alone accepts the root, which is a directory rather than a clip."""
    assert client.get("/api/clips/%2E/%2E/video").status_code == 404


# --- and the parity case, so the guard is not just "404 everything" ----------------------


def test_a_real_clip_is_still_served(client):
    """A guard that refuses everything is not a fix."""
    response = client.get("/api/clips/job123/clip_01.mp4/video")

    assert response.status_code == 200, response.text
    assert response.content == b"REAL_CLIP_BYTES"


def test_a_missing_clip_inside_the_root_still_404s(client):
    """The ordinary not-found path must be unchanged."""
    assert client.get("/api/clips/job123/absent.mp4/video").status_code == 404


# --- the helper, directly ----------------------------------------------------------------


def test_the_helper_resolves_inside_the_root(tmp_path, monkeypatch):
    root = tmp_path / "clips"
    (root / "job1").mkdir(parents=True)
    monkeypatch.setattr(settings, "clips_dir", root, raising=False)

    path, name = main._clip_path("job1", "a.mp4")

    assert name == "a.mp4"
    assert path == (root / "job1" / "a.mp4").resolve()
    assert path.is_relative_to(root.resolve())


@pytest.mark.parametrize("job_id,filename", [("..", "x"), (".", "."), ("", "x"), ("job1", "")])
def test_the_helper_refuses_anything_that_is_not_root_slash_job_slash_file(
    tmp_path, monkeypatch, job_id, filename
):
    root = tmp_path / "clips"
    (root / "job1").mkdir(parents=True)
    monkeypatch.setattr(settings, "clips_dir", root, raising=False)

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        main._clip_path(job_id, filename)
    assert excinfo.value.status_code == 404


def test_a_symlink_out_of_the_root_is_refused(tmp_path, monkeypatch):
    """`resolve()` is what makes this hold; a containment check on the unresolved path would not.

    An operator can create a symlink inside `clips_dir` without intending to publish its target.
    """
    root = tmp_path / "clips"
    job = root / "job1"
    job.mkdir(parents=True)
    outside = tmp_path / "secret.txt"
    outside.write_text("SUPER_SECRET_COOKIE")
    (job / "escape.mp4").symlink_to(outside)
    monkeypatch.setattr(settings, "clips_dir", root, raising=False)

    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        main._clip_path("job1", "escape.mp4")
