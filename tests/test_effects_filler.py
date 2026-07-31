"""Tests for filler-word/pause removal planning, rebasing, and cutting."""

from __future__ import annotations

from tests.conftest import FakeWord, probe_duration, requires_ffmpeg
from worker.effects import filler as fl


def test_plan_removes_filler_and_long_pause():
    words = [
        FakeWord(0.2, 0.6, "Hello"),
        FakeWord(1.0, 1.3, "um"),  # filler
        FakeWord(1.4, 1.8, "world"),
        FakeWord(2.0, 2.4, "today"),
        FakeWord(6.0, 6.5, "again"),  # preceded by a long (3.6s) pause
    ]
    plan = fl.plan_keep_intervals(words, duration=7.0)
    assert plan.changed
    assert plan.removed_fillers == 1
    assert plan.removed_seconds > 3.0
    # The dead-air gap (2.4 -> 6.0) must not be inside any keep segment.
    for k in plan.keeps:
        assert not (k.start > 3.0 and k.end < 5.5)


def test_plan_noop_when_tight():
    words = [FakeWord(0.0, 0.5, "hi"), FakeWord(0.6, 1.0, "there")]
    plan = fl.plan_keep_intervals(words, duration=1.1)
    assert not plan.changed
    assert len(plan.keeps) == 1


def test_rebase_words_drops_filler_and_shifts():
    words = [
        FakeWord(0.2, 0.6, "Hello"),
        FakeWord(1.0, 1.3, "um"),
        FakeWord(1.4, 1.8, "world"),
    ]
    plan = fl.plan_keep_intervals(words, duration=2.0)
    rebased = fl.rebase_words(words, plan.keeps)
    texts = [w.text for w in rebased]
    assert "um" not in texts
    assert "Hello" in texts and "world" in texts
    # Timeline is monotonic and starts at (or near) zero.
    starts = [w.start for w in rebased]
    assert starts == sorted(starts)
    assert starts[0] < 0.5


@requires_ffmpeg
def test_apply_keep_intervals_shortens_clip(make_video, tmp_path):
    src = make_video("filler_src.mp4", duration=8.0, w=640, h=360)
    keeps = [fl.Interval(0.0, 0.72), fl.Interval(1.32, 2.52), fl.Interval(5.88, 8.0)]
    dest = tmp_path / "tightened.mp4"
    fl.apply_keep_intervals(src, keeps, dest)
    assert dest.exists()
    total = sum(k.duration for k in keeps)
    assert abs(probe_duration(dest) - total) < 0.35
