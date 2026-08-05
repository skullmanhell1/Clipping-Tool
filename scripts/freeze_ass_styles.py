#!/usr/bin/env python3
"""Freeze every ASS document this repo writes, per configuration.

Writes `tests/golden/ass_documents.json`, which `tests/test_ass_style_parity.py` compares
against.

**Run this deliberately, and read the diff.** Every changed line is a change to how something
renders on screen. The `[V4+ Styles]` `Format:` line declares 23 fields and libass does not
complain when a `Style:` line carries a different number — it defaults the fields it could not
read and renders anyway. So none of these changes raise at render time: a dropped comma is a
caption in the wrong colour, not an error.

Deliberately a script rather than an `--update` flag on the test. A golden that rewrites itself
when it fails is not a guard, and the failure mode is silent: the suite goes green and the
regression ships.

    python scripts/freeze_ass_styles.py            # rewrite the file
    python scripts/freeze_ass_styles.py --check    # exit 1 if it would change
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from tests.test_ass_style_parity import CONFIGURATIONS, GOLDEN, build_case  # noqa: E402


def capture_all() -> dict:
    """Build every configuration, using pytest's own monkeypatch outside a test run."""
    frozen: dict = {}
    for name in CONFIGURATIONS:
        patch = pytest.MonkeyPatch()
        try:
            with tempfile.TemporaryDirectory(prefix="freeze-ass-") as raw:
                frozen[name] = build_case(name, patch, Path(raw))
        finally:
            patch.undo()
    return frozen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if the frozen file would change, without writing it")
    args = parser.parse_args(argv)

    frozen = capture_all()
    serialised = json.dumps(frozen, indent=2, sort_keys=True) + "\n"

    if args.check:
        current = GOLDEN.read_text(encoding="utf-8") if GOLDEN.exists() else ""
        if current != serialised:
            print("the frozen ASS documents are out of date", file=sys.stderr)
            return 1
        print(f"frozen ASS documents match ({len(frozen)} configurations)")
        return 0

    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(serialised, encoding="utf-8")
    styles = sum(line.startswith("Style: ")
                 for doc in frozen.values() for line in doc.splitlines())
    print(f"wrote {GOLDEN.relative_to(REPO)}: {len(frozen)} configurations, "
          f"{styles} Style: lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
