#!/usr/bin/env python3
"""Freeze the app's routing table.

Writes `tests/golden/route_table.json`, which `tests/test_api_route_table.py` compares against.

**Run this deliberately, and read the diff.** Every line is a change to the app's public HTTP
surface, and the frontend has 38 hard-coded `fetch` calls against it.

    python scripts/freeze_route_table.py            # rewrite the file
    python scripts/freeze_route_table.py --check    # exit 1 if it would change
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tests.test_api_route_table import GOLDEN, capture  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the frozen file would change, without writing it",
    )
    args = parser.parse_args(argv)

    table = capture()
    serialised = json.dumps(table, indent=2, sort_keys=True) + "\n"

    if args.check:
        current = GOLDEN.read_text(encoding="utf-8") if GOLDEN.exists() else ""
        if current != serialised:
            print("the frozen route table is out of date", file=sys.stderr)
            return 1
        print(f"frozen route table matches ({len(table['routes'])} endpoints)")
        return 0

    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(serialised, encoding="utf-8")
    print(
        f"wrote {GOLDEN.relative_to(REPO)}: {len(table['routes'])} endpoints, "
        f"{len(table['mounts'])} mounts, {len(table['builtin'])} built-in routes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
