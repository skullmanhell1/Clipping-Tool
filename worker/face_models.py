"""The Model_Manifest for vendored detector models, and offline verification.

Lives here rather than in ``scripts/fetch_models.py`` so that **one** definition serves both
callers: the maintainer-side fetch script and the render path, which has to decide whether the
model on disk is usable before constructing a backend against it. Putting the digests in the
script and a copy in the worker would be the duplicated-fact pattern that mutation testing has
caught twice in this repository — and the copy the renderer reads is the one that would be
free to go stale.

Nothing in this module touches the network. The fetch lives in the script; this half only ever
reads the working tree.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from config import settings

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


#: Every field is recorded so that "is this the right file?" is answerable offline, without
#: trusting the filename, and so a maintainer re-fetching it can tell whether upstream moved.
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


def entry_for_backend(backend: str) -> Model_Entry | None:
    """The manifest entry serving ``backend``, or ``None``."""
    for entry in MODEL_MANIFEST:
        if entry.backend == backend:
            return entry
    return None


def models_dir() -> Path:
    """Where the models are, from settings so the container can put them elsewhere."""
    return Path(settings.face_model_dir)


def digest(path: Path) -> str:
    """Streamed SHA-256, so a large model is not read into memory whole."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            hasher.update(block)
    return hasher.hexdigest()


def verify(directory: Path | None = None) -> list[str]:
    """Return human-readable problems; empty means every model verified.

    Performs no network access. Each problem names the file, because "a model is wrong" is not
    actionable and "blaze_face_short_range.tflite is 1024 bytes, expected 229746" is.
    """
    directory = Path(directory) if directory is not None else models_dir()
    problems: list[str] = []
    for entry in MODEL_MANIFEST:
        path = directory / entry.filename
        if not path.is_file():
            problems.append(f"{entry.filename}: missing from {directory}")
            continue
        actual_size = path.stat().st_size
        if actual_size != entry.size_bytes:
            problems.append(
                f"{entry.filename}: {actual_size} bytes, expected {entry.size_bytes} "
                "(truncated or replaced)"
            )
            continue
        actual = digest(path)
        if actual != entry.sha256:
            problems.append(
                f"{entry.filename}: sha256 {actual[:16]}..., expected {entry.sha256[:16]}..."
            )
            continue
        licence = directory / entry.licence_file
        if not licence.is_file():
            problems.append(
                f"{entry.licence_file}: licence text missing for {entry.filename} ({entry.licence})"
            )
    return problems


def resolve_model(backend: str, directory: Path | None = None) -> Path | None:
    """The path to ``backend``'s model when it is present and intact, else ``None``.

    **Size is checked, the full digest is not.** A ``stat`` is free and catches the realistic
    corruption — a truncated or partially written download — while hashing 230 KB on every
    clip buys protection against a *deliberately* substituted file of identical length, which
    is not a threat model this guard can address anyway. The full digest is checked by
    ``scripts/fetch_models.py --check``, which CI runs on every push; that is the right place
    for it, because it is a property of the tree rather than of a render.

    Returning ``None`` rather than raising is what lets the caller degrade to Haar and record
    a substitution, per the degradation ladder.
    """
    entry = entry_for_backend(backend)
    if entry is None:
        return None
    directory = Path(directory) if directory is not None else models_dir()
    path = directory / entry.filename
    try:
        if not path.is_file() or path.stat().st_size != entry.size_bytes:
            return None
    except OSError:
        return None
    return path
