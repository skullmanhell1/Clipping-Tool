"""Tests for the :mod:`worker.ffmpeg_utils` subprocess wrapper.

These deliberately drive **real** subprocesses rather than a mocked
``subprocess.run``. The bug this file's timeout tests exist for is precisely the kind
a mock cannot see: a hung child process produces no output and raises no exception, so
a fake that returns immediately looks identical to a correct implementation. ``sleep``
stands in for a stalled ffmpeg because it is guaranteed present, costs nothing, and
hangs on demand.

Validates: the bounded-subprocess contract of :func:`worker.ffmpeg_utils._run`.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from config import settings as app_settings
from worker.ffmpeg_utils import FFmpegError, _default_timeout, _run

#: A binary that hangs for longer than any ceiling used here.
SLEEP = shutil.which("sleep")

#: Ceiling short enough to keep the suite fast, long enough not to be flaky on a
#: loaded CI box (process spawn alone can take tens of milliseconds).
SHORT_TIMEOUT = 1.5

#: How long the stand-in "ffmpeg" would run if nothing stopped it.
HANG_SECONDS = 60

requires_sleep = pytest.mark.skipif(SLEEP is None, reason="no 'sleep' binary on PATH")


# ---------------------------------------------------------------------------
# Timeout enforcement
# ---------------------------------------------------------------------------


@requires_sleep
def test_a_hanging_command_raises_instead_of_blocking_forever():
    """A command that outlives its ceiling raises ``FFmpegError`` promptly.

    Regression: ``_run`` called ``subprocess.run`` with no ``timeout``. Because jobs
    are processed by a thread pool with a single worker, one stalled ffmpeg blocked
    every subsequent job forever, with no error raised and no way to recover short of
    restarting the process.
    """
    started = time.monotonic()
    with pytest.raises(FFmpegError) as excinfo:
        _run([SLEEP, str(HANG_SECONDS)], timeout=SHORT_TIMEOUT)
    elapsed = time.monotonic() - started

    assert "timed out" in str(excinfo.value).lower()
    # The point of the ceiling is that it returns control, so the elapsed time must
    # track the timeout and not the command's own duration.
    assert elapsed < HANG_SECONDS, "the ceiling did not interrupt the command"
    assert elapsed >= SHORT_TIMEOUT, "returned before the ceiling could have expired"


@requires_sleep
def test_the_timed_out_child_is_not_left_running():
    """The killed child is reaped, so a timeout leaks no process.

    ``subprocess.run`` kills and waits on expiry; this pins that behaviour rather
    than assuming it, since a surviving ffmpeg would keep consuming CPU and holding
    file handles on the workspace the caller is about to delete.
    """
    with pytest.raises(FFmpegError):
        _run([SLEEP, str(HANG_SECONDS)], timeout=SHORT_TIMEOUT)

    # A reaped child leaves no matching process behind. pgrep is not guaranteed
    # present, so absence of the tool is not a failure of the code under test.
    pgrep = shutil.which("pgrep")
    if pgrep is None:  # pragma: no cover - environment dependent
        pytest.skip("no 'pgrep' binary to verify reaping")
    found = subprocess.run(
        [pgrep, "-f", f"{SLEEP} {HANG_SECONDS}"], capture_output=True, text=True
    )
    assert found.returncode != 0, f"child survived the timeout: {found.stdout!r}"


@requires_sleep
def test_a_command_finishing_inside_its_ceiling_is_unaffected():
    """The ceiling does not interfere with a command that completes in time."""
    proc = _run([SLEEP, "0"], timeout=SHORT_TIMEOUT)
    assert proc.returncode == 0


@pytest.mark.parametrize("opt_out", [0, 0.0, -1])
@requires_sleep
def test_a_non_positive_timeout_means_unbounded(opt_out):
    """``timeout <= 0`` is the documented opt-out and must not raise.

    Uses a command that exits immediately: the assertion is that no ceiling was
    applied, which a fast command demonstrates without making the suite slow.
    """
    proc = _run([SLEEP, "0"], timeout=opt_out)
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# Which ceiling applies
# ---------------------------------------------------------------------------


def test_ffprobe_gets_the_probe_ceiling_and_ffmpeg_the_encode_ceiling(monkeypatch):
    """The two binaries are classified apart, so metadata reads are not given an
    hour-long ceiling and encodes are not cut off after a minute."""
    monkeypatch.setattr(app_settings, "ffmpeg_binary", "ffmpeg")
    monkeypatch.setattr(app_settings, "ffprobe_binary", "ffprobe")
    monkeypatch.setattr(app_settings, "ffmpeg_timeout_seconds", 111.0)
    monkeypatch.setattr(app_settings, "ffprobe_timeout_seconds", 7.0)

    assert _default_timeout(["ffprobe", "-i", "x"]) == 7.0
    assert _default_timeout(["ffmpeg", "-i", "x"]) == 111.0


def test_an_absolute_binary_path_is_classified_by_its_basename(monkeypatch):
    """``/usr/bin/ffprobe`` is still a probe.

    Operators routinely pin absolute paths (the Dockerfile and the static-build
    layouts both do), so classifying on the raw string would silently hand every
    probe the hour-long encode ceiling.
    """
    monkeypatch.setattr(app_settings, "ffprobe_binary", "/opt/static/ffprobe")
    monkeypatch.setattr(app_settings, "ffmpeg_timeout_seconds", 111.0)
    monkeypatch.setattr(app_settings, "ffprobe_timeout_seconds", 7.0)

    assert _default_timeout(["/usr/local/bin/ffprobe", "-i", "x"]) == 7.0
    assert _default_timeout(["/usr/local/bin/ffmpeg", "-i", "x"]) == 111.0


def test_an_empty_command_falls_back_to_the_encode_ceiling(monkeypatch):
    """A degenerate argv must not raise out of the ceiling lookup itself."""
    monkeypatch.setattr(app_settings, "ffmpeg_timeout_seconds", 111.0)
    assert _default_timeout([]) == 111.0


# ---------------------------------------------------------------------------
# The pre-existing failure modes still behave
# ---------------------------------------------------------------------------


def test_a_missing_binary_is_reported_as_such(tmp_path: Path):
    """A absent binary keeps its distinct message rather than looking like a timeout."""
    with pytest.raises(FFmpegError) as excinfo:
        _run([str(tmp_path / "definitely-not-a-binary")])
    assert "Binary not found" in str(excinfo.value)


def test_a_failing_command_reports_its_stderr():
    """A non-zero exit is still surfaced with captured stderr."""
    false_binary = shutil.which("false")
    if false_binary is None:  # pragma: no cover - environment dependent
        pytest.skip("no 'false' binary on PATH")
    with pytest.raises(FFmpegError) as excinfo:
        _run([false_binary], timeout=SHORT_TIMEOUT)
    assert "Command failed" in str(excinfo.value)
