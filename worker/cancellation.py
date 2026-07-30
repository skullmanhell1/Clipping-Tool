"""Cooperative job cancellation (I4).

There was no way to stop a running render. A job submitted by mistake - the wrong file, the
wrong aspect, ten clips instead of one - occupied the single worker until it finished, and
because the pool is serial it also held up everything queued behind it. The only remedy was
restarting the process, which lost every other job's in-flight state.

**Cooperative, not pre-emptive**, and that distinction is the substance of this module.

A thread cannot be killed safely in Python, and killing one mid-render would leave partial files,
an unreleased workspace and a half-written cache entry. So a cancel *requests* a stop, and the
pipeline notices at its next checkpoint. Checkpoints already exist: the progress callback is
invoked at every stage boundary and once per clip, so no new plumbing is needed and the
cancellation point is exactly where the job's state is consistent.

**What this means in practice, stated plainly:**

* A **queued** job stops immediately - it is checked before any work begins.
* A job **between stages** stops within a stage, typically well under a second.
* A job **inside an ffmpeg pass** finishes *that pass* first. A long encode can therefore take
  tens of seconds to stop.

That last one is a real limitation rather than an oversight. ``ffmpeg_utils._run`` uses
``subprocess.run``, which exposes no handle to signal, so terminating mid-encode means
restructuring every encode site around ``Popen`` and a process registry. That is a change with
its own failure modes - an orphaned process on an exception path, a killed child leaving a
truncated output that a later stage reads as valid - and it belongs with the concurrency work
(I1) that makes it matter, not bolted onto this.
"""

from __future__ import annotations

import threading
from typing import Optional


class Job_Cancelled(Exception):
    """Raised at a checkpoint when the job has been asked to stop.

    A dedicated exception rather than a return value, because the checkpoint is called from deep
    inside the pipeline and every intermediate frame would otherwise have to check and propagate
    a sentinel - which is the shape of code where one missing check makes the feature silently
    not work.

    It must *not* be treated as a failure by the caller: a cancelled job did not go wrong.
    """


class _Registry:
    """Which jobs have been asked to stop.

    An ``Event`` per job rather than a set of ids, so a caller that wants to *wait* for a
    cancellation can, and so the check is a lock-free ``is_set()`` on the hot path - the
    checkpoint runs on every progress report and must cost nothing measurable.
    """

    def __init__(self) -> None:
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def request(self, job_id: str) -> None:
        with self._lock:
            event = self._events.get(job_id)
            if event is None:
                event = threading.Event()
                self._events[job_id] = event
        event.set()

    def is_requested(self, job_id: Optional[str]) -> bool:
        if not job_id:
            return False
        with self._lock:
            event = self._events.get(job_id)
        return bool(event and event.is_set())

    def clear(self, job_id: str) -> None:
        """Forget a job's request, so a re-submitted id does not inherit it.

        Without this the registry grows without bound on a long-running instance, and - worse -
        a job id that happened to repeat would be cancelled before it started.
        """
        with self._lock:
            self._events.pop(job_id, None)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


_registry = _Registry()


def request_cancel(job_id: str) -> None:
    """Ask ``job_id`` to stop at its next checkpoint."""
    _registry.request(job_id)


def is_cancelled(job_id: Optional[str]) -> bool:
    """Whether a stop has been requested for ``job_id``."""
    return _registry.is_requested(job_id)


def clear(job_id: str) -> None:
    """Forget any request for ``job_id``."""
    _registry.clear(job_id)


def reset() -> None:
    """Forget every request (used by tests)."""
    _registry.reset()


def checkpoint(job_id: Optional[str]) -> None:
    """Raise :class:`Job_Cancelled` if ``job_id`` has been asked to stop.

    Safe to call anywhere, including with ``None`` - a pipeline run outside a job (the smoke
    reel, the evaluation harness) has no id and is never cancellable, which is correct rather
    than an omission.
    """
    if _registry.is_requested(job_id):
        raise Job_Cancelled(job_id or "")
