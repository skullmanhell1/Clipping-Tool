"""Time-base and Segment_List property module for the av-engines-foundation spec
(``worker/engines/timebase.py``).

Covers the design's numbered properties for the pure timing primitives:

* **P21** — ``Time_Base`` conversions round-trip and the fps fallback is recorded
  (task 2.4).
* **P22** — frame quantisation is bounded and snapping is idempotent (task 2.5).
* **P24** — segment normalisation yields a canonical, in-bounds Segment_List
  (task 2.6).
* **P25** — segment normalisation is idempotent (task 2.7).
* **P26** — Segment_List serialisation round-trips (task 2.8).

plus the ``from_media_info`` / defaults / ``to_dict``-``from_dict`` unit tests
(task 2.9, deliberately NOT numbered properties).

Generators are imported from the shared ``tests/strategies.py`` module — never
redefined here — so the sibling engine specs exercise the same input space. The
clip duration used by the timestamp properties is the same
``DEFAULT_SEGMENT_DURATION`` that ``st_segment_records`` generates against, so
every ``duration`` handed to ``normalize_segments`` matches the generator's
``duration=`` argument.

Everything here is pure and offline: no ffmpeg, no probe, no filesystem.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from tests.strategies import (
    DEFAULT_SEGMENT_DURATION,
    st_invalid_fps,
    st_segment_records,
    st_time_base,
)
from worker.engines.timebase import (
    DEFAULT_FPS,
    DEFAULT_SAMPLE_RATE,
    MAX_FPS,
    MIN_FPS,
    Rounding,
    Time_Base,
    Timeline_Segment,
    dump_segments,
    normalize_segments,
    parse_segments,
    total_duration,
)

#: Clip duration the timestamp properties draw ``t`` from. Identical to the
#: ``duration=`` default of ``st_segment_records`` so the two families of
#: properties agree on what "in-clip" means.
CLIP_DURATION = DEFAULT_SEGMENT_DURATION

#: Absolute slack allowed where IEEE-754 representation, not the algorithm,
#: decides the last bits — the design states the frame-multiple check holds
#: "within float tolerance". It is orders of magnitude below one frame at
#: ``MAX_FPS`` (1/240 s ~= 4.2e-3 s), so it cannot mask a real quantisation bug.
FLOAT_TOL = 1e-9


@dataclass(frozen=True)
class _FakeMediaInfo:
    """Hand-built stand-in for ``worker.ffmpeg_utils.MediaInfo`` (same fields).

    Built locally so this module stays stdlib-only: importing
    ``worker.ffmpeg_utils`` would drag in ``config.settings`` and the ffmpeg
    binaries, and ``Time_Base.from_media_info`` only ever reads ``fps``.
    """

    duration: float = CLIP_DURATION
    width: int = 1080
    height: int = 1920
    fps: Any = DEFAULT_FPS
    has_audio: bool = True


# --------------------------------------------------------------------------- #
# Reference normalisation, used only by P24                                     #
# --------------------------------------------------------------------------- #
def _reference_valid_interval(record: Any, duration: float) -> tuple[float, float] | None:
    """Return the clamped ``(start, end)`` of a *valid* record, else ``None``.

    Independent restatement of the acceptance criteria (Reqs 14.1, 14.3, 14.7):
    a record is valid when it is a mapping (or ``Timeline_Segment``) carrying
    real, finite, non-boolean numeric ``start``/``end`` with ``end >= start``.
    Everything else is malformed and must be discarded.
    """
    if isinstance(record, Timeline_Segment):
        raw_start: Any = record.start
        raw_end: Any = record.end
    elif isinstance(record, Mapping):
        if "start" not in record or "end" not in record:
            return None
        raw_start = record["start"]
        raw_end = record["end"]
    else:
        return None

    for raw in (raw_start, raw_end):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        if not math.isfinite(float(raw)):
            return None

    start = float(raw_start)
    end = float(raw_end)
    if end < start:
        return None

    start = min(max(start, 0.0), duration)
    end = min(max(end, 0.0), duration)
    if end <= start:  # zero-length after clamping: not a segment (Req 14.3)
        return None
    return (start, end)


def _reference_normalize(records: Any, duration: float) -> list[tuple[float, float]]:
    """Sort + merge the valid clamped intervals, overlapping *or* touching (Req 14.2)."""
    intervals = []
    for record in records:
        interval = _reference_valid_interval(record, duration)
        if interval is not None:
            intervals.append(interval)
    intervals.sort()

    merged: list[tuple[float, float]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            continue
        merged.append((start, end))
    return merged


# --------------------------------------------------------------------------- #
# Property 21 (task 2.4)                                                        #
# --------------------------------------------------------------------------- #
# Feature: av-engines-foundation, Property 21: Time_Base conversions round-trip and
# the fps fallback is recorded — *For any* fps in `[MIN_FPS, MAX_FPS]` and *for any*
# frame index within the clip, `seconds_to_frame(frame_to_seconds(f)) == f`, and
# likewise for samples; *for any* non-positive, non-finite, or out-of-range probed
# fps, `from_media_info` yields `DEFAULT_FPS` with `fps_substituted` true and the
# Engine_Context notes contain `fps_fallback:<value>`.
# (The Engine_Context clause is out of scope for task 2.4: `worker/engines/base.py`
# and `Engine_Context` do not exist until epic 3, where P21's note check lands.)
@settings(max_examples=100, deadline=None)
@given(time_base=st_time_base(), invalid_fps=st_invalid_fps(), data=st.data())
def test_p21_timebase_conversions_round_trip_and_fps_fallback(time_base, invalid_fps, data):
    """Validates: Requirements 13.1, 13.3, 13.4, 13.5

    The generated ``Time_Base`` always carries an fps inside
    ``[MIN_FPS, MAX_FPS]``; frame and sample conversions round-trip exactly for
    every in-clip index under both rounding modes; and every unusable probed fps
    (missing, zero, negative, NaN/inf, out of range) substitutes ``DEFAULT_FPS``
    and records ``fps_substituted=True``.
    """
    # The valid generator never leaves the accepted band.
    assert MIN_FPS <= time_base.fps <= MAX_FPS

    # --- frame round-trip (Req 13.4, 13.5) --------------------------------
    max_frame = int(math.floor(CLIP_DURATION * time_base.fps))
    frame = data.draw(st.integers(min_value=0, max_value=max_frame), label="frame")
    assert time_base.seconds_to_frame(time_base.frame_to_seconds(frame)) == frame

    # --- sample round-trip (Req 13.4, 13.5) -------------------------------
    max_sample = int(math.floor(CLIP_DURATION * time_base.sample_rate))
    sample = data.draw(st.integers(min_value=0, max_value=max_sample), label="sample")
    assert time_base.seconds_to_sample(time_base.sample_to_seconds(sample)) == sample

    # --- fps fallback is applied and recorded (Reqs 13.1, 13.3) -----------
    from_probe = Time_Base.from_media_info(_FakeMediaInfo(fps=invalid_fps))
    assert from_probe.fps == DEFAULT_FPS
    assert from_probe.fps_substituted is True

    # Direct construction sanitises identically, so no caller can hold an
    # unusable time base.
    constructed = Time_Base(fps=invalid_fps)
    assert constructed.fps == DEFAULT_FPS
    assert constructed.fps_substituted is True


# --------------------------------------------------------------------------- #
# Property 22 (task 2.5)                                                        #
# --------------------------------------------------------------------------- #
# Feature: av-engines-foundation, Property 22: Frame quantisation is bounded and
# snapping is idempotent — *For any* timestamp `t` in `[0, duration]`,
# `abs(frame_to_seconds(seconds_to_frame(t)) - t) < 1/fps`, `snap(t)` is an exact
# multiple of the frame duration within float tolerance with
# `abs(snap(t) - t) <= 1/(2*fps)`, and `snap(snap(t)) == snap(t)`.
@settings(max_examples=100, deadline=None)
@given(
    time_base=st_time_base(),
    t=st.floats(min_value=0.0, max_value=CLIP_DURATION, allow_nan=False, allow_infinity=False),
)
def test_p22_frame_quantisation_bounded_and_snap_idempotent(time_base, t):
    """Validates: Requirements 13.6, 15.3, 15.4

    Quantising a timestamp to a frame and back never moves it by a whole frame;
    ``snap`` lands on an exact frame boundary no further than half a frame away;
    and re-snapping a snapped value is a no-op (exact float equality).
    """
    frame_duration = 1.0 / time_base.fps

    # --- bounded quantisation error (Req 13.6) ----------------------------
    quantised = time_base.frame_to_seconds(time_base.seconds_to_frame(t))
    assert abs(quantised - t) < frame_duration

    # --- snap lands on an exact frame boundary (Req 15.3) -----------------
    snapped = time_base.snap(t)
    frames = snapped * time_base.fps
    assert abs(frames - round(frames)) <= FLOAT_TOL * max(1.0, abs(round(frames)))

    # --- snap moves by at most half a frame (Req 15.3) --------------------
    assert abs(snapped - t) <= frame_duration / 2.0 + FLOAT_TOL

    # --- snap is idempotent (Req 15.4) ------------------------------------
    assert time_base.snap(snapped) == snapped


# --------------------------------------------------------------------------- #
# Property 24 (task 2.6)                                                        #
# --------------------------------------------------------------------------- #
# Feature: av-engines-foundation, Property 24: Segment normalisation yields a
# canonical, in-bounds Segment_List — *For any* list of segment records (valid,
# malformed, inverted, NaN, out-of-range) and *for any* clip duration `D`, the
# normalised output is sorted by `start`, pairwise disjoint and non-touching, has
# every bound within `[0, D]` with `start <= end` and `duration > 0`, totals at most
# `D`, and contains exactly the normalised valid records (malformed ones discarded,
# the rest retained).
@settings(max_examples=100, deadline=None)
@given(records=st_segment_records(duration=CLIP_DURATION))
def test_p24_normalisation_yields_canonical_in_bounds_segment_list(records):
    """Validates: Requirements 14.1, 14.2, 14.3, 14.5, 14.7, 15.1, 15.5

    ``normalize_segments`` is checked against an independent restatement of the
    acceptance criteria: the same clip duration the generator produced records
    against is passed through, malformed records are discarded, and the surviving
    intervals are clamped, sorted and merged.
    """
    duration = CLIP_DURATION
    segments = normalize_segments(records, duration)

    # --- sorted by start (Req 14.2) ---------------------------------------
    starts = [segment.start for segment in segments]
    assert starts == sorted(starts)

    # --- in bounds, ordered, non-degenerate (Reqs 14.1, 14.3, 15.1) -------
    for segment in segments:
        assert 0.0 <= segment.start <= duration
        assert 0.0 <= segment.end <= duration
        assert segment.start <= segment.end
        assert segment.duration > 0.0

    # --- pairwise disjoint AND non-touching (Req 14.2) --------------------
    for previous, current in zip(segments, segments[1:], strict=False):
        assert previous.end < current.start
        assert not previous.overlaps(current)

    # --- total at most D (Reqs 14.5, 15.5) --------------------------------
    assert total_duration(segments) <= duration + FLOAT_TOL

    # --- exactly the normalised valid records (Reqs 14.7, 15.5) -----------
    expected = _reference_normalize(records, duration)
    assert [(segment.start, segment.end) for segment in segments] == expected


# --------------------------------------------------------------------------- #
# Property 25 (task 2.7)                                                        #
# --------------------------------------------------------------------------- #
# Feature: av-engines-foundation, Property 25: Segment normalisation is idempotent —
# *For any* list of segment records and any duration,
# `normalize_segments(normalize_segments(x, D), D) == normalize_segments(x, D)`.
@settings(max_examples=100, deadline=None)
@given(records=st_segment_records(duration=CLIP_DURATION))
def test_p25_segment_normalisation_is_idempotent(records):
    """Validates: Requirements 14.4

    Normalising an already-normalised Segment_List returns an identical list, so
    downstream engines can re-normalise defensively without drift.
    """
    duration = CLIP_DURATION
    once = normalize_segments(records, duration)
    twice = normalize_segments(once, duration)
    assert twice == once


# --------------------------------------------------------------------------- #
# Property 26 (task 2.8)                                                        #
# --------------------------------------------------------------------------- #
# Feature: av-engines-foundation, Property 26: Segment_List serialisation
# round-trips — *For any* normalised Segment_List `s` and duration `D`,
# `parse_segments(dump_segments(s), D) == s`, and the dumped form is JSON-encodable.
@settings(max_examples=100, deadline=None)
@given(records=st_segment_records(duration=CLIP_DURATION))
def test_p26_segment_list_serialisation_round_trips(records):
    """Validates: Requirements 14.6

    A normalised Segment_List survives ``dump_segments`` → ``parse_segments``
    unchanged, and the dumped form is genuinely JSON-encodable (asserted through
    ``json.dumps``).
    """
    duration = CLIP_DURATION
    segments = normalize_segments(records, duration)

    dumped = dump_segments(segments)
    json.dumps(dumped)  # must not raise: the wire form is JSON-encodable

    assert parse_segments(dumped, duration) == segments
    # The JSON text form parses back to the same Segment_List too.
    assert parse_segments(json.dumps(dumped), duration) == segments


# --------------------------------------------------------------------------- #
# Unit tests (task 2.9 — NOT numbered design properties)                        #
# --------------------------------------------------------------------------- #
def test_from_media_info_reads_probed_fps():
    """Validates: Requirements 13.1, 13.2 — ``from_media_info`` reads ``MediaInfo.fps``.

    A hand-built ``MediaInfo``-shaped record with a usable fps is carried
    through verbatim, with no substitution recorded, and the sample rate and
    rounding overrides are honoured.
    """
    info = _FakeMediaInfo(duration=12.5, width=1920, height=1080, fps=23.976)
    time_base = Time_Base.from_media_info(info)
    assert time_base.fps == 23.976
    assert time_base.fps_substituted is False
    assert time_base.sample_rate == DEFAULT_SAMPLE_RATE
    assert time_base.rounding is Rounding.NEAREST

    overridden = Time_Base.from_media_info(
        _FakeMediaInfo(fps=60.0), sample_rate=44100, rounding=Rounding.FLOOR
    )
    assert overridden.fps == 60.0
    assert overridden.sample_rate == 44100
    assert overridden.rounding is Rounding.FLOOR
    assert overridden.fps_substituted is False


def test_bare_instance_uses_documented_defaults():
    """Validates: Requirements 13.1 — documented defaults on a bare ``Time_Base``."""
    time_base = Time_Base()
    assert time_base.fps == DEFAULT_FPS
    assert time_base.sample_rate == DEFAULT_SAMPLE_RATE
    assert time_base.rounding is Rounding.NEAREST
    assert time_base.fps_substituted is False
    # The documented fallback constants themselves sit inside the accepted band.
    assert MIN_FPS <= DEFAULT_FPS <= MAX_FPS


def test_to_dict_from_dict_round_trip():
    """Validates: Requirements 13.1, 13.2 — ``to_dict``/``from_dict`` round-trip."""
    original = Time_Base(
        fps=59.94, sample_rate=44100, rounding=Rounding.FLOOR, fps_substituted=True
    )
    dumped = original.to_dict()
    json.dumps(dumped)  # the record is JSON-encodable
    assert Time_Base.from_dict(dumped) == original

    # A default instance round-trips too, and an empty mapping yields defaults.
    assert Time_Base.from_dict(Time_Base().to_dict()) == Time_Base()
    assert Time_Base.from_dict({}) == Time_Base()
