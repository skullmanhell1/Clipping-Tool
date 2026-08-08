"""Shared time-base and timeline primitives for AV engines.

Two concerns live here, both of them **pure**:

* :class:`Time_Base` — the per-clip timing record (frame rate, audio sample rate,
  rounding rule) every engine uses to convert seconds to frames or samples, plus
  the frame-boundary :meth:`Time_Base.snap` operation (Reqs 13, 15.3, 15.4).
* :class:`Timeline_Segment` and :func:`normalize_segments` — the canonical
  Segment_List representation and its normalisation invariants (Req 14).

The module imports **stdlib only** at module scope, so it loads even when every
optional heavy dependency (ffmpeg, OpenCV, torch, whisper, ...) is absent
(Req 1.4). ``MediaInfo`` is referenced for typing only: it is imported under
:data:`typing.TYPE_CHECKING` because ``worker.ffmpeg_utils`` imports
``config.settings`` and shells out to the ffmpeg/ffprobe binaries, and
:meth:`Time_Base.from_media_info` only ever reads a ``fps`` attribute — so the
argument is accepted duck-typed at runtime.

Nothing in this module touches ffmpeg, OpenCV, the network, the clock, or the
filesystem.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from worker.ffmpeg_utils import MediaInfo

__all__ = [
    "DEFAULT_FPS",
    "DEFAULT_SAMPLE_RATE",
    "MIN_FPS",
    "MAX_FPS",
    "Rounding",
    "Time_Base",
    "Timeline_Segment",
    "normalize_segments",
    "parse_segments",
    "dump_segments",
    "total_duration",
    "invert_segments",
    "clip_bounds",
]

DEFAULT_FPS = 30.0
"""Documented fallback frame rate used when the probed fps is unusable (Req 13.3)."""

DEFAULT_SAMPLE_RATE = 48000
"""Documented default audio sample rate (``MediaInfo`` carries no sample rate)."""

MIN_FPS = 1.0
"""Lowest frame rate accepted from a probe before the fallback kicks in."""

MAX_FPS = 240.0
"""Highest frame rate accepted from a probe before the fallback kicks in."""

# Relative tolerance used when quantising seconds to frames/samples. A value
# derived from an exact frame/sample index (``index / rate``) multiplied back by
# the rate lands within a couple of ULPs of that integer; absorbing that error
# is what makes the frame and sample round-trips exact for *both* rounding modes
# (Reqs 13.5, 13.6). It is far larger than the float error (~1e-16 relative) and
# far smaller than any timing difference an engine can express.
_ROUND_TOLERANCE = 1e-9


class Rounding(str, Enum):
    """How seconds are quantised to frame/sample indices (Req 13.1)."""

    NEAREST = "nearest"
    FLOOR = "floor"


def _is_number(value: Any) -> bool:
    """True for a real numeric value that is not a bool."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _coerce_seconds(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None`` when it is not usable."""
    if not _is_number(value):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _coerce_fps(value: Any) -> tuple[float, bool]:
    """Return ``(fps, substituted)`` for a probed/serialised frame rate.

    ``DEFAULT_FPS`` is substituted (with ``substituted=True``) when the value is
    missing, non-numeric, zero, negative, non-finite (NaN/inf), or outside
    ``[MIN_FPS, MAX_FPS]`` (Req 13.3).
    """
    number = _coerce_seconds(value)
    if number is None or number <= 0.0 or number < MIN_FPS or number > MAX_FPS:
        return DEFAULT_FPS, True
    return number, False


def _coerce_sample_rate(value: Any) -> int:
    """Return a positive integer sample rate, falling back to the default."""
    if not _is_number(value):
        return DEFAULT_SAMPLE_RATE
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        return DEFAULT_SAMPLE_RATE
    return int(number)


def _coerce_rounding(value: Any) -> Rounding:
    """Return a :class:`Rounding` member, defaulting to ``NEAREST``."""
    if isinstance(value, Rounding):
        return value
    if isinstance(value, str):
        try:
            return Rounding(value.strip().lower())
        except ValueError:
            return Rounding.NEAREST
    return Rounding.NEAREST


def _quantize(raw: float, rounding: Any = Rounding.NEAREST) -> int:
    """Quantise ``raw`` (a fractional frame/sample position) to an integer index.

    Half-up rounding is used for ``NEAREST`` so the result is deterministic (no
    banker's rounding) and the quantisation error never exceeds half a unit.
    Regardless of the mode, a ``raw`` value that sits within
    :data:`_ROUND_TOLERANCE` of an integer snaps to that integer — this is what
    keeps ``seconds_to_frame(frame_to_seconds(f)) == f`` exact under ``FLOOR``,
    where a naive ``math.floor`` would return ``f - 1`` whenever ``f / fps * fps``
    lands a fraction of a ULP below ``f``.
    """
    if not math.isfinite(raw):
        raise ValueError(f"cannot quantise a non-finite position: {raw!r}")
    nearest = math.floor(raw + 0.5)
    if abs(raw - nearest) <= _ROUND_TOLERANCE * max(1.0, abs(nearest)):
        return int(nearest)
    if rounding == Rounding.FLOOR:
        return int(math.floor(raw))
    return int(nearest)


@dataclass(frozen=True)
class Time_Base:
    """Shared timing record for one clip (Reqs 13.1, 13.7).

    Constructed values are sanitised: an unusable ``fps`` (zero, negative,
    non-finite, or outside ``[MIN_FPS, MAX_FPS]``) is replaced by
    :data:`DEFAULT_FPS` and records the substitution in
    :attr:`fps_substituted`, so no caller can end up with a division-by-zero
    time base.
    """

    fps: float = DEFAULT_FPS
    sample_rate: int = DEFAULT_SAMPLE_RATE
    rounding: Rounding = Rounding.NEAREST
    fps_substituted: bool = False

    def __post_init__(self) -> None:
        fps, substituted = _coerce_fps(self.fps)
        object.__setattr__(self, "fps", fps)
        object.__setattr__(self, "sample_rate", _coerce_sample_rate(self.sample_rate))
        object.__setattr__(self, "rounding", _coerce_rounding(self.rounding))
        object.__setattr__(self, "fps_substituted", bool(self.fps_substituted) or substituted)

    # ------------------------------------------------------------------ build

    @classmethod
    def from_media_info(
        cls,
        info: MediaInfo,
        *,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        rounding: Rounding = Rounding.NEAREST,
    ) -> Time_Base:
        """Build from ``worker.ffmpeg_utils.probe`` output (Req 13.2).

        Only ``info.fps`` is read, so any object exposing that attribute works
        (``info`` may also be ``None``). A missing, zero, negative, non-finite or
        out-of-range fps substitutes :data:`DEFAULT_FPS` and sets
        :attr:`fps_substituted` (Req 13.3).

        Args:
            info: Probed media metadata (``MediaInfo``) or ``None``.
            sample_rate: Audio sample rate to record; defaults to
                :data:`DEFAULT_SAMPLE_RATE` since ``MediaInfo`` carries none.
            rounding: Rounding rule for seconds-to-index conversions.

        Returns:
            The sanitised :class:`Time_Base` for the clip.
        """
        probed = getattr(info, "fps", None)
        fps, substituted = _coerce_fps(probed)
        return cls(
            fps=fps,
            sample_rate=_coerce_sample_rate(sample_rate),
            rounding=_coerce_rounding(rounding),
            fps_substituted=substituted,
        )

    # ------------------------------------------------------------ conversions

    def frame_duration(self) -> float:
        """Seconds occupied by one frame (``1 / fps``)."""
        return 1.0 / self.fps

    def seconds_to_frame(self, seconds: float) -> int:
        """Frame index for ``seconds`` using the configured rounding (Reqs 13.4, 13.6)."""
        value = _coerce_seconds(seconds)
        if value is None:
            raise ValueError(f"seconds must be a finite number, got {seconds!r}")
        return _quantize(value * self.fps, self.rounding)

    def frame_to_seconds(self, frame: int) -> float:
        """Timestamp in seconds of frame ``frame`` (``frame / fps``) — Req 13.4."""
        value = _coerce_seconds(frame)
        if value is None:
            raise ValueError(f"frame must be a finite number, got {frame!r}")
        return value / self.fps

    def seconds_to_sample(self, seconds: float) -> int:
        """Audio sample index for ``seconds`` using the configured rounding (Req 13.4)."""
        value = _coerce_seconds(seconds)
        if value is None:
            raise ValueError(f"seconds must be a finite number, got {seconds!r}")
        return _quantize(value * self.sample_rate, self.rounding)

    def sample_to_seconds(self, sample: int) -> float:
        """Timestamp in seconds of audio sample ``sample`` (Req 13.4)."""
        value = _coerce_seconds(sample)
        if value is None:
            raise ValueError(f"sample must be a finite number, got {sample!r}")
        return value / self.sample_rate

    def snap(self, seconds: float) -> float:
        """Align ``seconds`` to the nearest frame boundary; idempotent (Reqs 15.3, 15.4).

        Always rounds to the *nearest* boundary regardless of :attr:`rounding`,
        so the result never drifts by more than half a frame. The returned value
        is ``frame / fps`` for an integer ``frame``, and re-snapping it yields the
        identical float, making the operation exactly idempotent.
        """
        value = _coerce_seconds(seconds)
        if value is None:
            raise ValueError(f"seconds must be a finite number, got {seconds!r}")
        frame = _quantize(value * self.fps, Rounding.NEAREST)
        return frame / self.fps

    # ---------------------------------------------------------- serialisation

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-encodable mapping."""
        return {
            "fps": float(self.fps),
            "sample_rate": int(self.sample_rate),
            "rounding": str(self.rounding.value),
            "fps_substituted": bool(self.fps_substituted),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Time_Base:
        """Rebuild from :meth:`to_dict` output, tolerating missing/hostile fields."""
        if not isinstance(data, Mapping):
            return cls()
        return cls(
            fps=data.get("fps", DEFAULT_FPS),
            sample_rate=data.get("sample_rate", DEFAULT_SAMPLE_RATE),
            rounding=data.get("rounding", Rounding.NEAREST),
            fps_substituted=bool(data.get("fps_substituted", False)),
        )


# ---------------------------------------------------------------------------
# Timeline segments and Segment_List normalisation (Reqs 14, 15.1, 15.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Timeline_Segment:
    """Half-open clip-relative interval in seconds with ``start <= end`` (Req 14.1)."""

    start: float
    end: float

    def __post_init__(self) -> None:
        # Normalise numeric bounds to float so serialisation and equality are
        # stable (``2`` and ``2.0`` must not produce two distinct segments).
        for name in ("start", "end"):
            value = getattr(self, name)
            if _is_number(value):
                object.__setattr__(self, name, float(value))

    @property
    def duration(self) -> float:
        """Length of the interval in seconds (``end - start``)."""
        return float(self.end) - float(self.start)

    def overlaps(self, other: Timeline_Segment) -> bool:
        """True when the two half-open intervals share any time.

        Touching intervals (``a.end == b.start``) do **not** overlap; they are
        still merged by :func:`normalize_segments` (Req 14.2).
        """
        if other is None:
            return False
        start = _coerce_seconds(getattr(other, "start", None))
        end = _coerce_seconds(getattr(other, "end", None))
        own_start = _coerce_seconds(self.start)
        own_end = _coerce_seconds(self.end)
        if start is None or end is None or own_start is None or own_end is None:
            return False
        return own_start < end and start < own_end

    def to_dict(self) -> dict[str, float]:
        """Serialise to a JSON-encodable ``{"start": ..., "end": ...}`` mapping (Req 14.6)."""
        return {"start": float(self.start), "end": float(self.end)}

    @classmethod
    def from_dict(cls, data: Any) -> Timeline_Segment | None:
        """Parse one record; returns ``None`` for malformed or inverted input (Req 14.7).

        Accepted inputs are a mapping carrying numeric ``start``/``end`` keys and
        an existing :class:`Timeline_Segment`. Everything else — wrong types,
        missing keys, strings, ``None``, ``NaN``/``inf`` bounds, nested
        structures, and inverted records with ``end < start`` — yields ``None``.
        """
        if isinstance(data, Timeline_Segment):
            raw_start: Any = data.start
            raw_end: Any = data.end
        elif isinstance(data, Mapping):
            if "start" not in data or "end" not in data:
                return None
            raw_start = data["start"]
            raw_end = data["end"]
        else:
            return None

        start = _coerce_seconds(raw_start)
        end = _coerce_seconds(raw_end)
        if start is None or end is None:
            return None
        if end < start:
            return None
        return cls(start=start, end=end)


def _iter_records(segments: Any) -> list[Any]:
    """Return ``segments`` as a list of candidate records (never raises)."""
    if segments is None:
        return []
    if isinstance(segments, (str, bytes, bytearray)):
        return []
    if isinstance(segments, (Timeline_Segment, Mapping)):
        return [segments]
    if isinstance(segments, Iterable):
        try:
            return list(segments)
        except TypeError:
            return []
    return []


def normalize_segments(
    segments: Iterable[Any],
    duration: float,
    *,
    time_base: Time_Base | None = None,
    min_duration: float = 0.0,
) -> list[Timeline_Segment]:
    """Return a canonical Segment_List (Req 14.2).

    Drops malformed/inverted/non-finite records (14.7), clamps to
    ``[0, duration]``, snaps bounds to frame boundaries when ``time_base`` is
    given (15.3), sorts by ``start``, drops zero-length and sub-``min_duration``
    segments, and merges overlapping *or* touching segments.

    The result is sorted, pairwise disjoint and non-touching, in bounds, totals at
    most ``duration`` (14.3, 14.5, 15.5), and normalising it again returns an
    identical list (14.4).

    Args:
        segments: Any iterable of records (mappings, ``Timeline_Segment``s, junk).
        duration: Clip duration in seconds; a non-finite or non-positive value
            yields an empty list.
        time_base: When given, bounds are snapped to its frame grid.
        min_duration: Minimum kept segment length in seconds.

    Returns:
        The canonical list of :class:`Timeline_Segment`s.
    """
    limit = _coerce_seconds(duration)
    if limit is None or limit <= 0.0:
        return []

    floor_length = _coerce_seconds(min_duration)
    if floor_length is None or floor_length < 0.0:
        floor_length = 0.0

    bounds: list[tuple[float, float]] = []
    for record in _iter_records(segments):
        segment = Timeline_Segment.from_dict(record)
        if segment is None:
            continue

        start = float(segment.start)
        end = float(segment.end)

        # Clamp, snap, clamp again. The leading clamp matters for idempotence:
        # an out-of-range bound must be pulled to ``duration`` *before* snapping,
        # otherwise the first pass emits the un-snapped ``duration`` and a second
        # pass would snap it down to the frame boundary below. Snapping a value
        # already inside ``[0, duration]`` can only leave the range at the top
        # (and only when ``duration`` sits at least half a frame above its
        # boundary), in which case the trailing clamp pins it to ``duration`` and
        # every further pass reproduces exactly that value.
        start = min(max(start, 0.0), limit)
        end = min(max(end, 0.0), limit)
        if time_base is not None:
            start = min(max(time_base.snap(start), 0.0), limit)
            end = min(max(time_base.snap(end), 0.0), limit)
        if end <= start:
            continue

        length = end - start
        if length <= 0.0:
            continue
        if floor_length > 0.0 and length < floor_length:
            continue

        bounds.append((start, end))

    bounds.sort(key=lambda pair: (pair[0], pair[1]))

    merged: list[tuple[float, float]] = []
    for start, end in bounds:
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            if end > previous_end:
                merged[-1] = (previous_start, end)
            continue
        merged.append((start, end))

    return [Timeline_Segment(start=start, end=end) for start, end in merged]


def parse_segments(raw: Any, duration: float) -> list[Timeline_Segment]:
    """Parse a serialised Segment_List, then normalise it (Reqs 14.6, 14.7).

    Accepts the :func:`dump_segments` form (a sequence of mappings), a single
    mapping, and a JSON string/bytes encoding either. Unparseable input yields an
    empty list.
    """
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if isinstance(decoded, (str, bytes, bytearray)):
            return []
        return normalize_segments(decoded, duration)
    return normalize_segments(raw, duration)


def dump_segments(segments: Sequence[Timeline_Segment]) -> list[dict[str, float]]:
    """Serialise a Segment_List to JSON-encodable mappings (Req 14.6)."""
    dumped: list[dict[str, float]] = []
    for record in _iter_records(segments):
        segment = Timeline_Segment.from_dict(record)
        if segment is None:
            continue
        dumped.append(segment.to_dict())
    return dumped


def total_duration(segments: Sequence[Timeline_Segment]) -> float:
    """Summed length of ``segments`` in seconds, ignoring malformed records (Req 14.5)."""
    lengths: list[float] = []
    for record in _iter_records(segments):
        segment = Timeline_Segment.from_dict(record)
        if segment is None:
            continue
        lengths.append(max(0.0, segment.duration))
    if not lengths:
        return 0.0
    return math.fsum(lengths)


def invert_segments(
    segments: Sequence[Timeline_Segment], duration: float
) -> list[Timeline_Segment]:
    """Complement of a Segment_List within ``[0, duration]``.

    The input is normalised first, so the result is itself a canonical
    Segment_List: sorted, disjoint, non-touching, in bounds.
    """
    limit = _coerce_seconds(duration)
    if limit is None or limit <= 0.0:
        return []

    gaps: list[Timeline_Segment] = []
    cursor = 0.0
    for segment in normalize_segments(segments, limit):
        if segment.start > cursor:
            gaps.append(Timeline_Segment(start=cursor, end=segment.start))
        if segment.end > cursor:
            cursor = segment.end
    if cursor < limit:
        gaps.append(Timeline_Segment(start=cursor, end=limit))
    return gaps


def clip_bounds(words: Sequence[Any], duration: float) -> tuple[float, float]:
    """Return the clip-relative bounds ``(0.0, duration)`` (Req 15.1).

    Every engine timestamp is clip-relative, so the bounds depend only on the
    clip duration; ``words`` (the clip's Word_Timeline) is accepted so callers can
    pass their context through unchanged, and does not influence the result. A
    non-finite or negative ``duration`` collapses to ``(0.0, 0.0)``.
    """
    limit = _coerce_seconds(duration)
    if limit is None or limit <= 0.0:
        return (0.0, 0.0)
    return (0.0, limit)
