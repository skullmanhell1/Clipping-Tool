"""Speech repair and per-platform output: AU4, AU5, O7, O12.

* **AU4** speech de-noise (`afftdn`, or `arnndn` with a supplied model);
* **AU5** de-esser (the de-reverb half is deliberately absent - see below);
* **O7** per-platform output profiles;
* **O12** burned-in vs soft captions.

The filter strings here are checked against **real ffmpeg** wherever a filter is named, because
"is `deesser` in this build" is a fact about ffmpeg rather than about this repo. That check earned
its place: `arnndn` is present in ffmpeg and still unusable, since it needs a model file that
ffmpeg does not ship - a test that only asserted the filter name would have called that working.
"""

from __future__ import annotations

import subprocess

import pytest

from config import settings as app_settings
from tests.conftest import FFMPEG, requires_ffmpeg
from worker import output_profiles as op
from worker import subtitle_export
from worker.effects import audio
from worker.ffmpeg_utils import aspect_size, h264_args, mux_soft_subtitles
from worker.segmentation import resolve_length_range


def _filter_runs(chain: str) -> tuple[bool, str]:
    """Whether ffmpeg accepts ``chain`` as an audio filter chain, plus its full stderr.

    The whole stderr, not the first line: ffmpeg reports the *specific* cause (a bad model file,
    an unknown filter) before the generic ``Error initializing filters`` summary, so a first-line
    check tells you only that something went wrong.
    """
    proc = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.2",
            "-af",
            chain,
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0, (proc.stderr or "").strip()


# --------------------------------------------------------------------------- #
# AU4 - de-noise
# --------------------------------------------------------------------------- #


def test_au4_is_off_by_default():
    """Noise reduction is among the easiest ways to make a recording worse."""
    assert app_settings.speech_denoise == "off"
    assert audio.denoise_filter("off") is None
    assert audio.denoise_filter("") is None
    assert audio.denoise_filter("none") is None


@pytest.mark.parametrize("strength", ["light", "standard", "strong"])
def test_au4_each_strength_produces_an_afftdn_filter(strength):
    made = audio.denoise_filter(strength)
    assert made is not None
    assert made.startswith("afftdn=")


def test_au4_stronger_settings_remove_more():
    """The strengths have to be ordered, or they are three arbitrary presets."""
    levels = [audio.DENOISE_LEVELS[name] for name in ("light", "standard", "strong")]
    reductions = [nr for nr, _nf in levels]
    assert reductions == sorted(reductions)
    # The assumed noise floor rises with strength: a stronger setting treats more as noise.
    floors = [nf for _nr, nf in levels]
    assert floors == sorted(floors)


def test_au4_an_unknown_strength_falls_back_to_standard():
    """A typo should not silently disable a filter the operator asked for."""
    assert audio.denoise_filter("aggressive") == audio.denoise_filter("standard")


def test_au4_uses_arnndn_when_a_model_is_supplied(tmp_path):
    """arnndn is the better filter; it is only usable with a model."""
    model = tmp_path / "sh.rnnn"
    model.write_bytes(b"not-a-real-model")
    made = audio.denoise_filter("standard", str(model))
    assert made is not None
    assert made.startswith("arnndn=m=")
    assert "sh.rnnn" in made


def test_au4_a_missing_model_degrades_instead_of_failing(tmp_path):
    """The point of de-noising is a publishable clip, not a strict dependency check."""
    made = audio.denoise_filter("standard", str(tmp_path / "absent.rnnn"))
    assert made == audio.denoise_filter("standard", "")
    assert made is not None and made.startswith("afftdn=")


@requires_ffmpeg
@pytest.mark.parametrize("strength", ["light", "standard", "strong"])
def test_au4_the_emitted_filter_is_one_ffmpeg_accepts(strength):
    chain = audio.denoise_filter(strength)
    ok, error = _filter_runs(chain)
    assert ok, f"{chain!r} rejected: {error}"


@requires_ffmpeg
def test_au4_arnndn_exists_but_is_unusable_without_a_model(tmp_path):
    """Records *why* afftdn is the default, rather than leaving it looking like a preference.

    The plan item says arnndn "is available in ffmpeg". The filter is; the capability is not,
    because ffmpeg ships no models. This asserts both halves - the filter is present, and a
    plausible-looking model file is still rejected - so nobody removes the afftdn path believing
    arnndn was ready to use.
    """
    # A path that does not exist: ffmpeg names the missing model explicitly.
    missing_ok, missing_error = _filter_runs(f"arnndn=m='{tmp_path / 'absent.rnnn'}'")
    assert not missing_ok
    assert "model" in missing_error.lower(), missing_error

    # A file that exists but is not a model: also rejected, so simply shipping *a* file is not
    # enough either.
    bogus = tmp_path / "sh.rnnn"
    bogus.write_bytes(b"not-a-real-model")
    bogus_ok, bogus_error = _filter_runs(f"arnndn=m='{bogus}'")
    assert not bogus_ok

    # ...and the failures are about the model, not about arnndn being absent from this build.
    # A genuinely unknown filter fails differently, which is what makes that distinction real.
    unknown_ok, unknown_error = _filter_runs("notafilter")
    assert not unknown_ok
    assert "no such filter" in unknown_error.lower()
    for message in (missing_error, bogus_error):
        assert "no such filter" not in message.lower()


# --------------------------------------------------------------------------- #
# AU5 - de-esser
# --------------------------------------------------------------------------- #


def test_au5_is_off_by_default():
    assert app_settings.deesser == "off"
    assert audio.deesser_filter("off") is None


@pytest.mark.parametrize("strength", ["light", "standard", "strong"])
def test_au5_each_strength_produces_a_deesser_filter(strength):
    made = audio.deesser_filter(strength)
    assert made is not None
    assert made.startswith("deesser=i=")


def test_au5_even_the_strongest_setting_stays_short_of_full_intensity():
    """At i=1.0 consonants lose definition: a different defect, not a fix."""
    assert audio.DEESSER_LEVELS["strong"] < 1.0
    values = [audio.DEESSER_LEVELS[n] for n in ("light", "standard", "strong")]
    assert values == sorted(values)


@requires_ffmpeg
@pytest.mark.parametrize("strength", ["light", "standard", "strong"])
def test_au5_the_emitted_filter_is_one_ffmpeg_accepts(strength):
    chain = audio.deesser_filter(strength)
    ok, error = _filter_runs(chain)
    assert ok, f"{chain!r} rejected: {error}"


def test_au5_does_not_claim_to_de_reverb():
    """AU5 asks for de-reverb too. ffmpeg has no such filter, so it is absent, not faked.

    Guards against a later change that quietly relabels a high-pass or a gate as de-reverb: the
    two are not the same process, and shipping one under the other's name is worse than the gap.
    """
    assert not hasattr(audio, "dereverb_filter")
    for strength in ("light", "standard", "strong"):
        chain = audio.deesser_filter(strength) or ""
        assert "highpass" not in chain
        assert "agate" not in chain


# --------------------------------------------------------------------------- #
# the combined repair chain
# --------------------------------------------------------------------------- #


def test_the_repair_chain_cleans_before_it_de_esses():
    """De-noising changes the very band a de-esser keys on, so it has to run first."""
    chain = audio.speech_repair_chain(denoise="standard", deess="standard")
    assert len(chain) == 2
    assert chain[0].startswith("afftdn")
    assert chain[1].startswith("deesser")


def test_the_repair_chain_is_empty_when_both_are_off():
    """The default: the audio graph is exactly what it was before AU4/AU5."""
    assert audio.speech_repair_chain(denoise="off", deess="off") == []


def test_the_repair_chain_holds_only_what_is_enabled():
    assert len(audio.speech_repair_chain(denoise="light", deess="off")) == 1
    assert len(audio.speech_repair_chain(denoise="off", deess="light")) == 1


@requires_ffmpeg
def test_the_whole_repair_chain_runs_as_one_filter_string():
    chain = ",".join(audio.speech_repair_chain(denoise="standard", deess="standard"))
    ok, error = _filter_runs(chain)
    assert ok, f"{chain!r} rejected: {error}"


def test_the_repair_chain_reaches_the_compositor_graph(tmp_path, monkeypatch):
    """A filter builder nothing calls is not a feature."""
    from tests.test_kinetic_compositor import (
        MATRIX_HOOK,
        MATRIX_WORDS,
        _matrix_options,
        _parity_render,
    )
    from worker.effects import compositor

    monkeypatch.setattr(app_settings, "speech_denoise", "standard", raising=False)
    monkeypatch.setattr(app_settings, "deesser", "standard", raising=False)
    record = _parity_render(
        compositor,
        tmp_path / "repair",
        options=_matrix_options(captions=True),
        words=MATRIX_WORDS,
        hook_text=MATRIX_HOOK,
        contributions=None,
    )
    assert "afftdn" in record.graph
    assert "deesser" in record.graph
    assert "speech_denoise" in record.effects_applied
    assert "deesser" in record.effects_applied


def test_speech_repair_precedes_the_music_mix(tmp_path, monkeypatch):
    """Cleaning after the mix would attack the bed as well as the room tone.

    This has to render **with music present**, which is the whole point. Written first without a
    bed, where the music branch never executes: pointing the mixer back at the raw ``0:a`` - so
    that the cleaned audio was computed and then thrown away - passed that version untouched.
    """
    from tests.test_kinetic_compositor import (
        MATRIX_WORDS,
        _matrix_options,
        _parity_render,
    )
    from worker.effects import compositor

    bed = tmp_path / "bed.m4a"
    bed.parent.mkdir(parents=True, exist_ok=True)
    bed.write_bytes(b"stub-music")
    monkeypatch.setattr(app_settings, "speech_denoise", "standard", raising=False)
    monkeypatch.setattr(
        compositor.audio,
        "resolve_music_bed",
        lambda *_a, **_k: audio.MusicBed(path=bed, mood="chill", source=audio.SOURCE_USER_TRACK),
        raising=False,
    )
    record = _parity_render(
        compositor,
        tmp_path / "order",
        options=_matrix_options(captions=True, music="chill"),
        words=MATRIX_WORDS,
        hook_text="",
        contributions=None,
    )
    graph = record.graph
    assert "music:chill" in record.effects_applied, "the music branch did not run"
    # Cleaned from the raw input...
    assert "[0:a]afftdn" in graph
    # ...and the mix consumes the *cleaned* label, not the raw one.
    assert "[aclean]" in graph
    mix = [part for part in graph.split(";") if "amix" in part or "sidechain" in part]
    assert mix, graph
    assert any("aclean" in part for part in mix), mix


def test_no_repair_leaves_the_audio_graph_untouched(tmp_path, monkeypatch):
    from tests.test_kinetic_compositor import (
        MATRIX_WORDS,
        _matrix_options,
        _parity_render,
    )
    from worker.effects import compositor

    monkeypatch.setattr(app_settings, "speech_denoise", "off", raising=False)
    monkeypatch.setattr(app_settings, "deesser", "off", raising=False)
    record = _parity_render(
        compositor,
        tmp_path / "clean",
        options=_matrix_options(captions=True),
        words=MATRIX_WORDS,
        hook_text="",
        contributions=None,
    )
    assert "afftdn" not in record.graph
    assert "aclean" not in record.graph


# --------------------------------------------------------------------------- #
# O7 - per-platform output profiles
# --------------------------------------------------------------------------- #


def test_o7_no_platform_configured_means_no_profile():
    """The default has to be the previous behaviour: the explicit settings."""
    assert app_settings.output_platform == ""
    assert op.active_profile() is None
    assert op.describe() == {}
    assert op.duration_ceiling_s() is None


def test_o7_an_unknown_platform_has_no_opinion():
    """A typo must not silently re-encode everything at some default profile."""
    assert op.profile_for("myspace") is None
    assert op.profile_for("") is None


@pytest.mark.parametrize(
    "platform,aspect",
    [
        ("tiktok", "9:16"),
        ("instagram", "9:16"),
        ("youtube_shorts", "9:16"),
        ("youtube", "16:9"),
        ("whop", "9:16"),
    ],
)
def test_o7_each_platform_has_the_shape_its_product_uses(platform, aspect):
    profile = op.profile_for(platform)
    assert profile is not None
    assert profile.aspect == aspect


def test_o7_youtube_and_shorts_are_separate_profiles():
    """Guessing between a landscape upload and a vertical Short would be wrong half the time."""
    assert op.profile_for("youtube").aspect == "16:9"
    assert op.profile_for("youtube_shorts").aspect == "9:16"


def test_o7_the_shorts_ceiling_is_the_shorts_limit_not_youtubes():
    """Preflight has no Shorts entry, so reading it gave a one-hour ceiling for Shorts.

    Correctly so on preflight's side - a Shorts upload *is* a YouTube upload and is validated as
    one - which is exactly why the profile needs its own override.
    """
    shorts = op.profile_for("youtube_shorts")
    assert shorts.max_duration_s == 180.0
    assert op.profile_for("youtube").max_duration_s > shorts.max_duration_s


def test_o7_duration_ceilings_come_from_the_preflight_table():
    """One number, one home: the encoder and the validator cannot disagree."""
    from publishers.preflight import limits_for

    for platform in ("tiktok", "instagram", "youtube", "x", "whop"):
        assert op.profile_for(platform).max_duration_s == limits_for(platform).max_duration_s


def test_o7_the_profile_caps_clip_length(monkeypatch):
    """A clip longer than the destination takes fails at upload, after a full render."""
    monkeypatch.setattr(app_settings, "output_platform", "x", raising=False)
    _min_len, max_len, target = resolve_length_range("90s-3min")
    assert max_len == 140.0  # X's limit, down from the preset's 180
    assert target <= max_len


def test_o7_the_cap_never_raises_the_floor_or_exceeds_itself(monkeypatch):
    monkeypatch.setattr(app_settings, "output_platform", "x", raising=False)
    for option in ("auto", "<30s", "30-60s", "60-90s", "90s-3min"):
        min_len, max_len, target = resolve_length_range(option)
        assert min_len <= max_len
        assert min_len <= target <= max_len


def test_o7_a_generous_platform_does_not_shorten_clips(monkeypatch):
    """The cap only ever lowers a ceiling; it must not touch a range it does not bind."""
    monkeypatch.setattr(app_settings, "output_platform", "", raising=False)
    baseline = resolve_length_range("90s-3min")
    monkeypatch.setattr(app_settings, "output_platform", "youtube", raising=False)
    assert resolve_length_range("90s-3min") == baseline


def test_o7_the_profile_sets_the_resolution(monkeypatch):
    monkeypatch.setattr(app_settings, "output_platform", "x", raising=False)
    assert op.resolve_short_side() == 720
    assert aspect_size("16:9") == (1280, 720)


def test_o7_an_explicit_resolution_beats_the_profile(monkeypatch):
    """The profile fills in what the operator has not chosen; it does not overrule them."""
    monkeypatch.setattr(app_settings, "output_platform", "x", raising=False)
    monkeypatch.setattr(app_settings, "output_short_side", 1440, raising=False)
    assert op.resolve_short_side() == 1440


def test_o7_the_profile_sets_the_bitrate_ceiling(monkeypatch):
    monkeypatch.setattr(app_settings, "output_platform", "x", raising=False)
    assert op.resolve_max_bitrate_kbps() == 5_000
    args = h264_args(vbv_cap=True)
    assert "5000k" in args
    assert "10000k" in args  # bufsize is twice maxrate


def test_o7_an_explicit_bitrate_beats_the_profile(monkeypatch):
    """Regression on a real bug: the sentinel was compared against the wrong default.

    This was first written comparing the configured bitrate against a hardcoded 10000 while the
    field's declared default is 12000, so every install looked explicitly configured and the
    profile ceiling never applied.
    """
    monkeypatch.setattr(app_settings, "output_platform", "x", raising=False)
    monkeypatch.setattr(app_settings, "output_max_bitrate_kbps", 8_000, raising=False)
    assert op.resolve_max_bitrate_kbps() == 8_000


def test_o7_an_untouched_default_is_recognised_as_untouched():
    """The mechanism the two tests above rely on, asserted directly against the model."""
    assert op._is_untouched("output_max_bitrate_kbps") is True
    assert op._is_untouched("output_short_side") is True
    assert op._is_untouched("no_such_setting") is False


def test_o7_the_aspect_is_advisory_and_never_applied_silently(monkeypatch):
    """Re-shaping an explicit request because a platform was named fights the interface."""
    monkeypatch.setattr(app_settings, "output_platform", "youtube", raising=False)
    # The landscape profile is active, and a 9:16 render is still 9:16.
    assert op.active_profile().aspect == "16:9"
    assert aspect_size("9:16") == (1080, 1920)
    # There is no resolver that would rewrite it.
    assert not hasattr(op, "resolve_aspect")


def test_o7_describe_reports_what_is_actually_in_force(monkeypatch):
    monkeypatch.setattr(app_settings, "output_platform", "x", raising=False)
    described = op.describe()
    assert described["name"] == "x"
    assert described["effective_short_side"] == 720
    assert described["effective_max_bitrate_kbps"] == 5_000
    monkeypatch.setattr(app_settings, "output_short_side", 1440, raising=False)
    assert op.describe()["effective_short_side"] == 1440


def test_o7_profile_bitrates_scale_with_resolution():
    sides = sorted(op._BITRATE_BY_SHORT_SIDE)
    rates = [op._BITRATE_BY_SHORT_SIDE[s] for s in sides]
    assert rates == sorted(rates)


# --------------------------------------------------------------------------- #
# O12 - burned vs soft captions
# --------------------------------------------------------------------------- #


def test_o12_burned_is_the_default():
    """The feeds autoplay muted; nobody enables a subtitle track."""
    assert app_settings.caption_mode == "burned"


def test_o12_soft_mode_stops_the_burn_in(tmp_path, monkeypatch):
    from tests.test_kinetic_compositor import (
        MATRIX_WORDS,
        _matrix_options,
        _parity_render,
    )
    from worker.effects import compositor

    monkeypatch.setattr(app_settings, "caption_mode", "soft", raising=False)
    record = _parity_render(
        compositor,
        tmp_path / "soft",
        options=_matrix_options(captions=True),
        words=MATRIX_WORDS,
        hook_text="",
        contributions=None,
    )
    assert "subtitles=" not in record.graph


def test_o12_burned_and_both_still_burn(tmp_path, monkeypatch):
    from tests.test_kinetic_compositor import (
        MATRIX_WORDS,
        _matrix_options,
        _parity_render,
    )
    from worker.effects import compositor

    for mode in ("burned", "both"):
        monkeypatch.setattr(app_settings, "caption_mode", mode, raising=False)
        record = _parity_render(
            compositor,
            tmp_path / f"mode-{mode}",
            options=_matrix_options(captions=True),
            words=MATRIX_WORDS,
            hook_text="",
            contributions=None,
        )
        assert "subtitles=" in record.graph, mode


def test_o12_soft_mode_still_burns_the_hook_title(tmp_path, monkeypatch):
    """A hook is a title card, not a caption; there is no soft equivalent of one."""
    from tests.test_kinetic_compositor import (
        MATRIX_HOOK,
        _matrix_options,
        _parity_render,
    )
    from worker.effects import compositor

    monkeypatch.setattr(app_settings, "caption_mode", "soft", raising=False)
    record = _parity_render(
        compositor,
        tmp_path / "hook",
        options=_matrix_options(captions=False, hook_title=True),
        words=[],
        hook_text=MATRIX_HOOK,
        contributions=None,
    )
    assert "subtitles=" in record.graph
    assert "hook_title" in record.effects_applied


@requires_ffmpeg
def test_o12_the_soft_track_is_a_real_selectable_subtitle_stream(tmp_path, make_video):
    """The whole claim of soft captions: a player can find and switch off the track."""
    src = make_video("src.mp4", duration=3.0, w=640, h=360)
    srt = tmp_path / "caps.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:02,000\nhello there\n\n", encoding="utf-8")
    out = tmp_path / "soft.mp4"
    mux_soft_subtitles(src, srt, out)

    streams = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "s",
            "-show_entries",
            "stream=codec_name:stream_tags=language",
            "-of",
            "csv=p=0",
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert "mov_text" in streams
    assert "eng" in streams


@requires_ffmpeg
def test_o12_muxing_does_not_re_encode_the_video(tmp_path, make_video):
    """It is a remux: no generation loss, and no second encode of a finished clip."""
    src = make_video("src.mp4", duration=3.0, w=640, h=360)
    srt = tmp_path / "caps.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:02,000\nhi\n\n", encoding="utf-8")
    out = tmp_path / "soft.mp4"
    mux_soft_subtitles(src, srt, out)

    def video_md5(path):
        return subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-map",
                "0:v",
                "-c",
                "copy",
                "-f",
                "md5",
                "-",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    assert video_md5(out) == video_md5(src)


@requires_ffmpeg
def test_o12_the_soft_track_carries_the_caption_text(tmp_path, make_video):
    src = make_video("src.mp4", duration=3.0, w=640, h=360)
    srt = tmp_path / "caps.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:02,000\nunmistakable phrase\n\n", encoding="utf-8")
    out = tmp_path / "soft.mp4"
    mux_soft_subtitles(src, srt, out)
    extracted = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(out),
            "-map",
            "0:s:0",
            "-f",
            "srt",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "unmistakable phrase" in extracted


def test_o12_sidecar_srt_generation_is_reused_rather_than_reimplemented(tmp_path):
    """The soft track is built from the same cue generation as the O11 sidecars."""
    from tests.test_kinetic_compositor import MATRIX_WORDS

    written = subtitle_export.write_sidecars(MATRIX_WORDS, tmp_path / "clip", formats=("srt",))
    assert len(written) == 1
    assert written[0].suffix == ".srt"
    assert written[0].read_text(encoding="utf-8").strip()
