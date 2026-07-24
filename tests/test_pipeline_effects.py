"""End-to-end pipeline test with effects (whisper/LLM stubbed out)."""
from __future__ import annotations

from tests.conftest import probe_size, requires_ffmpeg
from worker.models import ProcessingOptions


@requires_ffmpeg
def test_pipeline_applies_effects(make_video, tmp_path, monkeypatch):
    import worker.pipeline as pl
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript, TranscriptSegment, Word

    src = make_video("source.mp4", duration=6.0, w=1280, h=720)

    def fake_transcribe(source, language=None, translate=False):
        words = [Word(0.3, 0.7, "This"), Word(0.8, 1.2, "um"), Word(1.3, 1.7, "is"),
                 Word(1.8, 2.3, "fire"), Word(2.4, 2.9, "and"), Word(5.0, 5.5, "money")]
        return Transcript(language="en",
                          segments=[TranscriptSegment(0.0, 6.0, "This um is fire and money", words)])

    monkeypatch.setattr(pl, "transcribe", fake_transcribe)
    monkeypatch.setattr(
        pl.sel, "select_moments",
        lambda *a, **k: [ClipCandidate(start=0.0, end=6.0, score=88.0,
                                       reason="t", title="T", text="This is fire and money")],
    )

    opts = ProcessingOptions(
        captions=True, filler_removal=True, color="warm", fades=True,
        progress_bar=True, metadata=False, aspect="9:16",
    )
    clips = pl.run_pipeline(src, opts, clips_dir=tmp_path / "clips",
                            temp_dir=tmp_path / "tmp")
    assert len(clips) == 1
    clip = clips[0]
    # Effects recorded on the clip.
    assert "filler_removal" in clip.effects_applied
    assert "captions" in clip.effects_applied
    assert "color:warm" in clip.effects_applied
    # Output exists at the target vertical resolution.
    out = tmp_path / "clips" / clip.filename
    assert out.exists()
    assert probe_size(out) == (1080, 1920)


@requires_ffmpeg
def test_pipeline_no_effects_still_produces_clip(make_video, tmp_path, monkeypatch):
    import worker.pipeline as pl
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript, TranscriptSegment, Word

    src = make_video("source2.mp4", duration=4.0, w=1280, h=720)
    monkeypatch.setattr(
        pl, "transcribe",
        lambda s, language=None, translate=False: Transcript(
            language="en",
            segments=[TranscriptSegment(0.0, 4.0, "hello there friend",
                                        [Word(0.2, 0.6, "hello"), Word(0.7, 1.1, "there")])],
        ),
    )
    monkeypatch.setattr(
        pl.sel, "select_moments",
        lambda *a, **k: [ClipCandidate(start=0.0, end=4.0, score=50.0, text="hello there")],
    )

    opts = ProcessingOptions(captions=False, metadata=False, aspect="9:16")
    clips = pl.run_pipeline(src, opts, clips_dir=tmp_path / "clips",
                            temp_dir=tmp_path / "tmp")
    assert len(clips) == 1
    assert (tmp_path / "clips" / clips[0].filename).exists()
