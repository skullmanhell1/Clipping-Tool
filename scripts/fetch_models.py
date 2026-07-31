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
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Where the models live in the working tree. The runtime reads this location from
#: ``settings.face_model_dir`` instead, because the container puts it elsewhere; this
#: constant is only the default for the script.
DEFAULT_MODELS_DIR = REPO_ROOT / "assets" / "models"

_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class Model_Entry:
    """One vendored model: what it is, where it came from, and how to prove it is intact."""

    filename: str
    sha256: str
    size_bytes: int
    source_url: str
    licence: str
    licence_file: str
    backend: str


#: The Model_Manifest. Every field is here so that "is this the right file?" is answerable
#: offline and without trusting the filename.
MODEL_MANIFEST: tuple[Model_Entry, ...] = (
    Model_Entry(
        filename="blaze_face_short_range.tflite",
        sha256="b4578f35940bf5a1a655214a1cce5cab13eba73c1297cd78e1a04c2380b0152f",
        size_bytes=229746,
        source_url=(
            "https://storage.googleapis.com/mediapipe-models/face_detector/"
            "blaze_face_short_range/float16/1/blaze_face_short_range.tflite"
        ),
        licence="Apache-2.0",
        licence_file="LICENSE-blazeface.txt",
        backend="mediapipe",
    ),
)


def _digest(path: Path) -> str:
    """Streamed SHA-256, so a large model is not read into memory whole."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "clipping-tool-build"})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        if response.status != 200:
            raise RuntimeError(f"{url} -> HTTP {response.status}")
        return response.read()


def verify(models_dir: Path) -> list[str]:
    """Return a list of human-readable problems; empty means every model verified.

    Performs no network access. Each problem names the file, because "a model is wrong" is
    not actionable and "blaze_face_short_range.tflite is 1024 bytes, expected 229746" is.
    """
    problems: list[str] = []
    for entry in MODEL_MANIFEST:
        path = models_dir / entry.filename
        if not path.is_file():
            problems.append(f"{entry.filename}: missing from {models_dir}")
            continue
        actual_size = path.stat().st_size
        if actual_size != entry.size_bytes:
            problems.append(
                f"{entry.filename}: {actual_size} bytes, expected {entry.size_bytes} "
                "(truncated or replaced)"
            )
            continue
        actual = _digest(path)
        if actual != entry.sha256:
            problems.append(
                f"{entry.filename}: sha256 {actual[:16]}..., expected {entry.sha256[:16]}..."
            )
            continue
        licence = models_dir / entry.licence_file
        if not licence.is_file():
            problems.append(
                f"{entry.licence_file}: licence text missing for {entry.filename} "
                f"({entry.licence})"
            )
    return problems


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
