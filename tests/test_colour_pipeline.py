"""Tone-mapping, colour tags, and range resolution end to end (O13, O14, O15).

**These tests probe the delivered file, not the argument list**, which is R3.8 and is the
discipline the whole spec insists on. `-colorspace bt709` appearing in argv proves a flag was
passed; it does not prove the muxed file carries it, and the two differ across ffmpeg versions
and containers. The same reasoning is already written into `test_output_compat.py`'s docstring
for pixel format and profile, and it applies harder here — colour tags are stream metadata that
a container can decline to store.

The two failures worth attacking first are the *plausible-looking* ones, because neither raises
and neither is visible in a log:

* **tone-mapping applied twice.** The pipeline runs three passes. Converting at the cut and again
  at the composite compresses the range twice and delivers a flat, muddy picture that still looks
  like a photograph of something. Nothing errors.
* **tone-mapping a mislabelled SDR source.** Visibly destroys a correct picture, and the only
  evidence is the pixels.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from config import settings as app_settings
from worker import colour
from worker.colour import Colour_Plan, Dynamic_Range
from worker.engines.capabilities import Capability_Status

FFMPEG = shutil.which(app_settings.ffmpeg_binary) or shutil.which("ffmpeg")
FFPROBE = shutil.which(app_settings.ffprobe_binary) or shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="no ffmpeg/ffprobe on PATH; the colour pipeline needs both",
)

COLOUR_ENTRIES = "stream=pix_fmt,profile,color_transfer,color_primaries,color_space,color_range"


def _probe_colour(path) -> dict[str, str]:
    proc = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            COLOUR_ENTRIES,
            "-of",
            "default=nw=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def _hdr_source(path, *, transfer: str = "smpte2084"):
    """A real PQ- or HLG-signalled 10-bit BT.2020 source."""
    proc = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p10le",
            "-profile:v",
            "high10",
            "-color_trc",
            transfer,
            "-color_primaries",
            "bt2020",
            "-colorspace",
            "bt2020nc",
            "-color_range",
            "tv",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return path


def _prober_missing(name: str):
    """A prober reporting every capability present except ffmpeg filter ``name``."""

    def prober(capability_id: str) -> Capability_Status:
        missing = capability_id == f"ffmpeg_filter:{name}"
        return Capability_Status(capability_id, not missing, "injected")

    return prober


def _prober_all_present():
    def prober(capability_id: str) -> Capability_Status:
        return Capability_Status(capability_id, True, "injected")

    return prober


# --- 2.7: HDR end to end, probing the output ---------------------------------------------


@requires_ffmpeg
@pytest.mark.real_binary
def test_an_hdr_source_is_delivered_as_rec709(tmp_path):
    """R2.1, R3.1, R3.2, R13.1: PQ in, Rec.709 out, and the file says so."""
    from worker import ffmpeg_utils as fu

    src = _hdr_source(tmp_path / "pq.mp4")
    info = fu.probe(src)
    plan = colour.plan_colour(
        transfer=info.color_transfer,
        primaries=info.color_primaries,
        matrix=info.color_space,
        source_range=info.color_range,
        prober=_prober_all_present(),
    )
    assert plan.source_range is Dynamic_Range.HDR
    assert plan.tone_mapped is True

    dest = tmp_path / "delivered.mp4"
    fu.cut_segment(src, 0.0, 0.8, dest, video_filters=plan.filter_chain, colour_tags=plan.tags)

    got = _probe_colour(dest)
    assert got["color_transfer"] == "bt709", got
    assert got["color_primaries"] == "bt709", got
    assert got["color_space"] == "bt709", got
    # R3.4/R3.6: O1 and O2 are untouched, and no tag contradicts the pixel format.
    assert got["pix_fmt"] == "yuv420p", got
    assert got["profile"] == "High", got


@requires_ffmpeg
@pytest.mark.real_binary
def test_hlg_is_delivered_as_rec709_too(tmp_path):
    """HLG is the transfer phones actually shoot, and its ffprobe spelling names no HDR."""
    from worker import ffmpeg_utils as fu

    src = _hdr_source(tmp_path / "hlg.mp4", transfer="arib-std-b67")
    info = fu.probe(src)
    plan = colour.plan_colour(
        transfer=info.color_transfer,
        primaries=info.color_primaries,
        matrix=info.color_space,
        source_range=info.color_range,
        prober=_prober_all_present(),
    )
    dest = tmp_path / "delivered_hlg.mp4"
    fu.cut_segment(src, 0.0, 0.8, dest, video_filters=plan.filter_chain, colour_tags=plan.tags)
    assert _probe_colour(dest)["color_transfer"] == "bt709"


@requires_ffmpeg
@pytest.mark.real_binary
def test_tone_mapping_measurably_changes_the_picture(tmp_path):
    """The conversion does something to the pixels, not merely to the metadata.

    Without this, every other test here would still pass if `tonemap` silently no-opped and only
    the tags were rewritten — which would be the worst possible outcome, since the file would
    then *claim* Rec.709 while carrying PQ-coded samples. That is strictly worse than the defect
    this fixes.

    Asserted as a direction and a floor rather than an exact value: the numbers depend on the
    ffmpeg build's tonemap implementation, and pinning them would make this a drift test for
    somebody else's filter.
    """
    from worker import ffmpeg_utils as fu

    src = _hdr_source(tmp_path / "pq_pixels.mp4")
    info = fu.probe(src)
    plan = colour.plan_colour(
        transfer=info.color_transfer,
        primaries=info.color_primaries,
        matrix=info.color_space,
        source_range=info.color_range,
        prober=_prober_all_present(),
    )

    mapped = tmp_path / "mapped.mp4"
    fu.cut_segment(src, 0.0, 0.8, mapped, video_filters=plan.filter_chain, colour_tags=plan.tags)
    plain = tmp_path / "plain.mp4"
    fu.cut_segment(src, 0.0, 0.8, plain)

    def mean_luma(path) -> float:
        proc = subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-nostats",
                "-i",
                str(path),
                "-vf",
                "signalstats,metadata=print:file=-",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        values = [
            float(line.partition("=")[2])
            for line in proc.stdout.splitlines()
            if "lavfi.signalstats.YAVG" in line
        ]
        assert values, "signalstats produced no YAVG readings"
        return sum(values) / len(values)

    assert mean_luma(mapped) != pytest.approx(mean_luma(plain), abs=1.0), (
        "tone-mapped and untone-mapped output are indistinguishable; the filter did nothing"
    )


# --- 2.8: absence and doubling ------------------------------------------------------------


def test_a_missing_filter_degrades_with_a_named_marker_and_does_not_fail():
    """R2.4, R2.5. The clip still ships; the marker names the capability that was missing.

    Both required filters are checked, because `zscale` and `tonemap` come from different ffmpeg
    build options and a build can genuinely have one without the other.
    """
    for missing in ("zscale", "tonemap"):
        plan = colour.plan_colour(
            transfer="smpte2084",
            primaries="bt2020",
            matrix="bt2020nc",
            source_range="tv",
            prober=_prober_missing(missing),
        )
        assert plan.tone_mapped is False, missing
        assert plan.filters == (), f"{missing} absent, so no chain may be emitted"
        assert f"tone_map_degraded:ffmpeg_filter:{missing}" in plan.markers, plan.markers
        # The tags describe what was actually delivered, which is still HDR (R3.2).
        assert "-color_trc" in plan.tags
        assert "smpte2084" in plan.tags, "an untone-mapped HDR file must not claim Rec.709"


def test_a_prober_that_raises_yields_an_unavailable_capability_not_a_crash():
    """A raising prober degrades, via `Capability_Report`'s own error handling.

    Worth being precise about which mechanism does the work here, because the obvious reading is
    wrong: this does **not** exercise `tonemap_filters_missing`' own `except`.
    `Capability_Report._probe` catches everything the prober throws and returns
    `available=False`, so the degradation happens on the ordinary path. The mutation harness is
    what established that -- breaking the `except` branch changed nothing observable, which meant
    the branch was unreachable from this test despite the test's original name claiming otherwise.
    """

    def exploding_prober(capability_id: str) -> Capability_Status:
        raise RuntimeError("probe unavailable")

    plan = colour.plan_colour(
        transfer="smpte2084",
        primaries="bt2020",
        matrix="bt2020nc",
        source_range="tv",
        prober=exploding_prober,
    )
    assert plan.tone_mapped is False
    assert any(m.startswith("tone_map_degraded:") for m in plan.markers), plan.markers


def test_the_capability_module_being_unimportable_also_fails_closed(monkeypatch):
    """The `except` branch in `tonemap_filters_missing`, reached the only way it can be.

    `Capability_Report` swallows prober errors, so the only route into that handler is the import
    itself failing. Setting the module to `None` in `sys.modules` makes `from ... import ...`
    raise `ImportError`, which is what a broken or partially-installed tree looks like.

    Fails **closed**: R2.5 forbids failing a job over tone-mapping, and emitting a `zscale` chain
    on a build without it is a filter-graph configuration error, which is a failed job. This is
    the deliberate opposite of `background_style_available`, which fails open because its
    fallback is another working background.
    """
    import sys

    monkeypatch.setitem(sys.modules, "worker.engines.capabilities", None)

    assert colour.tonemap_filters_missing() == colour.TONEMAP_REQUIRED_FILTERS[0]

    plan = colour.plan_colour(
        transfer="smpte2084", primaries="bt2020", matrix="bt2020nc", source_range="tv"
    )
    assert plan.tone_mapped is False, "an unprobeable build must not be handed a zscale chain"
    assert any(m.startswith("tone_map_degraded:") for m in plan.markers), plan.markers


def test_tone_mapping_can_be_switched_off_and_says_so():
    """R2.10/R12: the setting is honoured, and the reason is distinguishable from a limitation.

    `tone_map_skipped:disabled` and `tone_map_degraded:...` must not be confused: one is a choice
    the operator made and the other is something this machine could not do. An operator debugging
    grey output needs to know which.

    Missing until the mutation harness pointed it out -- the default is on, so nothing else in the
    suite ever took this branch.
    """
    plan = colour.plan_colour(
        transfer="smpte2084",
        primaries="bt2020",
        matrix="bt2020nc",
        source_range="tv",
        tone_map_enabled=False,
        prober=_prober_all_present(),
    )
    assert plan.tone_mapped is False
    assert plan.filters == (), "switched off must mean no conversion, not a no-op chain"
    assert "tone_map_skipped:disabled" in plan.markers, plan.markers
    assert not any(m.startswith("tone_map_degraded") for m in plan.markers), (
        "a deliberate choice must not be reported as a degradation"
    )
    # R3.2 still holds: we delivered the HDR source untouched, so the tags say so.
    assert "smpte2084" in plan.tags


def test_the_range_conversion_runs_in_the_right_direction():
    """O15: `in_range` is the source and `out_range` is the delivery, not the other way round.

    Inverting these still produces a valid filter and a plausible image -- washed and
    low-contrast rather than crushed -- and it reads as a grading choice rather than a bug. The
    end-to-end test cannot catch it either, because the *tag* would still say `tv` while the
    samples were full-range. Only the chain's direction distinguishes them.
    """
    chain = colour.range_convert_chain(source_range="pc", out_range="tv")
    assert chain == "scale=in_range=pc:out_range=tv", chain
    assert chain.index("in_range=pc") < chain.index("out_range=tv")
    # And symmetrically, in case a future default ever delivers full range.
    assert colour.range_convert_chain(source_range="tv", out_range="pc") == (
        "scale=in_range=tv:out_range=pc"
    )


def test_the_tone_map_is_spent_after_one_pass():
    """R2.8: at most one tone-map per clip, across all three passes.

    `consumed()` is the mechanism, and this asserts the two halves separately because they pull
    in opposite directions: the *filters* must not survive into a later pass, and the *tags*
    must, since every pass that writes a file should describe it.
    """
    plan = colour.plan_colour(
        transfer="smpte2084",
        primaries="bt2020",
        matrix="bt2020nc",
        source_range="tv",
        prober=_prober_all_present(),
    )
    assert plan.filters, "precondition: the plan should carry a chain"

    spent = plan.consumed()
    assert spent.filters == (), "a second pass must not re-apply the conversion"
    assert spent.filter_chain == ""
    assert spent.tags == plan.tags, "tags describe the delivered file and must survive"
    assert spent.tone_mapped is True, "the fact that it happened is still true"
    # Idempotent: spending an already-spent plan is not an error, because the pipeline calls
    # this once per clip and a fourth pass (filler/U4 concat) may or may not run.
    assert spent.consumed().filters == ()


def test_only_the_first_of_three_passes_receives_the_chain():
    """The same rule stated against the shape the pipeline actually uses.

    Modelled rather than rendered: three passes of real ffmpeg to assert an absence is slow and
    the thing being checked is the plumbing, not ffmpeg. The end-to-end tone-map is covered
    above.
    """
    plan = colour.plan_colour(
        transfer="smpte2084",
        primaries="bt2020",
        matrix="bt2020nc",
        source_range="tv",
        prober=_prober_all_present(),
    )
    passes: list[str] = []

    # Pass 1 (cut) consumes it; passes 2 (geometry) and 3 (composite) carry only tags.
    passes.append(plan.filter_chain)
    spent = plan.consumed()
    passes.append(spent.filter_chain)
    passes.append(spent.filter_chain)

    with_chain = [p for p in passes if p]
    assert len(with_chain) == 1, f"expected exactly one tone-map, got {len(with_chain)}"
    assert "tonemap=" in with_chain[0]


# --- R2.6/R2.7: never SDR, never unknown --------------------------------------------------


@pytest.mark.parametrize(
    "transfer",
    ["bt709", "smpte170m", "bt2020-10", "", "unknown", "some-curve-from-2031"],
)
def test_non_hdr_sources_are_never_tone_mapped(transfer):
    """R2.6, R2.7 together: the union of SDR and unknown gets no conversion."""
    plan = colour.plan_colour(
        transfer=transfer,
        primaries="bt709",
        matrix="bt709",
        source_range="tv",
        prober=_prober_all_present(),
    )
    assert plan.tone_mapped is False, transfer
    assert not any("tonemap" in f for f in plan.filters), transfer


def test_an_sdr_source_produces_no_filters_at_all():
    """The property that keeps an existing library rendering byte-identically.

    If this ever fails, every golden and parity fixture in the project moves — and a default
    that silently reformats every clip is exactly what the project's default-to-shipped-behaviour
    rule exists to prevent. Tone-mapping is the one deliberate exception to that rule, and it is
    only defensible because of this: it cannot fire on a source that is not positively HDR.
    """
    plan = colour.plan_colour(
        transfer="bt709",
        primaries="bt709",
        matrix="bt709",
        source_range="tv",
        prober=_prober_all_present(),
    )
    assert plan.filters == ()
    assert plan.markers == (), "an ordinary SDR clip should not accumulate markers either"


# --- O15: range -------------------------------------------------------------------------


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_full_range_source_is_delivered_limited(tmp_path):
    """O15/R3.3: full-range footage is converted rather than passed through.

    Passing `pc` through to a player expecting `tv` is what crushes blacks and clips highlights
    on phone footage, and it is the most common colour defect in the wild.
    """
    from worker import ffmpeg_utils as fu

    src = tmp_path / "full.mp4"
    proc = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuvj420p",
            "-color_range",
            "pc",
            "-color_trc",
            "bt709",
            "-color_primaries",
            "bt709",
            "-colorspace",
            "bt709",
            str(src),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr

    info = fu.probe(src)
    assert colour.normalise_range(info.color_range) == "pc", info.color_range

    plan = colour.plan_colour(
        transfer=info.color_transfer,
        primaries=info.color_primaries,
        matrix=info.color_space,
        source_range=info.color_range,
    )
    assert "colour_range_converted:pc:tv" in plan.markers, plan.markers
    assert plan.tone_mapped is False, "a range conversion is not a tone-map"

    dest = tmp_path / "limited.mp4"
    fu.cut_segment(src, 0.0, 0.8, dest, video_filters=plan.filter_chain, colour_tags=plan.tags)
    got = _probe_colour(dest)
    assert got["color_range"] == "tv", got
    assert got["pix_fmt"] == "yuv420p", got


def test_the_range_conversion_needs_no_optional_filter():
    """It uses `scale`, which is in every ffmpeg build.

    Deliberate: the tone-map may degrade on a build without `libzimg`, but the range fix must
    not, because it addresses the more common defect. Asserted so that "optimising" this to
    `zscale` for consistency with the tone-map chain cannot pass quietly.
    """
    chain = colour.range_convert_chain(source_range="pc", out_range="tv")
    assert chain.startswith("scale=")
    assert "zscale" not in chain


def test_an_unstated_range_records_the_default_it_applied():
    """R3.7. "We assumed limited" is the first fact you want when the blacks look wrong."""
    plan = colour.plan_colour(transfer="bt709", primaries="bt709", matrix="bt709", source_range="")
    assert "colour_range_assumed:tv" in plan.markers, plan.markers
    assert plan.delivered_range == "tv"


def test_a_matching_range_is_not_converted():
    """No filter when the source already delivers what we want — no pointless re-encode cost."""
    assert colour.range_convert_chain(source_range="tv", out_range="tv") == ""
    assert colour.range_convert_chain(source_range="", out_range="tv") == ""


# --- O14: tags describe the delivery, not the arrival -------------------------------------


def test_tags_never_claim_rec709_for_a_source_we_did_not_convert():
    """R3.2, from the other direction.

    A Rec.601 source passed through untouched *is* Rec.601 on delivery. Tagging it `bt709`
    because that is the modern default would make the file confidently wrong, which is worse
    than the absent tag a player would otherwise fill in with the same assumption.
    """
    plan = colour.plan_colour(
        transfer="smpte170m", primaries="smpte170m", matrix="smpte170m", source_range="tv"
    )
    assert plan.tone_mapped is False
    assert "smpte170m" in plan.tags
    assert "bt709" not in plan.tags


def test_absent_source_fields_produce_no_invented_tags():
    """A partially-tagged file beats a confidently mis-tagged one.

    A player meeting a missing tag falls back to Rec.709, which is right far more often than a
    wrong explicit value — and unlike a wrong value it leaves no record that we asserted
    anything.
    """
    plan = colour.plan_colour(transfer="", primaries="", matrix="", source_range="tv")
    assert "-color_trc" not in plan.tags
    assert "-color_primaries" not in plan.tags
    assert "-colorspace" not in plan.tags
    # Range is always resolved, so it is always stated.
    assert "-color_range" in plan.tags


def test_tone_mapped_output_declares_rec709_and_drops_the_source_curve():
    """The specific mis-tagging R3.2 exists to prevent.

    Copying `smpte2084` onto tone-mapped output tells a player to apply an HDR EOTF to SDR
    content. That is worse than writing no tags at all, because the player is now confidently
    wrong rather than falling back to a correct assumption.
    """
    plan = colour.plan_colour(
        transfer="smpte2084",
        primaries="bt2020",
        matrix="bt2020nc",
        source_range="tv",
        prober=_prober_all_present(),
    )
    assert plan.tone_mapped is True
    assert "smpte2084" not in plan.tags, plan.tags
    assert "bt2020" not in plan.tags, plan.tags
    assert plan.tags.count("bt709") == 3, plan.tags


def test_h264_args_is_unchanged_when_no_tags_are_passed():
    """The drift pin in `test_script_and_placement.py` keeps holding.

    `colour_tags` defaults to empty deliberately rather than for convenience: an unconditional
    addition to `h264_args` would have required re-freezing that exact-argv assertion, and
    re-freezing a pin as part of the change it is meant to catch is how `font_substituted:Arial`
    got baked into a golden as correct.
    """
    from worker.ffmpeg_utils import h264_args

    assert h264_args() == h264_args(colour_tags=())
    tagged = h264_args(colour_tags=("-color_range", "tv"))
    assert tagged[: len(h264_args())] == h264_args(), "tags must be appended, never interleaved"
    assert tagged[-2:] == ["-color_range", "tv"]


# --- operator handling ------------------------------------------------------------------


def test_an_unrecognised_operator_falls_back_to_the_default_without_raising():
    """A mistyped setting must not turn a deliverable clip into a failed job."""
    assert colour.resolve_operator("not-an-operator") == colour.DEFAULT_TONEMAP_OPERATOR
    assert colour.resolve_operator("") == colour.DEFAULT_TONEMAP_OPERATOR
    assert colour.resolve_operator("REINHARD") == "reinhard", "case should not matter"


def test_the_marker_names_the_operator_that_ran_not_the_one_requested():
    """Marker discipline (R11.4): report the resolved value.

    An operator reading back the setting they know they set learns nothing; the point of the
    marker is to tell them what actually happened when it differed.
    """
    plan = colour.plan_colour(
        transfer="smpte2084",
        primaries="bt2020",
        matrix="bt2020nc",
        source_range="tv",
        operator="nonsense",
        prober=_prober_all_present(),
    )
    assert f"tone_map:{colour.DEFAULT_TONEMAP_OPERATOR}:smpte2084" in plan.markers, plan.markers


def test_the_chain_orders_conversion_before_delivery_encoding():
    """R2.2's order, asserted on the chain itself.

    Linearise, then convert primaries, then compress the range, then re-encode to Rec.709.
    Getting these out of order produces output that still looks like a picture, which is why the
    order is pinned rather than left to review.
    """
    chain = colour.tonemap_chain()
    linear = chain.index("t=linear")
    primaries = chain.index("p=bt709")
    tonemap = chain.index("tonemap=tonemap=")
    out = chain.index("t=bt709")
    assert linear < primaries < tonemap < out, chain
    assert chain.endswith("format=yuv420p"), "the chain must land back on the output pixel format"


def test_prepend_filters_puts_colour_first():
    """R2.2. Tone-mapping after a scale interpolates perceptual values rather than light."""
    plan = Colour_Plan(filters=("zscale=t=linear", "tonemap=tonemap=hable"))
    assert colour.prepend_filters(plan, "crop=1:2:3:4,scale=100:200").startswith("zscale=t=linear")
    assert colour.prepend_filters(Colour_Plan(), "crop=1:2:3:4") == "crop=1:2:3:4"
    assert colour.prepend_filters(plan, "") == "zscale=t=linear,tonemap=tonemap=hable"


def test_merge_markers_does_not_duplicate():
    """The pipeline merges per clip; a fourth pass must not double a marker."""
    plan = Colour_Plan(markers=("tone_map:hable:smpte2084",))
    once = colour.merge_markers(["captions"], plan)
    twice = colour.merge_markers(once, plan)
    assert twice == ["captions", "tone_map:hable:smpte2084"]
