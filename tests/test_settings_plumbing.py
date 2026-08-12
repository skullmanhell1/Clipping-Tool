"""Four settings that were documented and read by nothing now take effect.

`scripts/check_wired.py` found thirteen `Settings` fields that no production code read. Setting one
did nothing at all, silently — which is worse than an unsupported option, because it *looks*
supported. Four are plumbed here; the other eight were retired, and the documentation gate
(`tests/test_config_documentation.py`) is what enforces that they left `.env.example` with them.

Each test below asserts a *difference*: two values must produce two behaviours. Asserting only that
the configured value arrives somewhere would pass just as well against a hard-coded default that
happened to match.
"""

from __future__ import annotations

import pytest

from tests.conftest import options_all_off, requires_ffmpeg
from worker.transcribe import Transcript, TranscriptSegment, Word


def _words() -> list[Word]:
    return [Word(0.2, 0.6, "one"), Word(0.8, 1.2, "two"), Word(1.4, 1.8, "three")]


def _spy_reformat(monkeypatch, tmp_path, make_video, **option_overrides) -> list[dict]:
    """Run one clip and return the kwargs every `reformat_aspect` call received."""
    import worker.pipeline as pl
    from worker.selection import ClipCandidate

    src = make_video("bg.mp4", duration=2.0, w=640, h=360)
    monkeypatch.setattr(
        pl,
        "transcribe",
        lambda *a, **k: Transcript(
            language="en", segments=[TranscriptSegment(0.0, 2.0, "one two three", _words())]
        ),
    )
    monkeypatch.setattr(
        pl.sel,
        "select_moments",
        lambda *a, **k: [
            ClipCandidate(start=0.0, end=2.0, score=90.0, reason="r", title="T", text="t")
        ],
    )
    monkeypatch.setattr(pl.compositor, "render_clip", lambda *a, **k: None)

    seen: list[dict] = []
    real = pl.fu.reformat_aspect

    def spy(source, dest, **kwargs):
        seen.append(dict(kwargs))
        return real(source, dest, **kwargs)

    monkeypatch.setattr(pl.fu, "reformat_aspect", spy)
    pl.run_pipeline(
        src,
        options_all_off(aspect="9:16", **option_overrides),
        clips_dir=tmp_path / "clips",
        temp_dir=tmp_path / "tmp",
    )
    return seen


# --------------------------------------------------------------------------- #
# V11 background_style / background_color                                     #
# --------------------------------------------------------------------------- #


@requires_ffmpeg
def test_the_configured_background_style_reaches_the_reformat(monkeypatch, tmp_path, make_video):
    """V11 built `blur | mirror | black | color | gradient` and no call site passed any of it.

    So `BACKGROUND_STYLE=black` — which the setting's own description recommends for screen
    recordings — was unreachable, and every clip got `blur` because that is `reformat_aspect`'s own
    parameter default.
    """
    import worker.pipeline as pl

    monkeypatch.setattr(pl.settings, "background_style", "black")
    calls = _spy_reformat(monkeypatch, tmp_path, make_video)

    crop_blur = [c for c in calls if c.get("mode") == "crop_blur"]
    assert crop_blur, f"no crop_blur reformat happened: {calls}"
    assert crop_blur[0].get("background") == "black"


@requires_ffmpeg
def test_two_background_styles_produce_two_different_filter_graphs(
    monkeypatch, tmp_path, make_video
):
    """The discriminator. Without it, the test above could pass against a coincidence."""
    import worker.ffmpeg_utils as fu

    blur = fu.background_chain(fu.resolve_background_style("blur"), 1080, 1920)
    black = fu.background_chain(fu.resolve_background_style("black"), 1080, 1920)

    assert blur != black


@requires_ffmpeg
def test_the_configured_fill_colour_reaches_the_reformat(monkeypatch, tmp_path, make_video):
    """`background_color` only matters for the `color` style, but must arrive regardless."""
    import worker.pipeline as pl

    monkeypatch.setattr(pl.settings, "background_style", "color")
    monkeypatch.setattr(pl.settings, "background_color", "0x123456")
    calls = _spy_reformat(monkeypatch, tmp_path, make_video)

    crop_blur = [c for c in calls if c.get("mode") == "crop_blur"]
    assert crop_blur
    assert crop_blur[0].get("background_color") == "0x123456"


@requires_ffmpeg
def test_the_pad_branch_is_not_given_a_background_it_would_ignore(
    monkeypatch, tmp_path, make_video
):
    """`reformat_aspect` consults `background` only in `crop_blur` mode.

    `pad` letterboxes with black by definition, so passing a style there would read as configured
    behaviour that silently does nothing — the exact class of defect this change is removing. The
    `pad` branch is taken when V24 classifies the clip as a screen recording.
    """
    import worker.pipeline as pl

    monkeypatch.setattr(pl.settings, "background_style", "mirror")
    monkeypatch.setattr(
        pl.content_class,
        "fit_instead_of_crop",
        lambda *a, **k: True,
    )
    calls = _spy_reformat(monkeypatch, tmp_path, make_video)

    pad = [c for c in calls if c.get("mode") == "pad"]
    assert pad, f"the pad branch was never taken, so this test proves nothing: {calls}"
    assert "background" not in pad[0], (
        "the pad branch was handed a background style it cannot honour"
    )


def test_an_unknown_background_style_falls_back_to_what_shipped(monkeypatch):
    """A typo must not reach ffmpeg as a filter fragment and fail the clip."""
    import worker.pipeline as pl

    monkeypatch.setattr(pl.settings, "background_style", "not-a-style")

    assert pl._background_style() == "blur"


def test_the_background_style_is_case_and_space_insensitive(monkeypatch):
    """`.env` files acquire trailing spaces and capitals; neither should silently disable a feature."""
    import worker.pipeline as pl

    monkeypatch.setattr(pl.settings, "background_style", "  BLACK ")

    assert pl._background_style() == "black"


# --------------------------------------------------------------------------- #
# music_default_volume                                                        #
# --------------------------------------------------------------------------- #


def test_the_configured_music_default_governs_a_job_that_does_not_specify_one(monkeypatch):
    """The literal `0.12` existed in four places and the documented setting in none of them."""
    import worker.models as wm

    monkeypatch.setattr(wm._settings, "music_default_volume", 0.4)

    assert wm.ProcessingOptions().music_volume == pytest.approx(0.4)


def test_an_explicit_music_volume_still_wins(monkeypatch):
    """A default is not an override. A job that states a value keeps it."""
    import worker.models as wm

    monkeypatch.setattr(wm._settings, "music_default_volume", 0.4)

    assert wm.ProcessingOptions(music_volume=0.9).music_volume == pytest.approx(0.9)


def test_a_malformed_music_volume_falls_back_to_the_configured_default(monkeypatch):
    """`from_dict` never raises; it falls back — and now to the setting, not to a fourth literal."""
    import worker.models as wm

    monkeypatch.setattr(wm._settings, "music_default_volume", 0.4)
    options = wm.ProcessingOptions.from_dict({"music_volume": "loud"})

    assert options.music_volume == pytest.approx(0.4)


def test_a_malformed_configured_music_default_falls_back_to_what_shipped(monkeypatch):
    """A bad setting must not become a bad filter argument."""
    import worker.models as wm

    monkeypatch.setattr(wm._settings, "music_default_volume", "very loud")

    assert wm.ProcessingOptions().music_volume == pytest.approx(0.12)


def test_an_out_of_range_configured_music_default_is_clamped(monkeypatch):
    """`amix` takes a 0..1 level; 4.0 would be a distorted bed rather than a loud one."""
    import worker.models as wm

    monkeypatch.setattr(wm._settings, "music_default_volume", 4.0)

    assert wm.ProcessingOptions().music_volume == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# face_detector_backend                                                       #
# --------------------------------------------------------------------------- #


def test_the_configured_detector_governs_a_job_that_does_not_specify_one(monkeypatch):
    """The setting documented itself as "the detector used when a job does not specify one".

    It was consulted by nothing: `resolve_detector` is only ever called with the per-job option, and
    that option's default was the literal `"haar"`. So the documented default was unreachable and
    `FACE_DETECTOR_BACKEND=mediapipe` had no effect on any render.
    """
    import worker.models as wm

    monkeypatch.setattr(wm._settings, "face_detector_backend", "mediapipe")

    assert wm.ProcessingOptions().face_detector == "mediapipe"


def test_an_explicit_detector_still_wins(monkeypatch):
    import worker.models as wm

    monkeypatch.setattr(wm._settings, "face_detector_backend", "mediapipe")

    assert wm.ProcessingOptions(face_detector="haar").face_detector == "haar"


def test_an_unknown_configured_detector_falls_back_to_haar(monkeypatch):
    """An unknown backend name would otherwise reach `resolve_detector` and disable detection."""
    import worker.models as wm

    monkeypatch.setattr(wm._settings, "face_detector_backend", "blazeface")

    assert wm.ProcessingOptions().face_detector == "haar"


def test_an_unknown_per_job_detector_falls_back_to_the_configured_default(monkeypatch):
    """`from_dict`'s enum fallback is the configured default now, not a literal."""
    import worker.models as wm

    monkeypatch.setattr(wm._settings, "face_detector_backend", "mediapipe")
    options = wm.ProcessingOptions.from_dict({"face_detector": "nonsense"})

    assert options.face_detector == "mediapipe"


# --------------------------------------------------------------------------- #
# The eight retired settings                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    [
        "api_host",
        "api_port",
        "redis_url",
        "rq_queue_name",
        "use_inprocess_fallback",
        "public_base_url",
        "x_api_key",
        "x_api_secret",
    ],
)
def test_a_retired_setting_is_gone_rather_than_inert(name):
    """These eight described behaviour this project does not have.

    `REDIS_URL`, `RQ_QUEUE_NAME` and `USE_INPROCESS_FALLBACK` described a queue that does not exist —
    there is no `import redis` or `import rq` anywhere, and `JobManager` is a single-worker
    `ThreadPoolExecutor`. `API_HOST` and `API_PORT` cannot override a bind that the container's `CMD`,
    `EXPOSE` and healthcheck URL all fix independently. `PUBLIC_BASE_URL` had no reader and not even a
    description. `X_API_KEY`/`X_API_SECRET` are OAuth1 consumer credentials, and `publishers/x.py`
    authenticates solely with a Bearer token — plumbing them would mean *implementing OAuth1 signing*,
    which is a feature, not a wiring fix.

    `Settings` uses `extra="ignore"`, so a stale key in someone's `.env` stays harmless. Keeping the
    field would have been the harmful choice: it reads as supported.
    """
    from config import Settings

    assert name not in Settings.model_fields, (
        f"{name} is back in Settings; if it is being reintroduced it needs a reader, not a default"
    )
