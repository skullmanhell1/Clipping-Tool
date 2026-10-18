"""Clip downloads cannot read a file outside the clips directory.

CodeQL's ``py/path-injection`` flagged six paths through the two clip-download routes at
security-severity 7.5, and that gate had been failing on ``main`` since at least 13 August. It was
the **only** failing check on the repository once the earlier Actions outage cleared, so it was
holding the whole pipeline red.

The finding was not a straightforward exploit. Both routes applied ``Path(...).name`` to each URL
component, and that genuinely strips directory parts — ``Path("../../etc/passwd").name`` is
``"passwd"``. What was missing was a **containment check**: a sanitiser applied component-wise and
then trusted, with nothing downstream proving the result lands under ``clips_dir``.

Two reasons that is worth fixing rather than suppressing:

* It is one refactor from mattering. Any future caller that passes a value it has not run through
  ``.name`` inherits a filesystem read with no guard at all.
* ``.name`` does not stop a **symlink** planted inside the clips directory from pointing outside it.
  That case was genuinely reachable, and it is the one the tests below single out.

So the resolver now resolves the candidate with ``os.path.realpath`` — which collapses ``..`` *and*
follows symlinks — and then proves the result is under the root before touching it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from api.main import _resolve_clip_file
from config import settings


@pytest.fixture
def clips_root(tmp_path, monkeypatch):
    """An isolated clips directory holding one legitimate clip in job ``j1``."""
    root = tmp_path / "clips"
    (root / "j1").mkdir(parents=True)
    (root / "j1" / "clip_01.mp4").write_bytes(b"\0" * 64)
    monkeypatch.setattr(settings, "clips_dir", root)
    return root


def test_a_legitimate_clip_resolves(clips_root):
    """The guard must not become a second failure mode of its own."""
    path, safe_name = _resolve_clip_file("j1", "clip_01.mp4")
    assert path == Path(clips_root / "j1" / "clip_01.mp4").resolve()
    assert safe_name == "clip_01.mp4"


@pytest.mark.parametrize(
    "filename",
    [
        "../../../etc/passwd",
        "..%2f..%2fetc%2fpasswd",
        "....//....//etc/passwd",
        "/etc/passwd",
        "..\\..\\windows\\win.ini",
        "clip_01.mp4/../../../../etc/passwd",
    ],
)
def test_traversal_in_the_filename_is_refused(clips_root, filename):
    with pytest.raises(HTTPException) as caught:
        _resolve_clip_file("j1", filename)
    # 404, not 400: an attacker learns nothing about whether the target exists.
    assert caught.value.status_code == 404


@pytest.mark.parametrize("job_id", ["../..", "../../etc", "/etc", "..\\..", "j1/../.."])
def test_traversal_in_the_job_id_is_refused(clips_root, job_id):
    with pytest.raises(HTTPException) as caught:
        _resolve_clip_file(job_id, "clip_01.mp4")
    assert caught.value.status_code == 404


def test_a_symlink_out_of_the_clips_directory_is_refused(clips_root, tmp_path):
    """The case ``Path(...).name`` could not have caught.

    A link planted inside the clips directory has a perfectly ordinary basename, so component-wise
    sanitisation passes it through and the old code would have opened the target. This is why the
    resolver uses ``realpath`` (which follows links) rather than ``Path.is_relative_to`` on an
    unresolved path (which would not).
    """
    secret = tmp_path / "secret.txt"
    secret.write_text("not yours to read")
    (clips_root / "j1" / "escape.mp4").symlink_to(secret)

    with pytest.raises(HTTPException) as caught:
        _resolve_clip_file("j1", "escape.mp4")
    assert caught.value.status_code == 404


def test_a_symlink_within_the_clips_directory_is_allowed(clips_root):
    """Containment, not a blanket ban on links.

    A link that stays inside the root is legitimate — the storage backend may well create one — and
    refusing it would be a different bug. The rule is where the target lands, not how it is reached.
    """
    (clips_root / "j1" / "alias.mp4").symlink_to(clips_root / "j1" / "clip_01.mp4")
    path, _name = _resolve_clip_file("j1", "alias.mp4")
    assert path.is_file()


def test_a_missing_file_is_a_404_not_a_crash(clips_root):
    with pytest.raises(HTTPException) as caught:
        _resolve_clip_file("j1", "nope.mp4")
    assert caught.value.status_code == 404


def test_a_directory_is_not_served_as_a_file(clips_root):
    """``clips_dir/j1`` resolves and is contained, but it is not a clip."""
    with pytest.raises(HTTPException) as caught:
        _resolve_clip_file("j1", "")
    assert caught.value.status_code == 404


def test_a_symlink_to_a_prefix_sharing_sibling_is_refused(clips_root, tmp_path):
    """The prefix check compares against ``root + os.sep``, not bare ``root``.

    ``startswith(root)`` is the classic way a containment check is written wrong: with
    ``root = /tmp/clips``, a target of ``/tmp/clips-old/leak.mp4`` satisfies it and escapes.

    Reaching that requires a **symlink**, and this is worth spelling out because the first version
    of this test could not do it. Going through the URL parameters cannot: both components are
    reduced to basenames first, so the candidate is always ``root/<basename>/<basename>`` and the
    sibling is unreachable — the test passed with the separator removed, i.e. it proved nothing. A
    link planted inside the clips directory is what actually crosses that boundary.
    """
    sibling = clips_root.parent / f"{clips_root.name}-old"
    sibling.mkdir()
    leak = sibling / "leak.mp4"
    leak.write_bytes(b"x")
    (clips_root / "j1" / "sneaky.mp4").symlink_to(leak)

    with pytest.raises(HTTPException) as caught:
        _resolve_clip_file("j1", "sneaky.mp4")
    assert caught.value.status_code == 404


# --------------------------------------------------------------------------- #
# The same guarantee, asserted through the HTTP routes                         #
# --------------------------------------------------------------------------- #
# Everything above tests `_resolve_clip_file`. That is the right place for the symlink and
# prefix-sharing cases, which are awkward to reach over HTTP — but on its own it leaves a real
# gap, and PR #148 named it precisely while describing the *other* route:
#
#   "`download_clip` was only accidentally safe, because it also looks the job up and
#    `store.get('..')` returns None. That is second-order protection that stops holding the
#    moment someone reorders the checks."
#
# Exactly so, and it applies to the resolver too. A helper-level test proves the helper is
# correct; it does not prove the routes still *call* it.
#
# **And that observation turns out to be load-bearing here, which is worth writing down.** Both
# routes cross-check the requested filename against the job record, and that check independently
# defeats traversal: `store.get("..")` returns `None`, and a traversing *filename* reduces under
# `.name` to something no clip is called. Measured by mutation:
#
#   * replace the resolver with the old `Path(x).name` construction and leave the job cross-check
#     -> every behavioural test below still passes. Only `test_every_clip_route_goes_through_the
#     _resolver` fails. The route is "accidentally safe" exactly as the quote describes.
#   * remove both -> three of the traversal cases leak the planted secret and the behavioural
#     tests fail.
#
# So neither style of test is redundant and neither is sufficient. The behavioural ones prove the
# vulnerability is really closed end to end (they fail on the genuine pre-fix code). The structural
# one is what notices containment being removed while a second-order check happens to mask it —
# which is the state this code was in before, and the state a future refactor would restore.
#
# Note also which spellings reach the handler: only the percent-encoded forms arrive as a literal
# `..` path component. A bare `..` is normalised by the client and router before dispatch, so it
# never reaches the route at all. That is why the encoded variants are the load-bearing cases.
#
# So these go through the real ASGI app, unauthenticated (`api_auth_token = None`, which is the
# default configuration and the one the original report was filed against), against a planted
# secret one directory above `clips_dir` — which in a real deployment is `storage/`, holding
# `jobs.db`, `history.db`, every uploaded source video, and whatever the operator put there.


@pytest.fixture
def served_app(tmp_path, monkeypatch):
    """The real app over an isolated ``storage/`` tree, with no auth token configured.

    Yields ``(client, secret_path, job_id, clip_name)``. The secret sits *directly inside*
    ``storage/`` — one level above ``clips_dir`` — because that is the position the traversal
    reached and the position the cookie-jar guidance used to recommend.
    """
    from fastapi.testclient import TestClient

    from api.main import app

    storage = tmp_path / "storage"
    clips = storage / "clips"
    (clips / "j1").mkdir(parents=True)
    (clips / "j1" / "clip_01.mp4").write_bytes(b"\0" * 64)

    secret = storage / "cookies.txt"
    secret.write_text("SECRET-COOKIE-JAR", encoding="utf-8")

    monkeypatch.setattr(settings, "clips_dir", str(clips))
    monkeypatch.setattr(settings, "api_auth_token", None)  # the default: unauthenticated

    # A real job record carrying the clip. Both routes cross-check the filename against the job
    # (deliberately — without it they would serve sidecar JSON and intermediates), so the positive
    # control needs one or it 404s for a reason that has nothing to do with containment. Registering
    # it is also what makes the negative cases meaningful: the traversal is refused by the *path*
    # guard, not incidentally by the job lookup, which is the "only accidentally safe" trap.
    from worker.jobs import get_manager
    from worker.models import ClipResult, Job, JobStatus, ProcessingOptions

    job = Job(input_type="file", source="seed.mp4", options=ProcessingOptions())
    job.id = "j1"
    job.status = JobStatus.COMPLETED
    job.clips = [ClipResult(id="c1", filename="clip_01.mp4", start=0.0, end=4.0, duration=4.0)]
    get_manager().store.add(job)

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, secret, "j1", "clip_01.mp4"


#: The traversal spellings from the original report, plus the encodings a denylist would miss.
_TRAVERSALS = [
    "%2E%2E",
    "..",
    "%2e%2e",
    "....%2F%2F",
    "%2e%2e%2f",
    ".%2E",
]


@pytest.mark.parametrize("job", _TRAVERSALS)
@pytest.mark.parametrize("route", ["video", "download"])
def test_no_route_serves_a_file_above_the_clips_directory(served_app, job, route):
    """Unauthenticated traversal must not read `storage/cookies.txt` through either route.

    ``Path("..").name`` is ``".."`` — pathlib treats it as an ordinary final component — so
    ``.name`` alone never stopped this. The assertion checks the **bytes**, not just the status
    code: a 200 carrying an error page and a 200 carrying the credential are very different
    outcomes, and only one of them is a breach.
    """
    client, secret, _job_id, _clip = served_app

    response = client.get(f"/api/clips/{job}/cookies.txt/{route}")

    assert secret.read_text(encoding="utf-8") == "SECRET-COOKIE-JAR"  # fixture is still valid
    assert b"SECRET-COOKIE-JAR" not in response.content, (
        f"/{route} leaked a file above clips_dir for job_id={job!r} (status {response.status_code})"
    )
    assert response.status_code == 404, response.status_code


@pytest.mark.parametrize("route", ["video", "download"])
def test_the_clips_root_itself_is_not_servable(served_app, route):
    """An empty job component resolves to the root, which is a directory, not a clip."""
    client, _secret, _job_id, clip = served_app
    assert client.get(f"/api/clips/%2E/{clip}/{route}").status_code == 404


def test_a_real_clip_is_still_served(served_app):
    """The guard must not decay into "404 everything", which would pass every test above."""
    client, _secret, job_id, clip = served_app

    response = client.get(f"/api/clips/{job_id}/{clip}/video")
    assert response.status_code == 200, response.text
    assert response.content == b"\0" * 64


def test_a_genuine_miss_inside_the_root_is_still_a_plain_404(served_app):
    """A missing clip and a refused traversal are both 404, which is intended.

    Distinguishing them in the response would tell an attacker which paths exist.
    """
    client, _secret, job_id, _clip = served_app
    assert client.get(f"/api/clips/{job_id}/absent.mp4/video").status_code == 404


@pytest.mark.parametrize("route", ["video", "download"])
def test_every_clip_route_goes_through_the_resolver(route):
    """A third route over the same directory is how the looser one gets forgotten.

    Asserted structurally as well as behaviourally: the tests above cover the two routes that
    exist today, and this one fails if a future route is added over ``clips_dir`` without the
    resolver. Comments are stripped, because the resolver's own docstring names the unsafe
    pattern in order to explain why it is unsafe.
    """
    import inspect

    from api import main as api_main

    handler = {"video": api_main.download_video_only, "download": api_main.download_clip}[route]
    raw = inspect.getsource(handler)
    code = "\n".join(
        line.split("#", 1)[0] for line in raw.splitlines() if not line.strip().startswith("#")
    )
    assert "_resolve_clip_file(" in code, f"{handler.__name__} no longer uses the resolver"
    assert "Path(settings.clips_dir)" not in code, (
        f"{handler.__name__} builds a clips path directly again, bypassing containment"
    )
