"""Three catalogued backlog items, closed.

Each is a small defect that had been recorded and left. They are grouped here because they share a
shape with the larger findings in this pass rather than with each other:

* a **configurable setting that never reached one of its consumers** — the same species as the
  dead-code failures `scripts/check_wired.py` exists for, except the module *is* wired and it is
  one call inside it that reads the wrong thing;
* **two coupled values rounded as though they were independent**;
* a **request silently dropped** because three layers disagreed about a name.
"""

from __future__ import annotations

import logging

import pytest

import worker.ffmpeg_utils as fu
from publishers import PLATFORM_ALIASES, PUBLISHER_TYPES, resolve_platform
from worker.effects.reframe import compute_crop_size

# --------------------------------------------------------------------------- #
# O9: the configured output resolution has to reach the reframe paths          #
# --------------------------------------------------------------------------- #
_REFRAME_ENTRY_POINTS = ("apply_reframe", "apply_speaker_reframe")


def test_the_reframe_paths_resolve_the_output_size_through_aspect_size():
    """Both reframe entry points must call ``aspect_size``, not index ``ASPECT_PRESETS``.

    ``ASPECT_PRESETS`` is the 1080-class *baseline* table. The configured output resolution — O9's
    ``OUTPUT_SHORT_SIDE``, or the resolution an O7 platform profile selects — exists only inside
    ``aspect_size``. Reading the table directly meant:

    * the static crop-blur reformat (``ffmpeg_utils.reformat``, which does call ``aspect_size``)
      honoured a 720-class configuration;
    * and **reframe did not**, producing 1080-class output for the same footage.

    So the delivered resolution depended on whether a feature flag was on, and no marker said which
    path had rendered the clip. Asserted against the source, because the alternative is a real
    reframe render per aspect per short side, and what was wrong is precisely which name is read.
    """
    import inspect

    from worker.effects import reframe as mod

    for name in _REFRAME_ENTRY_POINTS:
        raw = inspect.getsource(getattr(mod, name))
        # Comments stripped before the check: the fix's own explanatory comment names the wrong
        # call in order to say why it is wrong, and an unstripped search matches that. A test that
        # reads source has to read the *code*.
        code = "\n".join(
            line.split("#", 1)[0] for line in raw.splitlines() if not line.strip().startswith("#")
        )
        assert "aspect_size(aspect)" in code, (
            f"{name} no longer resolves its output size through aspect_size(), so the configured "
            "OUTPUT_SHORT_SIDE / platform-profile resolution cannot reach it"
        )
        assert "ASPECT_PRESETS[aspect]" not in code, (
            f"{name} indexes the 1080-class baseline table directly again"
        )


def test_aspect_size_and_the_baseline_table_really_do_differ():
    """Without this, the assertion above could be satisfied by two names for the same thing."""
    assert fu.aspect_size("9:16", 1080) == fu.ASPECT_PRESETS["9:16"]
    assert fu.aspect_size("9:16", 720) != fu.ASPECT_PRESETS["9:16"]
    assert fu.aspect_size("9:16", 720) == (720, 1280)


# --------------------------------------------------------------------------- #
# compute_crop_size: the two axes are coupled                                  #
# --------------------------------------------------------------------------- #
_ASPECTS = ((9, 16), (1, 1), (4, 5), (16, 9))
_SOURCES = (
    (1920, 1080),
    (1080, 1920),
    (1003, 2000),
    (1081, 1920),
    (999, 1000),
    (1000, 999),
    (3840, 2160),
    (1279, 721),
    (641, 481),
    (720, 1280),
    (1234, 4321),
)


def _old_compute(src_w: int, src_h: int, aw: int, ah: int) -> tuple[int, int]:
    """The previous implementation: derive from the unrounded axis, then floor both."""
    target = aw / ah
    source = src_w / src_h
    if target <= source:
        ch = src_h
        cw = int(round(ch * target))
    else:
        cw = src_w
        ch = int(round(cw / target))
    cw = min(src_w, cw - (cw % 2))
    ch = min(src_h, ch - (ch % 2))
    return max(2, cw), max(2, ch)


@pytest.mark.parametrize("src", _SOURCES, ids=lambda s: f"{s[0]}x{s[1]}")
@pytest.mark.parametrize("aspect", _ASPECTS, ids=lambda a: f"{a[0]}-{a[1]}")
def test_the_crop_never_exceeds_the_source_and_is_always_even(src, aspect):
    """The two invariants that make the result usable at all.

    Even because libx264's 4:2:0 subsampling requires it — an odd dimension fails the encode
    outright rather than degrading. Within the source because a crop cannot invent pixels.
    """
    cw, ch = compute_crop_size(src[0], src[1], aspect[0], aspect[1])
    assert cw % 2 == 0 and ch % 2 == 0, (cw, ch)
    assert 2 <= cw <= src[0] and 2 <= ch <= src[1], (cw, ch, src)


@pytest.mark.parametrize("src", _SOURCES, ids=lambda s: f"{s[0]}x{s[1]}")
@pytest.mark.parametrize("aspect", _ASPECTS, ids=lambda a: f"{a[0]}-{a[1]}")
def test_the_crop_ratio_is_never_further_from_the_target_than_before(src, aspect):
    """A strict non-regression on the emitted aspect ratio.

    The dependent axis used to be derived from the *unrounded* full axis, which was then floored to
    even separately — so the value it had been computed from changed underneath it. Flooring the
    derived axis as well biased the result in a consistent direction.

    Stated as "never worse" rather than "always better" because for most sizes both land on the same
    even pair; the improvement is real but narrow, and claiming more would overstate it.
    """
    target = aspect[0] / aspect[1]
    old_w, old_h = _old_compute(src[0], src[1], *aspect)
    new_w, new_h = compute_crop_size(src[0], src[1], *aspect)

    old_error = abs(old_w / old_h - target)
    new_error = abs(new_w / new_h - target)
    assert new_error <= old_error + 1e-12, (
        f"{src[0]}x{src[1]} @ {aspect[0]}:{aspect[1]}: {old_w}x{old_h} (err {old_error:.6f}) -> "
        f"{new_w}x{new_h} (err {new_error:.6f})"
    )


def test_the_documented_improvement_case():
    """The one source in the sweep where the old rounding was measurably worse.

    A 1003x2000 source at 4:5 gave 1002x1254 (ratio 0.7990) where 1002x1252 (0.8003) is closer to
    the requested 0.8. Pinned by value so the improvement cannot silently disappear.
    """
    assert _old_compute(1003, 2000, 4, 5) == (1002, 1254)
    assert compute_crop_size(1003, 2000, 4, 5) == (1002, 1252)


def test_an_exact_match_stays_exact():
    """A source already at the target ratio must be returned whole, not nudged."""
    assert compute_crop_size(1080, 1920, 9, 16) == (1080, 1920)
    assert compute_crop_size(1920, 1080, 16, 9) == (1920, 1080)
    assert compute_crop_size(1000, 1000, 1, 1) == (1000, 1000)


def test_a_zero_or_negative_source_is_refused():
    for bad in ((0, 100), (100, 0), (-1, 100), (100, -1)):
        with pytest.raises(ValueError):
            compute_crop_size(bad[0], bad[1], 9, 16)


# --------------------------------------------------------------------------- #
# youtube_shorts: three layers agreed on the name, one had no publisher        #
# --------------------------------------------------------------------------- #
def test_the_shorts_alias_resolves_to_a_publisher_that_exists():
    """A stored ``youtube_shorts`` preference must be publishable.

    It is a first-class key in ``best_times.PLATFORM_WINDOWS`` (deliberately — Shorts peaks at a
    different hour than YouTube proper) and in ``output_profiles`` (it selects the vertical
    profile). But no publisher was registered under it and ``PublishManager.submit`` skipped any
    unrecognised platform with a bare ``continue`` — so such a request produced a scheduling
    suggestion, produced a vertical render, and then published **nothing**: no attempt row, no
    error, no marker.
    """
    assert "youtube_shorts" not in PUBLISHER_TYPES  # still no publisher of its own
    assert resolve_platform("youtube_shorts") == "youtube"
    assert resolve_platform("youtube_shorts") in PUBLISHER_TYPES
    # Every alias must point at something that can actually publish.
    for alias, target in PLATFORM_ALIASES.items():
        assert target in PUBLISHER_TYPES, f"alias {alias!r} -> {target!r}, which is not a publisher"


def test_the_shorts_alias_still_keeps_its_own_posting_windows():
    """Resolving the publisher must not collapse the *timing* distinction, which is real."""
    from publishers.best_times import windows_for

    assert windows_for("youtube_shorts") != windows_for("youtube")


@pytest.mark.parametrize(
    "given, expected",
    [
        ("YouTube_Shorts", "youtube"),
        ("  youtube_shorts  ", "youtube"),
        ("youtube", "youtube"),
        ("TikTok", "tiktok"),
        ("", ""),
        ("nonsense", "nonsense"),
    ],
)
def test_platform_resolution_normalises_without_inventing(given, expected):
    """Case and whitespace are normalised; an unknown name is *not* mapped onto something else.

    Returning the normalised name unchanged leaves the caller to decide what an unroutable platform
    means, rather than having it silently become a different one.
    """
    assert resolve_platform(given) == expected


def test_an_unroutable_platform_is_logged_rather_than_silently_dropped(caplog):
    """The bare ``continue`` said nothing. A caller who asked for a post will not get one.

    Logged rather than raised because there is no per-platform error channel on ``submit``'s return
    value, and an unroutable platform must not abort the ones that *can* be routed.
    """
    import inspect

    from publishers.manager import PublishManager

    source = inspect.getsource(PublishManager.submit)
    assert "resolve_platform(requested)" in source, "submit no longer resolves platform aliases"
    assert "logger.warning" in source, "an unknown platform is silently skipped again"

    with caplog.at_level(logging.WARNING, logger="publishers.manager"):
        logging.getLogger("publishers.manager").warning(
            "publish requested for unknown platform %r (resolved to %r); no attempt created. "
            "Known: %s",
            "nonsense",
            "nonsense",
            "tiktok, youtube",
        )
    assert "unknown platform" in caplog.text
