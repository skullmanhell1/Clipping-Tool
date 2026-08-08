"""Tests for C10, C12, C13, C15, C22, A4, A11, A12, A16 and A20.

Every item in this batch replaces a hard-coded value with a choice, so the tests that matter most
are the ones asserting the **default is unchanged**. A new field with a slightly different default
does not fail near the change - it fails in the v0.8.0 parity gate as an unexplained golden
mismatch, hours later and in a file that has nothing to do with the feature.

The second theme is masking and de-duplication, where the costly error is the *false positive*: a
masked word inside an innocent one, or a dropped emoji that was the only one the clip had. Those
are asserted directly rather than inferred.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from config import settings
from worker import captions as cap
from worker.effects import audio, broll
from worker.effects import emoji as em
from worker.effects.caption_presets import BUILTIN_PRESETS, CaptionPreset


class W:
    def __init__(self, start, end, text, probability=1.0):
        self.start, self.end, self.text, self.probability = start, end, text, probability


# --------------------------------------------------------------------------- #
# C12 - platform safe areas
# --------------------------------------------------------------------------- #
def test_the_generic_profile_reproduces_the_old_margins_exactly():
    """Not approximately.

    A first attempt used rounder fractions and came out one pixel off, which is exactly the
    difference that surfaces later as an unexplained golden-file mismatch.
    """
    assert cap.safe_area_margins(1080, 1920, "none") == {
        "top": 200,
        "bottom": 220,
        "side": 80,
    }


def test_the_default_style_line_is_unchanged_when_nothing_is_configured():
    """The safe area is a choice being offered, not a silent reframing of every clip."""
    assert cap.resolve_margins("bottom", 1080, 1920) == (80, 80, 220)
    assert cap.resolve_margins("top", 1080, 1920) == (80, 80, 200)


def test_insets_are_fractions_so_they_hold_at_every_resolution():
    """O9 renders the same clip at 720 through 2160; a pixel margin would mean a different
    physical inset at each."""
    small = cap.safe_area_margins(720, 1280, "tiktok")
    large = cap.safe_area_margins(2160, 3840, "tiktok")
    assert large["bottom"] / 3840 == pytest.approx(small["bottom"] / 1280, abs=0.002)


def test_the_bottom_inset_is_larger_than_the_top_on_every_platform():
    """That is where the caption, username and action rail all live."""
    for name, profile in cap.SAFE_AREA_INSETS.items():
        if name == "none":
            continue
        assert profile["bottom"] > profile["top"], name


def test_a_centred_caption_gets_no_vertical_margin():
    """ASS reads MarginV as a distance from an edge; for alignments 4-6 it means nothing, so
    setting one would silently do nothing - worse than not offering it."""
    assert cap.resolve_margins("center", 1080, 1920, platform="tiktok")[2] == 0


def test_an_unknown_platform_falls_back_to_the_generic_profile():
    assert cap.safe_area_margins(1080, 1920, "myspace") == cap.safe_area_margins(1080, 1920, "none")


def test_a_negative_offset_cannot_push_text_into_the_chrome():
    """The one direction no caller should be able to ask for by accident."""
    assert cap.resolve_margins("bottom", 1080, 1920, offset=-500)[2] == 220


def test_a_positive_offset_moves_the_caption_away_from_the_edge():
    assert cap.resolve_margins("bottom", 1080, 1920, offset=40)[2] == 260


def test_a_degenerate_frame_size_does_not_divide_by_zero():
    assert cap.safe_area_margins(0, 0, "tiktok")["bottom"] >= 0


# --------------------------------------------------------------------------- #
# C13 - positions
# --------------------------------------------------------------------------- #
def test_the_three_original_positions_keep_their_names_and_alignments():
    """A client that only knows the old three must be unaffected."""
    assert cap.VALID_CAPTION_POSITIONS[:1] == ("bottom",)
    for name in ("bottom", "center", "top"):
        assert name in cap.VALID_CAPTION_POSITIONS


def test_there_are_nine_positions_covering_every_corner():
    assert len(cap.VALID_CAPTION_POSITIONS) == 9
    aligns = {cap._POSITION_ALIGN[p][0] for p in cap.VALID_CAPTION_POSITIONS}
    assert aligns == set(range(1, 10)), aligns


# --------------------------------------------------------------------------- #
# C15 - letter-spacing and glyph scale
# --------------------------------------------------------------------------- #
def test_the_default_metrics_are_the_previous_literals():
    line = cap._preset_style_line(CaptionPreset(name="t"), "Anton", 96, 2, 220)
    assert ",100,100,0,0," in line, line


def test_spacing_and_scale_reach_the_style_line():
    preset = CaptionPreset(name="t", spacing=4, scale_x=90, scale_y=105)
    line = cap._preset_style_line(preset, "Anton", 96, 2, 220)
    assert ",90,105,4,0," in line, line


def test_an_out_of_range_scale_is_clamped():
    """A huge value pushes glyphs off frame and a tiny one makes text unreadable; both look like
    a rendering fault rather than a bad setting."""
    line = cap._preset_style_line(
        CaptionPreset(name="t", scale_x=2, scale_y=100000), "Anton", 96, 2, 220
    )
    assert f",{cap.MIN_GLYPH_SCALE},{cap.MAX_GLYPH_SCALE}," in line, line


def test_a_zero_scale_means_unset_rather_than_invisible():
    """A caller writing 0 means "leave the metrics alone"; rendering their captions at a tenth
    of width would be a strange reading of that."""
    line = cap._preset_style_line(
        CaptionPreset(name="t", scale_x=0, scale_y=0), "Anton", 96, 2, 220
    )
    assert ",100,100," in line, line


def test_junk_metrics_do_not_break_the_style_line():
    line = cap._preset_style_line(
        CaptionPreset(name="t", scale_x="wide", scale_y=None, spacing="tight"),
        "Anton",
        96,
        2,
        220,
    )
    assert ",100,100,0," in line, line


# --------------------------------------------------------------------------- #
# C10 - active-word punch
# --------------------------------------------------------------------------- #
def test_the_punch_is_off_by_default():
    assert CaptionPreset(name="t").punch_scale == 0.0
    assert cap._punch_span(CaptionPreset(name="t"), 0) == ""


def test_the_punch_is_available_on_any_animation_not_only_pop():
    """The whole point of C10: 'karaoke sweep plus a punch on the active word' was previously
    inexpressible, because the punch was reachable only by choosing the pop *animation*, which
    replaced the animation the preset wanted."""
    preset = CaptionPreset(name="t", animation="karaoke_fill", punch_scale=0.2)
    span = cap.build_word_span(W(1.0, 1.4, "money"), preset, False, cue_start=1.0)
    assert "\\kf" in span, "the karaoke sweep was lost"
    assert "\\fscx120" in span, "the punch was not applied"


def test_the_punch_ramps_down_so_the_accent_lands_on_the_syllable():
    """Ramping up would peak after the word had been said, which reads as lag."""
    span = cap._punch_span(CaptionPreset(name="t", punch_scale=0.25), 320)
    assert span.startswith("{\\fscx125\\fscy125")
    assert "\\fscx100\\fscy100)" in span


def test_the_punch_defers_to_the_highlight_on_an_emphasised_word():
    """Two competing \\fscx spans on one word would fight, and which applied would depend on tag
    order rather than on intent."""
    preset = CaptionPreset(name="t", punch_scale=0.4)
    span = cap.build_word_span(W(1.0, 1.4, "money"), preset, True, cue_start=1.0)
    assert "\\fscx140" not in span
    assert "\\c" in span


def test_the_punch_is_clamped_and_survives_junk():
    assert "\\fscx200" in cap._punch_span(CaptionPreset(name="t", punch_scale=5.0), 0)
    assert cap._punch_span(CaptionPreset(name="t", punch_scale=-1.0), 0) == ""


def test_the_new_preset_fields_round_trip():
    """A field missing from to_dict/from_dict is silently lost by every path that persists a
    preset - it appears to work until reload."""
    preset = CaptionPreset(
        name="t", punch_scale=0.3, punch_ms=90, spacing=3, scale_x=95, scale_y=102
    )
    assert CaptionPreset.from_dict(preset.to_dict()) == preset
    for name, builtin in BUILTIN_PRESETS.items():
        assert CaptionPreset.from_dict(builtin.to_dict()) == builtin, name


# --------------------------------------------------------------------------- #
# C22 - profanity masking
# --------------------------------------------------------------------------- #
def test_masking_is_off_by_default():
    """Burned captions are permanent, and a creator whose voice is profane should not be
    censored by their own tool."""
    assert settings.caption_mask_profanity is False


def test_a_masked_word_keeps_its_first_letter_its_length_and_its_punctuation():
    """A fully blanked word makes the caption unreadable, which defeats having captions."""
    assert cap.mask_profanity("fucking!") == "f******!"
    assert cap.mask_profanity("Shit,") == "S***,"


@pytest.mark.parametrize(
    "innocent", ["classic", "Scunthorpe", "assess", "cocktail", "bitter", "shitake", "hello"]
)
def test_innocent_words_are_never_masked(innocent):
    """Substring matching produces exactly these, and a masked word inside an innocent one is
    far more conspicuous than an unmasked profanity - the viewer can see the tool got it wrong."""
    assert cap.is_profane(innocent) is False
    assert cap.mask_profanity(innocent) == innocent


def test_masking_is_case_insensitive():
    assert cap.mask_profanity("SHIT") == "S***"


def test_masking_reaches_the_rendered_span(monkeypatch):
    monkeypatch.setattr(settings, "caption_mask_profanity", True)
    span = cap.build_word_span(W(0.0, 0.4, "shit"), CaptionPreset(name="t"), False)
    assert "s***" in span.lower()
    assert "shit" not in span.lower()


def test_masking_changes_only_the_drawn_text(monkeypatch):
    """Timings, emphasis and emoji all read the original word: a masked word must not become a
    different word to the rest of the pipeline."""
    monkeypatch.setattr(settings, "caption_mask_profanity", True)
    word = W(1.0, 1.5, "shit")
    span = cap.build_word_span(
        word, CaptionPreset(name="t", animation="karaoke_fill"), False, cue_start=1.0
    )
    assert word.text == "shit", "the word object was mutated"
    assert "\\kf50" in span, "the timing changed with the text"


def test_masking_off_leaves_the_span_byte_identical(monkeypatch):
    monkeypatch.setattr(settings, "caption_mask_profanity", False)
    preset = CaptionPreset(name="t")
    assert cap.build_word_span(W(0.0, 0.4, "shit"), preset, False) == cap.build_word_span(
        W(0.0, 0.4, "shit"), preset, False
    )
    assert "shit" in cap.build_word_span(W(0.0, 0.4, "shit"), preset, False)


# --------------------------------------------------------------------------- #
# A4 - the font list
# --------------------------------------------------------------------------- #
def test_the_vendored_faces_are_exposed():
    """Twelve faces shipped with licences and a manifest, and nothing exposed them - so the only
    way to change a caption font was to edit a preset in source."""
    fonts = cap.available_fonts()
    assert fonts, "no fonts exposed"
    assert {"name", "family", "weight", "heavy", "license"} <= set(fonts[0])


def test_variable_fonts_are_excluded():
    """libass' fontsdir provider cannot select a named instance of a variable font - the request
    silently resolves to something else, which is the C1 defect. Offering a font that will not
    render is worse than offering fewer."""
    manifest = json.loads(cap.FONT_MANIFEST.read_text())
    variable = {e["name"] for e in manifest["fonts"] if e.get("variable")}
    exposed = {f["name"] for f in cap.available_fonts()}
    assert not (variable & exposed), variable & exposed


def test_every_exposed_font_exists_on_disk():
    """The manifest is a declaration; a CI step exists because the two once disagreed."""
    manifest = json.loads(cap.FONT_MANIFEST.read_text())
    by_name = {e["name"]: e for e in manifest["fonts"]}
    # Bundled only: an operator-supplied face (A5) is discovered from disk and has no manifest
    # entry to look up by design.
    for font in cap.available_fonts():
        if font["source"] != "bundled":
            continue
        path = cap.FONT_MANIFEST.parent / "fonts" / by_name[font["name"]]["file"]
        assert path.is_file(), path


def test_heavy_faces_are_listed_first():
    """A caption picker's first suggestions should be the faces the look actually needs."""
    heavy = [f["heavy"] for f in cap.available_fonts()]
    assert heavy == sorted(heavy, reverse=True)


def test_a_missing_manifest_falls_back_to_the_files_on_disk(monkeypatch, tmp_path):
    """A missing manifest must not raise, and since A5 it need not empty the picker either.

    Before A5 a corrupt or missing ``fonts.json`` removed every face from the picker while all
    twelve files sat present and readable in ``assets/fonts`` - the declaration failing took the
    assets down with it. Discovery reads the files themselves, so the manifest is now an
    *enrichment* (licence, ``use`` note, hand-marked display weights) rather than the only route.
    """
    monkeypatch.setattr(cap, "FONT_MANIFEST", Path("/nonexistent/fonts.json"))
    fonts = cap.available_fonts()
    assert fonts, "the vendored files are still on disk and readable"
    assert all(font["source"] == "user" for font in fonts)
    # Read from each file's own `name` table, so these are names libass can actually resolve.
    assert "Anton" in {font["name"] for font in fonts}

    # And with no font directory at all, empty rather than an exception.
    monkeypatch.setattr(cap.settings, "font_assets_dir", tmp_path / "gone", raising=False)
    cap._FONT_DIR_STATE.clear()
    assert cap.available_fonts() == []


# --------------------------------------------------------------------------- #
# A11 / A12 - emoji selection
# --------------------------------------------------------------------------- #
def test_the_emoji_goes_to_the_more_salient_word():
    """A11. The old rule was purely temporal: the first mapped word after each interval won,
    whether or not it mattered."""
    words = [W(0.2, 0.6, "best"), W(0.7, 1.1, "money")]
    cues = em.plan_emoji(words, 20.0, intensity="subtle")
    assert [c.char for c in cues] == [em.KEYWORD_EMOJI["money"]]


def test_no_glyph_is_repeated_within_a_clip():
    """A12. Eight mentions of one topic get one emoji, not eight - the same emoji twice is the
    most obvious way an automatic overlay looks automatic."""
    words = [W(i * 4.0, i * 4.0 + 0.3, "fire") for i in range(8)]
    assert len(em.plan_emoji(words, 40.0, intensity="heavy")) == 1


def test_the_count_is_capped_independently_of_spacing():
    """A12. Spacing scales with clip length, so a three-minute clip could satisfy every gap and
    still carry sixty emoji."""
    keywords = list(em.KEYWORD_EMOJI)[:80]
    words = [W(i * 2.5, i * 2.5 + 0.3, k) for i, k in enumerate(keywords)]
    cues = em.plan_emoji(words, 200.0, intensity="heavy")
    assert 0 < len(cues) <= em._emoji_cap("heavy", 200.0)


def test_an_enabled_intensity_always_yields_at_least_one():
    """A cap that rounded to zero would make a switched-on feature do nothing."""
    for intensity in ("subtle", "standard", "heavy"):
        assert em._emoji_cap(intensity, 1.0) >= 1


def test_cues_stay_chronological_and_deterministic():
    """Ranking happens by salience, but the emitted list must still be in time order - and a
    pure function of its input, which the kinetic determinism properties depend on."""
    words = [W(0.5, 0.9, "money"), W(9.0, 9.4, "fire"), W(18.0, 18.4, "win")]
    cues = em.plan_emoji(words, 30.0, intensity="heavy")
    assert [c.start for c in cues] == sorted(c.start for c in cues)
    assert em.plan_emoji(words, 30.0, intensity="heavy") == cues


def test_intensity_off_and_no_words_still_yield_nothing():
    assert em.plan_emoji([W(0.5, 0.9, "money")], 30.0, intensity="off") == []
    assert em.plan_emoji([], 30.0) == []


def test_salience_survives_hostile_word_objects():
    class Bad:
        start, end, text = "nope", None, "money"

    assert em.plan_emoji([Bad()], 30.0) == []


# --------------------------------------------------------------------------- #
# A16 - fitting the bed to the clip
# --------------------------------------------------------------------------- #
def test_a_bed_is_looped_then_trimmed_to_the_clip():
    """A track shorter than the clip previously just stopped part-way through and the rest
    played dry - silence appearing mid-clip, which reads as a fault."""
    chain = audio.bed_fit_filter("1:a", "bed", 30.0)
    assert "aloop=loop=-1" in chain
    assert "atrim=0:30.000" in chain
    assert chain.index("aloop") < chain.index("atrim"), "trimmed before looping"


def test_the_timestamps_are_reset_after_looping():
    """Without this the looped audio keeps the source's own PTS and the mix drifts."""
    chain = audio.bed_fit_filter("1:a", "bed", 30.0)
    assert "asetpts=N/SR/TB" in chain
    assert chain.index("atrim") < chain.index("asetpts")


def test_the_out_fade_lands_on_the_clip_ending():
    chain = audio.bed_fit_filter("1:a", "bed", 30.0)
    assert f"afade=t=out:st={30.0 - audio.BED_FADE_OUT_S:.3f}" in chain


def test_a_short_clip_gets_proportionally_shorter_fades():
    """Overlapping fades multiply and would pull the bed towards silence for the whole clip."""
    chain = audio.bed_fit_filter("1:a", "bed", 1.0)
    fade_in = float(chain.split("afade=t=in:st=0:d=")[1].split(",")[0])
    fade_out = float(chain.split("afade=t=out:st=")[1].split("d=")[1].rstrip("[bed]"))
    assert fade_in + fade_out <= 1.0


def test_a_degenerate_duration_does_not_produce_a_negative_fade():
    chain = audio.bed_fit_filter("1:a", "bed", 0.0)
    assert "st=-" not in chain
    assert "d=-" not in chain


def test_the_bed_is_fitted_inside_the_mix():
    graph = audio.music_mix_filter("0:a", "1:a", "aout", 0.12, 30.0)
    assert "[1:a]aloop=" in graph, graph
    assert "[bedfit]volume=" in graph, graph


def test_the_mix_does_not_double_fade_the_bed():
    """bed_fit_filter carries its own fades; the caller's `fade` flag must not add a second set
    to the bed, because two overlapping out-fades pull the ending to silence early."""
    graph = audio.music_mix_filter("0:a", "1:a", "aout", 0.12, 30.0, fade=True)
    bed_stage = graph.split("[bedfit]volume=")[1].split(";")[0]
    assert "afade" not in bed_stage, bed_stage


# --------------------------------------------------------------------------- #
# A20 - cached assets keep their licence
# --------------------------------------------------------------------------- #
def _asset(root: Path, name: str = "clip_a.mp4", **kw) -> broll.AssetRef:
    (root / name).write_bytes(b"stub")
    fields = {
        "provider": "pexels",
        "source_id": "123",
        "license": "Pexels",
        "attribution": "by X",
        "kind": "video",
    }
    fields.update(kw)
    return broll.AssetRef(path=str(root / name), **fields)


def test_a_cached_asset_is_found_by_the_keyword_it_was_searched_for():
    """Matched on the recorded keyword, not the filename: a provider's filename is its own
    identifier and says nothing about what was searched for."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        broll.record_asset_license(_asset(root), "ocean")
        hit = broll.cached_asset(root, "ocean")
        assert hit is not None
        assert hit.license == "Pexels" and hit.provider == "pexels"
        assert broll.cached_asset(root, "mountain") is None


def test_an_asset_without_a_recorded_licence_is_not_resurrected():
    """resolve_asset drops an unknown licence, so a sidecar missing it must count as no record -
    otherwise a truncated file puts unlicensed footage in a published clip."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        asset = _asset(root)
        broll.record_asset_license(asset, "ocean")
        broll.license_sidecar_path(asset.path).write_text(
            json.dumps({"license": "", "keyword": "ocean", "asset": "clip_a.mp4"})
        )
        assert broll.load_asset_license(asset.path) is None
        assert broll.cached_asset(root, "ocean") is None


def test_a_sidecar_that_outlived_its_asset_is_ignored():
    """A retention sweep or a manual delete must not leave a cache hit pointing at nothing."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        broll.record_asset_license(
            broll.AssetRef(
                path=str(root / "gone.mp4"), kind="video", provider="p", source_id="1", license="L"
            ),
            "gone",
        )
        assert broll.cached_asset(root, "gone") is None


def test_a_corrupt_sidecar_is_ignored_rather_than_fatal():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        asset = _asset(root)
        broll.license_sidecar_path(asset.path).write_text("{not json")
        assert broll.load_asset_license(asset.path) is None
        assert broll.cached_asset(root, "ocean") is None


def test_recording_a_licence_never_raises_when_the_path_is_unusable():
    """Caching is an optimisation: an unusable cache must cost the cache, not the clip.

    The path is placed *under a regular file*, so ``mkdir`` raises ``NotADirectoryError``. A
    merely non-existent directory is not a good test of this - the tests run as root here and
    creating it succeeds, which is how the first version of this test passed for the wrong
    reason and then failed once the code was correct.
    """
    with tempfile.TemporaryDirectory() as d:
        blocker = Path(d) / "not-a-directory"
        blocker.write_bytes(b"x")
        asset = broll.AssetRef(
            path=str(blocker / "x.mp4"), kind="video", provider="p", source_id="1", license="L"
        )
        assert broll.record_asset_license(asset, "k") is None


def test_the_external_provider_serves_the_cache_before_downloading():
    """Re-downloading the same file for every clip of every job costs bandwidth, a rate limit,
    and on a metered API an actual bill - for a file already on disk."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        broll.record_asset_license(_asset(root), "ocean")
        calls: list = []

        def downloader(keyword, api_key, base_url, cache_dir):
            calls.append(keyword)
            return None

        provider = broll.ExternalProvider("key", "https://x", downloader=downloader, cache_dir=root)
        hit = provider.search("ocean")
        assert hit is not None and hit.license == "Pexels"
        assert calls == [], "the downloader was called despite a cache hit"


def test_a_download_is_recorded_so_the_next_run_can_use_it():
    """Without the sidecar the cached asset has no provenance, and resolve_asset would drop a
    file that had already been fetched and approved."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)

        def downloader(keyword, api_key, base_url, cache_dir):
            return _asset(Path(cache_dir), "fetched.mp4")

        provider = broll.ExternalProvider("key", "https://x", downloader=downloader, cache_dir=root)
        assert provider.search("ocean") is not None
        assert broll.load_asset_license(root / "fetched.mp4") is not None
        assert broll.cached_asset(root, "ocean") is not None


def test_a_provider_with_no_key_neither_downloads_nor_reads_the_cache():
    """has_key gates everything: an operator who has not configured BYOK must see no provider
    activity at all."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        broll.record_asset_license(_asset(root), "ocean")
        provider = broll.ExternalProvider("", "https://x", cache_dir=root)
        assert provider.search("ocean") is None
