"""Every subprocess is bounded and never inherits this process's stdin.

**This is the gate that would have saved six hours per CI run.**

The Backend job hit the GitHub Actions six-hour job limit on every run from PR #148 onward — and
was killed, so the suite never reported. The same suite finishes in about eight minutes locally,
which is exactly what makes the cause hard to see: it is a property of the *environment*, not of
the code.

The mechanism: **ffmpeg reads stdin for interactive keystrokes unless told not to.** With no
redirection it inherits whatever the parent has. Under pytest on a CI runner that is an open pipe
which never delivers EOF, so ffmpeg blocks on the read and never exits. `ffmpeg_utils._run` passed
no `stdin=`, and `ffmpeg_timeout_seconds` defaults to **3600** — so a single stalled call burned an
hour, and six of them consumed the job. Three call sites had no timeout at all, and nothing could
end those.

The runner said so in its last line, and it is the give-away worth remembering:

    Terminate orphan process: pid (50388) (ffmpeg)

Two rules, enforced by walking the AST rather than by grepping, because a call spanning several
lines defeats a line-based search and that is precisely the shape most of these have:

1. every `subprocess` call passes `timeout=` — an unbounded call cannot be recovered from;
2. every one passes `stdin=` (or `input=`, which supplies stdin itself).

An allow-list is deliberately absent. There is no call in this project that legitimately wants an
inherited interactive stdin, and adding an exception mechanism invites the next hang.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from config import BASE_DIR
from tests import conftest

#: Directories owned by this project. `tests/` is excluded: a test may legitimately drive a
#: subprocess in an unusual way to exercise a failure path, and this rule is about production code.
_ROOTS = ("api", "evaluation", "publishers", "scripts", "storage_backends", "worker")


def _subprocess_calls() -> list[tuple[Path, int, set[str]]]:
    """Every ``subprocess.run``/``Popen``/``check_output`` call, with the kwargs it passes."""
    found: list[tuple[Path, int, set[str]]] = []
    for root in _ROOTS:
        for path in sorted((BASE_DIR / root).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr not in ("run", "Popen", "check_output"):
                    continue
                if getattr(node.func.value, "id", "") != "subprocess":
                    continue
                found.append(
                    (
                        path.relative_to(BASE_DIR),
                        node.lineno,
                        {k.arg for k in node.keywords if k.arg},
                    )
                )
    return found


def test_the_scan_finds_the_calls_it_is_meant_to_check():
    """A guard on the two tests below, which would pass vacuously on an empty list.

    This project is mostly a filter graph, so a scan of it that finds no subprocess calls has
    broken rather than found nothing — and it would report success while checking nothing.
    """
    calls = _subprocess_calls()
    assert len(calls) >= 25, f"only {len(calls)} subprocess calls found; the scan is broken"


@pytest.mark.parametrize(
    ("path", "lineno", "kwargs"),
    [pytest.param(p, n, k, id=f"{p}:{n}") for p, n, k in _subprocess_calls()],
)
def test_every_subprocess_call_is_bounded(path, lineno, kwargs):
    """An unbounded call is a hang nothing can end.

    ``worker/segmentation.py``'s ``silencedetect`` was one of these, and it runs on the fallback
    selection path — i.e. often.
    """
    assert "timeout" in kwargs, (
        f"{path}:{lineno} calls subprocess with no timeout. A hung child then blocks the parent "
        "for ever; jobs run on a pool with a single worker, so it blocks the whole queue."
    )


@pytest.mark.parametrize(
    ("path", "lineno", "kwargs"),
    [pytest.param(p, n, k, id=f"{p}:{n}") for p, n, k in _subprocess_calls()],
)
def test_no_subprocess_inherits_stdin(path, lineno, kwargs):
    """ffmpeg reads stdin unless told not to, and an inherited pipe never delivers EOF.

    ``input=`` satisfies this too: it supplies stdin as a pipe that is written and closed, so the
    child sees EOF. ``publishers/whop.py`` is the one call that takes that route, and passing both
    ``input=`` and ``stdin=`` raises ``ValueError`` — so this rule accepts either, not both.
    """
    assert "stdin" in kwargs or "input" in kwargs, (
        f"{path}:{lineno} calls subprocess without closing stdin. Pass "
        "stdin=subprocess.DEVNULL (or input=..., which supplies it). ffmpeg blocks reading an "
        "inherited interactive stdin, which is what took the CI job to its six-hour limit."
    )


def test_the_central_seam_closes_stdin():
    """Stated separately because it is the one that covers everything routed through it.

    Most ffmpeg in this project goes through ``ffmpeg_utils._run``. Fixing it there is worth more
    than fixing every remembered call site, and it is the line most likely to be "simplified" by
    someone who has not read this file.
    """
    source = (BASE_DIR / "worker" / "ffmpeg_utils.py").read_text(encoding="utf-8")
    assert "stdin=subprocess.DEVNULL" in source, (
        "ffmpeg_utils._run no longer closes stdin; every routed ffmpeg call can hang again"
    )


# --------------------------------------------------------------------------- #
# The suite's own subprocesses are bounded too                                 #
# --------------------------------------------------------------------------- #
# The production seam above was not enough, and the gap cost months of CI. Tests call
# `subprocess.run` **directly** in 156 places, 129 of them ffmpeg or ffprobe, and none of
# them passed a timeout or closed stdin. Any one of those can wedge the whole job:
# pytest's stdin is a pipe that never reaches EOF, ffmpeg reads it unless told not to, and
# a call that should fail in milliseconds waits for ever.
#
# It was `tests/test_subject_scale.py`'s — a test that deliberately runs a command
# *expected to abort*, so on a build where it blocks instead, the test written to prove a
# mechanism unusable is what made the suite unusable. Every hang stopped at `82%`, which is
# that file's position in the run order.
#
# The fix is a seam in `tests/conftest.py`, for the same reason the production fix is a seam
# in `worker.ffmpeg_utils._run`: it covers every existing call *and* every one written after
# today, where a 60-file mechanical edit covers only the ones somebody remembered.


def test_the_suite_bounds_its_own_subprocesses():
    """`tests/conftest.py` replaces ``subprocess.run`` with a bounded wrapper."""
    import subprocess

    from tests.conftest import TEST_SUBPROCESS_TIMEOUT_S, _bounded_run

    assert subprocess.run is _bounded_run, (
        "the conftest subprocess seam is not installed; every unbounded ffmpeg call in the "
        "suite can wedge the whole job again"
    )
    assert 30.0 <= TEST_SUBPROCESS_TIMEOUT_S <= 1800.0, TEST_SUBPROCESS_TIMEOUT_S


def test_the_seam_supplies_a_timeout_and_closes_stdin():
    """Asserted by observing what reaches the real ``subprocess.run``, not by reading source."""
    import subprocess

    seen: dict[str, object] = {}

    def _spy(*args, **kwargs):
        seen.update(kwargs)

        class _Done:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Done()

    original = conftest._UNPATCHED_RUN
    conftest._UNPATCHED_RUN = _spy
    try:
        subprocess.run([sys.executable, "-c", "pass"])
    finally:
        conftest._UNPATCHED_RUN = original

    assert seen.get("stdin") is subprocess.DEVNULL
    assert seen.get("timeout") == conftest.TEST_SUBPROCESS_TIMEOUT_S


def test_the_seam_does_not_override_a_deliberate_choice():
    """A test that sets its own ``timeout``/``stdin``/``input`` keeps it.

    ``input`` matters specifically: ``subprocess`` raises ``ValueError`` when both ``input``
    and ``stdin`` are given, so a seam that always set ``stdin`` would break every caller
    that pipes data in.
    """
    import subprocess

    seen: dict[str, object] = {}

    def _spy(*args, **kwargs):
        seen.clear()
        seen.update(kwargs)

        class _Done:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Done()

    original = conftest._UNPATCHED_RUN
    conftest._UNPATCHED_RUN = _spy
    try:
        subprocess.run([sys.executable, "-c", "pass"], timeout=7.5)
        assert seen.get("timeout") == 7.5

        subprocess.run([sys.executable, "-c", "pass"], input="hello", text=True)
        assert seen.get("input") == "hello"
        assert "stdin" not in seen, "stdin must not be supplied alongside input"

        subprocess.run([sys.executable, "-c", "pass"], stdin=subprocess.PIPE)
        assert seen.get("stdin") is subprocess.PIPE
    finally:
        conftest._UNPATCHED_RUN = original


def test_a_wedged_subprocess_fails_the_test_rather_than_the_job():
    """The point of the bound: a hang becomes a named failure in seconds.

    Without it the process waits until the platform kills the job — 360 minutes, with no
    attribution beyond the last progress line.
    """
    import subprocess

    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.3)
