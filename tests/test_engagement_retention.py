"""Pure tests for retention-first engagement scoring and edit planning."""

from __future__ import annotations

from types import SimpleNamespace

from worker.engagement import (
    ReactionEvent,
    parse_reactions,
    plan_edit,
    rerank,
    score_features,
)
from worker.models import ClipResult


def _word(start: float, end: float, text: str, probability: float = 1.0):
    return SimpleNamespace(start=start, end=end, text=text, probability=probability)


def _words(*rows):
    return [_word(*row) for row in rows]


def test_reactions_parse_sort_and_reject_invalid_values():
    events = parse_reactions(
        '{"reactions": [{"time": 4, "count": 2}, {"timestamp": 1, "weight": 5}, '
        '{"time": -1}, {"time": "nan"}, {"time": 2, "count": 0}]}'
    )
    assert events == (ReactionEvent(1.0, 5.0), ReactionEvent(4.0, 2.0))


def test_reactions_are_optional_and_malformed_json_is_safe():
    assert parse_reactions("") == ()
    assert parse_reactions("not-json") == ()
    assert parse_reactions({"reactions": []}) == ()


def test_score_features_treats_missing_optional_signals_as_neutral():
    score = score_features({"hook_score": 0.8, "standalone_score": 0.8})
    assert 0.5 < score <= 1.0
    assert score_features({}) == 0.5


def test_balanced_plan_adds_hook_zoom_and_caption_cues():
    words = _words(
        (0.1, 0.5, "WHY"),
        (0.55, 1.0, "this"),
        (1.05, 1.6, "actually"),
        (2.1, 2.5, "works"),
    )
    plan = plan_edit(
        words,
        {
            "hook_score": 0.9,
            "energy_onsets": [1.2],
            "pitch_events": [1.25],
        },
        6.0,
        mode="balanced",
    )
    assert plan.mode == "balanced"
    assert plan.hook_window_s == 3.0
    assert plan.zoom_beats == (0.35, 1.2)
    assert 0 in plan.emphasis_indices
    assert plan.pacing_cuts == ()
    assert "front_loaded_hook" in plan.reasons


def test_aggressive_plan_cuts_long_gaps_without_splitting_words():
    words = _words(
        (0.1, 0.4, "The"),
        (0.45, 0.9, "answer"),
        (2.2, 2.6, "surprised"),
        (2.7, 3.2, "everyone"),
    )
    plan = plan_edit(
        words,
        {"hook_score": 0.7, "energy_onsets": [], "pitch_events": []},
        5.0,
        mode="aggressive",
    )
    assert plan.pacing_cuts == ((0.9, 2.2),)
    assert plan.sfx_times == ()


def test_reactions_produce_one_bounded_visual_and_sfx_cue():
    words = _words((0.1, 0.5, "wait"), (0.6, 1.0, "for"), (1.1, 1.5, "this"))
    plan = plan_edit(
        words,
        {"hook_score": 0.4, "energy_onsets": [1.01, 1.05], "pitch_events": []},
        4.0,
        mode="aggressive",
        reactions=(ReactionEvent(2.0, 20.0), ReactionEvent(2.01, 10.0)),
    )
    assert plan.zoom_beats == (1.01, 2.0)
    assert plan.sfx_times == (1.01, 2.0)
    assert all(0 <= value < 4.0 for value in plan.zoom_beats)


def test_protected_reaction_gap_is_not_cut():
    words = _words((0.1, 0.4, "before"), (2.0, 2.4, "reaction"))
    plan = plan_edit(
        words,
        {"hook_score": 0.2},
        3.0,
        mode="aggressive",
        reactions=(ReactionEvent(1.0, 8.0),),
    )
    assert plan.pacing_cuts == ()


def test_off_mode_is_a_noop_plan():
    plan = plan_edit(
        _words((0.0, 0.5, "anything")),
        {"hook_score": 1.0, "energy_onsets": [0.2]},
        2.0,
        mode="off",
    )
    assert plan.to_dict() == {
        "mode": "off",
        "hook_score": 0.0,
        "hook_window_s": 0.0,
        "emphasis_indices": [],
        "zoom_beats": [],
        "sfx_times": [],
        "pacing_cuts": [],
        "reasons": [],
    }


def test_retention_rerank_is_stable_and_uses_engagement_evidence():
    first = SimpleNamespace(start=0.0, score=80.0, features={"engagement_score": 0.2})
    second = SimpleNamespace(start=10.0, score=70.0, features={"engagement_score": 0.95})
    ranked = rerank([first, second], "aggressive")
    assert ranked == [second, first]
    assert second.score > first.score


def test_clip_result_persists_retention_fields():
    original = ClipResult(
        id="clip-1",
        filename="clip.mp4",
        start=1.0,
        end=9.0,
        duration=8.0,
        engagement_score=87.5,
        retention_plan={"mode": "balanced", "zoom_beats": [0.35]},
    )
    restored = ClipResult.from_dict(original.to_dict())
    assert restored.engagement_score == 87.5
    assert restored.retention_plan == {"mode": "balanced", "zoom_beats": [0.35]}
