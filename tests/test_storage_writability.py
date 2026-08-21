"""Storage has to be *writable*, and existence is not evidence of that.

**Reported from a real run**, on Docker Desktop for Windows, with `storage/` bind-mounted from a host
directory the container's UID 10001 could not write: the image booted, served `/healthz`, served the
dashboard, and then `GET /api/history` returned a 500 ending in

    File "/app/publishers/history.py", line 58, in _init
        db.executescript(\"\"\"
    sqlite3.OperationalError: attempt to write a readonly database

This is the second report to end in that exact sentence, and the first fix -- making
`PRAGMA journal_mode=WAL` optional, see `tests/test_sqlite_wal_fallback.py` -- was correct and did
not prevent it. Two independent faults produced one symptom, and fixing the first moved the message
one line down, from the pragma to the `executescript` beneath it.

The second fault is that **nothing ever checked whether storage was writable**. `ensure_local_dirs()`
called `mkdir(parents=True, exist_ok=True)` on each directory, which does nothing at all when the
directory is already there -- and `storage/uploads`, `storage/clips`, `storage/temp` and
`storage/transcripts` are committed to this repository as `.gitkeep` files, so they exist in every
clone. Startup therefore wrote nothing and could not discover the problem. The comment in
`docker-compose.yml` asserting that the container would "exit immediately with `PermissionError`"
was verified against a checkout in which those directories were absent, which a real clone is not.

Two guarantees are covered here:

* `ensure_local_dirs()` proves writability with a **real write**, and a required directory that
  fails is fatal at boot, naming the path and the remedy.
* Both SQLite stores translate a filesystem failure into a message that says which file and why,
  instead of passing on SQLite's wording, which names neither and blames the database for what is
  usually a directory.

A note on `chmod`: this suite runs as root in some environments (including the container image used
for development), and **root is not constrained by mode bits** -- a `chmod(0o500)` directory is still
writable to it. A test that made a directory read-only and asserted a failure would therefore pass
by accident where it ran unprivileged and silently prove nothing where it did not, which is the kind
of vacuous check this project keeps finding. So the cases below use filesystem states that fail for
*any* uid -- a file where a directory is expected, a directory where a database file is expected --
and the genuine permission path was verified end-to-end in the built image, as UID 10001, against
the mount that produced the report.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

import config
from config import _assert_writable, settings
from worker.job_persistence import describe_store_failure

# --------------------------------------------------------------------------- #
# the write probe
# --------------------------------------------------------------------------- #


def test_the_probe_accepts_a_writable_directory(tmp_path):
    _assert_writable(tmp_path)  # does not raise


def test_the_probe_leaves_nothing_behind(tmp_path):
    """A probe file abandoned in `uploads/` would be a defect of its own.

    `uploads_dir` is enumerated by the API and the watch folder, so litter there is not cosmetic.
    """
    before = set(tmp_path.iterdir())

    _assert_writable(tmp_path)

    assert set(tmp_path.iterdir()) == before
    assert list(tmp_path.glob(".write-probe-*")) == []


def test_the_probe_really_writes_rather_than_asking_about_mode_bits(tmp_path, monkeypatch):
    """The distinction the whole fix rests on.

    `os.access(path, os.W_OK)` answers a question about mode bits, and every case that actually
    bites -- a read-only bind mount, a container UID no ACL covers -- can present a directory whose
    bits look fine. So this asserts a file is genuinely created, with an exclusive-create mode.
    """
    opened: list[tuple[Path, str]] = []
    real_open = Path.open

    def spy(self, mode="r", *args, **kwargs):
        opened.append((self, mode))
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy)

    _assert_writable(tmp_path)

    assert len(opened) == 1, opened
    probe, mode = opened[0]
    assert probe.parent == tmp_path
    assert probe.name.startswith(".write-probe-")
    # "x" so a stale leftover is an error rather than something silently overwritten.
    assert mode == "xb"


def test_the_probe_reports_a_directory_that_is_not_one(tmp_path):
    """Fails for every uid, root included, so it is worth asserting here."""
    not_a_dir = tmp_path / "afile"
    not_a_dir.write_text("x")

    with pytest.raises(OSError):
        _assert_writable(not_a_dir)


def test_the_probe_propagates_a_permission_error(tmp_path, monkeypatch):
    """The reported case, expressed the only way root can express it.

    See the module docstring: mode bits do not constrain root, so the permission itself is faked
    here and the real thing was verified in the container as UID 10001.
    """

    def refuse(self, mode="r", *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "open", refuse)

    with pytest.raises(PermissionError):
        _assert_writable(tmp_path)


# --------------------------------------------------------------------------- #
# ensure_local_dirs: required is fatal, optional warns
# --------------------------------------------------------------------------- #


@pytest.fixture
def local_dirs(tmp_path, monkeypatch):
    """Point every directory setting inside `tmp_path`, so the real repo tree is untouched."""
    for field in (*settings._REQUIRED_DIR_FIELDS, *settings._OPTIONAL_DIR_FIELDS):
        monkeypatch.setattr(settings, field, tmp_path / field, raising=False)
    return tmp_path


def test_the_happy_path_creates_every_directory(local_dirs):
    settings.ensure_local_dirs()

    for field in (*settings._REQUIRED_DIR_FIELDS, *settings._OPTIONAL_DIR_FIELDS):
        assert Path(getattr(settings, field)).is_dir(), field


def test_the_happy_path_leaves_no_probe_files(local_dirs):
    settings.ensure_local_dirs()

    assert list(local_dirs.rglob(".write-probe-*")) == []


def test_a_required_directory_that_cannot_be_created_is_fatal(local_dirs, monkeypatch):
    """A file where `storage/` should be. Fails for any uid."""
    blocked = local_dirs / "blocked"
    blocked.write_text("x")
    monkeypatch.setattr(settings, "storage_root", blocked)

    with pytest.raises(RuntimeError):
        settings.ensure_local_dirs()


def test_the_fatal_message_names_the_setting_the_path_and_the_remedy(local_dirs, monkeypatch):
    """The whole point of the change: SQLite's message named none of these three.

    Asserted rather than left to prose, because the reason the original report took a WAL diagnosis
    is that the error it produced described neither the path nor what to do about it.
    """
    blocked = local_dirs / "blocked"
    blocked.write_text("x")
    monkeypatch.setattr(settings, "uploads_dir", blocked)

    with pytest.raises(RuntimeError) as caught:
        settings.ensure_local_dirs()

    message = str(caught.value)
    assert "uploads_dir" in message
    assert str(blocked) in message
    assert "10001" in message
    assert "chown" in message


def test_a_required_directory_that_exists_but_cannot_be_written_is_fatal(local_dirs, monkeypatch):
    """The regression, stated directly: existence must not be accepted as writability.

    `mkdir` is made a no-op so it reports success exactly as it does on an already-present
    directory -- which is what every real clone has, because the `.gitkeep` files are committed.
    Under the old implementation that was the *only* check, so this passed and the app booted onto
    an unwritable mount.
    """
    monkeypatch.setattr(Path, "mkdir", lambda self, **kwargs: None)

    with pytest.raises(RuntimeError):
        settings.ensure_local_dirs()


def test_an_optional_directory_failure_warns_but_still_boots(local_dirs, monkeypatch, caplog):
    """`assets/` is legitimately read-only on a hardened deployment.

    Only optional extras write there -- a non-default emoji style, the b-roll cache -- so refusing
    to start would be the same over-reach in the opposite direction.
    """
    blocked = local_dirs / "blocked-music"
    blocked.write_text("x")
    monkeypatch.setattr(settings, "music_dir", blocked)

    with caplog.at_level("WARNING", logger="config"):
        settings.ensure_local_dirs()  # does not raise

    assert "music_dir" in caplog.text
    assert "not writable" in caplog.text


def test_every_directory_setting_is_classified(local_dirs):
    """A new directory setting must be declared required or optional, not silently unchecked.

    The defect this module exists for was a check that quietly covered nothing; a directory that
    belongs to neither tuple would be exactly that again.
    """
    #: Read-only by nature, and therefore neither created nor probed at startup. Each holds
    #: **vendored, committed** artefacts that the application only ever reads:
    #:
    #:   font_assets_dir  assets/fonts   -- handed to libass as `fontsdir`; read
    #:   face_model_dir   assets/models  -- the BlazeFace `.tflite` is loaded by path; read
    #:   sfx_dir          assets/sfx     -- looked up by name when an effect asks for one; read
    #:
    #: Listing them explicitly rather than filtering them out by pattern, so that adding a
    #: directory setting still forces a decision instead of matching an exemption by accident.
    read_only = {"font_assets_dir", "face_model_dir", "sfx_dir"}

    classified = set(settings._REQUIRED_DIR_FIELDS) | set(settings._OPTIONAL_DIR_FIELDS) | read_only
    declared = {
        name
        for name in type(settings).model_fields
        if name.endswith(("_dir", "_root")) and isinstance(getattr(settings, name, None), Path)
    }
    assert declared - classified == set(), sorted(declared - classified)


# --------------------------------------------------------------------------- #
# describe_store_failure: say which file, and why
# --------------------------------------------------------------------------- #


def test_a_missing_directory_is_named_as_such(tmp_path):
    path = tmp_path / "gone" / "history.db"

    message = describe_store_failure(path, "publish history", sqlite3.OperationalError("boom"))

    assert "does not exist" in message
    assert str(path.parent) in message
    assert "publish history" in message


def test_an_unwritable_directory_is_named_as_such(tmp_path, monkeypatch):
    """`os.access` is faked because root ignores mode bits; the container run covered the real one."""
    monkeypatch.setattr(os, "access", lambda *a, **k: False)

    message = describe_store_failure(
        tmp_path / "history.db", "publish history", sqlite3.OperationalError("readonly")
    )

    assert "is not writable by this user" in message
    assert str(tmp_path) in message


def test_an_unwritable_file_is_distinguished_from_an_unwritable_directory(tmp_path, monkeypatch):
    """The two call for different remedies, so the message must not conflate them."""
    db = tmp_path / "history.db"
    db.write_bytes(b"")
    monkeypatch.setattr(os, "access", lambda path, mode: Path(path) != db)

    message = describe_store_failure(db, "publish history", sqlite3.OperationalError("readonly"))

    assert "the file exists but is not writable" in message


def test_a_writable_directory_falls_back_to_a_neutral_cause(tmp_path):
    """Nothing about the filesystem looks wrong, so the message must not invent a cause."""
    message = describe_store_failure(
        tmp_path / "history.db", "job persistence", sqlite3.OperationalError("disk I/O error")
    )

    assert "the filesystem rejected the write" in message
    assert "disk I/O error" in message


def test_the_message_always_carries_sqlite_s_own_words(tmp_path):
    """Rewording must not discard the original, which is what a search engine will be given."""
    message = describe_store_failure(
        tmp_path / "history.db",
        "publish history",
        sqlite3.OperationalError("attempt to write a readonly database"),
    )

    assert "attempt to write a readonly database" in message


# --------------------------------------------------------------------------- #
# both stores, through their real constructors
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("label", ["publish history", "job persistence"])
def test_a_store_that_cannot_open_its_database_explains_why(tmp_path, label):
    """A directory where the database file should be: `sqlite3.connect` fails for any uid.

    Both stores are covered because the pattern has already been duplicated once and only one copy
    was reported -- the jobs database is created lazily on the first job while the history store is
    touched by the dashboard, so history is simply reached first.
    """
    from publishers.history import HistoryStore
    from worker.job_persistence import Job_Persistence

    store_cls = HistoryStore if label == "publish history" else Job_Persistence
    occupied = tmp_path / "occupied.db"
    occupied.mkdir()

    with pytest.raises(sqlite3.OperationalError) as caught:
        store_cls(path=occupied)

    message = str(caught.value)
    assert "cannot initialise the SQLite database" in message
    assert str(occupied) in message
    assert label in message


@pytest.mark.parametrize("label", ["publish history", "job persistence"])
def test_the_wrapped_failure_keeps_its_type_and_its_cause(tmp_path, label):
    """Callers that catch `sqlite3.OperationalError` must be unaffected; only the wording changes."""
    from publishers.history import HistoryStore
    from worker.job_persistence import Job_Persistence

    store_cls = HistoryStore if label == "publish history" else Job_Persistence
    occupied = tmp_path / "occupied.db"
    occupied.mkdir()

    with pytest.raises(sqlite3.OperationalError) as caught:
        store_cls(path=occupied)

    assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
    assert "unable to open database file" in str(caught.value.__cause__)


@pytest.mark.parametrize("label", ["publish history", "job persistence"])
def test_a_healthy_store_still_initialises(tmp_path, label):
    """The guard must not have made the working case fail."""
    from publishers.history import HistoryStore
    from worker.job_persistence import Job_Persistence

    store_cls = HistoryStore if label == "publish history" else Job_Persistence

    store = store_cls(path=tmp_path / "fine.db")

    assert Path(store.path).is_file()


def test_config_exposes_the_probe_for_the_stores_to_share():
    """Guards against the probe being quietly dropped, which would restore the original defect."""
    assert callable(config._assert_writable)
