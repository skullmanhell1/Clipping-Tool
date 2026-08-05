"""Tests for C21, V15, AU9 and O8.

All four fail *silently* when broken, and three of them fail silently when they are working as
written but reasoning from a wrong number:

* **C21** renders tofu boxes. libass reports no error, the ASS file is valid, the encode succeeds,
  and the clip record says ``captions``. The only symptom is in the pixels.
* **V15** puts heavy display type across a speaker's mouth. The crop is right and the captions are
  right; only their combination is wrong.
* **AU9** adds an accent that quietly lowers the speech for the whole clip, if ``amix`` normalises.
* **O8** swaps the encoder and keeps ``-crf``, which VideoToolbox *ignores* - falling back to its own
  default bitrate with no warning.

So the tests below are written to fail if each feature were inert, and to fail for the specific
wrong-but-plausible implementation in each case: coverage decided by ``fc-match`` (which always
answers), a caption moved on every clip rather than only colliding ones, an ``amix`` without
``normalize=0``, and a quality flag that does not match its encoder.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

import pytest

from config import settings
from worker import caption_placement as cp
from worker import captions as cap
from worker import script_support as ss
from worker import video_encoders as ve
from worker.effects import sfx

requires_ffmpeg = pytest.mark.skipif(
    subprocess.run(["which", settings.ffmpeg_binary], capture_output=True).returncode != 0,
    reason="ffmpeg not on PATH",
)
FFMPEG = settings.ffmpeg_binary

ARABIC = "\u0627\u0644\u0633\u0644\u0627\u0645 \u0639\u0644\u064a\u0643\u0645"
HAN = "\u4f60\u597d\u4e16\u754c\u5927\u5bb6"
DEVANAGARI = "\u092f\u0939 \u092c\u0939\u0941\u0924 \u0905\u091a\u094d\u0939\u093e"
CYRILLIC = "\u043f\u0440\u0438\u0432\u0435\u0442 \u043a\u0430\u043a \u0434\u0435\u043b\u0430"
HEBREW = "\u05e9\u05dc\u05d5\u05dd \u05dc\u05db\u05d5\u05dc\u05dd"


# --------------------------------------------------------------------------- #
# C21 - a font that can render what was said
# --------------------------------------------------------------------------- #


def test_c21_coverage_is_read_from_the_font_not_asked_of_fontconfig():
    """``fc-match`` is a *best match*, not a coverage test - it always answers.

    On the machine this was written on ``fc-match ':lang=ar'`` returns
    ``NotoSans[wdth,wght].ttf``, which contains no Arabic at all. Believing that reply is how you
    ship tofu while thinking you handled it.
    """
    noto = cap.FONT_MANIFEST.parent / "fonts" / "NotoSans[wdth,wght].ttf"
    assert noto.is_file()
    # What it really covers...
    assert ss.font_covers(noto, "latin")
    assert ss.font_covers(noto, "cyrillic")
    assert ss.font_covers(noto, "devanagari")
    # ...and what it does not, whatever fontconfig says.
    assert not ss.font_covers(noto, "arabic")
    assert not ss.font_covers(noto, "han")


def test_c21_the_vendored_coverage_gap_is_stated_rather_than_assumed():
    """The plan says "Noto covers CJK". True of the Noto *project*, not of what is vendored.

    CJK lives in Noto Sans CJK, a separate family of roughly 100 MB per weight. Pinning the actual
    gap means the day someone vendors it, this test is what tells them it worked.
    """
    report = ss.coverage_report()
    assert report["latin"], "every vendored face covers Latin"
    assert "Noto Sans" in report["devanagari"]
    assert report["cyrillic"]
    for script in ("arabic", "hebrew", "han", "hiragana", "hangul", "thai"):
        assert report[script] == [], f"{script} is vendored now; update this test and the docs"


def test_c21_a_requested_font_that_covers_the_script_is_kept():
    """A creator's chosen face is a brand decision.

    Overriding it because a clip contains one Greek letter would be worse than the problem.
    """
    plan = ss.plan_for_text(DEVANAGARI, "Poppins Black")
    assert plan.font == "Poppins Black"
    assert plan.marker == ""


def test_c21_a_requested_font_that_cannot_render_the_script_is_substituted():
    plan = ss.plan_for_text(DEVANAGARI, "Anton")
    assert plan.font != "Anton"
    assert ss.font_covers(cap.FONT_MANIFEST.parent / "fonts" / _file_for(plan.font), "devanagari")
    assert plan.marker.startswith("caption_font_substituted:devanagari:")


def _file_for(family: str) -> str:
    import json

    manifest = json.loads(cap.FONT_MANIFEST.read_text(encoding="utf-8"))
    return next(e["file"] for e in manifest["fonts"] if e["name"] == family)


def test_c21_a_vendored_face_is_preferred_over_a_system_one():
    """Two reasons, and the test is what keeps both.

    An offline install has no system fonts to fall back to, so a vendored hit is the only one that
    always works. And the manifest is ordered with the heavy display faces first, so taking the
    first vendored match keeps a substitution as close to the requested look as the coverage allows -
    where falling through to fontconfig would answer a request for Anton with a body face.

    Devanagari is the case that distinguishes them: it is covered by vendored faces *and* by a
    system Noto Sans, so an implementation that skipped the vendored search would still substitute
    something and still look like it worked.
    """
    import json

    manifest = json.loads(cap.FONT_MANIFEST.read_text(encoding="utf-8"))
    expected = next(
        entry["name"]
        for entry in manifest["fonts"]
        if ss.font_covers(cap.FONT_MANIFEST.parent / "fonts" / entry["file"], "devanagari")
    )
    plan = ss.plan_for_text(DEVANAGARI, "Anton")
    assert plan.font == expected, (
        "the substitution did not come from the manifest's first covering face; a system font "
        "was probably used instead"
    )


def test_c21_an_unrenderable_script_is_reported_rather_than_silently_boxed():
    """The point of the whole module.

    Tofu is invisible to every piece of code in the pipeline. Substituting a *different* Latin face
    would not help either, so the requested font is kept and the clip record carries the reason.
    """
    plan = ss.plan_for_text(ARABIC, "Anton")
    assert plan.font == "Anton", "no substitution can help, so the look is left alone"
    assert plan.marker == "caption_script_unsupported:arabic"
    assert plan.can_render is False


def test_c21_latin_text_is_untouched_and_unmarked():
    """The parity case: an English render must be byte-identical."""
    plan = ss.plan_for_text("hello there everyone", "Anton")
    assert plan.font == "Anton"
    assert plan.marker == ""
    assert plan.needs_shaping is False
    assert ss.wrap_style(plan) == 2


def test_c21_a_shaping_script_hands_wrapping_back_to_libass():
    """C6 sums per-glyph advance widths, which is simply wrong where letters join.

    An Arabic word's rendered width is not the sum of its isolated forms. Breaking lines from that
    number is worse than not controlling the breaks at all, so those scripts get ``WrapStyle: 0``.
    """
    for text, script in ((ARABIC, "arabic"), (DEVANAGARI, "devanagari")):
        plan = ss.plan_for_text(text, "Anton")
        assert plan.script == script
        assert plan.needs_shaping is True
        assert ss.wrap_style(plan) == 0


def test_c21_hebrew_is_rtl_but_not_a_shaping_script():
    """Hebrew is unjoined, so advance widths do add up.

    The reordering is libass' problem via FriBidi, not the measurement's - so measured wrapping
    stays on, and `WrapStyle: 2` with it.
    """
    plan = ss.plan_for_text(HEBREW, "Anton")
    assert plan.rtl is True
    assert plan.needs_shaping is False
    assert ss.wrap_style(plan) == 2


def test_c21_build_ass_uses_the_planned_font_and_wrap_style(tmp_path):
    from worker.effects.caption_presets import BUILTIN_PRESETS
    from worker.transcribe import Word

    preset = BUILTIN_PRESETS["hormozi"]
    cases = {
        "hello there everyone": ("Anton", 2, []),
        DEVANAGARI: (None, 0, ["caption_font_substituted"]),
        ARABIC: ("Anton", 0, ["caption_script_unsupported:arabic"]),
        HAN: ("Anton", 2, ["caption_script_unsupported:han"]),
    }
    for text, (expect_font, expect_wrap, expect_notes) in cases.items():
        notes: list[str] = []
        words = [Word(i * 0.4, i * 0.4 + 0.3, w) for i, w in enumerate(text.split())]
        out = cap.build_ass(
            [cap.Cue(0.0, 2.0, words)],
            tmp_path / f"{abs(hash(text))}.ass",
            preset=preset,
            notes=notes,
        )
        body = out.read_text(encoding="utf-8")
        wrap = int(re.search(r"^WrapStyle:\s*(\d+)", body, re.M).group(1))
        font = re.search(r"^Style: [^,]+,([^,]+),", body, re.M).group(1)
        assert wrap == expect_wrap, (text[:12], wrap)
        if expect_font:
            assert font == expect_font, (text[:12], font)
        for fragment in expect_notes:
            assert any(fragment in note for note in notes), (text[:12], notes)


def test_c21_the_shared_preset_is_never_mutated(tmp_path):
    """Presets are frozen and shared across every clip in a job.

    Writing a substituted font onto one would change the font for every later clip - including the
    Latin ones, which never needed it.
    """
    from worker.effects.caption_presets import BUILTIN_PRESETS
    from worker.transcribe import Word

    before = BUILTIN_PRESETS["hormozi"].font
    words = [Word(0.0, 0.4, w) for w in DEVANAGARI.split()]
    cap.build_ass(
        [cap.Cue(0.0, 2.0, words)],
        tmp_path / "x.ass",
        preset=BUILTIN_PRESETS["hormozi"],
        notes=[],
    )
    assert BUILTIN_PRESETS["hormozi"].font == before


def test_c21_punctuation_only_text_is_not_a_script_decision():
    plan = ss.plan_for_text("... !? 123", "Anton")
    assert plan.font == "Anton"
    assert plan.marker == ""


def test_c21_unsupported_scripts_are_enumerable_without_rendering_a_clip():
    """ "Which scripts can this install caption?" was previously unanswerable.

    The only way to find out was to render a clip and look at it.
    """
    missing = ss.unsupported_scripts()
    assert "arabic" in missing
    assert "latin" not in missing


# --------------------------------------------------------------------------- #
# V15 - captions off the mouth
# --------------------------------------------------------------------------- #


@dataclass
class Box:
    """Enough of a FaceBox for placement: the vertical extent."""

    y: int
    h: int
    x: int = 0
    w: int = 100
    t: float = 0.0


H = 1920


def _plan(requested, boxes, **kw):
    kw.setdefault("frame_height", H)
    kw.setdefault("font_size", 84)
    kw.setdefault("max_lines", 2)
    return cp.choose_position(requested, boxes, **kw)


def test_v15_a_caption_clear_of_every_face_is_not_moved():
    """The rule that stops this being a look change.

    A library of clips that had no problem must come back identical.
    """
    plan = _plan("bottom", [Box(y=700, h=400)])
    assert plan.position == "bottom"
    assert plan.moved is False
    assert plan.marker == ""


def test_v15_a_caption_over_the_mouth_is_moved():
    plan = _plan("bottom", [Box(y=1300, h=500)])
    assert plan.position == "top"
    assert plan.moved is True
    assert plan.marker == "caption_moved_off_face:top"


def test_v15_the_horizontal_alignment_is_preserved():
    """Answering "bottom-left covers the mouth" with "centre-top" changes two things to fix one."""
    assert _plan("bottom_left", [Box(y=1300, h=500)]).position == "top_left"
    assert _plan("bottom_right", [Box(y=1300, h=500)]).position == "top_right"


def test_v15_no_faces_means_no_decision_and_no_marker():
    """A clip over music, or a screen recording. Not a failure, and not worth a marker."""
    plan = _plan("bottom", [])
    assert plan.position == "bottom"
    assert plan.marker == ""


def test_v15_when_nothing_clears_the_face_it_changes_nothing_and_says_so():
    """A three-speaker panel occupies every band.

    Moving the caption from one speaker's mouth to another's is not an improvement, so the honest
    outcome is the requested position plus a marker - which is also what distinguishes this from
    "no faces detected".
    """
    panel = [Box(y=150, h=340), Box(y=800, h=340), Box(y=1450, h=340)]
    plan = _plan("bottom", panel)
    assert plan.position == "bottom"
    assert plan.moved is False
    assert plan.marker == "caption_face_overlap_unavoidable"


def test_v15_the_mouth_band_is_the_lower_part_of_the_face_not_the_whole_box():
    """Using the whole box would move captions off any face anywhere near the caption band.

    Which on bottom-framed footage is most of it.
    """
    bands = cp.mouth_bands([Box(y=0, h=1920)], H)
    assert len(bands) == 1
    assert bands[0].bottom == pytest.approx(1.0)
    # The band starts well down the face rather than at its top.
    assert bands[0].top == pytest.approx(1.0 - cp.MOUTH_FRACTION, abs=0.01)
    assert bands[0].top > 0.5


def test_v15_a_nonsense_detection_does_not_produce_a_band_covering_everything():
    """A detector returning a box far outside the frame is a bug in the detector.

    Clamping it would turn that bug into "every caption position collides", which looks like this
    feature failing rather than like the detector failing.
    """
    assert cp.mouth_bands([Box(y=-500, h=200)], H) == []
    assert cp.mouth_bands([Box(y=0, h=0)], H) == []
    assert cp.mouth_bands([Box(y=100, h=9000)], H) == []
    assert cp.mouth_bands([Box(y=100, h=200)], 0) == []


def test_v15_a_tiny_overlap_does_not_move_the_caption():
    """A one-pixel touch is not a legibility problem.

    Treating it as one would move captions on any footage where a face merely reaches the
    safe-area margin.
    """
    band = cp.caption_band("bottom", frame_height=H, font_size=84, max_lines=2)
    # A mouth band that just clips the very top of the caption band.
    nudge = band.height * cp.OVERLAP_TOLERANCE * 0.4
    mouth_bottom = band.top + nudge
    box_h = 300
    y = int((mouth_bottom * H) - box_h)
    plan = _plan("bottom", [Box(y=y, h=box_h)])
    assert plan.moved is False, (plan, band)


def test_v15_a_taller_caption_block_collides_sooner():
    """The band has to account for the line budget.

    A four-line caption reaches much further up the frame than a one-line caption, and reasoning
    about the one-line height would leave the four-line case covering the mouth.
    """
    one = cp.caption_band("bottom", frame_height=H, font_size=84, max_lines=1)
    four = cp.caption_band("bottom", frame_height=H, font_size=84, max_lines=4)
    assert four.top < one.top
    assert four.height > one.height


def test_v15_is_inert_when_disabled(monkeypatch):
    """Off by default: it costs a face pass, and it changes placement on the clips it acts on."""
    assert settings.caption_avoid_faces is False
    plan = cp.plan_for_clip(
        "unused.mp4",
        requested="bottom",
        frame_height=H,
        font_size=84,
        face_boxes=[Box(y=1300, h=500)],
    )
    assert plan.position == "bottom"
    assert plan.marker == ""


def test_v15_acts_when_enabled_using_boxes_the_caller_already_has(monkeypatch):
    """The reframe path detects faces anyway, so on a reframed clip this should cost nothing."""
    monkeypatch.setattr(settings, "caption_avoid_faces", True, raising=False)
    plan = cp.plan_for_clip(
        "unused.mp4",
        requested="bottom",
        frame_height=H,
        font_size=84,
        face_boxes=[Box(y=1300, h=500)],
    )
    assert plan.position == "top"


def test_v15_a_failed_face_pass_captions_where_the_user_asked(monkeypatch):
    """A placement refinement on a clip whose expensive work is already done.

    Every failure mode of the vision stack is a reason to caption where asked, not to lose the clip.
    """
    monkeypatch.setattr(settings, "caption_avoid_faces", True, raising=False)
    from worker.effects import reframe

    monkeypatch.setattr(
        reframe, "detect_faces", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no cv2"))
    )
    plan = cp.plan_for_clip("unused.mp4", requested="bottom", frame_height=H, font_size=84)
    assert plan.position == "bottom"
    assert plan.marker == "caption_face_detect_failed"


# --------------------------------------------------------------------------- #
# AU9 - sound-effect stings
# --------------------------------------------------------------------------- #


def test_au9_a_pop_is_synthesised_because_a_pop_really_is_a_filtered_noise_burst(
    tmp_path, monkeypatch
):
    """No degradation marker, because there is no degradation.

    This is the distinction A15 recorded for the music bed: two sine tones are not music, but a
    band-passed noise burst with a fast attack *is* a pop.
    """
    monkeypatch.setattr(settings, "sfx_dir", tmp_path / "absent", raising=False)
    for name in ("pop", "click"):
        sting, marker = sfx.resolve_sting(name, tmp_path)
        assert sting is not None, name
        assert sting.source == sfx.SOURCE_SYNTHESISED
        assert marker == ""
        assert sting.path.stat().st_size > 1000, "a real wav, not an empty file"


def test_au9_a_whoosh_is_not_synthesised_and_says_why(tmp_path, monkeypatch):
    """A whoosh needs a filter that *moves*, which ffmpeg cannot express in one pass.

    ``bandpass`` takes no expression and has no ``eval=frame``. A static band-passed noise swell is
    a hiss, and shipping a hiss under a name that promises a sweep is exactly the mislabelling A15
    exists to stop - so it is skipped, with a reason.
    """
    monkeypatch.setattr(settings, "sfx_dir", tmp_path / "absent", raising=False)
    for name in ("whoosh", "swipe"):
        sting, marker = sfx.resolve_sting(name, tmp_path)
        assert sting is None, name
        assert marker == f"sfx_missing:{name}"
        assert sfx.synth_filter(name) is None


def test_au9_a_user_file_always_wins_over_the_synthesised_version(tmp_path, monkeypatch):
    """The generated versions exist so the feature works without files, not so they are preferred."""
    directory = tmp_path / "sfx"
    directory.mkdir()
    mine = directory / "pop.wav"
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=0.1",
            str(mine),
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(settings, "sfx_dir", directory, raising=False)
    sting, marker = sfx.resolve_sting("pop", tmp_path)
    assert sting is not None and sting.path == mine
    assert sting.source == sfx.SOURCE_USER_FILE
    assert sting.synthesised is False
    assert marker == ""


def test_au9_a_user_whoosh_makes_the_unsynthesisable_sting_work(tmp_path, monkeypatch):
    directory = tmp_path / "sfx"
    directory.mkdir()
    (directory / "whoosh.wav").write_bytes(b"RIFF....WAVEfmt ")
    monkeypatch.setattr(settings, "sfx_dir", directory, raising=False)
    sting, marker = sfx.resolve_sting("whoosh", tmp_path)
    assert sting is not None
    assert marker == ""


def test_au9_off_is_the_default_and_places_nothing():
    assert settings.sfx_mode == "off"
    assert sfx.plan_hits(emoji_starts=[1.0, 2.0], transition_times=[0.0], duration=5.0) == []


def test_au9_two_stings_too_close_together_become_one():
    """Two accents 100 ms apart read as a stutter, not as two accents."""
    hits = sfx.plan_hits(emoji_starts=[1.0, 1.1, 1.2, 5.0], duration=10.0, mode="emoji")
    assert [round(at, 2) for at, _ in hits] == [1.0, 5.0]


def test_au9_a_transition_wins_a_contested_slot_at_its_own_moment():
    """A cut is structural; an emoji is decoration.

    Applied *within* the gap window, not only at exactly equal times - a single pass keeping the
    earlier candidate would drop a transition at 1.05 s for an emoji at 1.00 s.
    """
    hits = sfx.plan_hits(
        emoji_starts=[1.0, 1.1], transition_times=[1.05], duration=10.0, mode="both"
    )
    assert hits == [(1.05, "transition")]


def test_au9_only_the_selected_triggers_are_placed():
    emoji_only = sfx.plan_hits(
        emoji_starts=[2.0], transition_times=[0.0], duration=10.0, mode="emoji"
    )
    trans_only = sfx.plan_hits(
        emoji_starts=[2.0], transition_times=[0.0], duration=10.0, mode="transitions"
    )
    assert [t for _at, t in emoji_only] == ["emoji"]
    assert [t for _at, t in trans_only] == ["transition"]


def test_au9_a_sting_past_the_end_of_the_clip_is_dropped():
    """It would extend the audio past the video, or be silently trimmed - both wrong."""
    assert sfx.plan_hits(emoji_starts=[1.0, 99.0], duration=10.0, mode="emoji") == [(1.0, "emoji")]
    assert sfx.plan_hits(emoji_starts=[-1.0], duration=10.0, mode="emoji") == []


def test_au9_the_mix_never_lowers_the_speech(tmp_path, monkeypatch):
    """``amix`` normalises by default, dividing every input by the number of inputs.

    So adding one sting would make the *speech* quieter for the whole clip - a global change caused
    by a local accent, and the kind nobody attributes to the feature that caused it.
    """
    monkeypatch.setattr(settings, "sfx_dir", tmp_path / "absent", raising=False)
    pop, _marker = sfx.resolve_sting("pop", tmp_path)
    _args, graph = sfx.build_mix(
        [sfx.SfxHit(1.0, pop, "emoji")], "0:a", "aout", input_offset=1, volume=0.35
    )
    assert "normalize=0" in graph
    # And the clip keeps its own length.
    assert "duration=first" in graph
    # `all=1`, or a stereo sting is delayed on one channel and arrives as a flam.
    assert "adelay=1000:all=1" in graph


def test_au9_no_hits_adds_no_inputs_and_no_graph():
    """A disabled feature must add nothing at all to the command."""
    assert sfx.build_mix([], "0:a", "aout", input_offset=1, volume=0.35) == ([], "")


@requires_ffmpeg
def test_au9_the_mix_graph_is_accepted_and_leaves_the_speech_level_alone(tmp_path, monkeypatch):
    """The level claim has to be measured, not asserted from the flag.

    A string test passes for a graph ffmpeg rejects, and for one that normalises anyway.
    """
    monkeypatch.setattr(settings, "sfx_dir", tmp_path / "absent", raising=False)
    pop, _m = sfx.resolve_sting("pop", tmp_path)
    speech = tmp_path / "speech.wav"
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=300:duration=6",
            "-ar",
            "48000",
            "-ac",
            "1",
            str(speech),
        ],
        check=True,
        capture_output=True,
    )
    args, graph = sfx.build_mix(
        [sfx.SfxHit(1.0, pop, "emoji"), sfx.SfxHit(4.5, pop, "emoji")],
        "0:a",
        "aout",
        input_offset=1,
        volume=0.35,
    )
    out = tmp_path / "mixed.wav"
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(speech),
            *args,
            "-filter_complex",
            graph,
            "-map",
            "[aout]",
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    def rms(path, start, dur):
        proc = subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-nostats",
                "-i",
                str(path),
                "-af",
                f"atrim=start={start}:duration={dur},astats=metadata=1:reset=0",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return float(re.findall(r"RMS level dB:\s*(-?[\d.]+)", proc.stderr)[-1])

    quiet_src, quiet_mix = rms(speech, 3.0, 1.0), rms(out, 3.0, 1.0)
    sting_src, sting_mix = rms(speech, 1.0, 0.2), rms(out, 1.0, 0.2)
    assert abs(quiet_mix - quiet_src) < 0.05, (quiet_src, quiet_mix)
    assert sting_mix > sting_src + 0.2, (sting_src, sting_mix)


# --------------------------------------------------------------------------- #
# O8 - hardware encoding
# --------------------------------------------------------------------------- #


def test_o8_the_default_output_is_byte_identical_to_before():
    """Hardware encoders are not comparable with x264 at the same nominal quality.

    Defaulting to `auto` would change the output of every existing install the first time it landed
    on a machine with a GPU, with no setting changed - and would make the M1 golden renders
    machine-dependent.
    """
    from worker.ffmpeg_utils import h264_args

    assert settings.video_encoder == "libx264"
    assert h264_args() == [
        "-c:v",
        "libx264",
        "-preset",
        str(settings.x264_preset),
        "-crf",
        str(settings.x264_crf),
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "high",
        "-level",
        "4.0",
    ]


def test_o8_each_encoder_gets_its_own_quality_flag_not_crf():
    """`-crf` means nothing outside libx264.

    Passing it to VideoToolbox is *not* an error - it is ignored, and the encoder falls back to its
    own default bitrate. So the mapping has to be a table, and a swap that keeps `-crf` has to be
    detectable.
    """
    expected = {
        "libx264": ["-crf", "20"],
        "h264_nvenc": ["-rc", "vbr", "-cq", "20", "-b:v", "0"],
        "h264_qsv": ["-global_quality", "20"],
        "h264_vaapi": ["-qp", "20"],
    }
    for name, args in expected.items():
        assert ve.KNOWN_ENCODERS[name].quality_args(20) == args, name
    # VideoToolbox is the one that is both differently named *and* inverted.
    assert ve.KNOWN_ENCODERS["h264_videotoolbox"].quality_args(20) == ["-q:v", "61"]


def test_o8_videotoolbox_quality_is_inverted_and_rescaled():
    """1-100 where **higher is better**, against CRF's 0-51 where lower is better.

    Passing a CRF value straight through asks for near-worst quality, and it does so silently.
    """
    vt = ve.KNOWN_ENCODERS["h264_videotoolbox"]
    assert vt.quality_args(0) == ["-q:v", "100"]  # best CRF -> best q
    assert vt.quality_args(51) == ["-q:v", "1"]  # worst CRF -> worst q
    best = int(vt.quality_args(10)[1])
    worst = int(vt.quality_args(40)[1])
    assert best > worst, "the mapping is not inverted"


def test_o8_presets_are_translated_or_omitted():
    """`-preset veryfast` is meaningless to NVENC and an error on VideoToolbox."""
    assert ve.KNOWN_ENCODERS["h264_nvenc"].preset_args("veryfast") == ["-preset", "p2"]
    assert ve.KNOWN_ENCODERS["h264_nvenc"].preset_args("veryslow") == ["-preset", "p7"]
    assert ve.KNOWN_ENCODERS["h264_qsv"].preset_args("ultrafast") == ["-preset", "veryfast"]
    # No preset concept at all: passing one is an "Unrecognized option".
    assert ve.KNOWN_ENCODERS["h264_videotoolbox"].preset_args("veryfast") == []
    assert ve.KNOWN_ENCODERS["libx264"].preset_args("veryfast") == ["-preset", "veryfast"]


def test_o8_nvenc_asks_for_vbr_because_cq_alone_is_ignored():
    """Without `-rc vbr`, `-cq` is accepted and ignored and the encoder uses its default bitrate.

    Which is the same silent-wrong-output failure as passing `-crf` to VideoToolbox.
    """
    args = ve.KNOWN_ENCODERS["h264_nvenc"].quality_args(23)
    assert args[:2] == ["-rc", "vbr"]
    assert "-cq" in args


@requires_ffmpeg
def test_o8_availability_is_a_real_encode_not_a_listing():
    """The distinction is not hypothetical.

    This ffmpeg *lists* ``h264_v4l2m2m`` and fails the moment it is asked for a frame; NVENC does
    the same on a host with the libraries and no card. Reading the list turns a missing GPU into a
    failed job at the point where the transcription has already been paid for.
    """
    ve.reset_probe_cache()
    compiled = ve.compiled_encoders()
    assert "libx264" in compiled, "the software encoder must be present for anything to work"
    assert ve.encoder_available("libx264") is True
    # Compiled in on this build, and unusable.
    if "h264_v4l2m2m" in compiled:
        assert ve.encoder_available("h264_v4l2m2m") is False
    assert ve.encoder_available("definitely_not_an_encoder") is False


@requires_ffmpeg
def test_o8_an_unavailable_named_encoder_falls_back_and_says_so():
    """Silently ignoring an explicit request is how someone spends a week believing their GPU is
    in use."""
    ve.reset_probe_cache()
    choice = ve.resolve_encoder("h264_nvenc")
    assert choice.encoder.name == "libx264"
    assert choice.marker == "encoder_unavailable:h264_nvenc"
    assert choice.degraded is True


def test_o8_an_unknown_encoder_name_falls_back_rather_than_raising():
    """A typo in a setting must not fail a job after the transcription is paid for."""
    ve.reset_probe_cache()
    choice = ve.resolve_encoder("h264_nvnec")
    assert choice.encoder.name == "libx264"
    assert choice.marker == "encoder_unknown:h264_nvnec"


def test_o8_v4l2m2m_is_refused_with_a_reason_rather_than_quietly_absent():
    """It has no constant-quality mode - only `-b:v`.

    Using it would switch the whole pipeline from a quality target to a bitrate target without
    saying so. "Why is my Raspberry Pi encoder not used" deserves an answer.
    """
    encoder = ve.KNOWN_ENCODERS["h264_v4l2m2m"]
    assert encoder.supported is False
    assert "constant-quality" in encoder.unsupported_reason
    choice = ve.resolve_encoder("h264_v4l2m2m")
    assert choice.encoder.name == "libx264"
    assert choice.marker == "encoder_unsupported:h264_v4l2m2m"


@requires_ffmpeg
def test_o8_auto_falls_back_without_claiming_a_degradation():
    """`auto` asked for "the best available", and software *is* available.

    Reporting a degradation for the ordinary case would make the marker meaningless.
    """
    ve.reset_probe_cache()
    choice = ve.resolve_encoder("auto")
    assert choice.encoder.name == "libx264"
    assert choice.marker == ""
    assert choice.degraded is False


@requires_ffmpeg
def test_o8_the_probe_is_cached_so_it_costs_one_encode_per_process():
    """A one-frame encode per call would put a subprocess in front of every clip."""
    ve.reset_probe_cache()
    calls: list[list[str]] = []
    real_run = subprocess.run

    def counting(args, **kwargs):
        calls.append(list(args))
        return real_run(args, **kwargs)

    import worker.video_encoders as module

    original = module.subprocess.run
    module.subprocess.run = counting
    try:
        for _ in range(5):
            ve.encoder_available("libx264")
    finally:
        module.subprocess.run = original
    probe_calls = [c for c in calls if "-frames:v" in c]
    assert len(probe_calls) <= 1, f"probed {len(probe_calls)} times"


@requires_ffmpeg
def test_o8_the_software_argv_actually_encodes(tmp_path):
    """The argv is assembled from three pieces now; a wrong order or a stray flag is a hard error."""
    from worker.ffmpeg_utils import h264_args

    out = tmp_path / "x.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=25:duration=0.5",
            *h264_args(normalise_fps=True),
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert out.stat().st_size > 0


def test_au9_the_synthesisable_flag_and_the_generator_cannot_disagree():
    """Two sources of truth for "can this be synthesised", so they have to be checked against
    each other.

    ``SFX_NAMES`` says whether a sting is synthesisable and ``synth_filter`` actually generates it.
    Flipping the flag for ``whoosh`` without writing a generator would leave the flag claiming
    something the code cannot do - and the failure would be a sting that is silently skipped for a
    reason the marker no longer explains.
    """
    for name, synthesisable in sfx.SFX_NAMES.items():
        description = sfx.synth_filter(name)
        assert (description is not None) == synthesisable, (
            f"{name}: SFX_NAMES says synthesisable={synthesisable} but synth_filter "
            f"{'returns' if description else 'returns no'} a description"
        )


def test_au9_an_unrecognised_mode_behaves_as_off():
    """A typo in a setting must not place stings on every emoji in every clip.

    Silently doing nothing is the right failure here - the alternative is an audible change nobody
    asked for, on a setting they got slightly wrong.
    """
    for mode in ("", "on", "yes", "nonsense", "OFF"):
        assert (
            sfx.plan_hits(emoji_starts=[1.0, 5.0], transition_times=[0.0], duration=10.0, mode=mode)
            == []
        ), mode


def test_o8_the_default_reads_the_setting_rather_than_probing(monkeypatch):
    """`resolve_encoder()` with no argument must consult ``VIDEO_ENCODER``.

    Defaulting to ``auto`` internally would look identical on a machine with no GPU - which is every
    CI runner - and would silently switch to hardware on the one machine where it matters, changing
    output quality with no setting changed and nothing in the clip record to explain it.
    """
    ve.reset_probe_cache()
    monkeypatch.setattr(settings, "video_encoder", "h264_nvenc", raising=False)
    choice = ve.resolve_encoder()
    assert choice.requested == "h264_nvenc", "the setting was not read"
    assert choice.marker == "encoder_unavailable:h264_nvenc"

    monkeypatch.setattr(settings, "video_encoder", "libx264", raising=False)
    assert ve.resolve_encoder().requested == "libx264"
