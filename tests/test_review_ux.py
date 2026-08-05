"""Review UX: U6 brand kit, U7 per-clip re-render, U9 batch review.

The frontend halves (U3 player, U5 style picker, U11 shortcuts) are covered by
``frontend/src/components/*.test.jsx``; this file covers what the backend has to get right for
them to mean anything.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.conftest import FFMPEG, requires_ffmpeg
from worker import branding
from worker.effects.caption_presets import resolve_preset
from worker.models import ClipResult, Job, ProcessingOptions

# --------------------------------------------------------------------------- #
# U6 - colour conversion
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "hex_color,expected",
    [
        ("#FF0000", "&H000000FF"),   # red -> BB=00 GG=00 RR=FF
        ("#00FF00", "&H0000FF00"),
        ("#0000FF", "&H00FF0000"),
        ("#FFFFFF", "&H00FFFFFF"),
        ("#000000", "&H00000000"),
        ("00E5FF", "&H00FFE500"),    # a leading # is optional
    ],
)
def test_u6_hex_becomes_ass_with_the_bytes_reversed(hex_color, expected):
    """ASS stores colours blue-green-red, which is the detail that silently ruins a brand.

    Getting it wrong does not fail: it renders, in the wrong colour, so a brand's red becomes its
    blue and nothing reports a problem.
    """
    assert branding.hex_to_ass(hex_color) == expected


def test_u6_red_and_blue_do_not_survive_a_naive_conversion():
    """Pins the asymmetry directly: the two channels that a byte-order bug swaps."""
    assert branding.hex_to_ass("#FF0000") != branding.hex_to_ass("#0000FF")
    assert branding.hex_to_ass("#FF0000").endswith("FF")
    assert branding.hex_to_ass("#0000FF").endswith("00")


@pytest.mark.parametrize("value", ["", None, "nonsense", "#12345", "#GGGGGG", "rgb(1,2,3)"])
def test_u6_an_unparseable_colour_is_ignored(value):
    """An unset or malformed colour must leave the preset's own value alone."""
    assert branding.hex_to_ass(value) is None


def test_u6_an_ass_colour_passes_through():
    """A kit stored from a preset's own value keeps working."""
    assert branding.hex_to_ass("&H0000E5FF") == "&H0000E5FF"


def test_u6_ass_converts_back_for_a_colour_input():
    """A colour picker cannot display `&H00FFFFFF`, so the UI needs the reverse direction."""
    assert branding.ass_to_hex("&H000000FF") == "#ff0000"
    assert branding.ass_to_hex("&H00FF0000") == "#0000ff"
    assert branding.ass_to_hex("nonsense") is None


def test_u6_the_colour_round_trip_is_stable():
    for hex_color in ("#ff0000", "#00e5ff", "#123456", "#ffffff"):
        assert branding.ass_to_hex(branding.hex_to_ass(hex_color)) == hex_color


# --------------------------------------------------------------------------- #
# U6 - the kit overrides the preset
# --------------------------------------------------------------------------- #


def test_u6_no_kit_leaves_the_preset_untouched():
    """The default: an unconfigured install renders exactly as before."""
    preset, _ = resolve_preset("hormozi")
    result, markers = branding.apply_brand(preset, ProcessingOptions())
    assert result is preset
    assert markers == []


def test_u6_the_kit_overrides_the_presets_typography():
    preset, _ = resolve_preset("karaoke")
    options = ProcessingOptions(
        brand_font="Anton", brand_primary_color="#ff0000", brand_highlight_color="#00ff00"
    )
    result, markers = branding.apply_brand(preset, options)
    assert result.font == "Anton"
    assert result.colors.primary == "&H000000FF"
    assert result.colors.highlight == "&H0000FF00"
    assert set(markers) == {"brand_font", "brand_colors"}


def test_u6_the_kit_does_not_touch_the_look():
    """A preset is a *look*, the kit is an *identity*: hormozi's animation in a brand's typeface.

    If the kit replaced position or animation it would be a second preset system, and choosing a
    preset would stop meaning anything.
    """
    preset, _ = resolve_preset("hormozi")
    options = ProcessingOptions(brand_font="Bangers", brand_primary_color="#ff0000")
    result, _ = branding.apply_brand(preset, options)
    assert result.animation == preset.animation
    assert result.position == preset.position
    assert result.uppercase == preset.uppercase
    assert result.punch_scale == preset.punch_scale
    assert result.highlight_keywords == preset.highlight_keywords


def test_u6_a_partial_kit_only_overrides_what_it_sets():
    """Each field is additive; an unset one must not overwrite the preset with a default."""
    preset, _ = resolve_preset("karaoke")
    result, markers = branding.apply_brand(
        preset, ProcessingOptions(brand_font="Anton")
    )
    assert result.font == "Anton"
    assert result.colors.primary == preset.colors.primary
    assert markers == ["brand_font"]


def test_u6_a_kit_matching_the_preset_records_no_marker():
    """Markers describe changes. One recorded for a value that did not change is noise."""
    preset, _ = resolve_preset("karaoke")
    options = ProcessingOptions(
        brand_font=preset.font,
        brand_primary_color=branding.ass_to_hex(preset.colors.primary),
    )
    _result, markers = branding.apply_brand(preset, options)
    assert markers == []


def test_u6_a_missing_font_is_not_substituted_here():
    """`captions.resolve_font` owns substitution and records a marker for it.

    Doing it here too would duplicate that logic and hide the substitution behind a second one.
    """
    preset, _ = resolve_preset("karaoke")
    result, _ = branding.apply_brand(
        preset, ProcessingOptions(brand_font="No Such Face At All")
    )
    assert result.font == "No Such Face At All"


# --------------------------------------------------------------------------- #
# U6 - the logo watermark
# --------------------------------------------------------------------------- #


@pytest.fixture
def logo(tmp_path):
    path = tmp_path / "logo.png"
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1",
         "-frames:v", "1", str(path)],
        check=True, capture_output=True,
    )
    return path


def test_u6_no_logo_means_no_filter():
    assert branding.logo_filter(ProcessingOptions(), 1080, 1920) is None


def test_u6_a_missing_logo_costs_the_watermark_not_the_clip(tmp_path):
    absent = ProcessingOptions(brand_logo=str(tmp_path / "absent.png"))
    assert branding.logo_filter(absent, 1080, 1920) is None


def test_u6_a_non_image_is_ignored(tmp_path):
    """`movie` fails the whole filtergraph on a file it cannot decode."""
    notes = tmp_path / "notes.txt"
    notes.write_text("not an image", encoding="utf-8")
    assert branding.logo_filter(ProcessingOptions(brand_logo=str(notes)), 1080, 1920) is None


@requires_ffmpeg
def test_u6_the_logo_uses_no_extra_ffmpeg_input(logo):
    """The reason it is a `movie` source rather than a second `-i`.

    The compositor's input indices are load-bearing: engine contributions, music, b-roll and emoji
    each compute offsets from them, and that accounting is what keeps the v0.8.0 parity guarantee.
    An extra input would put all of those at risk to save nothing.
    """
    made = branding.logo_filter(ProcessingOptions(brand_logo=str(logo)), 1080, 1920)
    assert made is not None
    assert "movie=filename=" in made
    assert "-i" not in made


@requires_ffmpeg
@pytest.mark.parametrize(
    "position,band",
    [("top_right", (0, 300)), ("bottom_left", (1620, 1920)),
     ("top_left", (0, 300)), ("bottom_right", (1620, 1920))],
)
def test_u6_the_logo_renders_in_the_requested_corner(tmp_path, logo, position, band):
    """Asserted on rendered pixels: a filter string can be well-formed and place nothing."""
    base = tmp_path / "base.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", "color=c=gray:s=1080x1920:r=25:d=1",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", str(base)],
        check=True, capture_output=True,
    )
    options = ProcessingOptions(
        brand_logo=str(logo), brand_logo_position=position, brand_logo_scale=0.2
    )
    graph = branding.logo_filter(options, 1080, 1920, base_label="0:v", out_label="vbrand")
    out = tmp_path / f"wm_{position}.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-i", str(base), "-filter_complex", graph, "-map", "[vbrand]",
         "-frames:v", "3", "-pix_fmt", "yuv420p", "-c:v", "libx264", str(out)],
        check=True, capture_output=True,
    )

    def red_fraction(top, bottom):
        raw = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(out), "-frames:v", "1",
             "-vf", f"crop=iw:{bottom - top}:0:{top},format=rgb24", "-f", "rawvideo", "-"],
            check=True, capture_output=True,
        ).stdout
        pixels = [raw[i:i + 3] for i in range(0, len(raw), 3)]
        red = sum(1 for p in pixels if p[0] > 140 and p[1] < 90 and p[2] < 90)
        return red / max(1, len(pixels))

    assert red_fraction(*band) > 0.01, f"no logo in the {position} band"
    opposite = (1620, 1920) if band[0] == 0 else (0, 300)
    assert red_fraction(*opposite) < 0.001, f"logo bled into the {opposite} band"


def test_u6_the_logo_scale_is_clamped(logo):
    """An absurd scale should be bounded, not rendered as a full-frame overlay."""
    huge = branding.logo_filter(
        ProcessingOptions(brand_logo=str(logo), brand_logo_scale=9.0), 1080, 1920
    )
    tiny = branding.logo_filter(
        ProcessingOptions(brand_logo=str(logo), brand_logo_scale=-4.0), 1080, 1920
    )
    huge_width = int(huge.split("scale=", 1)[1].split(":", 1)[0])
    tiny_width = int(tiny.split("scale=", 1)[1].split(":", 1)[0])
    assert huge_width <= int(1080 * branding.MAX_LOGO_SCALE)
    assert tiny_width >= int(1080 * branding.MIN_LOGO_SCALE) - 1


def test_u6_the_logo_width_is_even(logo):
    """An odd width makes ffmpeg pick a chroma alignment in a 4:2:0 frame rather than fail.

    The scales are *searched* rather than listed. Written first with four hand-picked values, every
    one of which happened to round to an even width - so removing the rounding entirely still
    passed. ``odd_cases`` asserts the test actually exercised the case it is named for.
    """
    width = 1081
    odd_cases = 0
    for step in range(40, 401):
        scale = step / 1000
        if round(width * scale) % 2 == 1:
            odd_cases += 1
            made = branding.logo_filter(
                ProcessingOptions(brand_logo=str(logo), brand_logo_scale=scale), width, 1921
            )
            emitted = int(made.split("scale=", 1)[1].split(":", 1)[0])
            assert emitted % 2 == 0, f"scale {scale} gave an odd width {emitted}"
    assert odd_cases > 0, "no scale produced an odd width; the test proved nothing"


def test_u6_an_unknown_position_falls_back_rather_than_failing(logo):
    made = branding.logo_filter(
        ProcessingOptions(brand_logo=str(logo), brand_logo_position="middle_of_nowhere"),
        1080, 1920,
    )
    assert made is not None
    default = branding.logo_filter(
        ProcessingOptions(brand_logo=str(logo), brand_logo_position="top_right"), 1080, 1920
    )
    assert made == default


def test_u6_the_logo_scales_with_the_frame(logo):
    """One kit has to work at every output resolution, since O9 renders 720 to 2160."""
    small = branding.logo_filter(ProcessingOptions(brand_logo=str(logo)), 720, 1280)
    large = branding.logo_filter(ProcessingOptions(brand_logo=str(logo)), 2160, 3840)
    small_w = int(small.split("scale=", 1)[1].split(":", 1)[0])
    large_w = int(large.split("scale=", 1)[1].split(":", 1)[0])
    assert large_w > small_w * 2


def test_u6_the_brand_cta_becomes_the_end_card():
    """The CTA was regenerated per clip by the LLM, so a standing ask was reworded every time."""
    assert branding.end_card_text(ProcessingOptions(brand_cta="  Follow for more ")) == (
        "Follow for more"
    )
    assert branding.end_card_text(ProcessingOptions()) == ""


# --------------------------------------------------------------------------- #
# U6 - it reaches the render
# --------------------------------------------------------------------------- #


def _render(tmp_path, monkeypatch, **option_overrides):
    from tests.test_kinetic_compositor import (
        MATRIX_WORDS,
        _matrix_options,
        _parity_render,
    )
    from worker.effects import compositor

    return _parity_render(
        compositor,
        tmp_path,
        options=_matrix_options(captions=True, caption_preset="hormozi", **option_overrides),
        words=MATRIX_WORDS,
        hook_text="",
        contributions=None,
    )


def test_u6_the_kit_reaches_the_rendered_captions(tmp_path, monkeypatch):
    """A branding function nothing calls is not a feature."""
    plain = _render(tmp_path / "plain", monkeypatch)
    branded = _render(
        tmp_path / "branded", monkeypatch,
        brand_font="Bangers", brand_primary_color="#ff0000",
    )
    assert "brand_font" in branded.effects_applied
    assert "brand_colors" in branded.effects_applied
    assert "brand_font" not in plain.effects_applied
    # The ASS the renderer will read carries the brand values.
    assert "Bangers" in (branded.ass_text or "")
    assert "&H000000FF" in (branded.ass_text or "")


def test_u6_the_logo_reaches_the_graph(tmp_path, monkeypatch, logo):
    without = _render(tmp_path / "nologo", monkeypatch)
    with_logo = _render(tmp_path / "logo", monkeypatch, brand_logo=str(logo))
    assert "movie=filename=" not in without.graph
    assert "movie=filename=" in with_logo.graph
    assert "brand_logo" in with_logo.effects_applied


def test_u6_the_logo_sits_above_the_captions(tmp_path, monkeypatch, logo):
    """A watermark an emoji or a caption could cover is not a watermark."""
    record = _render(tmp_path / "order", monkeypatch, brand_logo=str(logo))
    graph = record.graph
    assert graph.index("subtitles=") < graph.index("movie=filename=")


def test_u6_an_unconfigured_kit_changes_no_graph(tmp_path, monkeypatch):
    """The default must be byte-identical to the pre-U6 render."""
    record = _render(tmp_path / "default", monkeypatch)
    assert "movie=filename=" not in record.graph
    assert not [m for m in record.effects_applied if m.startswith("brand")]


# --------------------------------------------------------------------------- #
# U7 - per-clip re-render
# --------------------------------------------------------------------------- #


def _job(tmp_path, source: Path) -> Job:
    return Job(
        input_type="file",
        source=str(source),
        options=ProcessingOptions(),
        source_path=str(source),
    )


def _clip() -> ClipResult:
    return ClipResult(
        id="01_abcdef",
        filename="clip_01_abcdef.mp4",
        start=1.0,
        end=3.0,
        duration=2.0,
        title="A title someone typed by hand",
        description="An edited description.",
        hashtags=["#kept"],
        score=77.0,
        reason="the good bit",
        review_state="approved",
    )


def test_u7_options_are_merged_not_replaced():
    from worker import rerender

    base = ProcessingOptions(color="warm", zoom=True)
    merged = rerender.merge_options(base, {"color": "vivid"})
    assert merged.color == "vivid"
    assert merged.zoom is True, "an unmentioned setting must survive"


def test_u7_unknown_override_keys_are_ignored():
    """The caller is a UI sending its whole settings blob."""
    from worker import rerender

    base = ProcessingOptions()
    assert rerender.merge_options(base, {"not_a_setting": 1}) is base


def test_u7_no_overrides_reuses_the_job_options():
    from worker import rerender

    base = ProcessingOptions()
    assert rerender.merge_options(base, None) is base
    assert rerender.merge_options(base, {}) is base


def test_u7_a_file_job_resolves_its_source(tmp_path):
    from worker import rerender

    source = tmp_path / "src.mp4"
    source.write_bytes(b"stub")
    job = Job(input_type="file", source=str(source), options=ProcessingOptions())
    assert rerender.resolve_source(job) == source


def test_u7_the_recorded_path_is_preferred_over_the_url(tmp_path):
    """A URL job's `source` is a URL and cannot be re-read; the download path can."""
    from worker import rerender

    downloaded = tmp_path / "downloaded.mp4"
    downloaded.write_bytes(b"stub")
    job = Job(
        input_type="url",
        source="https://example.com/video",
        options=ProcessingOptions(),
        source_path=str(downloaded),
    )
    assert rerender.resolve_source(job) == downloaded


def test_u7_a_vanished_source_is_a_clear_refusal(tmp_path):
    """Silently re-downloading would turn this into the whole-job run it exists to avoid."""
    from worker import rerender

    job = Job(
        input_type="url", source="https://example.com/v", options=ProcessingOptions(),
        source_path=str(tmp_path / "gone.mp4"),
    )
    with pytest.raises(rerender.RerenderError) as raised:
        rerender.resolve_source(job)
    assert "no longer available" in str(raised.value)


def test_u7_the_source_path_survives_serialisation(tmp_path):
    """A persisted job outliving a restart is the normal case, not an exceptional one."""
    source = tmp_path / "src.mp4"
    job = Job(
        input_type="url", source="https://example.com/v",
        options=ProcessingOptions(), source_path=str(source),
    )
    assert Job.from_dict(job.to_dict()).source_path == str(source)


def test_u7_preserved_fields_cover_everything_a_human_edits():
    """Re-rendering is a request about pixels; replacing edited copy would be data loss."""
    from worker import rerender

    for field in ("title", "description", "hashtags", "hook_text", "cta", "mentions",
                  "thumbnail_text", "review_state"):
        assert field in rerender.PRESERVED_FIELDS


@requires_ffmpeg
def test_u7_a_rerender_keeps_the_edited_metadata_and_the_filename(tmp_path, make_video, monkeypatch):
    """The end-to-end property: new pixels, same identity.

    The filename in particular must not change - every clip URL, publish record and history row
    already points at it, and a new name would orphan all of them.
    """
    from worker import rerender

    source = make_video("src.mp4", duration=6.0, w=640, h=360)
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    clip = _clip()
    original = clips_dir / clip.filename
    original.write_bytes(b"old-render")

    job = _job(tmp_path, source)
    # Keep the render cheap and offline: no captions, no metadata generation.
    from worker.transcribe import Transcript

    monkeypatch.setattr(
        "worker.pipeline.transcribe",
        lambda *a, **k: Transcript(language="en", segments=[]),
    )

    updated = rerender.rerender_clip(
        job, clip, option_overrides={"captions": False, "metadata": False, "color": "vivid"},
        clips_dir=clips_dir, temp_dir=tmp_path / "tmp",
    )

    assert updated.filename == clip.filename
    assert updated.id == clip.id
    assert original.read_bytes() != b"old-render", "the clip file was not replaced"
    # The metadata a human edited is carried across untouched.
    assert updated.title == clip.title
    assert updated.description == clip.description
    assert updated.hashtags == clip.hashtags
    assert updated.review_state == "approved"
    assert updated.score == clip.score


@requires_ffmpeg
def test_u7_a_failed_rerender_leaves_the_existing_clip_alone(tmp_path, make_video, monkeypatch):
    """The clip may already be published, and the file is what a viewer's platform links to."""
    from worker import rerender

    source = make_video("src.mp4", duration=4.0, w=320, h=240)
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    clip = _clip()
    original = clips_dir / clip.filename
    original.write_bytes(b"old-render")

    monkeypatch.setattr(
        "worker.rerender.run_pipeline",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError):
        rerender.rerender_clip(
            _job(tmp_path, source), clip, clips_dir=clips_dir, temp_dir=tmp_path / "tmp"
        )
    assert original.read_bytes() == b"old-render"


@requires_ffmpeg
def test_u7_no_staging_directory_is_left_behind(tmp_path, make_video, monkeypatch):
    from worker import rerender

    source = make_video("src.mp4", duration=4.0, w=320, h=240)
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    monkeypatch.setattr(
        "worker.rerender.run_pipeline",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError):
        rerender.rerender_clip(
            _job(tmp_path, source), _clip(), clips_dir=clips_dir, temp_dir=tmp_path / "tmp"
        )
    assert not list(clips_dir.glob(".rerender_*"))


def test_u7_explicit_candidates_skip_selection(monkeypatch, tmp_path, make_video):
    """The mechanism: an explicit window means no selection call at all.

    Without this a re-render would re-run selection, which with an LLM in it is not
    deterministic - so "restyle this clip" could return a different moment.
    """
    from worker import pipeline
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript

    source = make_video("src.mp4", duration=6.0, w=320, h=240)
    called = []

    monkeypatch.setattr(
        pipeline, "transcribe", lambda *a, **k: Transcript(language="en", segments=[])
    )

    def spy_select(*args, **kwargs):
        called.append(True)
        return [ClipCandidate(start=0.0, end=2.0)]

    monkeypatch.setattr(pipeline.visual_selection, "select_moments_visual", spy_select)

    pipeline.run_pipeline(
        source,
        ProcessingOptions(captions=False, metadata=False),
        clips_dir=tmp_path / "out",
        temp_dir=tmp_path / "tmp",
        explicit_candidates=[ClipCandidate(start=1.0, end=3.0)],
    )
    assert called == [], "selection ran even though an explicit window was given"


def test_u7_without_explicit_candidates_selection_still_runs(monkeypatch, tmp_path, make_video):
    """Guards the default path: every pre-U7 caller must be unaffected."""
    from worker import pipeline
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript

    source = make_video("src.mp4", duration=6.0, w=320, h=240)
    called = []
    monkeypatch.setattr(
        pipeline, "transcribe", lambda *a, **k: Transcript(language="en", segments=[])
    )

    def spy_select(*args, **kwargs):
        called.append(True)
        return [ClipCandidate(start=0.0, end=2.0)]

    monkeypatch.setattr(pipeline.visual_selection, "select_moments_visual", spy_select)
    pipeline.run_pipeline(
        source,
        ProcessingOptions(captions=False, metadata=False),
        clips_dir=tmp_path / "out2",
        temp_dir=tmp_path / "tmp2",
    )
    assert called == [True]


# --------------------------------------------------------------------------- #
# U9 - batch review
# --------------------------------------------------------------------------- #


@pytest.fixture
def review_client(monkeypatch):
    from fastapi.testclient import TestClient

    import api.deps as api_deps
    import api.main as main
    from worker.jobs import JobStore

    store = JobStore()
    job = Job(input_type="file", source="src.mp4", options=ProcessingOptions())
    job.clips = [
        ClipResult(id=f"clip{i}", filename=f"clip{i}.mp4", start=0.0, end=2.0, duration=2.0)
        for i in range(4)
    ]
    store.add(job)

    class _Manager:
        pass

    manager = _Manager()
    manager.store = store
    monkeypatch.setattr(api_deps, "get_manager", lambda: manager)
    monkeypatch.setattr(api_deps, "get_history", lambda: _NullHistory())
    return TestClient(main.app), job


class _NullHistory:
    def sync_clip(self, *_a, **_k):
        return None


def test_u9_a_clip_starts_unreviewed():
    """Never silently approved: `pending` is the honest default for a clip nobody has seen."""
    assert ClipResult(id="a", filename="a.mp4", start=0, end=1, duration=1).review_state == (
        "pending"
    )


def test_u9_the_review_state_survives_serialisation():
    clip = ClipResult(
        id="a", filename="a.mp4", start=0, end=1, duration=1,
        review_state="approved", review_note="good",
    )
    restored = ClipResult.from_dict(clip.to_dict())
    assert restored.review_state == "approved"
    assert restored.review_note == "good"


def test_u9_a_single_clip_can_be_approved(review_client):
    client, job = review_client
    response = client.post(
        f"/api/jobs/{job.id}/clips/clip0/review", json={"review_state": "approved"}
    )
    assert response.status_code == 200
    assert response.json()["review_state"] == "approved"


def test_u9_many_clips_can_be_judged_at_once(review_client):
    client, job = review_client
    response = client.post(
        f"/api/jobs/{job.id}/clips/review",
        json={"clip_ids": ["clip0", "clip2"], "review_state": "rejected"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert {c["id"] for c in body["updated"]} == {"clip0", "clip2"}
    assert all(c["review_state"] == "rejected" for c in body["updated"])


def test_u9_untouched_clips_keep_their_state(review_client):
    """A batch action must be scoped to the selection, not applied to the whole job."""
    client, job = review_client
    client.post(
        f"/api/jobs/{job.id}/clips/review",
        json={"clip_ids": ["clip1"], "review_state": "approved"},
    )
    states = {clip.id: clip.review_state for clip in job.clips}
    assert states == {
        "clip0": "pending", "clip1": "approved", "clip2": "pending", "clip3": "pending"
    }


@pytest.mark.parametrize("state", ["maybe", "", "APPROVED", "deleted"])
def test_u9_an_unknown_review_state_is_refused(review_client, state):
    client, job = review_client
    response = client.post(
        f"/api/jobs/{job.id}/clips/clip0/review", json={"review_state": state}
    )
    assert response.status_code == 400


def test_u9_an_empty_batch_is_refused(review_client):
    client, job = review_client
    response = client.post(
        f"/api/jobs/{job.id}/clips/review",
        json={"clip_ids": [], "review_state": "approved"},
    )
    assert response.status_code == 400


def test_u9_a_batch_with_one_bad_id_still_applies_the_rest(review_client):
    """The point of a batch action is to get through a list.

    Failing the whole call because one clip has since been deleted would discard the decisions the
    user made about all the others.
    """
    client, job = review_client
    response = client.post(
        f"/api/jobs/{job.id}/clips/review",
        json={"clip_ids": ["clip0", "nope", "clip1"], "review_state": "approved"},
    )
    assert response.status_code == 200
    assert response.json()["count"] == 2


def test_u9_a_batch_of_only_bad_ids_is_a_404(review_client):
    client, job = review_client
    response = client.post(
        f"/api/jobs/{job.id}/clips/review",
        json={"clip_ids": ["nope", "also-nope"], "review_state": "approved"},
    )
    assert response.status_code == 404


def test_u9_an_unknown_job_is_a_404(review_client):
    client, _job = review_client
    response = client.post(
        "/api/jobs/no-such-job/clips/review",
        json={"clip_ids": ["clip0"], "review_state": "approved"},
    )
    assert response.status_code == 404


def test_u9_a_verdict_can_be_reset(review_client):
    client, job = review_client
    client.post(f"/api/jobs/{job.id}/clips/clip0/review", json={"review_state": "approved"})
    response = client.post(
        f"/api/jobs/{job.id}/clips/clip0/review", json={"review_state": "pending"}
    )
    assert response.json()["review_state"] == "pending"


# --------------------------------------------------------------------------- #
# U5 - the API exposes what the picker needs
# --------------------------------------------------------------------------- #


def test_u5_preset_details_are_exposed_with_web_colours():
    """A style picker cannot preview a look it only knows the name of."""
    from fastapi.testclient import TestClient

    from api.main import app

    effects = TestClient(app).get("/api/info").json()["effects"]
    details = effects["caption_preset_details"]
    assert {d["name"] for d in details} == set(effects["caption_presets"])

    hormozi = next(d for d in details if d["name"] == "hormozi")
    # The renderer's own ASS values are still reported...
    assert hormozi["colors"]["primary"].startswith("&H")
    # ...and hex equivalents are *added*, because no colour input accepts an ASS colour.
    assert hormozi["colors_hex"]["primary"].startswith("#")
    # Plus the fields the preview actually needs.
    for field in ("font", "uppercase", "position", "border_style", "font_weight"):
        assert field in hormozi
