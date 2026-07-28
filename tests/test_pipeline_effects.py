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



# ===========================================================================
# Advanced AV engines (av-engines-foundation) — Tasks 10.5 / 10.6
# ---------------------------------------------------------------------------
# Two ffmpeg integration examples for the Task-10 pipeline hooks:
#
#   10.5  a COMPOSE-stage contribution is folded into the compositor's SINGLE
#         ffmpeg pass (extra ``-i`` input appended last, filters appended to the
#         same ``-filter_complex``), and the clip still lands at the target
#         resolution and duration;
#   10.6  an AUDIO-stage engine may hand back replacement clip media, and when
#         that same engine raises the clip is still produced from the pre-stage
#         media.
#
# Engines are registered into an **isolated** ``Engine_Registry`` that is injected
# into the host ``run_pipeline`` builds, and the capability report, storage backend
# and clock are injected too, so the process-wide default registry stays empty and
# ``host.active`` remains False for every other test in the suite. Each test
# asserts that at the end.
#
# INPUT-INDEX SEAM (the gap this note used to record is now closed):
# ``Engine_Context.first_input_index`` tells an engine the absolute ffmpeg ``-i``
# index of its own first input — the host reserves one contiguous block of
# ``AV_Engine.max_inputs`` indices per contributing engine immediately after the
# base clip, in registry ``(priority, engine_id)`` order — so an engine CAN now
# write ``[N:v]`` labels for its own inputs. The reservation rules and the
# zero-contribution parity guarantee are covered by
# ``tests/test_engine_host.py::test_compose_engines_receive_a_reserved_ffmpeg_input_block``
# and the two seam tests in ``tests/test_effects_compositor.py``.
# Task 10.5 below still uses an index-free video filter (``drawbox``) for the
# visible overlay, because the compositor appends a contribution's
# ``video_filters`` into an existing comma-joined filter *chain* where a second
# input link cannot be introduced; it asserts the input-block placement (base ->
# engines -> music -> b-roll -> emoji) instead.
# ===========================================================================
from dataclasses import dataclass

from tests.conftest import probe_duration
from tests.fakes import (
    FakeClock,
    FakeEngine,
    RaisingEngine,
    RecordingStorage,
    StaticProber,
)
from worker.effects import compositor
from worker.engines.base import Compose_Contribution, Compose_Input, Engine_Stage
from worker.engines.capabilities import Capability_Report, reset_report
from worker.engines.host import Engine_Host
from worker.engines.registry import Engine_Registry, get_registry, reset_registry


@dataclass
class Engine_Processing_Options(ProcessingOptions):
    """``ProcessingOptions`` plus the Feature_Flags of the engines used below.

    Declared as real dataclass fields (not attributes attached after the fact) so
    they survive the ``dataclasses.replace`` copies ``effective_options`` may make.
    """

    compose_probe_enabled: bool = False
    audio_probe_enabled: bool = False
    audio_raiser_enabled: bool = False


def _stub_source_transcript(monkeypatch, pl, span):
    """Stub transcribe + selection so exactly one clip covering ``span`` is cut."""
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript, TranscriptSegment, Word

    start, end = span

    def fake_transcribe(source, language=None, translate=False):
        words = [Word(0.2, 0.6, "hello"), Word(0.8, 1.2, "there")]
        return Transcript(
            language="en",
            segments=[TranscriptSegment(0.0, end, "hello there", words)],
        )

    monkeypatch.setattr(pl, "transcribe", fake_transcribe)
    monkeypatch.setattr(
        pl.sel, "select_moments",
        lambda *a, **k: [ClipCandidate(start=start, end=end, score=77.0,
                                       text="hello there")],
    )


def _inject_isolated_host(monkeypatch, pl, engines):
    """Register ``engines`` into an isolated registry and inject it into the host."""
    registry = Engine_Registry()
    for engine in engines:
        registry.register(engine)
    report = Capability_Report(StaticProber({}))
    storage = RecordingStorage()
    clock = FakeClock()

    def factory(options, **kwargs):
        return Engine_Host(options, registry=registry, capabilities=report,
                           storage=storage, clock=clock, **kwargs)

    monkeypatch.setattr(pl, "Engine_Host", factory)
    return registry


def _spy_compositor_run(monkeypatch):
    """Record every ffmpeg command the compositor issues, still running it."""
    calls: list = []
    real_run = compositor._run

    def spy(cmd):
        calls.append(list(cmd))
        return real_run(cmd)

    monkeypatch.setattr(compositor, "_run", spy)
    return calls


@requires_ffmpeg
def test_compose_stage_contribution_renders_in_one_ffmpeg_pass(
    make_video, png_asset, tmp_path, monkeypatch
):
    """Validates: Requirements 1.5, 22.5, 23.3

    A COMPOSE-stage engine's contribution (a still-image input plus a video
    filter) is folded into the compositor's **single** ffmpeg invocation: the
    image is appended last in the ``-i`` list at the index the documented input
    ordering predicts, the filter appears in the same ``-filter_complex``, and the
    finished clip still matches the target resolution and the clip duration.
    """
    import worker.pipeline as pl

    reset_registry()
    reset_report()

    span = (0.0, 2.5)
    clip_length = span[1] - span[0]
    src = make_video("engine_compose_src.mp4", duration=3.0, w=640, h=360)
    overlay_png = png_asset("engine_overlay.png", color="red")
    _stub_source_transcript(monkeypatch, pl, span)

    contribution = Compose_Contribution(
        engine_id="compose_probe",
        inputs=(Compose_Input(path=overlay_png, loop=True, duration=clip_length),),
        # Index-free filter: see the KNOWN API GAP note above.
        video_filters=("drawbox=x=16:y=16:w=72:h=72:color=red@0.6:t=fill",),
        z_order=5,
    )
    engine = FakeEngine(
        "compose_probe", Engine_Stage.COMPOSE,
        contribution=contribution, markers=("overlay",),
    )
    _inject_isolated_host(monkeypatch, pl, [engine])
    ffmpeg_calls = _spy_compositor_run(monkeypatch)

    opts = Engine_Processing_Options(
        captions=False, metadata=False, aspect="9:16", compose_probe_enabled=True,
    )
    clips = pl.run_pipeline(src, opts, clips_dir=tmp_path / "clips",
                            temp_dir=tmp_path / "tmp")

    assert len(clips) == 1
    clip = clips[0]
    assert engine.run_count == 1

    # Exactly ONE ffmpeg pass composited the clip (Reqs 1.5, 23.3).
    assert len(ffmpeg_calls) == 1
    cmd = ffmpeg_calls[0]
    inputs = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-i"]
    # Documented ordering: base -> music -> b-roll -> emoji -> engines (last).
    assert inputs == [str(tmp_path / "tmp" / f"geo_{clip.id}.mp4"), str(overlay_png)]
    assert inputs.index(str(overlay_png)) == 1
    assert "-loop" in cmd                      # still image looped, not decoded once
    assert "-filter_complex" in cmd
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "drawbox=x=16:y=16:w=72:h=72:color=red@0.6:t=fill" in graph

    # The engine's marker is attributed, and the clip matches the target.
    assert "engine:compose_probe:overlay" in clip.effects_applied
    out = tmp_path / "clips" / clip.filename
    assert out.exists()
    assert probe_size(out) == (1080, 1920)
    assert abs(probe_duration(out) - clip_length) < 0.5

    # The process-wide registry was never touched.
    assert len(get_registry()) == 0


@requires_ffmpeg
def test_audio_stage_engine_may_replace_clip_media(make_video, tmp_path, monkeypatch):
    """Validates: Requirements 8.3, 22.5

    An AUDIO-stage engine declaring ``produces_media`` hands back replacement clip
    media and the pipeline renders the clip from it with an unchanged duration;
    when the same engine raises instead, the clip is still produced from the
    pre-stage media and the failure is recorded as exactly one
    ``engine:<id>:failed`` marker.
    """
    import worker.pipeline as pl

    reset_registry()
    reset_report()

    span = (0.0, 3.0)
    clip_length = span[1] - span[0]
    src = make_video("engine_audio_src.mp4", duration=3.0, w=640, h=360)
    replacement = make_video("engine_audio_replacement.mp4", duration=clip_length,
                             w=320, h=240)
    _stub_source_transcript(monkeypatch, pl, span)

    # (A) the engine replaces the clip media.
    engine = FakeEngine("audio_probe", Engine_Stage.AUDIO, media=replacement,
                        markers=("replaced",))
    _inject_isolated_host(monkeypatch, pl, [engine])
    assert engine.produces_media is True

    opts = Engine_Processing_Options(
        captions=False, metadata=False, aspect="9:16", audio_probe_enabled=True,
    )
    clips = pl.run_pipeline(src, opts, clips_dir=tmp_path / "clips_a",
                            temp_dir=tmp_path / "tmp_a")

    assert len(clips) == 1
    replaced = clips[0]
    assert engine.run_count == 1
    assert "engine:audio_probe:replaced" in replaced.effects_applied
    out_a = tmp_path / "clips_a" / replaced.filename
    assert out_a.exists()
    assert probe_size(out_a) == (1080, 1920)
    assert abs(probe_duration(out_a) - clip_length) < 0.5

    # (B) the same engine raises: the clip is still produced from the pre-stage
    #     media, and the failure is recorded exactly once (Req 8.3).
    raiser = RaisingEngine("audio_raiser", Engine_Stage.AUDIO,
                           exc=RuntimeError("audio engine exploded"))
    _inject_isolated_host(monkeypatch, pl, [raiser])

    opts_failing = Engine_Processing_Options(
        captions=False, metadata=False, aspect="9:16", audio_raiser_enabled=True,
    )
    failed_clips = pl.run_pipeline(src, opts_failing, clips_dir=tmp_path / "clips_b",
                                   temp_dir=tmp_path / "tmp_b")

    assert len(failed_clips) == 1
    failed = failed_clips[0]
    assert raiser.run_count == 1
    assert failed.effects_applied.count("engine:audio_raiser:failed") == 1
    out_b = tmp_path / "clips_b" / failed.filename
    assert out_b.exists()
    assert probe_size(out_b) == (1080, 1920)
    assert abs(probe_duration(out_b) - clip_length) < 0.5

    assert len(get_registry()) == 0



# ===========================================================================
# Clip_Metadata seam (av-engines-foundation) — Task 15.4
# ---------------------------------------------------------------------------
# The ONLY test that proves the *Pipeline* populates Clip_Metadata rather than a
# test doing it by hand. The kinetic-typography engine read its hook title out of
# ``ctx.deps["hook_text"]`` and its property tests passed because they built that
# mapping themselves — while the Pipeline never populated it, so the hook title
# would have silently vanished in production with the suite green. A test that
# calls ``run_stage`` directly cannot catch that class of bug: it supplies the
# mapping, so it proves nothing about the caller. Hence this test drives the real
# ``run_pipeline`` and passes NO ``clip_metadata`` of its own.
#
# The metadata path is real too: ``options.metadata`` is ON and a ``MockLLMClient``
# is injected into ``run_pipeline``, so ``md.hook_text`` is produced by the
# Pipeline's own ``meta_mod.generate_metadata`` call rather than stubbed in.
# ``aspect`` is deliberately NOT the ``9:16`` default, so a ``clip_size`` that was
# hardcoded to the fallback preset fails here.
# ===========================================================================
@requires_ffmpeg
def test_pipeline_supplies_clip_metadata_to_compose_stage_engines(
    make_video, tmp_path, monkeypatch
):
    """Validates: Requirements 15.8, 23.6

    A recording COMPOSE-stage engine driven by the real Pipeline observes a
    Clip_Metadata mapping carrying the clip's non-empty ``hook_text`` (the
    Pipeline's ``md.hook_text``) and a ``clip_size`` equal to
    ``fu.ASPECT_PRESETS[options.aspect]`` for the requested aspect.
    """
    import worker.ffmpeg_utils as fu
    import worker.pipeline as pl
    from worker.llm_client import MockLLMClient

    reset_registry()
    reset_report()

    span = (0.0, 2.5)
    aspect = "4:5"                       # NOT the 9:16 fallback preset
    src = make_video("engine_metadata_src.mp4", duration=3.0, w=640, h=360)
    _stub_source_transcript(monkeypatch, pl, span)

    hook = "Wait for it..."
    llm = MockLLMClient(responses=[
        '{"title": "Metadata clip", "description": "d", "hashtags": ["#a"], '
        f'"hook_text": "{hook}", "cta": "Follow", "mentions": [], '
        '"thumbnail_text": "WOW"}'
    ])

    engine = FakeEngine("compose_probe", Engine_Stage.COMPOSE, markers=("saw_metadata",))
    _inject_isolated_host(monkeypatch, pl, [engine])

    opts = Engine_Processing_Options(
        captions=False, metadata=True, aspect=aspect, compose_probe_enabled=True,
    )
    clips = pl.run_pipeline(src, opts, clips_dir=tmp_path / "clips",
                            temp_dir=tmp_path / "tmp", llm_client=llm)

    assert len(clips) == 1
    clip = clips[0]
    assert engine.run_count == 1

    # The Pipeline — not this test — populated the mapping.
    seen = engine.last_context.clip_metadata
    assert seen, "the Pipeline supplied no Clip_Metadata at the COMPOSE hook"
    assert seen["hook_text"] == hook != ""
    assert seen["hook_text"] == clip.hook_text          # same value the clip records
    assert seen["clip_size"] == fu.ASPECT_PRESETS[aspect]
    assert seen["clip_size"] != fu.ASPECT_PRESETS["9:16"]   # not the hardcoded fallback

    # The clip itself still lands at that same target size.
    out = tmp_path / "clips" / clip.filename
    assert out.exists()
    assert probe_size(out) == fu.ASPECT_PRESETS[aspect]
    assert "engine:compose_probe:saw_metadata" in clip.effects_applied

    assert len(get_registry()) == 0
