#!/usr/bin/env python3
"""Freeze the ffmpeg command `compositor.render_clip` builds, per configuration.

Writes `tests/golden/compositor_commands.json`, which
`tests/test_compositor_graph_parity.py` compares against.

**Run this deliberately, and read the diff.** Every changed line describes a change to what
ffmpeg is asked to do — a different filter order, a different label, a different input index, a
different codec decision. None of those raise an error at render time: a mis-ordered overlay
still encodes, and a caption drawn under a b-roll clip still produces a valid file. The diff is
the only place that shows up before someone watches the output.

Deliberately a script rather than an `--update` flag on the test. A golden that rewrites itself
when it fails is not a guard, and the failure mode is silent: the suite goes green and the
regression ships.

    python scripts/freeze_compositor_graph.py            # rewrite the file
    python scripts/freeze_compositor_graph.py --check     # exit 1 if it would change
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from config import settings  # noqa: E402
from tests.conftest import FakeWord, options_all_off  # noqa: E402
from tests.test_compositor_graph_parity import (  # noqa: E402
    CONFIGURATIONS,
    GOLDEN,
    _normalise,
    resolvers,
)
from worker.effects import compositor  # noqa: E402


def _make_video(dest: Path, ffmpeg: str) -> Path:
    """The same synthetic clip the tests use, so the frozen numbers match theirs."""
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=1080x1920:rate=30:duration=3.0",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:duration=3.0",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return dest


def _make_recorder(recorded: list[list[str]]):
    """A stand-in for `compositor._run` that records the command instead of running ffmpeg.

    It still creates the destination file, because `render_clip` stats its own output.
    """

    def _fake_run(cmd, *a, **kw):
        recorded.append([str(part) for part in cmd])
        dest = Path(cmd[-1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\0" * 64)

    return _fake_run


def _words():
    return [
        FakeWord(0.2, 0.6, "This"),
        FakeWord(0.7, 1.1, "is"),
        FakeWord(1.2, 1.6, "fire"),
        FakeWord(1.7, 2.2, "money"),
    ]


def capture_all() -> dict:
    ffmpeg = shutil.which(settings.ffmpeg_binary) or shutil.which("ffmpeg")
    if not ffmpeg:
        sys.exit("ffmpeg is not on PATH; the frozen commands are built from a real probe")

    original_run = compositor._run
    frozen: dict = {}
    try:
        for name in sorted(CONFIGURATIONS):
            spec = dict(CONFIGURATIONS[name])
            option_kwargs = spec.pop("options")
            needs = spec.pop("needs", None)
            with tempfile.TemporaryDirectory(prefix="freeze-") as raw:
                tmp = Path(raw)
                spec.update(resolvers(needs, tmp))
                base = _make_video(tmp / "base.mp4", ffmpeg)
                recorded: list[list[str]] = []
                # Bound as a default rather than closed over: `recorded` is rebound every
                # iteration, and a closure that reads it from the enclosing scope would record
                # into whichever list happened to be current when it ran. It is only ever called
                # within this iteration, so the behaviour is the same -- but that was an
                # invariant of the call site, not of this function.
                compositor._run = _make_recorder(recorded)
                result = compositor.render_clip(
                    base,
                    tmp / "out.mp4",
                    options_all_off(**option_kwargs),
                    _words(),
                    tmp,
                    **spec,
                )

                if result is None:
                    frozen[name] = {"rendered": False}
                    continue
                cmd = [_normalise(part, tmp) for part in recorded[0]]
                graph = []
                if "-filter_complex" in cmd:
                    graph = cmd[cmd.index("-filter_complex") + 1].split(";")
                frozen[name] = {
                    "rendered": True,
                    "inputs": [cmd[i + 1] for i, p in enumerate(cmd) if p == "-i"],
                    "graph": graph,
                    "flags": [
                        p for p in cmd if p.startswith("-") and p not in ("-i", "-filter_complex")
                    ],
                    "maps": [cmd[i + 1] for i, p in enumerate(cmd) if p == "-map"],
                    "effects": sorted(result.effects_applied),
                }
    finally:
        compositor._run = original_run
    return frozen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the frozen file would change, without writing it",
    )
    args = parser.parse_args(argv)

    frozen = capture_all()
    serialised = json.dumps(frozen, indent=2, sort_keys=True) + "\n"

    if args.check:
        current = GOLDEN.read_text(encoding="utf-8") if GOLDEN.exists() else ""
        if current != serialised:
            print("the frozen compositor commands are out of date", file=sys.stderr)
            return 1
        print(f"frozen commands match ({len(frozen)} configurations)")
        return 0

    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(serialised, encoding="utf-8")
    rendered = sum(1 for entry in frozen.values() if entry["rendered"])
    print(
        f"wrote {GOLDEN.relative_to(REPO)}: {len(frozen)} configurations "
        f"({rendered} render, {len(frozen) - rendered} return None)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
