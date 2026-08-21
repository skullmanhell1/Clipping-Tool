"""WAL journalling is optional, because on a Docker Desktop bind mount it is impossible.

**Reported from a real run**, in Docker Desktop on Windows: the container booted, served `/healthz`,
served the dashboard, and then a request to the publish-history endpoint returned a 500 ending in

    File "/app/publishers/history.py", line 56, in _init
        db.executescript(\"\"\"
    sqlite3.OperationalError: attempt to write a readonly database

The message names the wrong cause, which is what made it interesting. `storage/` was writable —
`ensure_local_dirs()` had already created `uploads/`, `clips/` and `temp/` inside it during startup,
and the app would not have booted otherwise. Nothing was read-only.

`PRAGMA journal_mode=WAL` is the one pragma in either store that depends on the **filesystem** rather
than on SQLite. WAL needs a shared-memory `-shm` sidecar and mmap, which SMB, CIFS, virtiofs and 9p
do not provide — which is to say, Docker Desktop bind mounts on Windows and macOS. SQLite reports its
inability to create those sidecars as the database being read-only.

Two things follow, and both are fixed here:

* **WAL must be attempted separately from the schema.** Inside one `executescript`, a WAL failure
  takes the `CREATE TABLE` statements down with it, so the store is left unusable on a filesystem
  where nothing was actually wrong. That is the reported crash.
* **WAL is an optimisation, not a correctness requirement.** The default rollback journal is slower
  under concurrent writes and completely correct, so failing to get WAL must not be fatal.

Both SQLite stores had the identical pattern — `publishers/history.py` and
`worker/job_persistence.py`. Only the first was reported, because the jobs database is created
lazily on the first job while the history store is touched by the dashboard, so history simply
happened to be reached first. The jobs database would have failed the same way on the next click.
"""

from __future__ import annotations

import sqlite3

import pytest

from publishers.history import HistoryStore
from worker import job_persistence
from worker.job_persistence import Job_Persistence, _try_wal


@pytest.fixture(autouse=True)
def _forget_wal_warning():
    """The one-shot warning latch is module state, so each test starts from a clean one."""
    job_persistence._WAL_STATE.clear()
    yield
    job_persistence._WAL_STATE.clear()


class _RefusesWal:
    """A connection that fails exactly the way a network mount does, and only for WAL.

    Narrow on purpose: a stub that failed *every* statement would prove nothing about whether the
    schema survives a WAL refusal, which is the whole question.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str, *args):
        self.statements.append(sql)
        if "journal_mode=WAL" in sql:
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return None


def test_wal_refusal_is_tolerated_and_reported(caplog):
    """The fix, at the point of failure."""
    conn = _RefusesWal()

    with caplog.at_level("WARNING"):
        assert _try_wal(conn, "unit test") is False

    assert "WAL journalling unavailable" in caplog.text
    assert "Docker Desktop" in caplog.text, (
        "the warning must name the situation, or the next person diagnoses a permissions problem "
        "that does not exist -- which is exactly what happened here"
    )


def test_wal_success_is_not_reported(caplog):
    """A warning on every healthy boot is noise, and noise is what stops a warning being read."""

    class _AcceptsWal:
        def execute(self, sql, *args):
            return None

    with caplog.at_level("WARNING"):
        assert _try_wal(_AcceptsWal(), "unit test") is True

    assert "WAL journalling unavailable" not in caplog.text


def test_the_warning_is_logged_once_not_per_connection(caplog):
    """Every query opens a connection, so an un-latched warning would flood the log."""
    conn = _RefusesWal()

    with caplog.at_level("WARNING"):
        for _ in range(5):
            _try_wal(conn, "unit test")

    assert caplog.text.count("WAL journalling unavailable") == 1


# --------------------------------------------------------------------------- #
# The stores themselves, on a filesystem that refuses WAL                     #
# --------------------------------------------------------------------------- #


def _no_wal_connect(real_connect):
    """Wrap `sqlite3.connect` so the returned connection refuses only the WAL pragma."""

    class _Proxy:
        def __init__(self, inner):
            object.__setattr__(self, "_inner", inner)

        def execute(self, sql, *args):
            if "journal_mode=WAL" in sql:
                raise sqlite3.OperationalError("attempt to write a readonly database")
            return self._inner.execute(sql, *args)

        def executescript(self, script):
            # `executescript` is refused too, and that is the whole point rather than thoroughness.
            # A real network mount cannot provide WAL *however the pragma is issued*, so a proxy that
            # only guarded `execute` would let the pragma be moved back inside the schema script --
            # which is precisely the bug -- and every test here would still pass. Found by mutation.
            if "journal_mode=WAL" in script:
                raise sqlite3.OperationalError("attempt to write a readonly database")
            return self._inner.executescript(script)

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __setattr__(self, name, value):
            # Forwarded, or `row_factory = sqlite3.Row` lands on the proxy and every row comes back
            # as a tuple -- which fails inside the store with a message about tuple indices that has
            # nothing to do with what is being tested.
            setattr(self._inner, name, value)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

    def connect(*args, **kwargs):
        return _Proxy(real_connect(*args, **kwargs))

    return connect


def test_the_history_store_initialises_without_wal(tmp_path, monkeypatch):
    """The reported crash, reproduced and fixed.

    Before the fix this raised `OperationalError: attempt to write a readonly database` from
    `_init`, because the pragma and the `CREATE TABLE` statements shared one `executescript`.
    """
    monkeypatch.setattr(sqlite3, "connect", _no_wal_connect(sqlite3.connect))

    store = HistoryStore(tmp_path / "history.db")

    # Usable, not merely constructed: the tables the dashboard reads must exist.
    # `history()` returns {"clips": [...], "attempts": [...]}, so the shape is asserted rather than
    # equality against a list -- the point is that both tables exist and can be queried.
    listed = store.history(10, None)
    assert isinstance(listed, dict) and listed, "history() returned nothing queryable"
    assert all(v == [] for v in listed.values()), listed
    assert store.campaigns() == []


def test_the_history_store_still_writes_without_wal(tmp_path, monkeypatch):
    """The rollback journal is correct, so a degraded store is a working store.

    Without this the fix could pass by leaving a store that opens and then cannot record anything —
    which would move the failure from the dashboard to the first publish.
    """
    monkeypatch.setattr(sqlite3, "connect", _no_wal_connect(sqlite3.connect))
    store = HistoryStore(tmp_path / "history.db")

    campaign = store.save_campaign("launch week", {}, "")

    assert [c.name for c in store.campaigns()] == ["launch week"]
    assert campaign.name == "launch week"


def test_the_jobs_store_initialises_without_wal(tmp_path, monkeypatch):
    """The same defect, in the store that was *not* reported.

    `worker/job_persistence.py` had the identical `PRAGMA journal_mode=WAL` inside its schema script.
    It went unreported only because the jobs database is created lazily on the first job while the
    history store is touched by the dashboard — so history was reached first. This would have failed
    on the next click.
    """
    monkeypatch.setattr(sqlite3, "connect", _no_wal_connect(sqlite3.connect))

    store = Job_Persistence(tmp_path / "jobs.db")

    assert store.load_all() == []


def test_the_jobs_store_round_trips_without_wal(tmp_path, monkeypatch):
    """Persistence is the point of this store, so degraded must still mean durable."""
    monkeypatch.setattr(sqlite3, "connect", _no_wal_connect(sqlite3.connect))
    path = tmp_path / "jobs.db"

    # The real `Job`, not a stub: `save` reads `job.status.value`, so a plain string would be
    # swallowed by this store's never-raise contract and the assertion below would fail for a reason
    # that has nothing to do with WAL.
    from worker.models import Job, JobStatus, ProcessingOptions

    job = Job(input_type="file", source="/tmp/s.mp4", options=ProcessingOptions(), title="t")
    job.status = JobStatus.QUEUED

    store = Job_Persistence(path)
    store.save(job)

    # A second instance proves it reached the file rather than living in memory.
    reloaded = Job_Persistence(path).load_all()
    assert [j.id for j in reloaded] == [job.id]


# --------------------------------------------------------------------------- #
# And the normal case still gets WAL                                          #
# --------------------------------------------------------------------------- #


def test_wal_is_still_used_where_the_filesystem_supports_it(tmp_path):
    """The discriminator. Without it, "tolerate WAL failure" could mean "never ask for WAL".

    `tmp_path` is a local filesystem, so WAL must genuinely be in force — the concurrency it buys is
    why the pragma is there at all.
    """
    HistoryStore(tmp_path / "history.db")

    with sqlite3.connect(tmp_path / "history.db") as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

    assert mode.lower() == "wal", f"WAL was not enabled on a filesystem that supports it: {mode}"


def test_the_jobs_store_also_gets_wal_locally(tmp_path):
    Job_Persistence(tmp_path / "jobs.db")

    with sqlite3.connect(tmp_path / "jobs.db") as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

    assert mode.lower() == "wal"
