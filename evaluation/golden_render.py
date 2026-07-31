"""Perceptual frame hashing for golden-output render tests (M1).

The theme connecting the worst defects found in this project is that **nothing measured the real
output**: a capability probe hid 124 ffmpeg filters, two parity guards never ran in CI, and a
caption font was never the one requested. Every one of those passed a thorough test suite, because
the suite checked filter strings and arguments rather than pixels.

The existing v0.8.0 parity gate compares *filter graphs*, which catches a graph that changed and is
blind to everything else: a font resolving to a different face, a LUT that silently did nothing, an
overlay drawn off-frame, a colour matrix shift from an encoder upgrade. All of those keep the graph
byte-identical and change what a viewer sees.

**Why perceptual hashing rather than exact frame hashes.** An exact hash of decoded pixels is
reproducible only for one ffmpeg build: libx264 output is deterministic for a given version and
flags, and changes legitimately between versions. A golden of exact hashes would therefore fail on
every ffmpeg upgrade, be re-frozen without inspection, and stop meaning anything - the same failure
mode a golden is supposed to prevent. An average hash over a downscaled greyscale frame is stable
across encoder noise while still reacting to the things that matter: captions vanishing, a grade
changing, a watermark moving, a crop shifting.

**What this cannot detect.** A change smaller than the 8x8 luma grid - a one-pixel caption offset, a
subtle colour shift within a bucket, a font substituted for a metrically similar one. It is a
regression net for *visible* change, not a pixel-exact contract, and pretending otherwise would be
worse than the honest version because a green run would be read as a stronger guarantee than it is.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

#: Side of the greyscale grid each frame is reduced to before hashing.
#:
#: 8x8 (64 bits) is the classic average-hash size. Larger grids react to smaller changes and to
#: more encoder noise; at 16x16 a re-encode of the *same* source registered differences comparable
#: to a real caption change, which would make the tolerance meaningless.
HASH_GRID = 8

#: Default Hamming distance allowed between a frame and its golden, out of ``HASH_GRID**2`` bits.
#:
#: Measured rather than guessed: re-encoding an identical render with the same ffmpeg produced a
#: distance of 0, and a second pass through a different CRF produced at most 2. Six leaves room for
#: encoder and scaler differences while a vanished caption line moves 10-20 bits.
DEFAULT_TOLERANCE = 6


@dataclass(frozen=True)
class FrameHash:
    """One sampled frame: its timestamp, its average hash, and its mean luma.

    ``mean`` and ``spread`` are not decoration, and finding out which one was needed took
    measuring rather than reasoning.

    An average hash compares each cell against the frame's *own* mean, so it is invariant to global
    brightness and contrast **by construction** and cannot see a grade change. Measured on a
    testsrc2 clip: burning in a caption bar moved 11 bits, while pushing contrast to 1.6 and
    saturation to 1.8 moved 2 - inside the tolerance needed to absorb encoder noise.

    Adding the mean was not enough either. Contrast pivots around mid-grey, so the graded clip's
    mean moved 0.47 levels while a CRF 20 -> 32 re-encode moved 0.06 - no usable threshold between
    them. The **spread** is what separates them: 49.5 on the base clip, 49.5 re-encoded, 75.5
    graded. So all three signals are stored, each covering what the others miss.
    """

    at: float
    hash: str
    mean: float = 0.0
    spread: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": round(self.at, 3),
            "hash": self.hash,
            "mean": round(self.mean, 2),
            "spread": round(self.spread, 2),
        }


def _luma_grid(video: str | Path, at: float, ffmpeg: str = "ffmpeg") -> Optional[list[int]]:
    """The frame at ``at`` reduced to a ``HASH_GRID**2`` list of luma values, or ``None``."""
    result = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0.0, float(at)):.3f}", "-i", str(video),
            "-frames:v", "1",
            # Explicit scaler flags: the default changes between builds, and a different
            # downscale filter is a different hash for identical pixels.
            "-vf", f"scale={HASH_GRID}:{HASH_GRID}:flags=bilinear,format=gray",
            "-f", "rawvideo", "-",
        ],
        capture_output=True,
    )
    data = result.stdout
    if result.returncode != 0 or len(data) < HASH_GRID * HASH_GRID:
        return None
    return list(data[: HASH_GRID * HASH_GRID])


#: How far the mean luma and its spread may drift from the golden, in 0-255 levels.
#:
#: Measured, not chosen: a CRF 20 -> 32 re-encode of an identical render moved the mean by 0.06 and
#: the spread by 0.04, while burning in a caption bar moved the mean by 8.2 and a contrast/saturation
#: change moved the spread by 26. 4.0 sits well clear of encoder noise on both.
DEFAULT_MEAN_TOLERANCE = 4.0


def average_hash(video: str | Path, at: float, ffmpeg: str = "ffmpeg") -> Optional[str]:
    """The average hash of one frame as hex, or ``None`` when the frame cannot be read."""
    measured = _measure(video, at, ffmpeg=ffmpeg)
    return None if measured is None else measured[0]


def _measure(
    video: str | Path, at: float, ffmpeg: str = "ffmpeg"
) -> Optional[tuple[str, float, float]]:
    """``(average_hash, mean_luma, luma_spread)`` for one frame, or ``None``."""
    grid = _luma_grid(video, at, ffmpeg=ffmpeg)
    if grid is None:
        return None
    mean = sum(grid) / len(grid)
    spread = (sum((value - mean) ** 2 for value in grid) / len(grid)) ** 0.5
    bits = ["1" if value > mean else "0" for value in grid]
    digest = f"{int(''.join(bits), 2):0{HASH_GRID * HASH_GRID // 4}x}"
    return digest, mean, spread


def hash_frames(
    video: str | Path,
    duration: float,
    *,
    count: int = 5,
    ffmpeg: str = "ffmpeg",
) -> list[FrameHash]:
    """Sample ``count`` frames evenly across ``duration`` and hash each.

    Samples at bucket *midpoints* rather than at 0 and ``duration``: the first frame of an encode
    can be a keyframe with different quantisation, and the last is often past the final decodable
    frame, so both are noisier than the material between them.
    """
    span = max(0.001, float(duration))
    total = max(1, int(count))
    hashes: list[FrameHash] = []
    for index in range(total):
        at = span * (index + 0.5) / total
        measured = _measure(video, at, ffmpeg=ffmpeg)
        if measured is not None:
            digest, mean, spread = measured
            hashes.append(FrameHash(at=at, hash=digest, mean=mean, spread=spread))
    return hashes


def distance(left: str, right: str) -> int:
    """Hamming distance between two hex hashes, in bits."""
    return bin(int(left, 16) ^ int(right, 16)).count("1")


@dataclass
class Comparison:
    """The result of comparing a render against a golden."""

    ok: bool
    worst: int
    tolerance: int
    detail: list[dict[str, Any]]

    def summary(self) -> str:
        if self.ok:
            return f"matches the golden (worst frame {self.worst}/{HASH_GRID ** 2} bits)"
        lines = [
            f"differs from the golden: worst frame {self.worst} bits "
            f"(tolerance {self.tolerance}) or luma beyond tolerance"
        ]
        for entry in self.detail:
            if (entry["distance"] > self.tolerance
                    or entry.get("mean_shift", 0) > 0
                    or entry.get("spread_shift", 0) > 0):
                lines.append(
                    f"  at {entry['at']:.3f}s: {entry['distance']} bits, "
                    f"luma shift {entry.get('mean_shift', 0)}, "
                    f"spread shift {entry.get('spread_shift', 0)} "
                    f"({entry['golden']} -> {entry['actual']})"
                )
        return "\n".join(lines)


def compare(
    actual: list[FrameHash],
    golden: list[dict[str, Any]],
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    mean_tolerance: float = DEFAULT_MEAN_TOLERANCE,
) -> Comparison:
    """Compare sampled hashes against a stored golden.

    A **count mismatch fails**, and deliberately: a render that produced fewer readable frames than
    the golden has changed in a way worth looking at, and comparing only the overlap would report a
    truncated render as a pass.
    """
    if len(actual) != len(golden):
        return Comparison(
            ok=False,
            worst=HASH_GRID ** 2,
            tolerance=tolerance,
            detail=[{
                "at": 0.0,
                "distance": HASH_GRID ** 2,
                "golden": f"{len(golden)} frames",
                "actual": f"{len(actual)} frames",
            }],
        )

    detail: list[dict[str, Any]] = []
    worst = 0
    level_ok = True
    for sampled, expected in zip(actual, golden):
        gap = distance(sampled.hash, str(expected.get("hash") or "0"))
        worst = max(worst, gap)
        # The luma checks catch what the structural hash is blind to: mean for a brightness
        # change, spread for a contrast change. Neither alone separates a grade from a re-encode.
        golden_mean = expected.get("mean")
        level_gap = (
            abs(float(sampled.mean) - float(golden_mean))
            if golden_mean is not None else 0.0
        )
        golden_spread = expected.get("spread")
        spread_gap = (
            abs(float(sampled.spread) - float(golden_spread))
            if golden_spread is not None else 0.0
        )
        if level_gap > mean_tolerance or spread_gap > mean_tolerance:
            level_ok = False
        detail.append({
            "at": sampled.at,
            "distance": gap,
            "golden": expected.get("hash"),
            "actual": sampled.hash,
            "mean_shift": round(level_gap, 2),
            "spread_shift": round(spread_gap, 2),
        })
    return Comparison(
        ok=worst <= tolerance and level_ok,
        worst=worst,
        tolerance=tolerance,
        detail=detail,
    )


def load_golden(path: str | Path) -> Optional[list[dict[str, Any]]]:
    """Read a golden file, or ``None`` when it does not exist yet."""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    frames = raw.get("frames") if isinstance(raw, dict) else raw
    return frames if isinstance(frames, list) else None


def write_golden(path: str | Path, name: str, frames: list[FrameHash], notes: str = "") -> Path:
    """Write a golden file. Includes the grid size, so a change to it invalidates the golden."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "name": name,
                "grid": HASH_GRID,
                "notes": notes,
                "frames": [frame.to_dict() for frame in frames],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path
