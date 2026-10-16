"""Retention-plan cues are preserved through the compositor boundary."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import options_all_off
from worker.effects import compositor
from worker.ffmpeg_utils import MediaInfo
from worker.transcribe import Word


def test_retention_beats_and_emphasis_reach_the_rendered_graph(monkeypatch, tmp_path):
    """Video accents and caption emphasis use the post-filter word/timeline coordinates."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"test media")
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        compositor,
        "probe",
        lambda _path: MediaInfo(
            duration=5.0,
            width=720,
            height=1280,
            fps=30.0,
            has_audio=False,
        ),
    )
    monkeypatch.setattr(compositor, "_beat_times", lambda *_args: ())

    def build_video_chain(**kwargs):
        observed["beats"] = kwargs["beats"]
        return ["null"]

    monkeypatch.setattr(compositor.overlays, "build_video_chain", build_video_chain)
    monkeypatch.setattr(
        compositor.caption_contrast,
        "choose_for_clip",
        lambda _path, preset, **_kwargs: (preset, []),
    )

    def build_ass(_cues, _path, **kwargs):
        observed["keyword_indices"] = kwargs["keyword_indices"]

    monkeypatch.setattr(compositor.cap, "build_ass", build_ass)
    monkeypatch.setattr(compositor, "_run", lambda _cmd: None)

    # The empty first token is omitted from caption cues. Retention index 1
    # must therefore map from the source transcript onto cue-word index 0.
    words = [
        Word(0.0, 0.2, ""),
        Word(0.3, 0.8, "Bright"),
        Word(0.9, 1.3, "idea"),
    ]
    options = options_all_off(
        captions=True,
        zoom=True,
        caption_keyword_highlight=False,
        caption_emoji=False,
        transitions=False,
    )

    result = compositor.render_clip(
        source,
        tmp_path / "rendered.mp4",
        options,
        words,
        tmp_path / "temp",
        retention_beats=(2.0, 0.35, 2.0, float("nan")),
        retention_emphasis_indices=(1,),
    )

    assert result is not None
    assert observed["beats"] == (0.35, 2.0)
    assert observed["keyword_indices"] == {0}


def test_compositor_passes_transcript_words_to_music_ducking(monkeypatch, tmp_path):
    """The timed dip is based on the delivered, clip-relative transcript."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"test media")
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        compositor,
        "probe",
        lambda _path: MediaInfo(
            duration=5.0,
            width=720,
            height=1280,
            fps=30.0,
            has_audio=True,
        ),
    )
    monkeypatch.setattr(compositor, "_beat_times", lambda *_args: ())
    monkeypatch.setattr(
        compositor.audio,
        "resolve_music_bed",
        lambda mood, _duration, _temp, **_kwargs: compositor.audio.MusicBed(
            path=tmp_path / "music.wav",
            mood=mood,
            source=compositor.audio.SOURCE_USER_TRACK,
        ),
    )

    def music_mix_filter(_speech, _music, _out, _volume, _duration, **kwargs):
        observed["speech_intervals"] = kwargs["speech_intervals"]
        return "[0:a]anull[aout]"

    monkeypatch.setattr(compositor.audio, "music_mix_filter", music_mix_filter)
    monkeypatch.setattr(compositor, "_run", lambda _cmd: None)

    options = options_all_off(
        captions=False,
        music="chill",
        music_duck=True,
        fades=False,
    )
    result = compositor.render_clip(
        source,
        tmp_path / "rendered.mp4",
        options,
        [Word(0.1, 0.4, "Hello"), Word(0.48, 0.8, "there")],
        tmp_path / "temp",
    )

    assert result is not None
    intervals = observed["speech_intervals"]
    assert [(round(start, 2), round(end, 2)) for start, end in intervals] == [(0.02, 0.88)]


def test_compositor_schedules_explicit_retention_pops_even_when_sfx_mode_is_off(
    monkeypatch, tmp_path
):
    """Aggressive-retention accents are explicit cues, separate from decorative SFX settings."""
    monkeypatch.setattr(compositor.settings, "sfx_mode", "off")
    monkeypatch.setattr(
        compositor.sfx,
        "resolve_sting",
        lambda name, _temp: (
            compositor.sfx.Sting(
                name=name,
                path=Path(tmp_path / f"{name}.wav"),
                source=compositor.sfx.SOURCE_USER_FILE,
            ),
            "",
        ),
    )

    hits, markers = compositor._plan_sfx(
        emoji_starts=(),
        transition_times=(),
        retention_times=(1.0, 1.1, 3.0),
        duration=5.0,
        temp_dir=tmp_path,
    )

    assert [(hit.at, hit.trigger, hit.sting.name) for hit in hits] == [
        (1.0, "retention", "pop"),
        (3.0, "retention", "pop"),
    ]
    assert markers == []
