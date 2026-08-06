#!/usr/bin/env python3
"""Vendor and verify the detector models this project ships.

Modelled directly on ``scripts/fetch_emoji.py``, and for the same reason. A model that is
downloaded on first use is a model that is absent on the host where it matters: the
no-skips rule means a test cannot depend on a fetch, ``permissibility_mode`` exists to
guarantee no external sourcing, and a render that reaches the network is a render that
behaves differently on two machines. So the model is committed, and this script is the
maintainer-side step that puts it there.

Two modes, and the split is the point:

* **fetch** (default) — downloads each model in the manifest. Run by a maintainer, never by
  the render path, never by a test.
* ``--check`` — verifies the working tree against the manifest using **no network at all**,
  exits non-zero and names the offending file. This is what CI runs, and what makes a
  truncated or swapped model a build failure rather than a detection-time crash.

A digest rather than a existence check, deliberately: a half-downloaded ``.tflite`` is a file
that exists, has plausible size, and makes ``FaceDetector.create_from_options`` fail deep
inside the native graph at the first frame of a render.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The manifest and the verification live in `worker/face_models.py`, not here. The render path
# needs both in order to decide whether a model on disk is usable before constructing a backend
# against it, so a copy in this script would be a second definition of the digests -- and the
# copy the renderer reads would be the one free to go stale.
from worker.face_models import MODEL_MANIFEST, verify  # noqa: E402

#: Where the models live in the working tree. The runtime reads this from
#: ``settings.face_model_dir`` instead, because the container puts it elsewhere.
DEFAULT_MODELS_DIR = REPO_ROOT / "assets" / "models"


def _get(url: str) -> bytes:
    # The URLs are constants in worker/face_models.py, so the scheme is not attacker-controlled
    # today. Asserted rather than assumed anyway, because `urlopen` honours `file:` and custom
    # schemes: if a manifest entry ever gained a relative or `file:` URL, a model fetch would
    # quietly become an arbitrary local read. The noqa records that the check exists.
    if not url.startswith("https://"):
        raise ValueError(f"refusing to fetch a model over a non-HTTPS URL: {url!r}")
    request = urllib.request.Request(url, headers={"User-Agent": "clipping-tool-build"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        if response.status != 200:
            raise RuntimeError(f"{url} -> HTTP {response.status}")
        return response.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-fetch existing files")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify vendored models against the manifest; fetch nothing, exit 1 on a problem",
    )
    parser.add_argument(
        "--models-dir",
        default=None,
        help=f"directory holding the models (default: {DEFAULT_MODELS_DIR})",
    )
    args = parser.parse_args(argv)

    models_dir = Path(args.models_dir) if args.models_dir else DEFAULT_MODELS_DIR

    if args.check:
        problems = verify(models_dir)
        if problems:
            print(f"model verification FAILED ({len(problems)} problem(s)):")
            for problem in problems:
                print(f"  {problem}")
            print("run: python scripts/fetch_models.py")
            return 1
        names = ", ".join(entry.filename for entry in MODEL_MANIFEST)
        print(f"all {len(MODEL_MANIFEST)} detector model(s) verified: {names}")
        return 0

    models_dir.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    fetched = skipped = 0
    for entry in MODEL_MANIFEST:
        dest = models_dir / entry.filename
        if dest.is_file() and dest.stat().st_size > 0 and not args.force:
            skipped += 1
            continue
        try:
            payload = _get(entry.source_url)
            actual = hashlib.sha256(payload).hexdigest()
            if actual != entry.sha256:
                # Written nowhere: a digest mismatch on a fresh download means the upstream
                # artefact changed, which a maintainer must look at rather than commit.
                failures.append(
                    f"{entry.filename}: downloaded sha256 {actual} != manifest {entry.sha256}"
                )
                continue
            dest.write_bytes(payload)
            fetched += 1
        except Exception as exc:  # noqa: BLE001 - report every model, then exit non-zero
            failures.append(f"{entry.filename}: {exc}")

    print(f"fetched {fetched}, already present {skipped}, failed {len(failures)}")
    for failure in failures:
        print(f"  FAILED {failure}")
    if not failures:
        print("licence text is committed alongside each model and is not fetched here")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
