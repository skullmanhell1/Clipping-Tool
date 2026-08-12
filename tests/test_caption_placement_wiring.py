"""V15 face-aware caption placement reaches the rendered ASS.

`worker/caption_placement.py` shipped complete and tested with **no importer outside its own test
module**, so `caption_avoid_faces` could not take effect and the caption/mouth collision it exists to
prevent still shipped. `tests/test_script_and_placement.py` covers the module's geometry; this file
covers whether anything calls it.

Every assertion is on the emitted ASS style line, on the ffmpeg command, or on the clip record —
never on a pure function's return value. Two tests assert the *discriminator* (feature-off differs
from feature-on), because a placement test can otherwise pass on a fixture that never had a
collision, which is how a vacuous test survives review.

The kinetic engine gets its own tests. It supersedes the compositor's captions entirely, so wiring
only `build_ass` would leave V15 silently absent from a kinetic render — the same shape of defect as
the one this whole change is fixing.
"""

from __future__ import annotations

import pytest

from worker import captions as cap
from worker.effects.caption_presets import resolve_preset
from worker.effects.reframe import FaceBox
from worker.transcribe import Word

PRESET = resolve_preset("karaoke")[0]

#: Frame height every fixture reasons about, matching the 9:16 delivery size.
H = 1920


def _words(text: str = "one two three") -> list[Word]:
    return [
        Word(start=i * 0.4, end=(i * 0.4) + 0.35, text=token)
        for i, token in enumerate(text.split())
    ]


def _cues(words):
    return cap.words_to_cues(words)


# --------------------------------------------------------------------------- #
# Fixtures whose collisions are arithmetic, not guesswork                     #
# --------------------------------------------------------------------------- #
#
# A mouth band is the bottom `MOUTH_FRACTION` (0.34) of a face box. The default `bottom` caption band
# at 1920 tall sits at MarginV 220 with a height of `font_size * max_lines * 1.25`, so these boxes are
# constructed to land inside or outside it deliberately. Building them by eye is how a placement test
# ends up asserting nothing.


def _face_over_the_bottom_caption() -> FaceBox:
    """A face whose mouth band overlaps the default bottom caption band."""
    return FaceBox(t=0.0, x=100, y=1317, w=300, h=353)


def _face_over_the_top_caption() -> FaceBox:
    return FaceBox(t=0.0, x=100, y=184, w=300, h=200)


def _face_over_the_centre_caption() -> FaceBox:
    return FaceBox(t=0.0, x=100, y=798, w=300, h=200)


def _style_fields(path) -> list[str]:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("Style: Default"):
            return line.split(",")
    raise AssertionError("no default style line in the rendered ASS")


def _alignment(path) -> int:
    """The ASS Alignment number of the default style (field 19 of the Style line)."""
    return int(_style_fields(path)[18])


def _render(tmp_path, *, name="c.ass", **kwargs):
    notes: list[str] = []
    dest = cap.build_ass(
        _cues(_words()),
        tmp_path / name,
        preset=PRESET,
        clip_duration=3.0,
        notes=notes,
        **kwargs,
    )
    return dest, notes


# --------------------------------------------------------------------------- #
# build_ass -- the funnel every burned-in caption passes through              #
# --------------------------------------------------------------------------- #


def test_a_caption_over_a_mouth_moves_and_the_alignment_changes(tmp_path, monkeypatch):
    """The claim, asserted on the rendered file rather than on a returned plan.

    Alignment 2 is bottom-centre; 8 is top-centre. The horizontal component is preserved, which is
    the invariant `_alternatives` exists to guarantee — a caption that jumped to a corner would be a
    different kind of wrong.
    """
    monkeypatch.setattr(cap.settings, "caption_avoid_faces", True)

    dest, notes = _render(tmp_path, face_boxes=[_face_over_the_bottom_caption()])

    assert _alignment(dest) == 8, "the caption did not move off the mouth"
    assert "caption_moved_off_face:top" in notes


def test_with_the_feature_off_the_same_fixture_renders_byte_identically(tmp_path, monkeypatch):
    """The discriminator. Without this, the test above could pass for the wrong reason.

    Byte-identical rather than merely same-alignment: V15 must not perturb anything else on its way
    past, and the v0.8.0 parity goldens depend on that.
    """
    monkeypatch.setattr(cap.settings, "caption_avoid_faces", False)
    off, off_notes = _render(tmp_path, name="off.ass", face_boxes=[_face_over_the_bottom_caption()])

    monkeypatch.setattr(cap.settings, "caption_avoid_faces", True)
    on, _ = _render(tmp_path, name="on.ass", face_boxes=[_face_over_the_bottom_caption()])

    assert _alignment(off) == 2
    assert not [n for n in off_notes if n.startswith("caption_")]
    assert off.read_bytes() != on.read_bytes(), (
        "feature-on and feature-off produced the same file; the fixture has no collision and every "
        "other assertion here is vacuous"
    )


def test_a_caption_that_already_clears_the_mouth_is_left_alone(tmp_path, monkeypatch):
    """No collision means no move *and* no marker.

    "V15 found nothing to do" and "V15 could not help" must not look alike in the clip record, so the
    silent case has to be genuinely silent.
    """
    monkeypatch.setattr(cap.settings, "caption_avoid_faces", True)

    # A face high in the frame, nowhere near the bottom caption band.
    dest, notes = _render(tmp_path, face_boxes=[FaceBox(t=0.0, x=100, y=100, w=200, h=200)])

    assert _alignment(dest) == 2
    assert not [n for n in notes if n.startswith("caption_")]


def test_when_every_position_collides_nothing_moves_but_the_record_says_why(tmp_path, monkeypatch):
    """A close-up filling the frame.

    Moving the caption from the mouth to the eyes is not an improvement, so the honest outcome is to
    change nothing and say so — an absent feature that reports its absence.
    """
    monkeypatch.setattr(cap.settings, "caption_avoid_faces", True)

    dest, notes = _render(
        tmp_path,
        face_boxes=[
            _face_over_the_bottom_caption(),
            _face_over_the_top_caption(),
            _face_over_the_centre_caption(),
        ],
    )

    assert _alignment(dest) == 2, "position changed despite every alternative colliding"
    assert "caption_face_overlap_unavoidable" in notes


def test_no_media_and_no_boxes_leaves_placement_untouched(tmp_path, monkeypatch):
    """`caption_preview` calls `build_ass` with no media at all.

    A preview that moved its captions for faces the real render might place differently would be a
    preview of something else, so the absence of media is a decline rather than a detection attempt.
    """
    monkeypatch.setattr(cap.settings, "caption_avoid_faces", True)

    dest, notes = _render(tmp_path)

    assert _alignment(dest) == 2
    assert not [n for n in notes if n.startswith("caption_")]


def test_the_legacy_template_branch_gets_placement_too(tmp_path, monkeypatch):
    """C20's auto-contrast covers only the preset path.

    A legibility feature that silently depends on which caption *look* was chosen is the same defect
    in a different place, so the legacy branch is wired as well.
    """
    monkeypatch.setattr(cap.settings, "caption_avoid_faces", True)
    notes: list[str] = []
    dest = cap.build_ass(
        _cues(_words()),
        tmp_path / "legacy.ass",
        template="karaoke",
        position="bottom",
        notes=notes,
        face_boxes=[_face_over_the_bottom_caption()],
    )

    assert "caption_moved_off_face:top" in notes
    assert _alignment(dest) == 8


def test_none_position_is_preserved_rather_than_resolved(tmp_path, monkeypatch):
    """`None` means "use the preset's own position" and must survive an inert pass.

    Resolving it to a concrete name would produce an identical file today while making every later
    preset change silently ineffective — a bug that could sit undetected for a release.
    """
    monkeypatch.setattr(cap.settings, "caption_avoid_faces", True)
    seen: dict = {}
    real = cap._face_aware_position

    def spy(position, **kwargs):
        seen["returned"] = real(position, **kwargs)
        return seen["returned"]

    monkeypatch.setattr(cap, "_face_aware_position", spy)
    # A face that does not collide, so the plan does not move and `None` must come back out.
    _render(tmp_path, position=None, face_boxes=[FaceBox(t=0.0, x=1, y=50, w=100, h=100)])

    assert seen["returned"] is None


def test_the_margin_v15_reasons_about_follows_the_configured_safe_area(tmp_path, monkeypatch):
    """C12/C13 move the caption, so V15 must ask where it will actually be drawn.

    With a large safe-area inset the bottom caption sits much higher, which means a face that
    collided at the default margin no longer does. Reasoning at the default margin while the renderer
    uses another is how a collision gets missed or invented.
    """
    monkeypatch.setattr(cap.settings, "caption_avoid_faces", True)
    monkeypatch.setattr(cap.settings, "caption_safe_area", "")
    monkeypatch.setattr(cap.settings, "caption_offset_px", 0)
    plain, plain_notes = _render(
        tmp_path, name="plain.ass", face_boxes=[_face_over_the_bottom_caption()]
    )
    assert "caption_moved_off_face:top" in plain_notes
    assert _alignment(plain) == 8

    # Lift the caption well clear of that mouth band with a C13 offset.
    monkeypatch.setattr(cap.settings, "caption_offset_px", 500)
    lifted, lifted_notes = _render(
        tmp_path, name="lifted.ass", face_boxes=[_face_over_the_bottom_caption()]
    )

    assert not [n for n in lifted_notes if n.startswith("caption_moved")], (
        "V15 still moved a caption the offset had already lifted clear; it is reasoning about the "
        "default margin rather than the one being rendered"
    )
    assert _alignment(lifted) == 2


# --------------------------------------------------------------------------- #
# The compositor passes the media -- on both branches                         #
# --------------------------------------------------------------------------- #


def _compositor_build_ass_kwargs(monkeypatch, tmp_path, make_video, **option_overrides):
    """Render through the compositor and capture what `build_ass` was handed."""
    from tests.conftest import options_all_off
    from worker.effects import compositor as comp

    src = make_video("v15.mp4", duration=2.0, w=640, h=360)
    seen: dict = {}

    def spy(cues, dest, **kwargs):
        seen.update(kwargs)
        return real(cues, dest, **kwargs)

    real = comp.cap.build_ass
    monkeypatch.setattr(comp.cap, "build_ass", spy)
    monkeypatch.setattr(comp, "_run", lambda *a, **k: None)

    comp.render_clip(
        src,
        tmp_path / "out.mp4",
        options_all_off(aspect="9:16", captions=True, **option_overrides),
        _words(),
        tmp_path / "tmp",
    )
    return seen


@pytest.mark.usefixtures("make_video")
def test_the_compositor_hands_the_clip_to_the_preset_branch(monkeypatch, tmp_path, make_video):
    """Deleting `clip_path=base_clip` from the preset branch fails here.

    `use_preset` is `caption_preset != "karaoke"`, so a non-karaoke preset is what selects this
    branch — naming the value "karaoke" here would silently exercise the legacy path instead, which
    is a mistake this test made on the way in and which the mutation run caught.
    """
    seen = _compositor_build_ass_kwargs(monkeypatch, tmp_path, make_video, caption_preset="hormozi")

    assert seen.get("preset") is not None, "this fixture did not take the preset branch"
    assert seen.get("clip_path") is not None, "the preset branch passed no media for placement"


@pytest.mark.usefixtures("make_video")
def test_the_compositor_hands_the_clip_to_the_legacy_branch(monkeypatch, tmp_path, make_video):
    """And from the legacy `template` branch, which the default "karaoke" preset selects."""
    seen = _compositor_build_ass_kwargs(monkeypatch, tmp_path, make_video, caption_preset="karaoke")

    assert seen.get("preset") is None, "this fixture did not take the legacy branch"
    assert seen.get("clip_path") is not None, "the legacy branch passed no media for placement"


# --------------------------------------------------------------------------- #
# The kinetic engine, which supersedes build_ass entirely                     #
# --------------------------------------------------------------------------- #


def _kinetic_opts(**overrides):
    from worker.engines.kinetic import Kinetic_Options

    return Kinetic_Options(**overrides)


def _engine():
    from worker.engines.kinetic import Kinetic_Typography_Engine

    return Kinetic_Typography_Engine()


def _kctx(tmp_path, *, boxes, options=None, words=(), duration=0.0):
    """A minimal COMPOSE-stage context carrying V15's boxes on the real channel.

    `face_boxes` on `clip_metadata` is how the Pipeline publishes them, alongside `hook_text` and
    `clip_size`. Hand-building the context here keeps these tests free of a workspace they do not
    need; `tests/test_kinetic_engine.py` covers the full `Engine_Context`.
    """

    class _Ctx:
        clip_path = tmp_path / "clip.mp4"
        clip_metadata = {"clip_size": (1080, 1920), "face_boxes": tuple(boxes)}
        time_base = None
        remaining = None

    ctx = _Ctx()
    ctx.options = options
    ctx.words = tuple(words)
    ctx.duration = duration
    return ctx


def _faces_covering_every_position() -> list[FaceBox]:
    """Mouth bands overlapping the top, centre and bottom caption bands at once."""
    return [
        _face_over_the_bottom_caption(),
        _face_over_the_top_caption(),
        _face_over_the_centre_caption(),
    ]


def test_the_kinetic_engine_applies_placement_from_the_published_boxes(tmp_path, monkeypatch):
    """V15 on the kinetic path, with the purity contract intact.

    The engine may not create a subprocess and may not import `config`, so it neither detects faces
    nor reads the setting: the Pipeline publishes boxes on `clip_metadata` and the engine applies
    `choose_position`, which is pure geometry. This asserts the geometry is really applied — no stub
    stands in for it — using the same arithmetic fixture as the `build_ass` tests above.
    """
    engine = _engine()

    opts = _kinetic_opts(position="bottom")
    moved = engine._face_aware_options(
        _kctx(tmp_path, boxes=[_face_over_the_bottom_caption()]), opts
    )

    assert moved.position == "top"
    assert "caption_moved_off_face:top" in moved.notes
    assert opts.position == "bottom", "the original options were mutated rather than replaced"


def test_the_kinetic_planner_is_actually_given_the_moved_position(tmp_path):
    """Drives `_plan_for`, not `_face_aware_options`.

    **This test exists because the first version of this file did not have it.** Every other kinetic
    assertion here calls `_face_aware_options` directly, so replacing `_plan_for`'s call to it with a
    plain `_resolved_options(ctx)` — deleting the wiring outright — left all of them passing. That is
    precisely the "tests the seam, not the call site" failure that let V15 ship dead in the first
    place, so the fix is a test that goes through the caller.
    """
    ctx = _kctx(
        tmp_path,
        boxes=[_face_over_the_bottom_caption()],
        options=_kinetic_opts(position="bottom"),
        words=(Word(0.0, 0.4, "hello"), Word(0.5, 0.9, "there")),
        duration=1.0,
    )

    plan = _engine()._plan_for(ctx, "Poppins ExtraBold")

    assert plan.position == "top", "the planner was handed the un-moved position"
    assert plan.align == 8, "Alignment does not reflect the moved position"
    assert any("caption_moved_off_face:top" in m for m in plan.markers), (
        "the marker did not reach the plan, so the clip record cannot report the move"
    )


def test_the_kinetic_engine_is_inert_when_no_boxes_were_published(tmp_path):
    """The feature being off is expressed as an absence of boxes, so this is the off path.

    The very same options object comes back, which is the strongest available statement that nothing
    was recomputed or re-normalised on the default path.
    """
    opts = _kinetic_opts(position="bottom")

    assert _engine()._face_aware_options(_kctx(tmp_path, boxes=[]), opts) is opts


def test_the_kinetic_engine_tolerates_a_context_with_no_metadata_channel(tmp_path):
    """Total against a hand-built context, like every other reader of this channel."""

    class _Bare:
        clip_metadata = None

    opts = _kinetic_opts(position="bottom")

    assert _engine()._face_aware_options(_Bare(), opts) is opts


def test_a_kinetic_refusal_records_the_reason_without_moving_anything(tmp_path):
    """`caption_face_overlap_unavoidable` is a note, not a move."""
    result = _engine()._face_aware_options(
        _kctx(tmp_path, boxes=_faces_covering_every_position()),
        _kinetic_opts(position="bottom"),
    )

    assert result.position == "bottom"
    assert "caption_face_overlap_unavoidable" in result.notes


def test_an_inherited_kinetic_position_stays_inherited_when_nothing_moves(tmp_path):
    """`position=""` means "inherit the Base_Preset" (Req 7.4) and must survive an inert pass.

    Found by mutation: writing `plan.position` back unconditionally passes every other test here,
    because a refusal returns the requested position unchanged and the two are then equal. They are
    *not* equal when the position is inherited — `requested` has already been resolved to the
    preset's concrete name, so an unconditional write turns `""` into `"bottom"`. The file renders
    identically today and silently ignores any later change to the preset's position, which is the
    same latent defect as resolving `None` on the `build_ass` path.
    """
    # A refusal, so `requested` comes back unchanged -- already resolved to a concrete name.
    result = _engine()._face_aware_options(
        _kctx(tmp_path, boxes=_faces_covering_every_position()), _kinetic_opts(position="")
    )

    assert result.position == "", (
        "an inherited position was resolved to a concrete name; the preset can no longer change it"
    )


# --------------------------------------------------------------------------- #
# The Pipeline publishes the boxes -- the impure half                         #
# --------------------------------------------------------------------------- #


def test_the_pipeline_publishes_face_boxes_only_when_the_feature_is_on(monkeypatch, tmp_path):
    """The setting is honoured by the Pipeline, because the engine cannot read it.

    Both directions asserted: with the feature off no detection is attempted at all -- this is a
    decode, and a clip that never had a collision must not pay for one to find out.
    """
    import worker.pipeline as pl

    calls: list = []

    def counting_detect(clip):
        calls.append(clip)
        return [[_face_over_the_bottom_caption()]]

    monkeypatch.setattr(pl.reframe, "detect_faces", counting_detect)

    monkeypatch.setattr(pl.settings, "caption_avoid_faces", False)
    assert pl._caption_face_boxes(tmp_path / "c.mp4", None) == ()
    assert not calls, "faces were detected with the feature off"

    monkeypatch.setattr(pl.settings, "caption_avoid_faces", True)
    boxes = pl._caption_face_boxes(tmp_path / "c.mp4", None)
    assert len(boxes) == 1
    assert isinstance(boxes, tuple), "published boxes must be immutable on the metadata channel"
    assert len(calls) == 1


def test_a_detection_failure_in_the_pipeline_does_not_lose_the_clip(monkeypatch, tmp_path):
    """Every failure mode of the vision stack is a reason to caption where the user asked."""
    import worker.pipeline as pl

    monkeypatch.setattr(pl.settings, "caption_avoid_faces", True)
    monkeypatch.setattr(
        pl.reframe,
        "detect_faces",
        lambda _clip: (_ for _ in ()).throw(RuntimeError("libGL.so.1: cannot open shared object")),
    )

    assert pl._caption_face_boxes(tmp_path / "c.mp4", None) == ()


def test_the_compose_stage_carries_the_boxes_to_the_engine(monkeypatch, tmp_path, make_video):
    """The call site. Deleting `face_boxes=` from `clip_metadata` fails here and nowhere else."""
    import worker.pipeline as pl
    from tests.conftest import options_all_off
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript, TranscriptSegment

    src = make_video("v15_pipeline.mp4", duration=2.0, w=640, h=360)
    words = _words()
    monkeypatch.setattr(
        pl,
        "transcribe",
        lambda *a, **k: Transcript(
            language="en", segments=[TranscriptSegment(0.0, 2.0, "one two three", words)]
        ),
    )
    monkeypatch.setattr(
        pl.sel,
        "select_moments",
        lambda *a, **k: [
            ClipCandidate(start=0.0, end=2.0, score=90.0, reason="r", title="T", text="t")
        ],
    )
    monkeypatch.setattr(pl.settings, "caption_avoid_faces", True)
    monkeypatch.setattr(pl.reframe, "detect_faces", lambda _c: [[_face_over_the_bottom_caption()]])

    seen: dict = {}

    class _StageResult:
        """The minimum surface `run_pipeline` reads back from a stage."""

        media = None
        markers: tuple = ()
        contributions: tuple = ()

    class _Host:
        """An active host that records COMPOSE and stays usable for every other stage.

        The first version of this stub returned `None` from every stage, which made the AUDIO hook
        raise before COMPOSE was ever reached — so `clip_metadata` was never captured, the assertion
        below sat behind a falsy guard, and deleting `face_boxes=` from the Pipeline left the test
        green. A conditional assertion that never runs is worse than no test.
        """

        active = True

        def run_source(self, *a, **k):
            return None

        def run_stage(self, stage, **kwargs):
            if stage is pl.Engine_Stage.COMPOSE:
                seen.update(kwargs)
            return _StageResult()

        def finish_clip(self, *a, **k):
            return ()

        def finish_job(self, *a, **k):
            return None

    monkeypatch.setattr(pl, "Engine_Host", lambda *a, **k: _Host())
    monkeypatch.setattr(pl.compositor, "render_clip", lambda *a, **k: None)

    pl.run_pipeline(
        src,
        options_all_off(aspect="9:16", captions=True),
        clips_dir=tmp_path / "clips",
        temp_dir=tmp_path / "tmp",
    )

    assert seen, "the COMPOSE stage never ran, so this test proves nothing"
    metadata = seen.get("clip_metadata") or {}
    assert metadata.get("face_boxes"), "the COMPOSE stage was published no face boxes"


def test_detection_failure_degrades_to_the_requested_position(tmp_path, monkeypatch):
    """Every failure mode of the vision stack is a reason to caption where the user asked."""
    monkeypatch.setattr(cap.settings, "caption_avoid_faces", True)

    import worker.caption_placement as cp

    def boom(_clip):
        raise RuntimeError("libGL.so.1: cannot open shared object file")

    monkeypatch.setattr(
        cp, "choose_position", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    monkeypatch.setattr("worker.effects.reframe.detect_faces", boom)

    dest, notes = _render(tmp_path, clip_path=tmp_path / "missing.mp4")

    assert _alignment(dest) == 2
    assert "caption_face_detect_failed" in notes
