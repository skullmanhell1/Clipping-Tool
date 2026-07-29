"""Watch-folder mode.

Monitors a directory and auto-processes any video dropped into it using the
current default settings. Implemented with lightweight polling (no external
watchdog dependency) so it works identically in Docker and locally.

Toggle behaviour:
    * :meth:`WatchFolder.start` begins polling in a background thread.
    * :meth:`WatchFolder.stop` halts polling.
    * :meth:`WatchFolder.set_options` updates the settings applied to newly
      detected files.

A file is only submitted once its size has been stable across two polls (so we
don't start processing a file that is still being copied), and each path is
remembered so it is not processed twice.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from config import settings
from worker.jobs import JobManager, get_manager
from worker.models import ProcessingOptions

# Recognised video file extensions.
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv"}


class WatchFolder:
    """Polls a folder and submits new videos to the :class:`JobManager`."""

    def __init__(
        self,
        folder: str | Path | None = None,
        manager: Optional[JobManager] = None,
        poll_interval: float = 2.0,
    ) -> None:
        self.folder = Path(folder or (Path(settings.storage_root) / "watch"))
        self.manager = manager or get_manager()
        self.poll_interval = poll_interval

        self._options = ProcessingOptions()
        self._enabled = False
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        # path -> last observed size, and the set of already-submitted paths.
        self._sizes: dict[str, int] = {}
        self._processed: set[str] = set()

    # -- configuration -----------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_options(self, options: ProcessingOptions) -> None:
        """Update the processing options applied to newly detected files."""
        with self._lock:
            self._options = options

    def status(self) -> dict:
        """Return a JSON-friendly status snapshot for the API."""
        return {
            "enabled": self._enabled,
            "folder": str(self.folder),
            "processed_count": len(self._processed),
        }

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> dict:
        """Enable watching and start the background poll thread (idempotent)."""
        self.folder.mkdir(parents=True, exist_ok=True)
        if self._enabled:
            return self.status()

        # Treat files already present at start-up as "seen" so we only process
        # files dropped in *after* the watcher is enabled.
        for p in self._iter_videos():
            self._processed.add(str(p))

        self._stop.clear()
        self._enabled = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self.status()

    def stop(self) -> dict:
        """Disable watching and stop the poll thread."""
        self._enabled = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_interval + 1)
            self._thread = None
        return self.status()

    # -- internals ---------------------------------------------------------

    def _iter_videos(self):
        """Yield video files currently in the watch folder."""
        if not self.folder.exists():
            return
        for p in sorted(self.folder.iterdir()):
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS:
                yield p

    def _loop(self) -> None:
        """Poll loop: detect size-stable, unseen files and submit them."""
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception:
                # Never let a transient error kill the watch thread.
                pass
            self._stop.wait(self.poll_interval)

    def _scan_once(self) -> list[str]:
        """Single scan pass. Returns the paths submitted this pass (for tests)."""
        submitted: list[str] = []
        for path in self._iter_videos():
            key = str(path)
            if key in self._processed:
                continue

            size = path.stat().st_size
            last = self._sizes.get(key)
            self._sizes[key] = size

            # Require a stable, non-zero size across two consecutive polls.
            if last is None or size == 0 or size != last:
                continue

            with self._lock:
                options = self._options
            self.manager.submit(
                input_type="file",
                source=key,
                options=options,
                title=path.name,
            )
            self._processed.add(key)
            submitted.append(key)
        return submitted


# --- process-wide singleton -------------------------------------------------
_watcher: Optional[WatchFolder] = None
_watcher_lock = threading.Lock()


def get_watcher() -> WatchFolder:
    """Return the shared :class:`WatchFolder` singleton."""
    global _watcher
    with _watcher_lock:
        if _watcher is None:
            _watcher = WatchFolder()
        return _watcher
