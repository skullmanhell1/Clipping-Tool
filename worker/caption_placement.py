"""Keeping captions off the speaker's mouth (V15).

Caption position is a fixed choice - bottom by default - and the speaker's face is wherever the
footage put it. On a lot of vertical footage those two collide: a talking head framed low, or a
reframed crop that centres a face in the lower half, ends up with three lines of heavy display type
across the speaker's mouth. It is the single most obvious way an automatically captioned clip looks
automatic, and it is invisible to everything upstream - the crop is correct, the captions are
correct, and only their *combination* is wrong.

**What "the mouth" means here.** A detected face box is the whole face; the mouth sits in its lower
portion. This uses the bottom third, which is deliberately generous: the cost of being wrong in that
direction is moving a caption that would have been fine, and the cost of being wrong in the other is
the defect this exists to fix. It is not mouth *detection* - that would be another model - and
calling it a mouth band rather than a mouth is the honest description.

**Three rules that keep this from being a look change.**

1. **It only acts on an actual overlap.** A clip whose captions were already clear is not touched,
   so this cannot quietly restyle a library of clips that had no problem.
2. **It only moves between positions a user could have chosen** - the nine C13 positions, keeping
   the horizontal alignment the preset asked for. It never invents a pixel offset, because an
   offset is a value nobody picked and nobody can preview.
3. **When every position overlaps a face it changes nothing and says so.** A close-up filling the
   frame has no clear band, and moving the caption from the mouth to the eyes is not an
   improvement. The marker is what makes that case distinguishable from "no faces detected".

**The union over the whole clip, not one frame.** A caption that is clear for two seconds and covers
the mouth for one is still wrong, and it is wrong in the way that is hardest to notice when
reviewing a still. So the bands are unioned across every sampled frame.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

#: The fraction of a face box, measured from its bottom, treated as the mouth region.
#:
#: Generous on purpose - see the module note. A tighter band would move fewer captions and miss more
#: of the collisions this exists to prevent.
MOUTH_FRACTION = 0.34

#: How much of the caption band may overlap a mouth band before it counts as a collision.
#:
#: Not zero: a one-pixel touch is not a legibility problem, and treating it as one would move
#: captions on footage where a face merely reaches the safe-area margin. Expressed as a fraction of
#: the *caption* band, because that is the thing being obscured.
OVERLAP_TOLERANCE = 0.12

#: Line height as a multiple of font size, for estimating how tall the caption block is.
#:
#: libass uses the font's own metrics, so this is an approximation. 1.25 is close for the vendored
#: display faces and errs slightly large, which is the safe direction here: over-estimating the
#: caption's height can only make it avoid a face it would have cleared.
LINE_HEIGHT_FACTOR = 1.25


@dataclass(frozen=True)
class Band:
    """A vertical region of the frame, as fractions of frame height from the top."""

    top: float
    bottom: float

    @property
    def height(self) -> float:
        return max(0.0, self.bottom - self.top)

    def overlap(self, other: "Band") -> float:
        """The overlapping height of two bands, in the same fractional units."""
        return max(0.0, min(self.bottom, other.bottom) - max(self.top, other.top))


@dataclass(frozen=True)
class PlacementPlan:
    """Where the caption should go, and why it is not where it was asked to go."""

    position: str
    requested: str
    marker: str = ""

    @property
    def moved(self) -> bool:
        return self.position != self.requested


def mouth_bands(boxes: Iterable[Any], frame_height: int) -> list[Band]:
    """The mouth band of every face box, as fractions of frame height.

    Accepts anything with ``y`` and ``h`` attributes, so a :class:`worker.effects.reframe.FaceBox`
    works and so does a test double. Boxes outside the frame or with no height are skipped rather
    than clamped: a detector returning nonsense should not produce a band that covers everything.
    """
    if frame_height <= 0:
        return []
    bands: list[Band] = []
    for box in boxes:
        try:
            y = float(getattr(box, "y"))
            h = float(getattr(box, "h"))
        except (AttributeError, TypeError, ValueError):
            continue
        if h <= 0 or y < 0 or y + h > frame_height * 1.5:
            continue
        mouth_top = y + h * (1.0 - MOUTH_FRACTION)
        band = Band(
            top=max(0.0, mouth_top / frame_height),
            bottom=min(1.0, (y + h) / frame_height),
        )
        if band.height > 0:
            bands.append(band)
    return bands


def caption_band(
    position: str,
    *,
    frame_height: int,
    font_size: int,
    max_lines: int = 2,
    margin_px: Optional[int] = None,
) -> Band:
    """The vertical band a caption at ``position`` would occupy.

    ``margin_px`` defaults to the position's own margin from ``captions._POSITION_ALIGN``, which is
    the number the ASS file will actually carry unless a C12 safe area is configured - in which case
    the caller should pass the safe-area margin it computed, because otherwise this would reason
    about a caption placed somewhere else.

    Height is estimated from the font size and line budget (see :data:`LINE_HEIGHT_FACTOR`); libass
    lays out from the font's real metrics, so this is approximate and errs large.
    """
    from worker.captions import _POSITION_ALIGN

    align, default_margin = _POSITION_ALIGN.get(position, _POSITION_ALIGN["bottom"])
    margin = float(default_margin if margin_px is None else margin_px)
    height = float(font_size) * max(1, int(max_lines)) * LINE_HEIGHT_FACTOR
    if frame_height <= 0:
        return Band(0.0, 0.0)

    if align in (1, 2, 3):          # bottom-anchored
        bottom = frame_height - margin
        top = bottom - height
    elif align in (7, 8, 9):        # top-anchored
        top = margin
        bottom = top + height
    else:                           # middle-anchored: the margin is not used
        centre = frame_height / 2.0
        top = centre - height / 2.0
        bottom = centre + height / 2.0

    return Band(
        top=max(0.0, top / frame_height),
        bottom=min(1.0, bottom / frame_height),
    )


def _collides(position: str, mouths: Sequence[Band], **band_kwargs: Any) -> bool:
    band = caption_band(position, **band_kwargs)
    if band.height <= 0:
        return False
    allowed = band.height * OVERLAP_TOLERANCE
    return any(band.overlap(mouth) > allowed for mouth in mouths)


def _alternatives(requested: str) -> list[str]:
    """Positions to try instead of ``requested``, in preference order.

    The horizontal component is preserved - a preset that asked for bottom-left is a design
    decision, and answering it with centre-top changes two things to fix one. Vertical order puts
    ``top`` before ``center`` because a centred caption over footage is the most intrusive of the
    three even when it clears the mouth.
    """
    parts = requested.split("_", 1)
    suffix = f"_{parts[1]}" if len(parts) == 2 else ""
    vertical = parts[0]
    order = {
        "bottom": ["top", "center"],
        "top": ["bottom", "center"],
        "center": ["bottom", "top"],
    }.get(vertical, ["top", "bottom", "center"])
    return [f"{name}{suffix}" for name in order]


def choose_position(
    requested: str,
    face_boxes: Iterable[Any],
    *,
    frame_height: int,
    font_size: int,
    max_lines: int = 2,
    margin_px: Optional[int] = None,
) -> PlacementPlan:
    """Pick a caption position that clears the speakers' mouths (V15).

    Returns ``requested`` unchanged - with no marker - when it already clears them, when no faces
    were detected, or when nothing clears them. Those three cases are distinguished by the marker,
    because "V15 found nothing to do" and "V15 could not help" look identical in the output and only
    one of them is a limitation worth knowing about.
    """
    position = str(requested or "bottom")
    mouths = mouth_bands(face_boxes, frame_height)
    if not mouths:
        return PlacementPlan(position, position)

    band_kwargs = {
        "frame_height": frame_height,
        "font_size": font_size,
        "max_lines": max_lines,
        "margin_px": margin_px,
    }
    if not _collides(position, mouths, **band_kwargs):
        return PlacementPlan(position, position)

    for candidate in _alternatives(position):
        if not _collides(candidate, mouths, **band_kwargs):
            return PlacementPlan(
                candidate, position, marker=f"caption_moved_off_face:{candidate}"
            )

    # A close-up filling the frame. Moving the caption from the mouth to the eyes is not an
    # improvement, so nothing changes - but the clip record says why.
    return PlacementPlan(position, position, marker="caption_face_overlap_unavoidable")



def plan_for_clip(
    clip: Any,
    *,
    requested: str,
    frame_height: int,
    font_size: int,
    max_lines: int = 2,
    face_boxes: Optional[Iterable[Any]] = None,
) -> PlacementPlan:
    """Choose a caption position for ``clip``, detecting faces if needed (V15).

    Returns ``requested`` unchanged and inert when ``caption_avoid_faces`` is off. That default is
    not timidity: this needs a face-detection pass over the clip, and a render that never had a
    collision would be paying for it to learn that. It is also a *look* change on the clips it does
    act on, and every other new visual behaviour in this project defaults to what already shipped so
    the golden renders can still detect an accidental change.

    ``face_boxes`` lets a caller pass boxes it already has - the reframe path detects them anyway,
    so on a reframed clip this should cost nothing extra.
    """
    from config import settings

    position = str(requested or "bottom")
    if not getattr(settings, "caption_avoid_faces", False):
        return PlacementPlan(position, position)

    boxes: list[Any] = list(face_boxes) if face_boxes is not None else []
    if not boxes:
        try:
            from worker.effects import reframe

            # Flattened across sampled frames: the union over the whole clip is the question, and
            # `detect_faces` returns a list per frame.
            boxes = [box for frame in reframe.detect_faces(clip) for box in frame]
        except Exception as exc:      # noqa: BLE001
            # Deliberately broad: this is a placement refinement on a clip whose expensive work is
            # already done, and every failure mode of the vision stack (a missing cv2, an
            # unopenable file, a cascade that will not load) is a reason to caption where the user
            # asked rather than to lose the clip.
            logger.warning("V15: face detection failed for %s: %s", clip, exc)
            return PlacementPlan(position, position, marker="caption_face_detect_failed")

    return choose_position(
        position, boxes,
        frame_height=frame_height, font_size=font_size, max_lines=max_lines,
    )
