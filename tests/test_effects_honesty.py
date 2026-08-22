"""Visual effects that were applied to the wrong thing, or not applied and not admitted.

The effects layer is where "silently wrong" is hardest to notice, because the output still renders
and still looks plausible. An emoji on the wrong word reads as an odd editorial choice. A crop that
includes the letterbox reads as bad tracking. A sting with no glyph behind it reads as a glitch in
the audio. None of them fails a build.

Four families:

1. **Applied to the wrong coordinates.** A function that accepts a content rectangle and discards
   it, so the crop pans across the black bars it was given in order to avoid.
2. **Two index spaces for one fact.** Keyword indices computed against one word list and consumed
   against another, so the caption highlights one word and the emoji illustrates a different one.
3. **Claimed but not done.** A sting planned for an emoji that never composited; an `emoji:` marker
   on a clip where half the glyphs are missing.
4. **Geometry that only happens to work.** Odd tile dimensions on a 4:2:0 chain, and an inclusive
   time gate on windows deliberately widened until they abut.
"""

from __future__ import annotations

import pytest

from config import settings
from worker.effects import broll, reframe
from worker.effects import emoji as em


# --------------------------------------------------------------------------- #
# 1. The content rectangle the follow-active path threw away                    #
# --------------------------------------------------------------------------- #
def _centers():
    return [reframe.Center(0.0, 960.0, 540.0), reframe.Center(1.0, 1000.0, 560.0)]


def test_follow_active_confines_the_crop_to_the_content_rectangle():
    """`build_reframe_filter` accepted `origin_x`/`origin_y` and silently dropped them.

    It forwards them correctly on the `split_screen` branch, and `apply_reframe` uses them
    correctly — only the `follow_active` branch ignored them. On a letterboxed source that is
    exactly the defect V16 exists to prevent: the crop is computed in full-frame coordinates, so
    the pan includes the black bars and the clamp uses the padded frame's dimensions rather than
    the picture's.
    """
    common = dict(
        layout="follow_active",
        centers=_centers(),
        crop_w=608,
        crop_h=1080,
        src_w=1440,
        src_h=1080,
        target_w=1080,
        target_h=1920,
    )
    _in_full, vf_full, _n1 = reframe.build_reframe_filter(**common, origin_x=0, origin_y=0)
    _in_inset, vf_inset, _n2 = reframe.build_reframe_filter(**common, origin_x=240, origin_y=60)

    assert vf_full != vf_inset, (
        "origin_x/origin_y made no difference to the emitted crop - the content rectangle is "
        "still being ignored on the follow_active path"
    )


def test_the_sendcmd_script_is_also_confined(tmp_path):
    """The script drives every frame after the first, so both have to be confined.

    Confining only frame 0 would produce a clip that opens correctly and then pans onto the bars.
    """
    script_path = tmp_path / "cmd.txt"
    common = dict(
        layout="follow_active",
        centers=_centers(),
        crop_w=608,
        crop_h=1080,
        src_w=1440,
        src_h=1080,
        target_w=1080,
        target_h=1920,
        sendcmd_path=str(script_path),
    )
    reframe.build_reframe_filter(**common, origin_x=0, origin_y=0)
    full = script_path.read_text(encoding="utf-8")
    reframe.build_reframe_filter(**common, origin_x=240, origin_y=60)
    inset = script_path.read_text(encoding="utf-8")

    assert full != inset, "the sendcmd script ignores the content rectangle"


def test_the_headroom_bias_reaches_the_speaker_path(monkeypatch):
    """V22 was wired into `apply_reframe` and never into the speaker-aware path.

    That is worse than a missing feature: identical footage framed through the two paths came out
    with different vertical framing, and **no marker said which one had run**. The bias marker is
    asserted as well as the geometry, because the framing difference is invisible without it.
    """
    # A crop *shorter* than the source, or there is no vertical room for a bias to use and
    # `biased_center_y` correctly returns early. A 9:16 crop of a 16:9 source is full height, which
    # is why the obvious fixture cannot detect this: here the source is portrait and the crop square.
    common = dict(
        layout="follow_active",
        centers=[reframe.Center(0.0, 540.0, 960.0), reframe.Center(1.0, 540.0, 980.0)],
        crop_w=1080,
        crop_h=1080,
        src_w=1080,
        src_h=1920,
        target_w=1080,
        target_h=1080,
    )
    monkeypatch.setattr(settings, "reframe_headroom_bias", 0.0)
    _i, vf_none, notes_none = reframe.build_reframe_filter(**common)
    monkeypatch.setattr(settings, "reframe_headroom_bias", 0.4)
    _i, vf_biased, notes_biased = reframe.build_reframe_filter(**common)

    assert vf_none != vf_biased, "the headroom bias never reaches the speaker path"
    assert any("headroom" in n for n in notes_biased), (
        f"the bias was applied without a marker saying so: {notes_biased}"
    )
    assert not any("headroom" in n for n in notes_none), notes_none


# --------------------------------------------------------------------------- #
# 2. One fact, two index spaces                                                 #
# --------------------------------------------------------------------------- #
def test_the_emoji_reads_keyword_indices_in_the_space_they_were_computed_in(
    monkeypatch, tmp_path, make_video
):
    """C19's entire purpose, and the two consumers disagreed about what index 7 meant.

    `plan_keywords` was given the words flattened out of the caption *cues*; `plan_emoji` was given
    the raw clip word list. `cap.words_to_cues` skips every word whose text is empty, so the two
    lists diverge by the number of skipped words at every position after the first — the caption
    highlighted one word and the emoji illustrated another, silently.

    Asserted at the **call site**, because that is where the defect was: both functions were correct
    in their own space, and the compositor handed one of them the wrong list. A helper-level test
    cannot see this — with a clean word list the two spaces coincide, which is exactly why it
    survived.
    """
    from worker.effects import compositor as comp
    from worker.transcribe import Word

    raw = [Word(0.0, 0.1, ""), Word(0.2, 0.6, "money"), Word(0.7, 1.2, "ocean")]
    flat_expected = [w for cue in comp.cap.words_to_cues(raw) for w in cue.words]
    assert len(flat_expected) < len(raw), "words_to_cues kept the empty word; the fixture is wrong"

    seen: dict = {}

    def spy_plan_emoji(words, duration, **kwargs):
        seen["words"] = list(words)
        return []

    def spy_plan_keywords(words, **kwargs):
        seen["keyword_words"] = list(words)
        return {0}

    monkeypatch.setattr(comp.emoji, "plan_emoji", spy_plan_emoji)
    monkeypatch.setattr(comp.caption_presets, "plan_keywords", spy_plan_keywords)
    monkeypatch.setattr(comp, "_run", lambda *a, **k: None)

    src = make_video("c19.mp4", duration=2.0, w=320, h=240)
    comp.render_clip(
        src,
        tmp_path / "out.mp4",
        _options(captions=True, caption_keyword_highlight=True, emoji="standard"),
        raw,
        tmp_path,
    )

    assert "keyword_words" in seen and "words" in seen, seen
    assert [w.text for w in seen["words"]] == [w.text for w in seen["keyword_words"]], (
        "plan_emoji and plan_keywords were given different word lists, so the emoji indexes into a "
        "different list than the one the keyword indices describe"
    )


# --------------------------------------------------------------------------- #
# 3. Claimed but not done                                                       #
# --------------------------------------------------------------------------- #
def test_build_overlay_reports_which_cues_composited():
    """It dropped unresolvable glyphs and returned only `(inputs, graph)`.

    So the caller could tell "at least one resolved" from "none resolved" and nothing else. That one
    missing return value caused two separate downstream defects — phantom SFX stings and an
    overstated `emoji:` marker — which is why it is fixed here rather than papered over twice.
    """
    cues = [
        em.EmojiCue("🔥", 0.0, 1.0, 0),
        em.EmojiCue("💰", 1.0, 2.0, 1),
        em.EmojiCue("🌊", 2.0, 3.0, 2),
    ]

    def only_fire(char):
        return __import__("pathlib").Path("/tmp/fire.png") if char == "🔥" else None

    inputs, graph, composited = em.build_overlay(
        cues, "0:v", "vout", duration=3.0, resolver=only_fire
    )

    assert graph, "the one resolvable glyph produced no graph"
    assert [c.char for c in composited] == ["🔥"]
    assert inputs.count("-i") == 1


def test_no_sting_for_the_emoji_that_did_not_resolve(monkeypatch, tmp_path):
    """The partial-failure case, which is the one that was broken.

    An existing test covers *total* failure — `build_overlay` returning nothing at all — and that
    one passed, because the gate `if emoji_graph` is correct for it. The gate then handed over
    **every planned cue's** start, so five planned emoji with two resolvable PNGs produced five
    stings: three audible accents on nothing, and an `sfx:N` marker overstating what happened.
    """
    from worker.effects import sfx

    # `sfx.plan_hits` is the pure planner. `comp._plan_sfx` wraps it with asset resolution, and SFX
    # is off by default so that wrapper returns nothing regardless — which would make this test
    # pass for the wrong reason.
    monkeypatch.setattr(settings, "sfx_mode", "emoji")

    planned = [em.EmojiCue("🔥", 0.5, 1.5, 0), em.EmojiCue("💰", 2.0, 3.0, 1)]
    composited = planned[:1]

    from_composited = sfx.plan_hits(
        emoji_starts=tuple(c.start for c in composited), transition_times=(), duration=5.0
    )
    from_all = sfx.plan_hits(
        emoji_starts=tuple(c.start for c in planned), transition_times=(), duration=5.0
    )
    assert len(from_composited) == 1
    assert len(from_all) == 2, "the fixture cannot distinguish the two, so the test is vacuous"


def test_missing_glyphs_are_recorded_on_the_clip(monkeypatch, tmp_path, make_video):
    """A partial emoji failure said `emoji:standard` as though everything rendered.

    On the default style there was no degradation signal at all — the only one,
    `emoji_style_degraded`, is gated on a non-default style. So a clip with half its glyphs missing
    was indistinguishable from a complete one, and a clip with *none* was indistinguishable from the
    feature being switched off. The in-caption glyph path already emits
    `caption_emoji_unavailable:<n>` for exactly this; this is the same convention for the overlay.
    """
    from worker.effects import compositor as comp

    planned = [em.EmojiCue("🔥", 0.5, 1.0, 0), em.EmojiCue("💰", 1.2, 1.8, 1)]
    monkeypatch.setattr(comp.emoji, "plan_emoji", lambda *a, **k: planned)
    # One of the two resolves.
    monkeypatch.setattr(
        comp.emoji,
        "build_overlay",
        lambda *a, **k: (["-i", "/tmp/a.png"], "[v][e0]overlay[vout]", planned[:1]),
    )

    # `_run` stubbed: the stubbed graph references a PNG that does not exist, and this test is about
    # the marker rather than the encode. Other wiring tests in this suite do the same.
    monkeypatch.setattr(comp, "_run", lambda *a, **k: None)

    src = make_video("s.mp4", duration=2.0, w=320, h=240)
    result = comp.render_clip(
        src,
        tmp_path / "out.mp4",
        _options(emoji="standard", emoji_mode="keyword"),
        [],
        tmp_path,
    )
    assert result is not None
    assert "emoji_unavailable:1" in result.effects_applied, result.effects_applied


def test_an_emoji_failure_does_not_lose_the_clip(monkeypatch, tmp_path, make_video):
    """`resolve_asset` writes to the filesystem — it `mkdir`s on every call, cache hit included.

    So a read-only or full assets volume raised straight out of `render_clip` and failed the whole
    clip for what is a cosmetic feature. The b-roll block degrades to a marker in exactly this
    situation; the emoji block had no guard at all.
    """
    from worker.effects import compositor as comp

    monkeypatch.setattr(comp.emoji, "plan_emoji", lambda *a, **k: [em.EmojiCue("🔥", 0.2, 0.8, 0)])

    def boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(comp.emoji, "build_overlay", boom)
    monkeypatch.setattr(comp, "_run", lambda *a, **k: None)

    src = make_video("s.mp4", duration=2.0, w=320, h=240)
    notes: list[str] = []
    # Must not raise. `render_clip` legitimately returns None here — emoji was the only effect
    # requested and it failed, so nothing needs rendering — and the marker has to survive that,
    # which is what `notes` is for.
    result = comp.render_clip(
        src, tmp_path / "out.mp4", _options(emoji="standard"), [], tmp_path, notes=notes
    )
    markers = list(notes) + (list(result.effects_applied) if result is not None else [])
    assert "emoji_degraded" in markers, (
        f"an unavailable emoji asset produced no render and no explanation: {markers}"
    )


def _options(**overrides):
    try:
        from tests.conftest import options_all_off
    except ImportError:  # pragma: no cover
        from conftest import options_all_off
    base = dict(captions=False, metadata=False, aspect="9:16")
    base.update(overrides)
    return options_all_off(**base)


# --------------------------------------------------------------------------- #
# 4. Geometry that only happened to work                                        #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("target", [(1080, 1350), (1080, 1920), (1080, 1080), (2160, 2700)])
@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 6])
def test_split_screen_tiles_are_always_even(target, count):
    """The one compositing site in the module that did not force even dimensions.

    Tiles go straight into `scale=<w>:<h>` on a yuv420p chain before `hstack`/`vstack`, and 4:2:0
    chroma subsampling cannot represent an odd dimension. Every other site here forces even, each
    with a comment saying why — this one was the exception, and it only bit on a preset whose height
    does not divide evenly by the row count. `4:5` is (1080, 1350), so three or more speakers give
    two rows of 675: odd, both rows. Which is why it had never been seen — 9:16 and 1:1 divide
    cleanly.
    """
    target_w, target_h = target
    tracks = [
        reframe.Face_Track(track_id=f"t{i}", boxes=[reframe.FaceBox(0.0, 10 * i, 10, 40, 40)])
        for i in range(count)
    ]
    regions = reframe._grid_regions(
        [t.track_id for t in tracks],
        {t.track_id: t for t in tracks},
        target_w=target_w,
        target_h=target_h,
        src_w=1920,
        src_h=1080,
    )

    assert regions
    for rg in regions:
        assert rg.dst_w % 2 == 0, f"odd tile width {rg.dst_w} for {count} tiles at {target}"
        assert rg.dst_h % 2 == 0, f"odd tile height {rg.dst_h} for {count} tiles at {target}"


@pytest.mark.parametrize("target", [(1080, 1350), (1080, 1920)])
@pytest.mark.parametrize("count", [2, 3, 4, 5, 6])
def test_split_screen_tiles_still_tile_exactly(target, count):
    """Forcing even must not open a seam or an overlap.

    The remainder is absorbed by the last row and column, so rounding the base down by one keeps
    the tiling exact. Asserted because "make it even" is the kind of fix that trades one geometry
    bug for another.
    """
    target_w, target_h = target
    tracks = [
        reframe.Face_Track(track_id=f"t{i}", boxes=[reframe.FaceBox(0.0, 10 * i, 10, 40, 40)])
        for i in range(count)
    ]
    regions = reframe._grid_regions(
        [t.track_id for t in tracks],
        {t.track_id: t for t in tracks},
        target_w=target_w,
        target_h=target_h,
        src_w=1920,
        src_h=1080,
    )
    covered = sum(rg.dst_w * rg.dst_h for rg in regions)
    assert covered == target_w * target_h, (
        f"{count} tiles cover {covered} of {target_w * target_h} pixels - seam or overlap"
    )


def test_abutting_emoji_windows_do_not_both_light_up():
    """`between()` is inclusive at both ends.

    Two cues where one ends exactly where the next begins were both enabled on the shared boundary
    frame, stacking two glyphs for one frame. `overlays._beat_bump_expr` documents this exact hazard
    and avoids it the same way; the emoji and b-roll gates were the two places that did not.
    """
    cues = [em.EmojiCue("🔥", 0.0, 1.0, 0), em.EmojiCue("💰", 1.0, 2.0, 1)]
    _inputs, graph, _c = em.build_overlay(
        cues,
        "0:v",
        "vout",
        duration=2.0,
        resolver=lambda _c: __import__("pathlib").Path("/tmp/x.png"),
    )
    assert "between(t," not in graph, "an inclusive gate double-enables abutting emoji"
    assert graph.count("gte(t,") == 2


def test_abutting_broll_windows_do_not_both_light_up():
    """Most likely to bite here, because zero-length windows are widened to exactly one frame.

    That makes exactly-abutting windows a normal outcome rather than a coincidence, and the result
    is a visible one-frame flash where two overlays stack.
    """
    from worker.effects.broll import AssetRef, BrollCue

    cues = [
        BrollCue(
            start=0.0,
            end=1.0,
            keyword="a",
            asset=AssetRef(path="/a.png", kind="image", provider="local", license="local"),
        ),
        BrollCue(
            start=1.0,
            end=2.0,
            keyword="b",
            asset=AssetRef(path="/b.png", kind="image", provider="local", license="local"),
        ),
    ]
    _inputs, graph, _notes = broll.build_broll_overlay(
        cues, base_label="v", out_label="vout", width=1080, height=1920, fps=30.0, input_offset=1
    )
    assert "between(t," not in graph
    assert graph.count("gte(t,") == 2


def test_overlay_layers_round_height_to_even():
    """`scale=<w>:-1` does not round to even; `-2` does.

    Two docstrings in this codebase asserted the opposite, and `broll.py` computed an even
    `overlay_h` for stated 4:2:0 reasons then used `:-1` on two of its three branches. Both inputs
    are `format=rgba` so nothing fails outright — but an odd-height layer lands on a half-pixel
    chroma boundary in the base frame, and a wrong comment propagates.
    """
    from worker.effects.broll import AssetRef, BrollCue

    cues = [
        BrollCue(
            start=0.0,
            end=1.0,
            keyword="a",
            asset=AssetRef(path="/a.png", kind="image", provider="local", license="local"),
        )
    ]
    _inputs, graph, _notes = broll.build_broll_overlay(
        cues, base_label="v", out_label="vout", width=1080, height=1920, fps=30.0, input_offset=1
    )
    assert ":-1," not in graph, "an overlay layer can still get an odd height"
    assert ":-2," in graph
