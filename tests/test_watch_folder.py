"""Watch-folder ingest: the rule that decides a file has finished copying.

`worker/watch_folder.py` is 162 lines with a UI toggle, three API routes and **no test that
ever constructed it** — 27% covered, entirely incidentally, by a route test that asserts a 401
and therefore never reaches the handler.

What it does is easy to get wrong in a way nobody notices. A file appearing in a directory is
not a file that has finished arriving: a 400 MB drag-and-drop is visible at 12 MB, and
submitting then means transcoding a truncated file. The module's answer is a **two-poll
size-stability rule** rather than a timer, and every behavioural consequence of that choice is
pinned below — a file is never submitted on the poll that first sees it, a zero-byte file is
never submitted at all, and a growing file resets the count.

Driven through `_scan_once()` rather than the thread wherever possible. It returns the paths it
submitted specifically so a test can drive one pass at a time, which makes the stability rule
assertable without sleeping. The thread gets its own small set of lifecycle tests.

Nothing here touches ffmpeg, the network or the real `JobManager`: the manager is injected, and
the folder is `tmp_path`. `WatchFolder` only reads `settings.storage_root`, and only when no
folder is passed.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from worker import watch_folder
from worker.models import ProcessingOptions
from worker.watch_folder import VIDEO_EXTENSIONS, WatchFolder, get_watcher


@dataclass
class FakeManager:
    """Records `submit` calls instead of running a pipeline.

    The real `JobManager` owns a thread pool and the jobs database, and `submit` is the door to
    the whole render path — so a test that used it would be testing the pipeline.
    """

    calls: list[dict] = field(default_factory=list)
    raises: BaseException | None = None

    def submit(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return f"job-{len(self.calls)}"

    @property
    def sources(self) -> list[str]:
        return [call["source"] for call in self.calls]


def make_watcher(tmp_path: Path, manager: FakeManager | None = None) -> WatchFolder:
    """A watcher on `tmp_path` with a poll interval short enough not to slow the suite."""
    return WatchFolder(folder=tmp_path, manager=manager or FakeManager(), poll_interval=0.01)


def write(path: Path, size: int) -> Path:
    path.write_bytes(b"\0" * size)
    return path


# --------------------------------------------------------------------------- #
# Discovery                                                                     #
# --------------------------------------------------------------------------- #
def test_only_video_extensions_are_picked_up(tmp_path):
    """The directory is a drop box, so it will contain things that are not videos.

    Submitting a `.txt` means handing it to ffmpeg, which fails the job — a failure the user
    caused by putting a file somewhere reasonable.
    """
    for name in ("a.mp4", "b.mov", "c.mkv", "d.txt", "e.jpg", "f.srt", "g"):
        write(tmp_path / name, 10)
    watcher = make_watcher(tmp_path)

    found = {p.name for p in watcher._iter_videos()}
    assert found == {"a.mp4", "b.mov", "c.mkv"}


def test_the_extension_check_is_case_insensitive(tmp_path):
    """Cameras and phones write `.MP4` and `.MOV` in upper case."""
    write(tmp_path / "PHONE.MP4", 10)
    write(tmp_path / "Camera.MOV", 10)
    watcher = make_watcher(tmp_path)
    assert {p.name for p in watcher._iter_videos()} == {"PHONE.MP4", "Camera.MOV"}


def test_directories_are_not_treated_as_videos(tmp_path):
    """A directory can be named `footage.mp4`, and `stat().st_size` on one is meaningless."""
    (tmp_path / "footage.mp4").mkdir()
    write(tmp_path / "real.mp4", 10)
    watcher = make_watcher(tmp_path)
    assert [p.name for p in watcher._iter_videos()] == ["real.mp4"]


def test_discovery_is_not_recursive(tmp_path):
    """`iterdir`, not `rglob` — pinned because it is a design choice, not an accident.

    Recursing would pick up whatever the user happens to have nested in there, including a
    library they were browsing.
    """
    nested = tmp_path / "sub"
    nested.mkdir()
    write(nested / "deep.mp4", 10)
    write(tmp_path / "top.mp4", 10)
    watcher = make_watcher(tmp_path)
    assert [p.name for p in watcher._iter_videos()] == ["top.mp4"]


def test_a_missing_folder_yields_nothing_rather_than_raising(tmp_path):
    """The folder can be deleted while the watcher is running."""
    watcher = make_watcher(tmp_path / "does-not-exist")
    assert list(watcher._iter_videos()) == []
    assert watcher._scan_once() == []


def test_discovery_order_is_deterministic(tmp_path):
    """`sorted()`, so a batch of files submits in a predictable order.

    Without it the order is filesystem-dependent, which makes "which clip came out first"
    unreproducible across machines for no reason.
    """
    for name in ("c.mp4", "a.mp4", "b.mp4"):
        write(tmp_path / name, 10)
    watcher = make_watcher(tmp_path)
    assert [p.name for p in watcher._iter_videos()] == ["a.mp4", "b.mp4", "c.mp4"]


def test_the_extension_set_matches_the_upload_allow_list_in_spirit():
    """A sanity check on the constant itself, so the set cannot quietly empty.

    Not asserted equal to `ALLOWED_UPLOAD_EXTENSIONS`: that one includes audio-only formats,
    which a *video* watch folder deliberately does not.
    """
    assert ".mp4" in VIDEO_EXTENSIONS and ".mov" in VIDEO_EXTENSIONS
    assert all(ext.startswith(".") and ext.islower() for ext in VIDEO_EXTENSIONS)
    assert ".txt" not in VIDEO_EXTENSIONS


# --------------------------------------------------------------------------- #
# The stability rule — the reason this module exists                            #
# --------------------------------------------------------------------------- #
def test_a_file_is_never_submitted_on_the_poll_that_first_sees_it(tmp_path):
    """The core of it. One sighting proves nothing about whether the copy has finished."""
    manager = FakeManager()
    watcher = make_watcher(tmp_path, manager)
    write(tmp_path / "a.mp4", 100)

    assert watcher._scan_once() == [], "submitted on first sight, before size was confirmed"
    assert manager.calls == []


def test_a_file_is_submitted_once_its_size_repeats(tmp_path):
    manager = FakeManager()
    watcher = make_watcher(tmp_path, manager)
    path = write(tmp_path / "a.mp4", 100)

    watcher._scan_once()
    assert watcher._scan_once() == [str(path)]
    assert manager.sources == [str(path)]


def test_a_growing_file_is_not_submitted_until_it_stops_growing(tmp_path):
    """A copy in progress. This is the case the whole rule exists for."""
    manager = FakeManager()
    watcher = make_watcher(tmp_path, manager)
    path = tmp_path / "big.mp4"

    for size in (100, 5_000, 250_000):
        write(path, size)
        assert watcher._scan_once() == [], f"submitted mid-copy at {size} bytes"

    write(path, 250_000)  # the copy finished; size now repeats
    assert watcher._scan_once() == [str(path)]


def test_a_zero_byte_file_is_never_submitted_however_stable(tmp_path):
    """A created-but-unwritten file is stable at zero bytes for as long as you look at it.

    Many copy tools create the destination before writing a byte, so "stable" alone is not
    enough — the size has to be stable *and* non-zero.
    """
    manager = FakeManager()
    watcher = make_watcher(tmp_path, manager)
    write(tmp_path / "empty.mp4", 0)

    for _ in range(5):
        assert watcher._scan_once() == []
    assert manager.calls == []


def test_a_file_is_submitted_only_once_across_many_polls(tmp_path):
    """Otherwise a folder the user leaves files in re-renders them on every poll, forever."""
    manager = FakeManager()
    watcher = make_watcher(tmp_path, manager)
    write(tmp_path / "a.mp4", 100)

    submissions = [watcher._scan_once() for _ in range(6)]
    assert sum(len(batch) for batch in submissions) == 1
    assert len(manager.calls) == 1


def test_a_deleted_and_recreated_file_is_not_resubmitted(tmp_path):
    """`_processed` is keyed by path, so the same name does not come round again.

    Documenting the trade rather than claiming it is ideal: replacing a file with a different
    file of the same name will not be picked up. Renaming it will.
    """
    manager = FakeManager()
    watcher = make_watcher(tmp_path, manager)
    path = write(tmp_path / "a.mp4", 100)
    watcher._scan_once()
    watcher._scan_once()
    assert len(manager.calls) == 1

    path.unlink()
    write(tmp_path / "a.mp4", 100)
    watcher._scan_once()
    watcher._scan_once()
    assert len(manager.calls) == 1, "the same path was submitted twice"


def test_several_files_each_get_their_own_stability_check(tmp_path):
    manager = FakeManager()
    watcher = make_watcher(tmp_path, manager)
    settled = write(tmp_path / "a.mp4", 100)
    growing = tmp_path / "b.mp4"
    write(growing, 100)

    watcher._scan_once()
    write(growing, 900)  # b is still arriving
    assert watcher._scan_once() == [str(settled)]
    assert watcher._scan_once() == [str(growing)]


# --------------------------------------------------------------------------- #
# What gets submitted                                                           #
# --------------------------------------------------------------------------- #
def test_the_submission_carries_the_file_and_the_current_options(tmp_path):
    """A watch-folder job is a file job, and it must use the options set on the watcher."""
    manager = FakeManager()
    watcher = make_watcher(tmp_path, manager)
    options = ProcessingOptions(aspect="1:1")
    watcher.set_options(options)
    path = write(tmp_path / "a.mp4", 100)

    watcher._scan_once()
    watcher._scan_once()

    call = manager.calls[0]
    assert call["input_type"] == "file"
    assert call["source"] == str(path)
    assert call["title"] == "a.mp4"
    assert call["options"] is options


def test_options_changed_between_polls_apply_to_the_next_submission(tmp_path):
    """The UI can change settings while the watcher runs, and the next file should use them."""
    manager = FakeManager()
    watcher = make_watcher(tmp_path, manager)
    watcher.set_options(ProcessingOptions(aspect="9:16"))
    write(tmp_path / "a.mp4", 100)
    watcher._scan_once()
    watcher._scan_once()

    watcher.set_options(ProcessingOptions(aspect="16:9"))
    write(tmp_path / "b.mp4", 100)
    watcher._scan_once()
    watcher._scan_once()

    assert [call["options"].aspect for call in manager.calls] == ["9:16", "16:9"]


# --------------------------------------------------------------------------- #
# Lifecycle                                                                     #
# --------------------------------------------------------------------------- #
def test_start_ignores_files_that_were_already_there(tmp_path):
    """Enabling the feature must not submit the user's existing library.

    Someone who has been using the folder as storage and then turns watching on would
    otherwise start dozens of renders at once.
    """
    manager = FakeManager()
    write(tmp_path / "old.mp4", 100)
    watcher = make_watcher(tmp_path, manager)

    watcher.start()
    try:
        watcher._scan_once()
        watcher._scan_once()
        assert manager.calls == []
    finally:
        watcher.stop()


def test_a_file_dropped_after_start_is_picked_up_by_the_thread(tmp_path):
    """The one test that exercises the real thread end to end."""
    manager = FakeManager()
    watcher = make_watcher(tmp_path, manager)
    watcher.start()
    try:
        path = write(tmp_path / "new.mp4", 100)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not manager.calls:
            time.sleep(0.02)
        assert manager.sources == [str(path)], "the polling thread never submitted the file"
    finally:
        watcher.stop()


def test_start_reports_enabled_and_stop_reports_disabled(tmp_path):
    watcher = make_watcher(tmp_path)
    assert watcher.enabled is False
    assert watcher.start()["enabled"] is True
    assert watcher.enabled is True
    assert watcher.stop()["enabled"] is False
    assert watcher.enabled is False


def test_start_is_idempotent(tmp_path):
    """The UI toggle can be double-clicked; two threads polling one folder would double-submit."""
    watcher = make_watcher(tmp_path)
    watcher.start()
    try:
        first = watcher._thread
        watcher.start()
        assert watcher._thread is first, "a second polling thread was started"
    finally:
        watcher.stop()


def test_stop_is_safe_when_never_started(tmp_path):
    watcher = make_watcher(tmp_path)
    assert watcher.stop()["enabled"] is False


def test_stop_joins_the_thread(tmp_path):
    """A daemon thread left running holds the folder open and keeps submitting."""
    watcher = make_watcher(tmp_path)
    watcher.start()
    thread = watcher._thread
    watcher.stop()
    assert thread is not None and not thread.is_alive()
    assert watcher._thread is None


def test_start_creates_the_folder(tmp_path):
    """The default folder is under `storage_root` and will not exist on a fresh install."""
    target = tmp_path / "watch"
    watcher = make_watcher(target)
    assert not target.exists()
    watcher.start()
    try:
        assert target.is_dir()
    finally:
        watcher.stop()


def test_status_reports_what_the_ui_reads(tmp_path):
    """`SettingsPanel.jsx` renders `enabled` and `folder`; both must be present and typed."""
    watcher = make_watcher(tmp_path)
    status = watcher.status()
    assert status["enabled"] is False
    assert status["folder"] == str(tmp_path)
    assert isinstance(status["folder"], str), "the UI interpolates this directly"
    assert status["processed_count"] == 0


# --------------------------------------------------------------------------- #
# Failure paths                                                                 #
# --------------------------------------------------------------------------- #
def test_a_raising_submit_does_not_kill_the_polling_thread(tmp_path):
    """`_loop` swallows every exception, and this is what that buys.

    Correct as a design: a database hiccup must not silently switch the feature off until the
    process restarts. The consequence, pinned here rather than left to be discovered, is that a
    *persistent* failure retries the same file on every poll — silently, because `_loop` has no
    logger at all. That is a real weakness rather than a defect to fix in this phase: adding
    logging to this path is the observability work of Phase 7 and belongs with the rest of it.

    Exercised through the real thread, because the swallow lives in the thread's loop.
    """
    manager = FakeManager(raises=RuntimeError("job store is down"))
    watcher = make_watcher(tmp_path, manager)
    watcher.start()
    try:
        # After `start`, so it is not seeded as already-seen.
        write(tmp_path / "a.mp4", 100)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and len(manager.calls) < 2:
            time.sleep(0.02)

        assert len(manager.calls) >= 2, "a failing submit was not retried on the next poll"
        assert watcher._thread is not None and watcher._thread.is_alive(), (
            "the polling thread died on a submit error; the feature would appear to be on "
            "while doing nothing"
        )
    finally:
        watcher.stop()


def test_a_file_that_vanishes_between_listing_and_stat_surfaces_from_scan_once(tmp_path):
    """`iterdir` then `stat` is a race, and `_scan_once` does not guard it.

    Asserted rather than fixed: the loop's blanket `except` is what makes this harmless in
    production, so the raise is only reachable by a caller driving `_scan_once` directly. Pinning
    it means a future guard is a deliberate change rather than an accident.
    """
    watcher = make_watcher(tmp_path)
    ghost = tmp_path / "ghost.mp4"  # listed but never created
    watcher._iter_videos = lambda: iter([ghost])  # type: ignore[method-assign]

    with pytest.raises(FileNotFoundError):
        watcher._scan_once()


# --------------------------------------------------------------------------- #
# The singleton                                                                 #
# --------------------------------------------------------------------------- #
def test_get_watcher_returns_one_instance(monkeypatch):
    """Three API routes call `get_watcher()`; two instances would mean two polling threads."""
    monkeypatch.setattr(watch_folder, "_watcher", None)
    monkeypatch.setattr(watch_folder, "get_manager", lambda: FakeManager())
    first = get_watcher()
    try:
        assert get_watcher() is first
    finally:
        monkeypatch.setattr(watch_folder, "_watcher", None)


def test_get_watcher_is_thread_safe(monkeypatch):
    """Built lazily under a lock. Two concurrent first requests must not create two watchers."""
    monkeypatch.setattr(watch_folder, "_watcher", None)
    monkeypatch.setattr(watch_folder, "get_manager", lambda: FakeManager())
    seen: list[WatchFolder] = []
    barrier = threading.Barrier(8)

    def race() -> None:
        barrier.wait()
        seen.append(get_watcher())

    threads = [threading.Thread(target=race) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    try:
        assert len({id(w) for w in seen}) == 1, "the singleton was constructed more than once"
    finally:
        monkeypatch.setattr(watch_folder, "_watcher", None)
