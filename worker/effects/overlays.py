"""Composable ffmpeg video-filter builders for the "easy" visual effects.

Every builder returns a **filter string** (or ``None`` when disabled) so the
:mod:`worker.effects.compositor` can assemble them into a single efficient
video pass. Keeping them as pure string builders makes them trivially testable
without invoking ffmpeg.

Effects covered here:
    * zoom / punch-in (``zoompan``)
    * colour adjustment presets (``eq`` / ``hue``)
    * fade in-out (video ``fade``; audio handled in :mod:`worker.effects.audio`)
    * progress bar (``drawbox`` with a time-based width)

Text (hook titles, captions) is rendered via libass in :mod:`worker.captions`
rather than ``drawtext`` so it works on ffmpeg builds without freetype.
"""

from __future__ import annotations

from typing import Optional

# Colour presets -> an ffmpeg `eq`/`hue` filter chain.
COLOR_PRESETS: dict[str, str] = {
    "vivid": "eq=contrast=1.12:saturation=1.35:brightness=0.02",
    "warm": "eq=saturation=1.15:gamma_r=1.06:gamma_b=0.94",
    "cool": "eq=saturation=1.1:gamma_b=1.08:gamma_r=0.95",
    "cinematic": "eq=contrast=1.15:saturation=0.9:gamma=0.95,curves=preset=medium_contrast",
    "bw": "hue=s=0,eq=contrast=1.1",
}

MUSIC_MOODS = ("upbeat", "chill", "dramatic", "corporate", "suspense")


def color_filter(preset: str) -> Optional[str]:
    """Return an ``eq``/``hue`` filter chain for a colour preset, or ``None``."""
    if not preset:
        return None
    return COLOR_PRESETS.get(preset)


#: Opening-transition styles (V9).
#:
#: A note on what V9 asks for versus what this product can hold. The plan says "no clip-to-clip
#: transitions exist anywhere ... add cross-dissolve, whip-pan, zoom-cut". There is nowhere for a
#: *clip-to-clip* transition to live: every clip is an independent deliverable, published to a
#: different place at a different time, and a transition needs two shots that meet. What each of
#: those named effects can be here is the treatment of a clip's own opening, which is where they
#: would be seen anyway - the first half-second, which is also what S6 measures.
#:
#: * ``punch_in``  - the original: start at 1.18x and settle. Eased, so it reads as camera move.
#: * ``zoom_cut``  - start wider and *step* to the base zoom with no easing, which reads as an
#:                   edit rather than a movement.
#: * ``whip_pan``  - a fast lateral slide that decelerates into place.
#: * ``dissolve``  - fade up from black, the mildest of the four.
TRANSITION_STYLES: tuple[str, ...] = ("punch_in", "zoom_cut", "whip_pan", "dissolve")

#: How long an opening transition lasts, in seconds. Short on purpose: the opening is the most
#: valuable time in a short-form clip, and a transition spending a second of it is a transition
#: that cost more than it gave.
TRANSITION_S = 0.5


def dissolve_filter(style: str, duration: float) -> Optional[str]:
    """The fade component of a ``dissolve`` opening (V9), or ``None`` for other styles.

    Kept separate from :func:`zoom_filter` because a dissolve is not a zoom: expressing it as
    one would mean either faking a fade with a zoom or attaching an unrelated fade inside a
    function whose whole contract is "return a zoompan".
    """
    if style != "dissolve":
        return None
    length = min(TRANSITION_S, max(0.1, float(duration) / 4.0))
    return f"fade=t=in:st=0:d={length:.3f}"


def zoom_filter(
    duration: float,
    fps: float,
    width: int,
    height: int,
    ken_burns: bool = False,
    punch_in: bool = False,
    *,
    style: str = "punch_in",
) -> Optional[str]:
    """Return a ``zoompan`` filter for a slow zoom and/or a punch-in intro.

    Args:
        duration: Clip duration in seconds.
        fps: Output frame rate to drive the per-frame zoom expression.
        width/height: Target frame size (kept unchanged; the frame is zoomed).
        ken_burns: Slowly zoom from 1.0x to ~1.12x across the whole clip.
        punch_in: Start at ~1.18x and settle to the base zoom over ~0.5s.

    Returns ``None`` when neither effect is requested.
    """
    # A dissolve is a fade, not a zoom, so it contributes nothing here - and with Ken Burns off
    # there is then no zoompan at all, which keeps the pass out of the graph entirely.
    # An unrecognised style degrades to the shipped punch-in rather than to *no transition*.
    # Silently dropping the effect would be the worse failure: a typo in a setting would look
    # like the feature had been switched off, which is indistinguishable from it being broken.
    if style not in TRANSITION_STYLES:
        style = "punch_in"
    zooming = punch_in and style in ("punch_in", "zoom_cut", "whip_pan")
    if not (ken_burns or zooming):
        return None

    fps = max(1.0, float(fps or 30.0))
    total = max(1, int(round(max(0.1, duration) * fps)))
    settle = max(1, int(round(TRANSITION_S * fps)))

    # Base (end-state) zoom: a gentle Ken Burns ramp, or a constant 1.0.
    if ken_burns:
        base = f"(1+0.12*on/{total})"
    else:
        base = "1"

    x_expr = "iw/2-(iw/zoom/2)"
    if zooming and style == "zoom_cut":
        # A step, not a ramp: held wide, then cut to base. `lt` alone gives exactly that, and
        # the absence of easing is the whole effect - it reads as an edit rather than a move.
        expr = f"if(lt(on,{settle}),1.35,{base})"
    elif zooming and style == "whip_pan":
        # Zoomed enough to have somewhere to pan from, sliding in from the left. Quadratic
        # ease-out, so it decelerates into place instead of stopping dead.
        expr = f"if(lt(on,{settle}),1.20,{base})"
        offset = f"(1-pow(on/{settle},2))"
        x_expr = f"if(lt(on,{settle}),iw/2-(iw/zoom/2)-(iw/zoom/2)*{offset},iw/2-(iw/zoom/2))"
    elif zooming:
        # punch_in, the original: ease from 1.18 down to base over `settle` frames.
        expr = f"if(lt(on,{settle}),1.18-(1.18-{base})*on/{settle},{base})"
    else:
        expr = base

    # zoompan pans around the centre as it zooms; d=1 keeps 1 input->1 output.
    return (
        f"zoompan=z='{expr}':d=1:fps={fps:g}:s={width}x{height}:"
        f"x='{x_expr}':y='ih/2-(ih/zoom/2)'"
    )


def video_fade_filter(
    duration: float, fade_dur: float = 0.4
) -> Optional[str]:
    """Return a video ``fade`` in/out filter, or ``None`` for very short clips."""
    if duration <= 2 * fade_dur + 0.2:
        # Too short to fade both ends cleanly; fade in only.
        return f"fade=t=in:st=0:d={min(fade_dur, duration / 3):.3f}"
    out_start = max(0.0, duration - fade_dur)
    return (
        f"fade=t=in:st=0:d={fade_dur:.3f},"
        f"fade=t=out:st={out_start:.3f}:d={fade_dur:.3f}"
    )


#: Where the progress bar sits (V13).
PROGRESS_POSITIONS: tuple[str, ...] = ("bottom", "top")

#: How it is drawn (V13).
#:
#: ``bar`` is the original solid fill. ``track`` draws a dim full-width rail under the fill, so
#: the viewer can see how much is *left* rather than only how much has passed - which is the
#: information a progress bar exists to convey, and the original could not express it.
PROGRESS_STYLES: tuple[str, ...] = ("bar", "track")


def progress_bar_filter(
    duration: float,
    width: int,
    height: int,
    thickness: int = 12,
    color: str = "0x22D3EE",
    *,
    position: str = "bottom",
    style: str = "bar",
    track_color: str = "0xFFFFFF",
) -> str:
    """Return the ``drawbox`` filter(s) drawing a progress bar that fills (V13).

    The bar width grows linearly with playback time ``t`` across ``duration``.

    Position and style were both hard-coded: a 12px bar in one cyan, always at the bottom -
    where, on a 9:16 clip, it sits directly under the captions and competes with them. ``top``
    exists for that reason rather than for variety.

    The track is drawn *before* the fill so the fill covers it; drawn after, the rail would sit
    on top of the progress it is meant to sit behind.
    """
    dur = max(0.1, duration)
    thickness = max(1, int(thickness))
    y = "0" if position == "top" else f"ih-{thickness}"

    boxes = []
    if style == "track":
        boxes.append(
            f"drawbox=x=0:y={y}:w=iw:h={thickness}:color={track_color}@0.25:t=fill"
        )
    boxes.append(
        f"drawbox=x=0:y={y}:w='iw*t/{dur:.3f}':h={thickness}:color={color}@0.9:t=fill"
    )
    return ",".join(boxes)


def build_video_chain(
    *,
    duration: float,
    fps: float,
    width: int,
    height: int,
    color: str = "",
    zoom: bool = False,
    transitions: bool = False,
    fades: bool = False,
    progress_bar: bool = False,
    subtitles: Optional[str] = None,
    transition_style: str = "punch_in",
    progress_position: str = "bottom",
    progress_style: str = "bar",
    progress_color: str = "0x22D3EE",
    progress_thickness: int = 12,
) -> list[str]:
    """Assemble the ordered list of video filters for the single look pass.

    Order: colour grade -> zoom/punch-in -> opening dissolve -> fades ->
    captions/hook (subtitles) -> progress bar (drawn last so it stays on top).
    Disabled effects are omitted, so an all-off configuration yields an empty
    (or subtitles-only) chain.

    The V9/V13 style arguments are keyword-only with the previous values as defaults, so every
    existing caller - and the v0.8.0 parity gate - produces a byte-identical chain.
    """
    chain: list[str] = []

    c = color_filter(color)
    if c:
        chain.append(c)

    z = zoom_filter(
        duration, fps, width, height,
        ken_burns=zoom, punch_in=transitions, style=transition_style,
    )
    if z:
        chain.append(z)

    if transitions:
        # V9: a dissolve opening is a fade rather than a zoom, so it is a separate filter. It
        # goes before the general fades, so on a clip with both the opening fade is the shorter
        # one and reads as the transition rather than fighting it.
        d = dissolve_filter(transition_style, duration)
        if d:
            chain.append(d)

    if fades:
        f = video_fade_filter(duration)
        if f:
            chain.append(f)

    if subtitles:
        chain.append(subtitles)

    if progress_bar:
        chain.append(
            progress_bar_filter(
                duration, width, height,
                thickness=progress_thickness, color=progress_color,
                position=progress_position, style=progress_style,
            )
        )

    return chain
