"""Offline and real-binary checks for brand intro/outro concatenation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import options_all_off, probe_duration, requires_ffmpeg
from worker import brand_bumpers
from worker.ffmpeg_utils import FFmpegError


def test_bumpers_normalize_segments_and_add_silence_for_audio_less_assets(tmp_path, monkeypatch):
    intro = tmp_path / "intro.mp4"
    main = tmp_path / "main.mp4"
    outro = tmp_path / "outro.mp4"
    for path in (intro, main, outro):
        path.write_bytes(b"input")
    output = tmp_path / "out.mp4"
    seen: dict[str, list[str]] = {}

    def fake_probe(path):
        return SimpleNamespace(
            duration=1.0,
            width=1080,
            height=1920,
            has_audio=Path(path).name == "main.mp4",
        )

    def fake_run(command):
        seen["command"] = command
        Path(command[-1]).write_bytes(b"output")

    monkeypatch.setattr(brand_bumpers, "probe", fake_probe)
    monkeypatch.setattr(brand_bumpers, "_run", fake_run)
    monkeypatch.setattr(brand_bumpers, "h264_args", lambda **_kwargs: ["-c:v", "libx264"])
    monkeypatch.setattr(brand_bumpers, "aac_args", lambda: ["-c:a", "aac"])

    result = brand_bumpers.apply_bumpers(
        main,
        output,
        intro=intro,
        outro=outro,
        width=1080,
        height=1920,
        fps=30,
    )

    assert result == output
    assert output.exists()
    command = " ".join(seen["command"])
    assert "concat=n=3:v=1:a=1" in command
    assert "anullsrc=channel_layout=stereo:sample_rate=48000" in command


def test_bumpers_refuse_missing_assets(tmp_path):
    main = tmp_path / "main.mp4"
    main.write_bytes(b"input")

    with pytest.raises(FFmpegError, match="does not exist"):
        brand_bumpers.apply_bumpers(
            main,
            tmp_path / "out.mp4",
            intro=tmp_path / "missing.mp4",
            width=1080,
            height=1920,
        )


@requires_ffmpeg
def test_pipeline_appends_materialized_bumpers_and_updates_duration(
    make_video, tmp_path, monkeypatch
):
    import worker.pipeline as pipeline
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript, TranscriptSegment, Word

    monkeypatch.setattr(
        pipeline,
        "transcribe",
        lambda source, language=None, translate=False, **_kwargs: Transcript(
            language="en",
            segments=[
                TranscriptSegment(
                    0.0,
                    4.0,
                    "hello there friend",
                    [Word(0.2, 0.6, "hello"), Word(0.7, 1.1, "there")],
                )
            ],
        ),
    )
    monkeypatch.setattr(
        pipeline.sel,
        "select_moments",
        lambda *args, **kwargs: [ClipCandidate(start=0.0, end=4.0, score=50.0, text="hello there")],
    )

    source = make_video("source.mp4", duration=4.0, w=640, h=360)
    intro = make_video("intro.mp4", duration=0.6, w=640, h=360)
    outro = make_video("outro.mp4", duration=0.7, w=640, h=360)
    clips = pipeline.run_pipeline(
        source,
        options_all_off(
            captions=False,
            metadata=False,
            aspect="9:16",
            brand_intro_asset=str(intro),
            brand_outro_asset=str(outro),
        ),
        clips_dir=tmp_path / "clips",
        temp_dir=tmp_path / "tmp",
    )

    assert len(clips) == 1
    clip = clips[0]
    output = tmp_path / "clips" / clip.filename
    assert "brand_bumpers" in clip.effects_applied
    assert probe_duration(output) > 4.8
