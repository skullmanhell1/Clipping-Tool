"""Offline tests for transcript-timed background-music ducking."""

from __future__ import annotations

from types import SimpleNamespace

from worker.effects import audio


def test_speech_intervals_pad_and_merge_nearby_words():
    words = [
        SimpleNamespace(start=1.0, end=1.2),
        SimpleNamespace(start=1.28, end=1.5),
        SimpleNamespace(start=2.0, end=2.1),
    ]

    assert audio.speech_intervals(words, 3.0, padding=0.05, merge_gap=0.04) == [
        (0.95, 1.55),
        (1.95, 2.15),
    ]


def test_speech_intervals_clip_boundary_padding_to_duration():
    words = [
        SimpleNamespace(start=0.02, end=0.12),
        SimpleNamespace(start=1.8, end=2.0),
    ]

    assert audio.speech_intervals(words, 2.0, padding=0.2, merge_gap=0.0) == [
        (0.0, 0.32),
        (1.6, 2.0),
    ]


def test_speech_duck_filter_is_one_smoothed_volume_expression():
    graph = audio.speech_duck_filter(
        "bed",
        "ducked",
        [(1.0, 2.0), (1.5, 2.5)],
        attenuation=0.75,
        attack=0.1,
        release=0.3,
    )

    assert graph.startswith("[bed]volume=volume='")
    assert "eval=frame[ducked]" in graph
    assert "0.750" in graph
    assert "0.100" in graph
    assert "0.300" in graph
    # Direct filter construction merges overlapping windows instead of multiplying dips.
    assert graph.count("between(t,1.000") == 1


def test_music_mix_filter_uses_timed_ducking_when_speech_is_available():
    graph = audio.music_mix_filter(
        "0:a",
        "1:a",
        "aout",
        0.2,
        5.0,
        speech_intervals=[(1.0, 2.0)],
        duck_attenuation=0.5,
        duck_attack=0.1,
        duck_release=0.2,
    )

    assert "sidechaincompress" not in graph
    assert "volume=volume='" in graph
    assert "0.500" in graph
    assert graph.endswith("[aout]")


def test_music_mix_filter_falls_back_safely_without_speech_intervals():
    graph = audio.music_mix_filter("0:a", "1:a", "aout", 0.2, 5.0, speech_intervals=())

    assert "sidechaincompress" in graph
    assert graph.endswith("[aout]")


def test_speech_duck_filter_missing_intervals_is_a_pass_through():
    assert audio.speech_duck_filter("bed", "ducked", []) == "[bed]anull[ducked]"


def test_music_mix_filter_missing_music_label_is_a_pass_through():
    assert audio.music_mix_filter("speech", "", "mixed", 0.2, 5.0) == "[speech]anull[mixed]"
