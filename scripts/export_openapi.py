#!/usr/bin/env python3
"""Write the OpenAPI document to ``openapi.json``, or check the committed one is current.

The API surface had no reviewable record. ``/openapi.json`` is served at runtime, which means the
only way to see that a PR renamed a field, dropped a route or changed a response shape was to
notice it in the diff of the code that produced it - and a breaking change to a response model is
exactly the kind of thing that reads as a small refactor. Committing the document turns every such
change into an explicit line in the PR diff.

A committed document that has drifted is worse than none: it looks authoritative while describing
an API that no longer exists. So this has a ``--check`` mode and CI blocks on it, which is the same
argument the repo already makes for ``black --check`` and for the dependency locks.

**``info.version`` is deliberately normalised out.** It comes from the ``VERSION`` file, and
``.github/workflows/release.yml`` fires on a change to that file, so a real version would make the
committed document stale on every release - the drift check would fail the release PR itself, for a
line that says nothing about the API. Worse, it would fail *predictably*, which is how a check
becomes something people learn to override. The version is not API surface; it is metadata on a
release schedule unrelated to the shape of the endpoints. What this document exists to make
reviewable is the surface, so the version is replaced with a placeholder on both sides of the
comparison and the real value stays in ``VERSION``, which is already the single source of truth.

Keys are sorted. FastAPI's own ordering is deterministic today (verified across processes), but it
follows route-registration order, so moving an `include_router` call would otherwise produce a
large diff describing no change at all.

Usage::

    python scripts/export_openapi.py            # write openapi.json
    python scripts/export_openapi.py --check     # exit 1 if it is stale
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# So `import api` works when this is run as `python scripts/export_openapi.py`, which puts
# `scripts/` on the path rather than the repo root. Same two lines as `scripts/smoke_reel.py`.
sys.path.insert(0, str(ROOT))

TARGET = ROOT / "openapi.json"

#: What ``info.version`` is replaced with. Not ``0.0.0``, which is the genuine value
#: ``_read_version`` falls back to when the VERSION file cannot be read - reusing it would make a
#: real failure indistinguishable from this placeholder. The text says what it is instead.
VERSION_PLACEHOLDER = "normalised-see-VERSION-file"


def document() -> dict:
    """The OpenAPI document with the version normalised.

    Imported here rather than at module scope so ``--help`` works without constructing the whole
    application, and so an import error in the app is reported while doing something the user
    asked for rather than on startup.
    """
    from api.main import app

    schema = app.openapi()
    # A copy, because `app.openapi()` caches its result on the app object: mutating it in place
    # would serve this placeholder to real clients from `/openapi.json` for the life of the
    # process. Harmless in a script that exits immediately, and not harmless in the test suite,
    # which imports the same app object.
    schema = json.loads(json.dumps(schema))
    schema.setdefault("info", {})["version"] = VERSION_PLACEHOLDER
    return schema


def serialise(schema: dict) -> str:
    # A trailing newline so the file is a well-formed text file: without one, `git diff` reports
    # "\ No newline at end of file" on every change and some editors add one silently, which
    # would make the check fail for a reason unrelated to the API.
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the committed document differs from the code",
    )
    args = parser.parse_args(argv)

    current = serialise(document())

    if not args.check:
        TARGET.write_text(current, encoding="utf-8")
        paths = len(json.loads(current).get("paths", {}))
        print(f"wrote {TARGET.relative_to(ROOT)} ({paths} paths)")
        return 0

    if not TARGET.is_file():
        print(f"::error::{TARGET.name} is missing. Run: python scripts/export_openapi.py")
        return 1

    committed = TARGET.read_text(encoding="utf-8")
    if committed == current:
        print(f"{TARGET.name} matches the code ({len(json.loads(current)['paths'])} paths)")
        return 0

    # The diff is printed rather than just the failure, because the useful information is *what*
    # changed - a renamed field and a dropped route need different responses from the author.
    print(f"::error::{TARGET.name} is stale. Run: python scripts/export_openapi.py")
    sys.stdout.writelines(
        difflib.unified_diff(
            committed.splitlines(keepends=True),
            current.splitlines(keepends=True),
            fromfile=f"{TARGET.name} (committed)",
            tofile=f"{TARGET.name} (generated from the code)",
        )
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
