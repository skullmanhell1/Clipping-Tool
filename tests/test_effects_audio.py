"""Tests for background-music resolution, synthesis, and mixing filters."""

from __future__ import annotations

from tests.conftest import probe_duration, requires_ffmpeg
from worker.effects import audio


def test_available_moods():
    moods = audio.available_moods()
    assert {"upbeat", "chill", "dramatic", "corporate", "suspense"} <= set(moods)


def test_resolve_music_empty_mood_is_none(tmp_path):
    assert audio.resolve_music("", 5.0, tmp_path) is None


def test_find_user_track_priority(tmp_path, monkeypatch):
    monkeypatch.setattr(audio.settings, "music_dir", tmp_path)
    track = tmp_path / "chill.mp3"
    track.write_bytes(b"ID3fake")
    assert audio.find_user_track("chill") == track
    assert audio.find_user_track("upbeat") is None


def test_music_mix_filter_structure():
    f = audio.music_mix_filter("0:a", "1:a", "aout", 0.2, 10.0, fade=False)
    assert "volume=0.200" in f
    assert "amix=inputs=2:duration=first:normalize=0[aout]" in f
    faded = audio.music_mix_filter("0:a", "1:a", "aout", 0.2, 10.0, fade=True)
    assert "afade=t=in" in faded and "afade=t=out" in faded


@requires_ffmpeg
def test_synthesize_bed_produces_audio(tmp_path):
    dest = audio.synthesize_bed("chill", 2.0, tmp_path / "bed.m4a")
    assert dest.exists()
    assert abs(probe_duration(dest) - 2.0) < 0.3


@requires_ffmpeg
def test_resolve_music_synthesizes_when_no_user_track(tmp_path, monkeypatch):
    monkeypatch.setattr(audio.settings, "music_dir", tmp_path / "empty")
    path = audio.resolve_music("upbeat", 1.5, tmp_path)
    assert path is not None and path.exists()
