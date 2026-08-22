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
