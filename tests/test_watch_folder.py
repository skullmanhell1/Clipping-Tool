"""Watch-folder ingest submits a dropped video exactly once, and only when it is complete.

``worker/watch_folder.py`` is the only user-facing feature in the backend that had no test
module: it has a UI toggle, three API routes (``GET /api/watch``, ``POST /api/watch/toggle``,
``POST /api/watch/options``) and a background thread, and every one of those was unverified.

The two rules that carry the feature are both invisible when they break:

1. **Size stable across two consecutive polls.** A file dropped in by a copy, an rsync or a
   browser download appears at its final path immediately and grows afterwards. Submitting on
   first sighting means ffprobe reads a truncated container, and the resulting job fails with a
   decode error that points at the video rather than at the watcher. The defect only shows up for
   files large enough to still be copying one poll later — i.e. never in a hand test with a 2 KB
   fixture, and always on a real 4 GB recording.
2. **``_processed`` is consulted before submitting.** Nothing in the folder is moved or deleted
   after ingest, so the same path is seen on every subsequent poll forever. A missing membership
   check does not duplicate a job once; it re-submits the same file every two seconds until the
   disk fills.

Every test here injects a :class:`RecordingManager` through the ``manager`` seam. The real
``JobManager.submit`` writes a row to ``jobs.db`` and hands the job straight to a thread pool
that would download and render it, so a test that let the real manager through would either
render a fake video or leave queued jobs behind in the shared store.

Where the poll *loop* is not itself the subject, ``_scan_once`` is called directly rather than
slept on — it exists and returns its submissions for exactly that reason. The two tests that do
need the thread use a 10 ms interval and join it before returning, so no thread outlives a test.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from api.main import app
from config import settings
from worker import watch_folder as watch_module
from worker.models import ProcessingOptions
from worker.watch_folder import VIDEO_EXTENSIONS, WatchFolder, get_watcher


class RecordingManager:
    """A :class:`~worker.jobs.JobManager` stand-in recording every submission.

    ``submit`` is keyword-only on purpose: the watcher calls it with four keywords, and pinning
    the call form here means a rename on either side is a failure rather than a ``TypeError``
    swallowed by the poll loop's bare ``except Exception``.

    ``errors`` is a queue of exceptions (or ``None`` for a success) consumed one per call, so a
    failing submit can be followed by a working one within a single test.
    """

    def __init__(self, errors=()):
        self.calls: list[dict] = []
        self._errors = list(errors)

    def submit(self, *, input_type, source, options, title):
        self.calls.append(
            {"input_type": input_type, "source": source, "options": options, "title": title}
        )
        if self._errors:
            error = self._errors.pop(0)
            if error is not None:
                raise error
        return object()  # the watcher discards the returned Job

    @property
    def sources(self) -> list[str]:
        return [call["source"] for call in self.calls]


@pytest.fixture
def manager():
    return RecordingManager()


@pytest.fixture
def watch_dir(tmp_path):
    folder = tmp_path / "watch"
    folder.mkdir()
    return folder


@pytest.fixture
def watcher(watch_dir, manager):
    """A watcher over a temporary folder, always stopped even if the test fails.

    The interval is short because two tests drive the real thread; the rest never wait on it.
    """
    instance = WatchFolder(folder=watch_dir, manager=manager, poll_interval=0.01)
    yield instance
    instance.stop()


def drop(folder, name, size=1024):
    """Write ``name`` into ``folder`` with a definite size, and return the path."""
    path = folder / name
    path.write_bytes(b"\x00" * size)
    return path


def wait_for(predicate, timeout=5.0):
    """Poll ``predicate`` until true or ``timeout`` elapses; returns its final value.

    Used only by the two thread tests. Generous timeout, short sleep: the assertion is that the
    thread does the work at all, not how fast a loaded CI runner gets round to it.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


# --------------------------------------------------------------------------- #
# The two-poll debounce                                                         #
# --------------------------------------------------------------------------- #
def test_a_file_is_not_submitted_on_the_first_sighting(watcher, watch_dir, manager):
    """First sighting only records a size; there is nothing yet to compare it against.

    This is the whole protection against ingesting a partial copy, and it is one ``last is None``
    away from being absent.
    """
    drop(watch_dir, "clip.mp4")
    assert watcher._scan_once() == []
    assert manager.calls == []


def test_a_file_still_growing_is_not_submitted_until_its_size_settles(watcher, watch_dir, manager):
    """The defect this prevents: ffprobe reading a container that is still being written.

    Three sightings at three different sizes must submit nothing, and the fourth — the first to
    repeat a size — must submit. Asserting the *negative* on every growing poll rather than only
    the final positive, because a watcher that submits on any second sighting regardless of size
    passes a test that checks the end state alone.
    """
    path = watch_dir / "growing.mp4"
    for size in (1024, 4096, 16384):
        path.write_bytes(b"\x00" * size)
        assert watcher._scan_once() == [], f"submitted while still growing at {size} bytes"
    assert watcher._scan_once() == [str(path)]
    assert manager.sources == [str(path)]


def test_a_file_that_shrinks_between_polls_is_not_submitted(watcher, watch_dir, manager):
    """Truncate-then-rewrite is how several tools stage a file, and it must not look stable.

    The rule is equality with the previous observation, not monotonic growth, so a shrink has to
    reset the wait exactly as growth does.
    """
    path = watch_dir / "rewritten.mp4"
    path.write_bytes(b"\x00" * 8192)
    assert watcher._scan_once() == []
    path.write_bytes(b"\x00" * 512)
    assert watcher._scan_once() == []
    assert manager.calls == []


def test_an_empty_file_is_never_submitted_however_stable_it_looks(watcher, watch_dir, manager):
    """A zero-byte file is a placeholder some copy tools create before writing any data.

    Its size is trivially "stable" across every poll, so without the explicit ``size == 0`` guard
    it would be the *first* thing ingested and would fail every time. Scanned four times here so
    a guard that merely delays the submission by a poll would still fail.
    """
    drop(watch_dir, "placeholder.mp4", size=0)
    for _ in range(4):
        assert watcher._scan_once() == []
    assert manager.calls == []


# --------------------------------------------------------------------------- #
# Submitted once, and only once                                                 #
# --------------------------------------------------------------------------- #
def test_a_stable_file_is_submitted_once_and_never_again(watcher, watch_dir, manager):
    """Nothing moves or deletes the file after ingest, so it is re-seen on every later poll.

    Without ``_processed`` this is not a duplicate job, it is a new job every poll interval for as
    long as the watcher runs. Ten further scans, so a check that de-duplicates only against the
    immediately preceding pass would not pass this.
    """
    path = drop(watch_dir, "clip.mp4")
    watcher._scan_once()
    assert watcher._scan_once() == [str(path)]
    for _ in range(10):
        assert watcher._scan_once() == []
    assert manager.sources == [str(path)]


def test_the_submission_describes_the_file_as_a_local_path_job(watcher, watch_dir, manager):
    """``input_type`` decides whether the ingest step downloads or opens a path.

    ``"url"`` here would hand an absolute filesystem path to yt-dlp, and ``title`` is what the
    operator sees in the job list — a watch-folder job with an empty title is unattributable among
    a dozen others.
    """
    path = drop(watch_dir, "Interview Final.mov")
    watcher._scan_once()
    watcher._scan_once()
    (call,) = manager.calls
    assert call["input_type"] == "file"
    assert call["source"] == str(path)
    assert call["title"] == "Interview Final.mov"


def test_several_files_are_ingested_in_a_deterministic_order(watcher, watch_dir, manager):
    """``iterdir`` order is filesystem-dependent; the watcher sorts, and that is worth pinning.

    Directory order on ext4 is effectively hash order, so without the sort a batch of files
    dropped together would be queued differently on every machine — which makes an ingest bug
    reported by a user impossible to reproduce locally.
    """
    for name in ("c.mp4", "a.mp4", "b.mp4"):
        drop(watch_dir, name)
    watcher._scan_once()
    submitted = watcher._scan_once()
    assert submitted == [str(watch_dir / n) for n in ("a.mp4", "b.mp4", "c.mp4")]
    assert manager.sources == submitted


# --------------------------------------------------------------------------- #
# What counts as a video                                                        #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name",
    [
        "clip.mp4.part",
        "clip.crdownload",
        "notes.txt",
        "captions.srt",
        "cover.jpg",
        "archive.zip",
        "README",
        ".hidden.swp",
    ],
    ids=[
        "ytdlp-partial",
        "chrome-partial",
        "text",
        "subtitles",
        "image",
        "archive",
        "no-extension",
        "editor-swap",
    ],
)
def test_files_that_are_not_videos_are_ignored(watcher, watch_dir, manager, name):
    """The two partial-download suffixes are the reason this filter matters, not tidiness.

    yt-dlp writes ``x.mp4.part`` and Chrome writes ``.crdownload`` *beside* the eventual file, and
    both are being appended to for as long as the download runs. Extension filtering is what stops
    those being treated as videos in their own right; the size debounce alone would happily submit
    one the moment a download stalled for a poll interval.
    """
    drop(watch_dir, name)
    for _ in range(3):
        assert watcher._scan_once() == []
    assert manager.calls == []


@pytest.mark.parametrize("name", ["CLIP.MP4", "Clip.MoV", "clip.MKV"])
def test_extension_matching_ignores_case(watcher, watch_dir, manager, name):
    """Cameras and phones write upper-case extensions (``DSC_0001.MOV``) constantly.

    A case-sensitive match would silently ignore the single most common source of files a user
    drops into this folder, and the folder would just appear to do nothing.
    """
    path = drop(watch_dir, name)
    watcher._scan_once()
    assert watcher._scan_once() == [str(path)]


def test_the_extension_table_is_stored_in_the_form_it_is_compared_in(watcher):
    """``VIDEO_EXTENSIONS`` is matched against ``suffix.lower()``, so an upper-case entry is dead.

    A leading dot is required for the same reason: ``Path.suffix`` includes it, so ``"mp4"`` in
    this set would never match anything. Both mistakes add an extension that looks supported in
    the source and is not, which is why the invariant is asserted rather than trusted.
    """
    assert VIDEO_EXTENSIONS
    for extension in VIDEO_EXTENSIONS:
        assert extension == extension.lower(), f"{extension!r} can never match suffix.lower()"
        assert extension.startswith("."), f"{extension!r} is missing its leading dot"


def test_a_directory_with_a_video_name_is_not_submitted(watcher, watch_dir, manager):
    """Exporters create working directories like ``Project.mp4/``, and ``stat`` on one succeeds.

    So the ``is_file`` check is what stops a directory being size-debounced (its size *is* stable)
    and submitted as a job that cannot possibly ingest.
    """
    (watch_dir / "Project.mp4").mkdir()
    for _ in range(3):
        assert watcher._scan_once() == []
    assert manager.calls == []


# --------------------------------------------------------------------------- #
# Empty and missing folders                                                     #
# --------------------------------------------------------------------------- #
def test_an_empty_folder_scans_cleanly(watcher, manager):
    """The steady state of this feature — an enabled watcher with nothing to do."""
    assert watcher._scan_once() == []
    assert manager.calls == []


def test_a_folder_that_does_not_exist_scans_cleanly_without_creating_it(tmp_path, manager):
    """The folder can be deleted, unmounted or on a network share that has dropped out.

    A raising scan would be caught and discarded by the poll loop, so the visible symptom would be
    a watcher that reports ``enabled`` and silently ingests nothing forever. Also asserting the
    folder is *not* created, because only ``start`` may do that: scanning is a read.
    """
    missing = tmp_path / "not-there"
    watcher = WatchFolder(folder=missing, manager=manager, poll_interval=0.01)
    assert watcher._scan_once() == []
    assert list(watcher._iter_videos()) == []
    assert not missing.exists()
    assert manager.calls == []


# --------------------------------------------------------------------------- #
# Options                                                                       #
# --------------------------------------------------------------------------- #
def test_the_options_in_force_at_submission_are_the_ones_applied(watcher, watch_dir, manager):
    """Asserting the options that came *out* of the submission, not the ones handed in.

    A watcher that captured its options at construction — or read a settings default at submit
    time — would still pass a test that only checked ``set_options`` stored something.
    """
    options = ProcessingOptions(aspect="1:1", num_clips="3", captions=False)
    watcher.set_options(options)
    drop(watch_dir, "clip.mp4")
    watcher._scan_once()
    watcher._scan_once()
    (call,) = manager.calls
    assert call["options"] is options
    assert call["options"].aspect == "1:1"
    assert call["options"].num_clips == "3"
    assert call["options"].captions is False


def test_options_changed_mid_debounce_apply_to_the_pending_file(watcher, watch_dir, manager):
    """The options are read at submit time, one poll after the file was first seen.

    That ordering is the useful one: an operator who fixes the aspect ratio while a large file is
    still copying expects the corrected setting to be used. Reading them at first sighting instead
    would apply the old settings to a file that had not started processing yet.
    """
    watcher.set_options(ProcessingOptions(aspect="16:9"))
    drop(watch_dir, "clip.mp4")
    watcher._scan_once()
    watcher.set_options(ProcessingOptions(aspect="9:16"))
    watcher._scan_once()
    (call,) = manager.calls
    assert call["options"].aspect == "9:16"


def test_a_default_watcher_has_usable_options_before_any_are_set(watch_dir, manager):
    """Enabling the folder without opening the settings panel must still produce a real job.

    ``None`` here would reach ``JobManager.submit`` and fail inside the pipeline rather than at the
    watcher, so the default is what keeps the simplest possible use of the feature working.
    """
    watcher = WatchFolder(folder=watch_dir, manager=manager, poll_interval=0.01)
    drop(watch_dir, "clip.mp4")
    watcher._scan_once()
    watcher._scan_once()
    (call,) = manager.calls
    assert isinstance(call["options"], ProcessingOptions)


# --------------------------------------------------------------------------- #
# status()                                                                      #
# --------------------------------------------------------------------------- #
def test_status_reports_the_resolved_folder_not_the_requested_one(watch_dir, manager):
    """The UI shows this string, and it is the only way an operator learns where to drop files.

    Asserting the resolved value: the constructor accepts ``str``, ``Path`` or ``None`` and has to
    render one absolute path regardless of which arrived.
    """
    from_string = WatchFolder(folder=str(watch_dir), manager=manager).status()
    from_path = WatchFolder(folder=watch_dir, manager=manager).status()
    assert from_string["folder"] == str(watch_dir) == from_path["folder"]


def test_status_folder_defaults_under_the_storage_root(manager):
    """Unconfigured, the watched directory must sit inside the configured storage root.

    In Docker that root is the mounted volume, so a watcher defaulting anywhere else would watch a
    path inside the container that the user cannot reach from the host — the feature would appear
    completely inert with no error anywhere.
    """
    folder = WatchFolder(manager=manager).folder
    assert folder.name == "watch"
    assert folder.parent == settings.storage_root


def test_status_counts_what_has_actually_been_ingested(watcher, watch_dir):
    """``processed_count`` is a progress signal, so it must move only on a real submission.

    Counting sightings instead would make it increment on the first poll of a file still copying,
    which reads as "ingested" for something that has not been.
    """
    assert watcher.status()["processed_count"] == 0
    drop(watch_dir, "one.mp4")
    watcher._scan_once()
    assert watcher.status()["processed_count"] == 0, "counted a file that was not submitted"
    watcher._scan_once()
    assert watcher.status()["processed_count"] == 1
    watcher._scan_once()
    assert watcher.status()["processed_count"] == 1


def test_status_is_json_serialisable(watcher):
    """It is returned straight from ``GET /api/watch``, so a ``Path`` in it is a 500.

    ``folder`` is a ``Path`` internally and is the field most likely to be passed through
    unconverted by a later edit.
    """
    import json

    assert json.loads(json.dumps(watcher.status())) == watcher.status()


# --------------------------------------------------------------------------- #
# Lifecycle: start, stop, and the poll thread                                   #
# --------------------------------------------------------------------------- #
def test_start_creates_the_watch_folder(tmp_path, manager):
    """Nobody can drop a file into a directory that does not exist.

    The default location is under the storage root and will not exist on a fresh install, so
    creating it is what makes the toggle work first time instead of after an ``mkdir``.
    """
    folder = tmp_path / "deep" / "nested" / "watch"
    watcher = WatchFolder(folder=folder, manager=manager, poll_interval=0.01)
    try:
        status = watcher.start()
        assert folder.is_dir()
        assert status["enabled"] is True
    finally:
        watcher.stop()


def test_files_already_present_when_watching_starts_are_not_ingested(watcher, watch_dir, manager):
    """Enabling the toggle must not queue the folder's entire backlog.

    A user who has been using ``storage/watch`` as an ordinary directory, or who re-enables the
    toggle after a restart, would otherwise re-render everything in it at once — which on a folder
    of past inputs is hours of CPU nobody asked for. Stopped immediately so the assertions run
    against ``_scan_once`` with no thread mutating the same state.
    """
    existing = drop(watch_dir, "already-here.mp4")
    watcher.start()
    watcher.stop()
    assert str(existing) in watcher._processed
    for _ in range(3):
        assert watcher._scan_once() == []
    assert manager.calls == []
    assert watcher.status()["processed_count"] == 1


def test_a_file_dropped_after_start_is_ingested_by_the_background_thread(
    watcher, watch_dir, manager
):
    """The only test that exercises the real thread end to end, which is why it exists.

    Everything else calls ``_scan_once``; if ``_loop`` were never wired to it — or the thread were
    created but not started — every one of those tests would still pass and the shipped feature
    would do nothing at all.
    """
    watcher.start()
    path = drop(watch_dir, "dropped.mp4")
    assert wait_for(lambda: manager.sources == [str(path)]), manager.sources
    watcher.stop()
    assert watcher._thread is None


def test_start_is_idempotent_and_does_not_leave_a_second_thread_behind(watcher):
    """``POST /api/watch/toggle`` with ``enabled: true`` twice is one double-click apart.

    FastAPI runs sync routes in a threadpool, so this is genuinely reachable. Two poll threads
    over one folder both pass the debounce on the same file, and whichever loses the race to
    ``_processed`` submits a duplicate job. Asserting on thread identity because the second
    ``start`` returning a plausible status is exactly what a leaked thread also does.
    """
    first = watcher.start()
    thread = watcher._thread
    second = watcher.start()
    assert watcher._thread is thread, "start() created a second poll thread"
    assert thread is not None and thread.is_alive()
    assert first == second == watcher.status()
    watcher.stop()
    assert not thread.is_alive()


def test_stop_before_any_start_is_harmless(watcher, manager):
    """The UI sends ``enabled: false`` on load if the user's saved state is off.

    With no thread to join, ``stop`` has to be a no-op that reports honestly rather than raising on
    ``None.join`` — an exception here would surface as a 500 on page load.
    """
    status = watcher.stop()
    assert status["enabled"] is False
    assert watcher._thread is None


def test_stop_is_idempotent(watcher):
    """Second stop must not re-join an already-discarded thread."""
    watcher.start()
    assert watcher.stop()["enabled"] is False
    assert watcher.stop()["enabled"] is False
    assert watcher._thread is None


def test_the_thread_does_not_outlive_stop(watcher):
    """A daemon thread that survives ``stop`` keeps ingesting after the user disabled the feature.

    Daemon status hides this in production — the process exits regardless — so the leak would only
    ever be noticed as files being processed while the toggle reads "off".
    """
    watcher.start()
    thread = watcher._thread
    assert thread is not None
    watcher.stop()
    assert not thread.is_alive()
    assert watcher.enabled is False


def test_stopping_and_restarting_resumes_watching(watcher, watch_dir, manager):
    """Toggling off and on again is the most likely thing a confused user does.

    A ``threading.Event`` that is set on stop and never cleared makes the second ``start`` return a
    happy status over a loop that exits on its first iteration — enabled, threaded, and completely
    inert.
    """
    watcher.start()
    watcher.stop()
    watcher.start()
    path = drop(watch_dir, "after-restart.mp4")
    assert wait_for(lambda: manager.sources == [str(path)]), manager.sources
    watcher.stop()


def test_the_enabled_flag_tracks_the_toggle(watcher):
    """``enabled`` is read by the UI to render the switch, so it must follow the transitions."""
    assert watcher.enabled is False
    watcher.start()
    assert watcher.enabled is True
    assert watcher.status()["enabled"] is True
    watcher.stop()
    assert watcher.enabled is False
    assert watcher.status()["enabled"] is False


# --------------------------------------------------------------------------- #
# Failure inside a scan                                                         #
# --------------------------------------------------------------------------- #
def test_a_failing_scan_does_not_end_the_poll_loop(watch_dir):
    """One bad file must not silently disable the whole feature until the process restarts.

    Driven by running ``_loop`` on *this* thread with a manager that raises and signals the stop
    event in the same call: the loop must swallow the error and reach its wait, where the
    already-set event ends it. That is deterministic — no sleeping, no second thread — and it
    proves the ``except`` covers the scan rather than merely existing.
    """
    watcher = WatchFolder(folder=watch_dir, manager=RecordingManager(), poll_interval=0.01)

    class ExplodingManager:
        def __init__(self):
            self.calls = 0

        def submit(self, **kwargs):
            self.calls += 1
            watcher._stop.set()  # end the loop after this iteration
            raise RuntimeError("job store unavailable")

    watcher.manager = ExplodingManager()
    drop(watch_dir, "clip.mp4")
    watcher._scan_once()  # first sighting: records the size, submits nothing

    watcher._loop()  # must return, not raise

    assert watcher.manager.calls == 1


def test_a_file_whose_submit_failed_is_retried_rather_than_dropped(watcher, watch_dir):
    """``_processed`` is only marked *after* a successful submit, which is the useful order.

    A transient failure — the job store locked, the executor saturated — would otherwise mark the
    file as done and it would never be ingested, with the only record of it being an exception the
    poll loop discarded.
    """
    watcher.manager = RecordingManager(errors=[RuntimeError("transient"), None])
    path = drop(watch_dir, "clip.mp4")
    watcher._scan_once()
    with pytest.raises(RuntimeError, match="transient"):
        watcher._scan_once()
    assert str(path) not in watcher._processed
    assert watcher._scan_once() == [str(path)]
    assert watcher.manager.sources == [str(path), str(path)]


# --------------------------------------------------------------------------- #
# The three API routes                                                          #
# --------------------------------------------------------------------------- #
@pytest.fixture
def api(monkeypatch, watch_dir, manager):
    """The routes wired to an injected watcher instead of the process-wide singleton.

    ``api.main`` calls ``get_watcher()`` per request, so replacing the module global is enough —
    and it is necessary: the real singleton watches the shared storage root with a real
    ``JobManager``, and enabling it from a test would start a thread over a directory other tests
    write to.
    """
    instance = WatchFolder(folder=watch_dir, manager=manager, poll_interval=0.01)
    monkeypatch.setattr(watch_module, "_watcher", instance)
    yield TestClient(app), instance
    instance.stop()


def test_the_status_route_returns_the_watcher_state(api):
    """``GET /api/watch`` is what the UI polls to render the toggle and the folder path."""
    client, watcher = api
    response = client.get("/api/watch")
    assert response.status_code == 200
    assert response.json() == watcher.status()


def test_the_toggle_route_enables_and_disables_watching(api):
    """The route returns the *new* status, so the UI does not need a second request to render it.

    Both directions are asserted against the watcher itself as well as the response body, because a
    handler that returns a hand-built dict without touching the watcher looks identical from
    outside.
    """
    client, watcher = api
    enabled = client.post("/api/watch/toggle", json={"enabled": True})
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    assert watcher.enabled is True

    disabled = client.post("/api/watch/toggle", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert watcher.enabled is False
    assert watcher._thread is None


def test_the_toggle_route_applies_the_options_it_was_sent(api):
    """The toggle carries the settings panel's contents, and they must reach the watcher.

    Asserting the resolved ``ProcessingOptions`` rather than the request body: the route converts
    an ``OptionsModel`` through ``to_options()``, and a field dropped in that conversion is a
    setting the user chose and silently did not get.
    """
    client, watcher = api
    response = client.post(
        "/api/watch/toggle",
        json={"enabled": True, "options": {"aspect": "1:1", "num_clips": "5", "topic": "cooking"}},
    )
    assert response.status_code == 200
    assert watcher._options.aspect == "1:1"
    assert watcher._options.num_clips == "5"
    assert watcher._options.topic == "cooking"


def test_the_options_route_updates_settings_without_enabling_watching(api):
    """Editing settings with the toggle off must not turn the feature on.

    ``/api/watch/options`` and ``/api/watch/toggle`` share ``set_options``; if the options route
    also started the watcher, adjusting a dropdown would begin ingesting files.
    """
    client, watcher = api
    response = client.post("/api/watch/options", json={"aspect": "4:5"})
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert watcher.enabled is False
    assert watcher._options.aspect == "4:5"


def test_the_options_route_applies_to_an_already_running_watcher(api, watch_dir, manager):
    """Changing settings while enabled must affect the next file, not require a restart.

    Exercised through the route and observed at the submission, which is the only place the
    difference is real.
    """
    client, watcher = api
    client.post("/api/watch/toggle", json={"enabled": True, "options": {"aspect": "16:9"}})
    assert client.post("/api/watch/options", json={"aspect": "9:16"}).status_code == 200
    path = drop(watch_dir, "clip.mp4")
    assert wait_for(lambda: manager.sources == [str(path)]), manager.sources
    watcher.stop()
    assert manager.calls[0]["options"].aspect == "9:16"


# --------------------------------------------------------------------------- #
# The singleton                                                                 #
# --------------------------------------------------------------------------- #
def test_get_watcher_returns_one_shared_instance(monkeypatch):
    """Three routes call ``get_watcher()`` independently, and they must reach the same watcher.

    A factory returning a new instance per call would make the toggle appear to do nothing: the
    status route would report the state of a watcher nobody started. Reset to ``None`` first so
    the construction path itself runs rather than whatever a previous test left behind.
    """
    monkeypatch.setattr(watch_module, "_watcher", None)
    first = get_watcher()
    assert get_watcher() is first
    assert first.enabled is False, "the shared watcher must not start itself"
