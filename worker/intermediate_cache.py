"""Content-addressed cache for expensive per-source measurements (I3).

`T8` cached the most expensive stage - transcription - and stopped there. Three other
whole-file decodes still ran on every job for the same source:

* **silence detection** (`AU7`), which `silencedetect` cannot do without decoding the whole audio;
* **the energy envelope** (`S2`), one `astats` pass over the whole file;
* **keyframe sampling** (`S14`), 48 seeks and decodes at 480 px.

None of them depends on anything a user changes between runs of the same video, so re-running a
source to try a different caption preset paid for all three again.

The design follows `T8`'s two rules, because they are the ones that make a cache safe rather than
merely fast:

* **The key is the content, not the path.** Hashing a gigabyte costs a couple of seconds where
  decoding it costs minutes, so correctness is cheap here. Path-and-mtime keying is the usual
  shortcut and it is wrong in exactly the case that matters - footage re-exported over the same
  filename.
* **Every parameter that changes the answer is in the key.** A silence map measured at -30 dB is
  not interchangeable with one measured at -25, and an envelope at a 1 s window is not the same
  data as one at 0.1 s. Keying on the file alone would serve the wrong measurement silently and
  permanently, which is worse than having no cache.

Every failure path degrades to computing the value. A cache must never be the reason a job fails.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from config import settings
from worker.transcript_cache import hash_source

logger = logging.getLogger(__name__)

#: Bumped when a cached payload's shape changes, so an old entry is a miss rather than a
#: mis-parse. Cheaper and safer than migrating data that can always be recomputed.
SCHEMA = 1


def cache_dir() -> Path:
    """Where intermediates live. Created on demand."""
    directory = Path(
        getattr(settings, "intermediate_cache_dir", "")
        or (Path(settings.temp_dir) / "intermediates")
    )
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def enabled() -> bool:
    return bool(getattr(settings, "intermediate_cache_enabled", True))


def _fingerprint(params: dict[str, Any]) -> str:
    """A short, stable fingerprint of the parameters that shaped a measurement.

    Sorted and JSON-encoded so ``{"a": 1, "b": 2}`` and ``{"b": 2, "a": 1}`` are one key rather
    than two entries holding identical data.
    """
    import hashlib

    encoded = json.dumps(params, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def key_for(name: str, source: str | Path, params: dict[str, Any] | None = None) -> str:
    """The cache key for measurement ``name`` of ``source`` under ``params``."""
    return f"{name}-{hash_source(source)}-{_fingerprint(params or {})}"


def path_for(key: str) -> Path:
    return cache_dir() / f"{key}.json"


def load(key: str) -> Any | None:
    """The cached value for ``key``, or ``None`` on a miss or any problem reading it."""
    path = path_for(key)
    try:
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        return None
    return raw.get("value")


def store(key: str, value: Any) -> None:
    """Cache ``value`` under ``key``. Never raises.

    Written to a temporary file and renamed, so a process killed mid-write cannot leave a
    truncated entry that every later run has to detect and discard.
    """
    try:
        directory = cache_dir()
        payload = json.dumps({"schema": SCHEMA, "stored_at": time.time(), "value": value})
        handle, temporary = tempfile.mkstemp(dir=str(directory), suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
            os.replace(temporary, path_for(key))
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except (OSError, TypeError, ValueError):
        logger.debug("I3: could not cache %s", key, exc_info=True)


def memoise(
    name: str,
    source: str | Path,
    compute: Callable[[], Any],
    params: dict[str, Any] | None = None,
) -> Any:
    """Return the cached measurement for ``source``, computing and storing it on a miss.

    An **empty** result is cached like any other. That is deliberate: "this file has no detectable
    silence" is a real and expensive answer, and treating it as a miss would re-decode the whole
    file on every run of exactly the sources where the measurement costs most and yields least.
    """
    if not enabled():
        return compute()
    try:
        key = key_for(name, source, params)
    except (OSError, ValueError):
        # An unhashable source (deleted mid-run, unreadable) is a miss, not a failure.
        return compute()

    cached = load(key)
    if cached is not None:
        return cached

    value = compute()
    store(key, value)
    return value


def frames_dir_for(source: str | Path, params: dict[str, Any] | None = None) -> Path | None:
    """A stable directory for this source's sampled keyframes, or ``None`` when disabled.

    Keyframes are *files*, so they are cached as files rather than serialised into JSON. The
    directory name is the content key, which is what lets a second run find frames the first
    extracted - and what stops two different sources sharing a directory, which would silently
    mix one video's frames into another's selection.
    """
    if not enabled():
        return None
    try:
        directory = cache_dir() / f"frames-{hash_source(source)}-{_fingerprint(params or {})}"
        directory.mkdir(parents=True, exist_ok=True)
        return directory
    except OSError:
        return None


def prune(max_entries: int | None = None) -> int:
    """Delete the oldest entries beyond ``max_entries``. Returns how many were removed.

    An unbounded cache of whole-file measurements is a slow disk leak on a long-lived instance:
    every source ever processed leaves an entry behind, and the frame directories are the larger
    part of it. Pruning by modification time keeps the sources someone is actually working on.
    """
    limit = int(
        max_entries
        if max_entries is not None
        else getattr(settings, "intermediate_cache_max_entries", 200)
    )
    if limit <= 0:
        return 0
    try:
        directory = cache_dir()
        entries = [
            item
            for item in directory.iterdir()
            if item.suffix == ".json" or item.name.startswith("frames-")
        ]
    except OSError:
        return 0

    if len(entries) <= limit:
        return 0

    def _mtime(item: Path) -> float:
        """Modification time, or 0.0 (prune first) when it cannot be read.

        `item.stat()` guarded by `item.exists()` was a TOCTOU race: the check and the read are two
        syscalls, and a concurrent prune or sweep between them makes `stat()` raise `OSError` out of
        `sort` — outside the `try` above, so it propagated into whichever selection pass happened to
        trigger the prune. Catching it here is also more correct: an entry that has just vanished
        sorts oldest, which is exactly where a deleted entry belongs.
        """
        try:
            return item.stat().st_mtime
        except OSError:
            return 0.0

    entries.sort(key=_mtime)
    removed = 0
    for item in entries[: len(entries) - limit]:
        try:
            if item.is_dir():
                import shutil

                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    return removed
