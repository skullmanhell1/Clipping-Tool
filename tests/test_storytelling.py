"""Focused tests for the pure storytelling planner."""

from __future__ import annotations

import json

from worker.storytelling import (
    detect_cliffhanger,
    plan_pattern_interrupters,
    plan_storytelling,
)


def _words(*items: tuple[float, float, str]) -> list[dict[str, object]]:
    return [{"start": start, "end": end, "text": text} for start, end, text in items]


def test_full_plan_has_contextual_cards_hooks_and_end_card() -> None:
    words = _words(
        (0.0, 0.4, "Why"),
        (0.4, 0.8, "did"),
        (0.8, 1.3, "growth"),
        (1.3, 1.8, "actually"),
        (2.0, 2.5, "happen?"),
        (3.0, 3.6, "The"),
        (3.6, 4.0, "chart"),
        (10.0, 10.5, "Follow"),
        (10.5, 11.0, "for"),
        (11.0, 11.6, "Part"),
        (11.6, 12.0, "2"),
    )
    entities = [
        {"text": "Atomic Habits", "type": "book", "start": 3.0, "end": 4.0},
        {"text": "@creator's tweet", "type": "tweet", "start": 4.0, "end": 5.0},
    ]
    scene_metadata = {
        "scenes": [{"start": 0, "end": 5, "type": "talking_head"}],
        "shot_boundaries": [4.2, 9.0],
    }

    plan = plan_storytelling(words, 12.0, entities=entities, scene_metadata=scene_metadata)
    result = plan.to_dict()

    assert [card["entity_type"] for card in result["entity_proof_cards"]] == ["book", "tweet"]
    assert all(card["is_suggestion"] for card in result["entity_proof_cards"])
    assert all(card["asset_required"] for card in result["entity_proof_cards"])
    assert all(
        card["asset_status"] == "operator_provided_or_licensed_media_required"
        for card in result["entity_proof_cards"]
    )
    assert {item["concept"] for item in result["concept_overlays"]} >= {"growth", "plot_twist"}
    assert any(item["overlay_kind"] == "meme" for item in result["concept_overlays"])
    assert result["interactive_overlays"][0]["overlay_kind"] == "question"
    assert result["curiosity_loop"]["detected"] is True
    assert result["end_card"] == {
        "detected": True,
        "kind": "part_2",
        "start": 10.0,
        "end": 12.0,
        "text": "PART 2",
        "reason": "detected continuation phrase: part 2",
        "is_suggestion": True,
        "asset_status": "suggestion_only",
        "asset_required": False,
    }
    assert result["entity_proof_cards"][0]["scene_context"] == "talking_head"


def test_pattern_interrupters_are_bounded_and_use_scene_cuts() -> None:
    first = plan_pattern_interrupters(
        13.0,
        {"shot_boundaries": [4.2, 9.1]},
    )
    second = plan_pattern_interrupters(
        13.0,
        {"shot_boundaries": [4.2, 9.1]},
    )

    assert first == second
    assert [item.start for item in first] == [4.2, 9.1]
    assert all(0.0 <= item.start < item.end <= 13.0 for item in first)
    assert all(4.0 <= right.start - left.start <= 6.0 for left, right in zip(first, first[1:]))
    assert plan_pattern_interrupters(4.0) == ()


def test_pattern_interval_is_clamped_to_the_attention_reset_window() -> None:
    result = plan_pattern_interrupters(20.0, interval_seconds=6)
    assert [item.start for item in result] == [4.0, 10.0, 16.0]


def test_empty_and_malformed_input_is_safe_and_json_serializable() -> None:
    plan = plan_storytelling(
        [None, {"start": "nan", "end": "inf", "text": ""}, object()],
        "not-a-duration",
        entities=[None, {"text": None}, {"type": []}],
        scene_metadata={"scenes": [None, {"start": "bad"}], "cuts": ["nan", object()]},
    )

    assert plan.to_dict() == {
        "duration": 0.0,
        "entity_proof_cards": [],
        "concept_overlays": [],
        "meme_overlays": [],
        "pattern_interrupters": [],
        "interactive_overlays": [],
        "curiosity_loop": {
            "detected": False,
            "start": 0.0,
            "end": 0.0,
            "opener": "",
            "promise": "",
            "reason": "",
            "is_suggestion": True,
            "asset_status": "suggestion_only",
            "asset_required": False,
        },
        "end_card": {
            "detected": False,
            "kind": "none",
            "start": 0.0,
            "end": 0.0,
            "text": "",
            "reason": "",
            "is_suggestion": True,
            "asset_status": "suggestion_only",
            "asset_required": False,
        },
    }
    json.dumps(plan.to_dict())


def test_cliffhanger_is_absent_when_continuation_is_not_at_the_end() -> None:
    words = _words(
        (0.0, 0.5, "Part"),
        (0.5, 1.0, "2"),
        (11.0, 11.5, "The"),
        (11.5, 12.0, "answer"),
    )
    assert detect_cliffhanger(words, 12.0).detected is False
