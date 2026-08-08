"""The SQLite stores close their connections.

``with sqlite3.connect(...) as conn`` is a *transaction* manager: it commits or rolls
back, and does **not** close the connection. Every call site in both stores uses that
form, so connections were only reclaimed whenever the garbage collector got to them and
descriptors accumulated in the meantime.

It matters most in ``Job_Persistence.save``, which runs on every job update — including
each progress tick of a render, so a single long job performs many writes.

Measured before the fix: 26 descriptors still held after 200 saves.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from publishers.history import HistoryStore
from worker.job_persistence import Job_Persistence
from worker.models import ClipResult, Job, ProcessingOptions

#: Enough iterations that a per-call leak is unambiguous rather than noise.
ITERATIONS = 200

#: Some slack for WAL/shm handles and interpreter noise. A per-call leak would show up as
#: roughly ITERATIONS descriptors, so this threshold cannot hide the defect.
ALLOWED_GROWTH = 5

pytestmark = pytest.mark.skipif(
    not os.path.isdir("/proc/self/fd"),
    reason="descriptor counting needs /proc (Linux)",
)


def _open_descriptors() -> int:
    return len(os.listdir(f"/proc/{os.getpid()}/fd"))


def _job() -> Job:
    job = Job(input_type="file", source="/tmp/source.mp4", options=ProcessingOptions())
    job.clips = [ClipResult(id="c1", filename="c1.mp4", start=0.0, end=5.0, duration=5.0)]
    return job


def test_repeated_saves_do_not_leak_descriptors(tmp_path: Path):
    """``save`` is called once per job update; it must not cost a descriptor each time."""
    store = Job_Persistence(tmp_path / "jobs.db")
    job = _job()
    store.save(job)  # first call creates the WAL/shm handles; measure after that

    before = _open_descriptors()
    for _ in range(ITERATIONS):
        store.save(job)
    growth = _open_descriptors() - before

    assert growth <= ALLOWED_GROWTH, (
        f"{ITERATIONS} saves leaked {growth} descriptors; connections are not being closed"
    )


def test_repeated_loads_do_not_leak_descriptors(tmp_path: Path):
    """Reads go through the same connection helper, so they are covered too."""
    store = Job_Persistence(tmp_path / "jobs.db")
    store.save(_job())
    store.load_all()

    before = _open_descriptors()
    for _ in range(ITERATIONS):
        store.load_all()
    growth = _open_descriptors() - before

    assert growth <= ALLOWED_GROWTH, f"{ITERATIONS} loads leaked {growth} descriptors"


def test_history_store_does_not_leak_descriptors(tmp_path: Path):
    """``HistoryStore`` has the same pattern at 14 call sites."""
    store = HistoryStore(tmp_path / "history.db")
    store.history(10)

    before = _open_descriptors()
    for _ in range(ITERATIONS):
        store.history(10)
    growth = _open_descriptors() - before

    assert growth <= ALLOWED_GROWTH, f"{ITERATIONS} reads leaked {growth} descriptors"


def test_writes_are_still_committed(tmp_path: Path):
    """The fix must not break the transaction semantics the call sites rely on.

    The inner ``with conn`` is what commits; a naive "just close it" change would drop
    writes instead of leaking descriptors, which is far worse.
    """
    db = tmp_path / "jobs.db"
    job = _job()
    Job_Persistence(db).save(job)

    # A brand-new store over the same file only sees committed data.
    reloaded = Job_Persistence(db).load_all()
    assert [j.id for j in reloaded] == [job.id]


def test_a_failing_write_does_not_leave_the_connection_open(tmp_path: Path):
    """An exception inside the block still closes the connection (the ``finally``)."""
    store = Job_Persistence(tmp_path / "jobs.db")
    store.save(_job())

    before = _open_descriptors()
    for _ in range(50):
        # sqlite3.OperationalError specifically, not a blanket Exception: a bare
        # `raises(Exception)` would also pass if the connection helper itself broke,
        # which is the opposite of what this asserts.
        with pytest.raises(sqlite3.OperationalError):
            with store._connect() as db:
                db.execute("SELECT * FROM no_such_table")
    growth = _open_descriptors() - before

    assert growth <= ALLOWED_GROWTH, f"failed writes leaked {growth} descriptors"
