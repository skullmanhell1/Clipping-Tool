"""Durable storage for :class:`worker.models.Job` records.

The job store was process memory only, so a restart — a deploy, a crash, an OOM kill —
silently discarded every job. The clips themselves survived on disk and in the publish
history, which made the loss look like corruption rather than absence: the history view
listed clips whose job no longer existed, so their download links returned 404.

Design notes:

* **One JSON blob per job, plus indexed scalar columns.** Job records are read whole
  (the API serialises the entire job on every poll) and their shape changes with every
  feature, so a column-per-field schema would demand a migration for each new option
  while buying nothing. ``batch_id`` and ``created_at`` are lifted out because they are
  the only fields ever *queried* rather than returned.
* **Best-effort by design.** Persistence failures must never fail a job: losing a record
  is a reporting problem, whereas raising here would abort a video render that may have
  been running for minutes. Every write is wrapped and logged.
* **Interrupted jobs are resolved on load,** not left claiming to be running. See
  :meth:`Job_Persistence.load_all`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from config import settings
from worker.models import Job, JobStatus

logger = logging.getLogger(__name__)

#: Statuses that cannot survive a process restart: no worker thread exists any more, so
#: nothing will ever advance them. Recorded as failures on load.
INTERRUPTED_STATUSES = frozenset({JobStatus.QUEUED.value, JobStatus.PROCESSING.value})

#: Stage text given to a job that was interrupted mid-flight.
INTERRUPTED_STAGE = "Interrupted by restart"

#: Error text given to a job that was interrupted mid-flight.
INTERRUPTED_ERROR = (
    "The server restarted while this job was running, so it was not completed. "
    "Re-submit the source to try again."
)

#: Error text for an interrupted job that *can* be resumed (I5).
#:
#: Distinct from the message above because the two call for different actions, and telling someone
#: to re-submit a job whose finished clips are sitting on disk is advice that costs them the whole
#: render a second time.
INTERRUPTED_RESUMABLE_ERROR = (
    "The server restarted while this job was running. {done} of {planned} clip(s) were "
    "finished; resume the job to render the rest."
)


class Job_Persistence:
    """SQLite-backed durable store for job records."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or settings.jobs_db)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection, committing on success and **closing** either way.

        A context manager rather than a bare connection because
        ``with sqlite3.connect(...) as conn`` commits or rolls back the transaction and
        does **not** close the connection — it is a transaction manager, not a closing
        one. Every call site here uses ``with``, so the connections were only reclaimed
        whenever the garbage collector happened to get to them, leaking descriptors in
        the meantime. That matters because ``save()`` runs on every job update, including
        each progress tick of a render.

        Measured before this change: 26 descriptors still held after 200 saves.

        The inner ``with conn`` preserves the commit/rollback semantics every call site
        was already relying on, so no call site needs to change.
        """
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init(self) -> None:
        with self._lock, self._connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY,
                  batch_id TEXT,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  status TEXT NOT NULL,
                  data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_batch ON jobs(batch_id);
                """
            )
            # U12's `owner` deliberately has **no column of its own**. This schema's rule is
            # that a field is promoted out of the JSON blob when it is *queried* (see the
            # module docstring), and nothing queries owner: `JobStore` holds every job in
            # memory - `load_all` selects all of them at startup and the table is pruned to
            # MAX_PERSISTED_JOBS - so owner filtering happens there, over a few hundred
            # objects. A column and index would buy nothing and would put the same fact in
            # two places, where the copy nothing reads is free to be wrong.

    # -- writes ------------------------------------------------------------

    def save(self, job: Job) -> None:
        """Insert or replace one job record.

        Never raises: see the module docstring on why a persistence failure must not
        propagate into the pipeline.
        """
        try:
            payload = job.to_dict()
            with self._lock, self._connect() as db:
                db.execute(
                    "INSERT INTO jobs (id,batch_id,created_at,updated_at,status,data) "
                    "VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET batch_id=excluded.batch_id,"
                    "updated_at=excluded.updated_at,status=excluded.status,"
                    "data=excluded.data",
                    (
                        job.id,
                        job.batch_id,
                        float(job.created_at),
                        float(job.updated_at),
                        job.status.value,
                        json.dumps(payload),
                    ),
                )
        except Exception:  # pragma: no cover - defensive
            logger.exception("failed to persist job %s", getattr(job, "id", "?"))

    def delete(self, job_id: str) -> None:
        """Remove one job record."""
        try:
            with self._lock, self._connect() as db:
                db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        except Exception:  # pragma: no cover - defensive
            logger.exception("failed to delete job %s", job_id)

    # -- reads -------------------------------------------------------------

    def load_all(self) -> list[Job]:
        """Every persisted job, newest first, with interrupted ones resolved.

        A job stored as ``queued`` or ``processing`` was mid-flight when the process
        died. No thread is going to pick it up, so reporting it as still running would
        leave a permanently spinning progress bar in the UI. Such jobs are rewritten as
        ``failed`` with an explanation — and the rewrite is persisted, so the resolution
        is durable rather than recomputed on every start-up.
        """
        try:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT data FROM jobs ORDER BY created_at DESC"
                ).fetchall()
        except Exception:  # pragma: no cover - defensive
            logger.exception("failed to load persisted jobs from %s", self.path)
            return []

        jobs: list[Job] = []
        interrupted: list[Job] = []
        for row in rows:
            try:
                data = json.loads(row["data"])
            except (TypeError, ValueError):
                logger.warning("skipping unreadable job record in %s", self.path)
                continue
            job = Job.from_dict(data)
            if str(data.get("status") or "") in INTERRUPTED_STATUSES:
                job.status = JobStatus.FAILED
                job.stage = INTERRUPTED_STAGE
                # I5: say whether resuming is possible, and how much is already done. A job that
                # recorded its plan can render only the missing clips; one interrupted before
                # selection finished genuinely has to start over.
                planned = len(job.planned_clips or [])
                done = len(job.clips or [])
                if planned and done < planned:
                    job.error = INTERRUPTED_RESUMABLE_ERROR.format(done=done, planned=planned)
                else:
                    job.error = INTERRUPTED_ERROR
                job.updated_at = time.time()
                interrupted.append(job)
            jobs.append(job)

        for job in interrupted:
            self.save(job)
        if interrupted:
            logger.warning(
                "marked %d interrupted job(s) as failed after restart", len(interrupted)
            )
        return jobs

    def prune(self, *, keep: int) -> int:
        """Delete all but the ``keep`` newest records, returning how many were removed.

        Bounds the table so a long-lived instance does not accumulate job rows forever.
        ``keep <= 0`` is treated as "keep nothing pruned" and is a no-op, so a
        misconfiguration cannot wipe the store.
        """
        if keep <= 0:
            return 0
        try:
            with self._lock, self._connect() as db:
                cursor = db.execute(
                    "DELETE FROM jobs WHERE id NOT IN "
                    "(SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?)",
                    (keep,),
                )
                return int(cursor.rowcount or 0)
        except Exception:  # pragma: no cover - defensive
            logger.exception("failed to prune jobs in %s", self.path)
            return 0

    def count(self) -> int:
        """Number of persisted job records."""
        try:
            with self._connect() as db:
                row = db.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
            return int(row["n"]) if row else 0
        except Exception:  # pragma: no cover - defensive
            return 0


_persistence: Optional[Job_Persistence] = None
_lock = threading.Lock()


def get_job_persistence() -> Job_Persistence:
    """Return the shared :class:`Job_Persistence` singleton."""
    global _persistence
    with _lock:
        if _persistence is None:
            _persistence = Job_Persistence()
        return _persistence
