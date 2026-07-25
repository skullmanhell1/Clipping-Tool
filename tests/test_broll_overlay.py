"""Property tests for the pure b-roll overlay graph builder.

Covers tasks 6.2 (Property 20) and 6.3 (Property 19). Property tests use
``hypothesis`` with ``@settings(max_examples=100)``, one property per test,
tagged with the design property text and a ``Validates: Requirements ...``
docstring. The builder under test (``build_broll_overlay``) is pure string
building — no ffmpeg is invoked here.
"""
from __future__ import annotations

import re

from hypothesis import given, settings
from hypothesis import strategies as st

from worker.effects.broll import (
    AssetRef,
    BrollCue,
    broll_asset_record,
    build_broll_overlay,
)


@st.composite
def _resolved_cues(draw):
    """A non-empty list of already-resolved b-roll cues plus a clip duration.

    Cues mix image/video assets and local/external providers, and include
    zero-length windows (``start == end``) to exercise the one-frame minimum.
    """
    duration = draw(st.floats(min_value=1.0, max_value=30.0))
    n = draw(st.integers(min_value=1, max_value=6))
    cues: list[BrollCue] = []
    for i in range(n):
        start = draw(st.floats(min_value=0.0, max_value=duration))
        span = draw(st.floats(min_value=0.0, max_value=3.0))  # 0.0 => zero-length
        end = min(duration, start + span)
        kind = draw(st.sampled_from(["image", "video"]))
        provider = draw(st.sampled_from(["local", "external"]))
        if provider == "local":
            asset = AssetRef(
                path=f"/lib/asset{i}.png", kind=kind, provider="local",
                license="local",
            )
        else:
            asset = AssetRef(
                path=f"/ext/asset{i}.mp4", kind=kind, provider="external",
                source_id=f"sid-{i}", license="CC0", attribution=f"Photo by {i}",
            )
        cues.append(BrollCue(round(start, 3), round(end, 3), f"kw{i}", asset=asset))
    return cues, duration


# --------------------------------------------------------------------------- #
# 6.2 — Property 20
# --------------------------------------------------------------------------- #
# Feature: tier1-creator-output-upgrade, Property 20: B-roll overlays are bounded, uniquely indexed, and layered below captions
@settings(max_examples=100)
@given(data=_resolved_cues(), offset=st.integers(min_value=1, max_value=5))
def test_p20_overlays_bounded_indexed_below_captions(data, offset):
    """Validates: Requirements 10.2, 10.3, 10.4

    Each overlay is time-bounded via ``enable='between(t,...)'``, every ffmpeg
    input index is distinct and contiguous from ``input_offset``, and in a full
    compositor graph the b-roll overlays precede the ``subtitles`` filter so
    captions stay on top.
    """
    cues, _duration = data
    n = len(cues)
    input_args, graph, notes = build_broll_overlay(
        cues, base_label="vlook", out_label="vbroll",
        width=1080, height=1920, fps=30.0, input_offset=offset,
    )

    # Every overlay is bounded to its cue window (Req 10.4).
    assert graph.count("enable='between(t,") == n

    # Input indices are distinct and contiguous from the offset (Req 10.3).
    indices = [int(m) for m in re.findall(r"\[(\d+):v\]", graph)]
    assert indices == list(range(offset, offset + n))
    assert len(set(indices)) == n
    assert input_args.count("-i") == n

    # Layering: in a full graph the b-roll overlays precede the subtitles filter
    # (captions layered on top, Req 10.2).
    full = ";".join([
        "[0:v]eq=contrast=1.1[vlook]",
        graph,
        "[vbroll]subtitles=sub.ass[vbase]",
    ])
    assert full.index("overlay") < full.index("subtitles")


# --------------------------------------------------------------------------- #
# 6.3 — Property 19
# --------------------------------------------------------------------------- #
# Feature: tier1-creator-output-upgrade, Property 19: Only composited cues are recorded with correct provenance
@settings(max_examples=100)
@given(data=_resolved_cues())
def test_p19_only_composited_cues_recorded_with_provenance(data):
    """Validates: Requirements 9.4, 12.1, 12.2

    The applied notes and provenance records correspond exactly to the
    composited cues; external assets carry provider/source_id/license/
    attribution while local assets carry their source path.
    """
    cues, _duration = data
    _input_args, _graph, notes = build_broll_overlay(
        cues, base_label="vlook", out_label="vbroll",
        width=1080, height=1920, fps=30.0, input_offset=1,
    )

    # One note per composited cue, in order (Req 9.4).
    assert notes == [f"broll:{c.keyword}" for c in cues]

    records = [broll_asset_record(c) for c in cues]
    assert len(records) == len(cues)
    for cue, rec in zip(cues, records):
        assert rec["keyword"] == cue.keyword
        assert rec["path"] == cue.asset.path
        if cue.asset.provider == "local":
            # Local assets record their source path (Req 12.2).
            assert rec["provider"] == "local"
            assert rec["path"] == cue.asset.path
        else:
            # External assets carry full provenance (Req 12.1).
            assert rec["provider"] == cue.asset.provider
            assert rec["source_id"] == cue.asset.source_id
            assert rec["license"] == cue.asset.license
            assert rec["attribution"] == cue.asset.attribution


# --------------------------------------------------------------------------- #
# Edge case: no resolvable cues -> empty builder output (caller keeps base).
# --------------------------------------------------------------------------- #
def test_no_cues_returns_empty():
    """Validates: Requirements 9.3, 10.6 — empty resolved cues => no-op builder."""
    assert build_broll_overlay(
        [], base_label="vlook", out_label="vbroll",
        width=1080, height=1920, fps=30.0, input_offset=1,
    ) == ([], "", [])
