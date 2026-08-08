"""Headroom and eye-line composition for the reframe crop (V22).

`reframe.py` centres the crop on the detected face's centre. That is the most recognisable
auto-crop tell there is: a human editor places a subject's **eyes** near the upper third of the
frame, not their nose in the exact middle. Centred framing reads as machine-made even when the
tracking is perfect.

It has a second cost that matters more here. Centring the face vertically pushes the **mouth**
towards the middle of the frame — which on 9:16 is precisely where captions sit. So the default
framing actively creates the collision `V15` (face-aware caption placement) exists to work around,
and improving the framing reduces how often that has to fire at all.

**The bias is applied after smoothing** (R1.5), and that is not a detail. A constant offset folded
into the samples before the EMA survives in steady state but is re-attenuated on every cut, because
`smooth_centers` resets the filter at each detected shot boundary (`reset_at=breaks`). The bias would
then ramp in over the first frames of every shot — a slow vertical drift after each cut, which looks
like bad tracking rather than like a setting. Applying it downstream of the smoother makes it exact
everywhere.

**The eye line comes from the face box, never from a fixed pixel offset** (R1.2). A fixed offset is
wrong at every distance: 60 px above centre is the forehead on a close-up and the sky on a wide
shot. Derived as a fraction of the detected box height, it scales with the subject.

**Default zero** (R1.7, R1.8). This is a *look* change, and the spec is explicit that a non-zero
default needs a preference trial (M12) rather than an assertion. Zero reproduces v0.11.0 framing
exactly, so no golden or parity fixture moves until somebody actually judges it.
"""

from __future__ import annotations

#: Where the eyes sit within a detected face box, as a fraction of box height from its top.
#:
#: Face detectors return a box spanning roughly brow to chin, and within that the eyes sit a little
#: above the middle. 0.4 rather than the 0.33 of the classical "rule of thirds for heads", because
#: that rule measures from the top of the *skull* and a detector box usually starts lower.
#:
#: PROVISIONAL: this positions the reference line the bias is measured against, and only a
#: preference trial could justify moving it.
EYE_LINE_FRACTION = 0.4

#: Maximum bias accepted, as a fraction of crop height.
#:
#: A quarter of the frame. Beyond this the subject is against the top edge and the composition is
#: worse than centred, so an out-of-range setting is clamped rather than honoured — a mistyped
#: value should not produce a clip with a head touching the frame edge.
MAX_BIAS = 0.25


def eye_line(face_top: float, face_height: float) -> float:
    """The estimated eye-line y for a face box (R1.2).

    A fraction of the box, so it scales with the subject: the same function gives a sensible answer
    on a close-up and on a wide shot, where a fixed pixel offset gives the forehead on one and empty
    sky on the other.
    """
    return float(face_top) + float(face_height) * EYE_LINE_FRACTION


def clamp_bias(bias: float) -> float:
    """Bring ``bias`` into the accepted range.

    Negative values are clamped to zero rather than honoured: a negative bias moves the subject
    *down*, which is not headroom and is never what someone meant to ask for. An over-large value is
    clamped rather than rejected, because a mistyped setting must not fail a render.
    """
    try:
        value = float(bias)
    except (TypeError, ValueError):
        return 0.0
    if value != value:  # NaN
        return 0.0
    return max(0.0, min(MAX_BIAS, value))


def headroom_shift(crop_h: float, bias: float) -> float:
    """Pixels to move the crop window **down**, which moves the subject **up** in frame.

    Expressed as a fraction of crop height (R1.3) rather than of source height, because the crop is
    what the viewer sees: the same bias then produces the same composition whether the source is
    1080p or 4K.

    The direction is worth stating because it inverts. The crop's top edge is ``cy - crop_h/2``, so
    *increasing* ``cy`` slides the window down the source, which lifts the subject within the
    delivered frame.
    """
    return max(0.0, float(crop_h)) * clamp_bias(bias)


def biased_center_y(
    center_y: float,
    crop_h: float,
    src_h: float,
    *,
    bias: float,
    origin_y: float = 0.0,
) -> float:
    """``center_y`` shifted for headroom, clamped so the crop stays inside valid pixels (R1.4).

    The clamp is the requirement rather than defensive coding: an unclamped bias on a subject already
    near the bottom of the frame slides the crop past the source's edge, and ffmpeg's `crop` with an
    out-of-range offset either errors or silently clamps depending on version — so the geometry has
    to be correct here rather than hopefully correct later.

    ``origin_y``/``src_h`` describe the *content* rectangle, which is how `V16` letterbox handling
    and `V21` stabilisation both confine the crop. Respecting them means the bias cannot push the
    window into a letterbox bar it was specifically kept out of.
    """
    # No vertical room at all: the crop is as tall as the content, or taller. There is exactly one
    # valid position (or none), so biasing is meaningless -- and *clamping* here would be worse than
    # doing nothing, because it would move a crop whose geometry is already fully determined.
    # Returned before the shift is even computed, so the intent is unambiguous.
    if float(crop_h) >= float(src_h):
        return float(center_y)

    shifted = float(center_y) + headroom_shift(crop_h, bias)

    # The crop's top-left must stay within [origin, origin + src - crop].
    half = float(crop_h) / 2.0
    lowest_center = float(origin_y) + half
    highest_center = float(origin_y) + (float(src_h) - float(crop_h)) + half
    return max(lowest_center, min(highest_center, shifted))


def eye_line_is_above_midpoint(
    face_top: float,
    face_height: float,
    center_y: float,
    crop_h: float,
) -> bool:
    """Whether the subject's eye line lands above the delivered frame's vertical midpoint (R1.1).

    A predicate rather than an assertion inside the geometry, so the requirement can be tested
    directly and so a caller can report the outcome. ``center_y`` is the *biased* centre, so the
    frame's midpoint in source coordinates is exactly ``center_y``.
    """
    return eye_line(face_top, face_height) < float(center_y)


def marker(bias: float) -> str:
    """The ``Effects_Applied`` entry, or ``""`` when no bias was applied (R1.9).

    Names the **resolved** value, not the requested one, so a clamped setting reports what actually
    happened. No marker at zero: a marker on every clip is noise, and noise is what stops a marker
    being read.
    """
    applied = clamp_bias(bias)
    return f"headroom_bias:{applied:.3f}" if applied > 0 else ""
