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
from pathlib import Path

import pytest

from config import BASE_DIR

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
                    (path.relative_to(BASE_DIR), node.lineno, {k.arg for k in node.keywords if k.arg})
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
