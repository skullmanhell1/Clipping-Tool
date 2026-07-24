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


def zoom_filter(
    duration: float,
    fps: float,
    width: int,
    height: int,
    ken_burns: bool = False,
    punch_in: bool = False,
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
    if not (ken_burns or punch_in):
        return None

    fps = max(1.0, float(fps or 30.0))
    total = max(1, int(round(max(0.1, duration) * fps)))
    settle = max(1, int(round(0.5 * fps)))  # punch-in settle window (frames)

    # Base (end-state) zoom: a gentle Ken Burns ramp, or a constant 1.0.
    if ken_burns:
        base = f"(1+0.12*on/{total})"
    else:
        base = "1"

    if punch_in:
        # Ease from 1.18 down to the base over `settle` frames, then hold base.
        expr = f"if(lt(on,{settle}),1.18-(1.18-{base})*on/{settle},{base})"
    else:
        expr = base

    # zoompan pans around the centre as it zooms; d=1 keeps 1 input->1 output.
    return (
        f"zoompan=z='{expr}':d=1:fps={fps:g}:s={width}x{height}:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
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


def progress_bar_filter(
    duration: float,
    width: int,
    height: int,
    thickness: int = 12,
    color: str = "0x22D3EE",
) -> str:
    """Return a ``drawbox`` filter drawing a bottom progress bar that fills.

    The bar width grows linearly with playback time ``t`` across ``duration``.
    """
    dur = max(0.1, duration)
    return (
        f"drawbox=x=0:y=ih-{thickness}:w='iw*t/{dur:.3f}':h={thickness}:"
        f"color={color}@0.9:t=fill"
    )


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
) -> list[str]:
    """Assemble the ordered list of video filters for the single look pass.

    Order: colour grade -> zoom/punch-in -> fades -> captions/hook (subtitles)
    -> progress bar (drawn last so it stays on top). Disabled effects are
    omitted, so an all-off configuration yields an empty (or subtitles-only)
    chain.
    """
    chain: list[str] = []

    c = color_filter(color)
    if c:
        chain.append(c)

    z = zoom_filter(duration, fps, width, height, ken_burns=zoom, punch_in=transitions)
    if z:
        chain.append(z)

    if fades:
        f = video_fade_filter(duration)
        if f:
            chain.append(f)

    if subtitles:
        chain.append(subtitles)

    if progress_bar:
        chain.append(progress_bar_filter(duration, width, height))

    return chain
