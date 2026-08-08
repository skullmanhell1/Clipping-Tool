"""Headroom and eye-line composition (V22).

Two requirements carry the weight, and both are easy to satisfy incorrectly in a way that still
produces a plausible-looking clip:

* **R1.5 — the bias is applied after smoothing.** `smooth_centers` resets its EMA at every detected
  cut, so a bias folded into the samples upstream ramps in again after each shot boundary. The
  result is a slow vertical drift per cut that reads as bad tracking rather than as a setting.
* **R1.4 — the bias can never push the crop outside valid pixels.** ffmpeg's `crop` with an
  out-of-range offset either errors or silently clamps depending on version, so the geometry has to
  be right here rather than hopefully right later.

And R1.7: zero is the default and must be **byte-identical** to v0.11.0, which is asserted against
the real `build_sendcmd` rather than against the pure helper.
"""

from __future__ import annotations

import pytest

from worker import headroom as hr
from worker.effects.reframe import Center, build_sendcmd

# --- R1.7: zero is exact parity -------------------------------------------------------------


def test_zero_bias_produces_identical_sendcmd_output():
    """R1.7. The default must move nothing at all.

    Asserted through `build_sendcmd` rather than the helper, because that is what writes the crop
    geometry — a helper returning the right number while the call site rounds differently would
    still move every golden.
    """
    centers = [Center(t / 10.0, 500.0 + t, 400.0 + t) for t in range(30)]
    with_bias = build_sendcmd(centers, 1080, 1920, 2160, 3840, headroom_bias=0.0)
    without = build_sendcmd(centers, 1080, 1920, 2160, 3840)
    assert with_bias == without


def test_the_configured_default_is_zero():
    """R1.7/R1.8. A look change needs a preference trial, not an opinion."""
    from config import settings

    assert float(settings.reframe_headroom_bias) == 0.0


# --- R1.1/R1.3: the bias lifts the subject --------------------------------------------------


def test_the_crop_moves_down_so_the_subject_moves_up():
    """The direction inverts, which is the easiest thing here to get backwards.

    The crop's top edge is `cy - crop_h/2`, so *increasing* cy slides the window down the source and
    lifts the subject within the delivered frame.
    """
    baseline = hr.biased_center_y(540.0, 1080, 2160, bias=0.0)
    lifted = hr.biased_center_y(540.0, 1080, 2160, bias=0.10)
    assert lifted > baseline


def test_the_shift_is_a_fraction_of_crop_height_not_source_height():
    """R1.3. The crop is what the viewer sees, so the same bias must compose the same way.

    Two sources of very different height, identical crop: the shift must match, or the composition
    would depend on the source resolution.
    """
    small = hr.biased_center_y(540.0, 1080, 1200, bias=0.05) - 540.0
    large = hr.biased_center_y(540.0, 1080, 4000, bias=0.05) - 540.0
    assert small == pytest.approx(large)
    assert small == pytest.approx(1080 * 0.05)


@pytest.mark.parametrize(("bias", "expected"), [(0.0, 0.0), (0.05, 54.0), (0.10, 108.0)])
def test_the_shift_is_exactly_the_documented_fraction(bias, expected):
    """Computed from the crop height here, independently of the implementation."""
    assert hr.headroom_shift(1080, bias) == pytest.approx(expected)


def test_the_bias_increases_the_eye_lines_margin_above_the_midpoint():
    """R1.1, stated carefully.

    A centred face already has its eye line marginally above the frame midpoint, because eyes sit
    above the middle of a brow-to-chin box. So "is it above centre?" is true even unbiased, and an
    earlier version of this test asserted the unbiased case was *false* — which was simply wrong
    about the geometry rather than about the code.

    What the bias actually buys is **margin**: the eye line moves meaningfully clear of the midpoint
    instead of sitting a few pixels above it. That is the measurable claim.
    """
    crop_h, src_h = 1080, 2160
    face_top, face_height = 480.0, 240.0  # box centred on 600
    eyes = hr.eye_line(face_top, face_height)

    unbiased = hr.biased_center_y(600.0, crop_h, src_h, bias=0.0)
    biased = hr.biased_center_y(600.0, crop_h, src_h, bias=0.08)

    assert hr.eye_line_is_above_midpoint(face_top, face_height, biased, crop_h)
    margin_before = unbiased - eyes
    margin_after = biased - eyes
    assert margin_after > margin_before
    # And the gain is the documented shift, derived here from the crop height.
    assert margin_after - margin_before == pytest.approx(crop_h * 0.08)


# --- R1.2: derived from the box, never a fixed offset ---------------------------------------


def test_the_eye_line_scales_with_the_face_box():
    """R1.2. A fixed pixel offset is the forehead on a close-up and empty sky on a wide shot."""
    close_up = hr.eye_line(100.0, 600.0) - 100.0
    wide = hr.eye_line(100.0, 80.0) - 100.0
    assert close_up > wide * 5, "the offset must scale with the subject, not be constant"
    assert close_up == pytest.approx(600.0 * hr.EYE_LINE_FRACTION)


def test_the_eye_line_sits_above_the_box_centre():
    """Eyes are above the middle of a brow-to-chin box, which is what the fraction encodes."""
    top, height = 0.0, 100.0
    assert hr.eye_line(top, height) < top + height / 2.0


# --- R1.4: the clamp ------------------------------------------------------------------------


def test_the_bias_cannot_push_the_crop_below_the_source():
    """R1.4. A subject near the bottom edge plus a bias must not slide the window off the source."""
    crop_h, src_h = 540, 1080
    biased = hr.biased_center_y(1000.0, crop_h, src_h, bias=0.25)
    top = biased - crop_h / 2.0
    assert top >= 0.0
    assert top + crop_h <= src_h + 1e-9


def test_the_clamp_respects_a_content_rectangle():
    """`origin_y`/`src_h` describe the *content* rectangle, not the whole frame.

    That is how V16 letterbox handling and V21 stabilisation both confine the crop, so the bias must
    not push the window into a bar it was specifically kept out of.
    """
    biased = hr.biased_center_y(300.0, 400, 600, bias=0.25, origin_y=100.0)
    top = biased - 400 / 2.0
    assert top >= 100.0 - 1e-9
    assert top + 400 <= 100.0 + 600 + 1e-9


def test_a_crop_with_no_vertical_room_leaves_the_centre_alone():
    """There is no valid range to bias within, so nothing is attempted.

    Returning the original centre rather than a clamped one matters: clamping would *move* the crop
    on a source whose geometry is already fully determined. Both the taller-than and the
    exactly-equal case, since equality is where an earlier `<` comparison let the clamp through.
    """
    assert hr.biased_center_y(500.0, 1200, 1000, bias=0.2) == 500.0
    assert hr.biased_center_y(500.0, 1080, 1080, bias=0.2) == 500.0


@pytest.mark.parametrize("bias", [0.0, 0.01, 0.05, 0.1, 0.25, 0.5, 5.0, -1.0])
def test_the_crop_stays_valid_for_every_bias(bias):
    """The clamp holds across the range, including values outside the accepted one."""
    crop_h, src_h = 960, 1080
    biased = hr.biased_center_y(700.0, crop_h, src_h, bias=bias)
    top = biased - crop_h / 2.0
    assert -1e-9 <= top <= src_h - crop_h + 1e-9


# --- bias validation ------------------------------------------------------------------------


def test_a_negative_bias_is_clamped_to_zero_rather_than_inverted():
    """A negative bias moves the subject *down*, which is not headroom and nobody asked for it."""
    assert hr.clamp_bias(-0.1) == 0.0
    assert hr.headroom_shift(1080, -0.1) == 0.0


def test_an_over_large_bias_is_clamped_rather_than_rejected():
    """A mistyped setting must not fail a render; beyond a quarter-frame the composition is worse."""
    assert hr.clamp_bias(0.9) == hr.MAX_BIAS
    assert hr.clamp_bias(5.0) == hr.MAX_BIAS


def test_unusable_values_fall_back_to_zero():
    assert hr.clamp_bias(float("nan")) == 0.0
    assert hr.clamp_bias("not-a-number") == 0.0
    assert hr.clamp_bias(None) == 0.0


# --- R1.5: applied after smoothing ----------------------------------------------------------


def test_the_bias_is_not_attenuated_after_a_cut():
    """R1.5, and the reason it is a requirement rather than a detail.

    `smooth_centers` resets its EMA at each detected cut. A bias folded into the samples upstream
    would ramp in again after every shot boundary, producing a slow vertical drift per cut that
    reads as bad tracking.

    Modelled by checking the property that matters: the shift is **identical on every frame**,
    including the first, where an EMA would still be converging.
    """
    # The subject must sit clear of both edges, or the crop is already pinned by the clamp and the
    # bias has nowhere to move it -- which is how an earlier version of this fixture measured a zero
    # shift and looked like a broken bias. 1920 is the middle of a 3840-tall source.
    centers = [Center(i / 12.0, 1080.0, 1920.0) for i in range(24)]
    biased = build_sendcmd(centers, 1080, 1920, 2160, 3840, headroom_bias=0.10)
    unbiased = build_sendcmd(centers, 1080, 1920, 2160, 3840, headroom_bias=0.0)

    def crop_ys(script: str) -> list[int]:
        ys = []
        for line in script.splitlines():
            if "y " in line:
                ys.append(int(line.rsplit(None, 1)[-1].rstrip(";")))
        return ys

    biased_ys, plain_ys = crop_ys(biased), crop_ys(unbiased)
    assert biased_ys and len(biased_ys) == len(plain_ys)
    deltas = {b - p for b, p in zip(biased_ys, plain_ys, strict=True)}
    assert len(deltas) == 1, f"the shift must be constant across frames, got {sorted(deltas)}"
    assert deltas.pop() > 0


def test_the_bias_leaves_the_horizontal_axis_untouched():
    """Headroom is vertical. A horizontal shift would be a framing change nobody asked for."""
    centers = [Center(0.0, 800.0, 1920.0), Center(1.0, 1300.0, 1920.0)]
    biased = build_sendcmd(centers, 1080, 1920, 2160, 3840, headroom_bias=0.10)
    plain = build_sendcmd(centers, 1080, 1920, 2160, 3840, headroom_bias=0.0)

    def x_terms(script: str) -> list[str]:
        """Just the `crop x N` term from each command.

        The whole line reads `0.000 crop x 0, crop y 0;`, so comparing lines compares the y value
        too and can never distinguish the axes — which is what an earlier version of this test did.
        """
        out = []
        for line in script.splitlines():
            for term in line.split(","):
                if " x " in term:
                    out.append(term.strip().rstrip(";"))
        return out

    assert x_terms(biased) == x_terms(plain)
    assert x_terms(biased), "precondition: the script must contain x terms to compare"


# --- R1.9: the marker -----------------------------------------------------------------------


def test_the_marker_names_the_applied_value_not_the_requested_one():
    """R1.9, and the project's standing marker rule.

    An operator reading back the setting they know they set learns nothing; the marker exists to
    report what actually happened when it differed.
    """
    assert hr.marker(0.08) == "headroom_bias:0.080"
    assert hr.marker(0.9) == f"headroom_bias:{hr.MAX_BIAS:.3f}", "a clamped value must be reported"


def test_no_marker_at_the_default():
    """A marker on every clip is noise, and noise is what stops a marker being read."""
    assert hr.marker(0.0) == ""
    assert hr.marker(-1.0) == ""
