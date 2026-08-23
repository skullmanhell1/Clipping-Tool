#!/usr/bin/env python3
"""Render one clip with every effect on, for eyeball review before a release (M2).

The problem this addresses is recorded in the improvement plan: *nothing measured the real
output*. The suite is thorough about filter strings, markers and graph shapes, and a font
that resolved to the wrong face, an emoji upscaled 2.1x, and "music" that was a sine drone all
survived it — because a caption can be spelled correctly in an ASS file and still look wrong
on screen. Some defects only exist visually, and the only instrument for those is a person
looking at a video.

So this is deliberately not a test. It asserts nothing about appearance. It produces one file
with everything turned on, prints what went into it, and leaves the judgement to you.

    python scripts/smoke_reel.py                     # writes storage/temp/smoke_reel.mp4
    python scripts/smoke_reel.py --source clip.mp4   # use real footage instead of a pattern
    python scripts/smoke_reel.py --out /tmp/reel.mp4

With no ``--source`` it synthesises a test pattern with a spoken-word-shaped audio track.
That exercises the whole render path, but bear in mind what a synthetic source *cannot* show
you: face-tracked reframing has no face to find, and captions come from a supplied word list
rather than from Whisper. Pass real footage for a genuine pre-release look.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from config import settings  # noqa: E402
from worker.effects import audio, compositor  # noqa: E402
from worker.models import BUILTIN_PROFILES, ProcessingOptions  # noqa: E402

#: Words for the caption track, chosen to exercise several things at once: keyword emphasis
#: (C11 ranks these), inline and overlay emoji (A10 stems "winning" to "win"), and a line
#: long enough to wrap at the 3-word cue limit (C5).
SMOKE_WORDS = [
    ("this", 0.20, 0.55),
    ("changed", 0.60, 1.05),
    ("everything", 1.10, 1.75),
    ("we", 1.85, 2.05),
    ("won", 2.10, 2.45),
    ("$5000", 2.50, 3.10),
    ("in", 3.15, 3.30),
    ("one", 3.35, 3.65),
    ("week", 3.70, 4.20),
    ("and", 4.30, 4.50),
    ("the", 4.55, 4.70),
    ("fire", 4.75, 5.25),
    ("was", 5.30, 5.55),
    ("insane", 5.60, 6.30),
]


class _Word:
    """A minimal Whisper-word stand-in (``start``/``end``/``text``/``probability``)."""

    __slots__ = ("start", "end", "text", "probability")

    def __init__(self, text: str, start: float, end: float) -> None:
        self.text = text
        self.start = start
        self.end = end
        self.probability = 0.95


def _synthetic_source(dest: Path, ffmpeg: str, seconds: float = 7.0) -> Path:
    """A 1080x1920 test pattern with a speech-shaped audio track.

    The audio is a tone gated into bursts rather than a continuous one, so ducking and
    loudness normalisation have something with gaps in it to work on — a solid tone would make
    the sidechain compressor look permanently engaged.
    """
    gate = "+".join(f"between(t,{start:.2f},{end:.2f})" for _text, start, end in SMOKE_WORDS)
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=s=1080x1920:d={seconds}:r=30",
            "-f",
            "lavfi",
            "-i",
            f"sine=f=220:d={seconds}:sample_rate=48000",
            "-af",
            f"volume='0.35*({gate})':eval=frame",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-y",
            str(dest),
        ],
        check=True,
        capture_output=True,
        timeout=300,
        stdin=subprocess.DEVNULL,
    )
    return dest


def _face_frames(dest_dir: Path, seconds: float, fps: int) -> Path:
    """Draw a frame sequence containing a profile turn and a two-shot.

    The face-detection upgrade needs a source with faces in it, and this script's docstring
    already records why the plain test pattern is not enough: *face-tracked reframing has no
    face to find*. Drawn rather than vendored because a photograph of a real person is a
    licensing and privacy question a smoke reel does not need to answer.

    Three acts, so the two things a better detector is supposed to fix are both on screen:

    1. **frontal, drifting** -- the easy case, and the baseline both backends should manage;
    2. **profile turn** -- the face narrows and its features slide to one side. A frontal Haar
       cascade characteristically loses this, which is visible as the crop freezing;
    3. **two-shot** -- two faces at once, where the interesting question is which one the crop
       follows and whether it swaps between them.
    """
    from PIL import Image, ImageDraw

    width, height = 1280, 720
    dest_dir.mkdir(parents=True, exist_ok=True)
    total = int(seconds * fps)

    def draw_face(draw, cx, cy, scale=1.0, profile=0.0):
        """``profile`` 0 = frontal, 1 = fully turned; features slide and the oval narrows."""
        rx = int(95 * scale * (1.0 - 0.45 * profile))
        ry = int(125 * scale)
        shift = int(28 * scale * profile)
        draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=(226, 190, 160))
        draw.ellipse(
            [cx - rx - 6, cy - ry - int(30 * scale), cx + rx + 6, cy - ry + int(55 * scale)],
            fill=(60, 42, 32),
        )
        for offset in (-38, 38):
            if profile > 0.75 and offset < 0:
                continue  # the far eye disappears as the head turns
            ex = cx + int(offset * scale * (1.0 - 0.6 * profile)) + shift
            draw.ellipse(
                [
                    ex - int(16 * scale),
                    cy - int(32 * scale),
                    ex + int(16 * scale),
                    cy - int(10 * scale),
                ],
                fill=(250, 250, 250),
            )
            draw.ellipse(
                [
                    ex - int(7 * scale),
                    cy - int(27 * scale),
                    ex + int(7 * scale),
                    cy - int(13 * scale),
                ],
                fill=(35, 28, 24),
            )
            draw.rectangle(
                [
                    ex - int(18 * scale),
                    cy - int(46 * scale),
                    ex + int(18 * scale),
                    cy - int(40 * scale),
                ],
                fill=(60, 42, 32),
            )
        nose_x = cx + shift
        draw.polygon(
            [
                (nose_x, cy - int(8 * scale)),
                (nose_x - int(11 * scale), cy + int(26 * scale)),
                (nose_x + int(11 * scale), cy + int(26 * scale)),
            ],
            fill=(203, 165, 138),
        )
        draw.ellipse(
            [
                nose_x - int(34 * scale),
                cy + int(45 * scale),
                nose_x + int(34 * scale),
                cy + int(72 * scale),
            ],
            fill=(150, 70, 70),
        )

    for index in range(total):
        t = index / fps
        frac = t / seconds
        image = Image.new("RGB", (width, height), (58, 74, 96))
        draw = ImageDraw.Draw(image)
        # A little background structure, so the crop's movement is visible on screen.
        for gx in range(0, width, 160):
            draw.line([(gx, 0), (gx, height)], fill=(48, 62, 82), width=2)

        if frac < 0.4:  # act 1: frontal, drifting right
            draw_face(draw, int(380 + 340 * (frac / 0.4)), 360)
        elif frac < 0.7:  # act 2: profile turn
            draw_face(draw, 760, 360, profile=(frac - 0.4) / 0.3)
        else:  # act 3: two-shot
            draw_face(draw, 330, 380, scale=0.85)
            draw_face(draw, 950, 350, scale=0.85)

        image.save(dest_dir / f"f{index:05d}.png")
    return dest_dir


def _face_source(dest: Path, ffmpeg: str, seconds: float = 7.0, fps: int = 30) -> Path:
    """Encode the drawn frames into a source with the same speech-shaped audio track."""
    frames = _face_frames(dest.parent / "smoke_faces", seconds, fps)
    gate = "+".join(f"between(t,{start:.2f},{end:.2f})" for _text, start, end in SMOKE_WORDS)
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-framerate",
            str(fps),
            "-i",
            str(frames / "f%05d.png"),
            "-f",
            "lavfi",
            "-i",
            f"sine=f=220:d={seconds}:sample_rate=48000",
            "-af",
            f"volume='0.35*({gate})':eval=frame",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-y",
            str(dest),
        ],
        check=True,
        capture_output=True,
        timeout=600,
        stdin=subprocess.DEVNULL,
    )
    return dest


def _options(profile: str | None) -> ProcessingOptions:
    """Everything on: the shipped defaults, plus what they deliberately leave off.

    A smoke reel is the one place the withheld defaults belong. ``music`` is included so the
    synthesised bed can be *heard* and judged (A15 labels it, but a marker is not a listen),
    and kinetic typography because it replaces the caption layer and that swap is exactly the
    kind of thing worth looking at.

    ``broll`` stays off: ``assets/broll`` is empty, so it would contribute nothing but a
    degradation marker.
    """
    data = {
        "profile": profile or "",
        "captions": True,
        "caption_preset": "hormozi",
        "hook_title": True,
        "reframe": True,
        "zoom": True,
        "transitions": True,
        "fades": True,
        "progress_bar": True,
        "color": "vivid",
        "emoji": "heavy",
        "caption_keyword_highlight": True,
        "caption_emoji": True,
        "music": "upbeat",
        "music_duck": True,
        "loudness_normalise": True,
        "platform": "tiktok",
    }
    if profile:
        # A profile is a bundle plus explicit overrides; passing both shows the profile's own
        # choices winning where this script has no opinion.
        data = {"profile": profile, "music": "upbeat", "platform": "tiktok"}
    return ProcessingOptions.from_dict(data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="real footage to use instead of a pattern")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(settings.temp_dir) / "smoke_reel.mp4",
        help="where to write the reel",
    )
    parser.add_argument(
        "--profile",
        choices=sorted(BUILTIN_PROFILES),
        help="render with a built-in profile instead of everything-on",
    )
    parser.add_argument(
        "--face-detector",
        choices=["haar", "mediapipe"],
        default=None,
        help="which face detector to reframe with (default: the configured backend)",
    )
    parser.add_argument(
        "--faces",
        action="store_true",
        help="synthesise a source WITH faces (a profile turn and a two-shot) instead of a "
        "plain test pattern, so face-tracked reframing has something to follow",
    )
    args = parser.parse_args()

    ffmpeg = shutil.which(settings.ffmpeg_binary) or shutil.which("ffmpeg")
    if not ffmpeg:
        print("ffmpeg is not on PATH; the smoke reel needs it", file=sys.stderr)
        return 1

    work = Path(args.out).parent
    work.mkdir(parents=True, exist_ok=True)

    if args.source:
        source = Path(args.source)
        if not source.exists():
            print(f"source does not exist: {source}", file=sys.stderr)
            return 1
    elif args.faces:
        print("no --source given; synthesising a source with faces")
        print("  three acts: frontal drift, a profile turn, then a two-shot")
        source = _face_source(work / "smoke_source_faces.mp4", ffmpeg)
    else:
        print("no --source given; synthesising a test pattern")
        print("  note: no face to track, and captions come from a fixed word list")
        print("  pass --faces for a source the reframe path can actually follow")
        source = _synthetic_source(work / "smoke_source.mp4", ffmpeg)

    words = [_Word(text, start, end) for text, start, end in SMOKE_WORDS]
    options = _options(args.profile)
    if args.face_detector:
        from dataclasses import replace as _replace

        options = _replace(options, face_detector=args.face_detector)
        print(f"  face detector: {args.face_detector}")

    before = audio.measure_loudness(source)
    result = compositor.render_clip(
        source,
        args.out,
        options,
        words,
        work,
        hook_text="watch what happened next",
    )
    if result is None:
        print(
            "the compositor reported nothing to do, which should be impossible here",
            file=sys.stderr,
        )
        return 1

    after = audio.measure_loudness(args.out)

    print(f"\nwrote {args.out}")
    print(f"  profile     : {options.profile or '(everything on)'}")
    print(f"  effects     : {len(result.effects_applied)}")
    for marker in result.effects_applied:
        # Degradations are the ones worth reading, so they are called out rather than listed.
        flag = "  <-- degraded" if "degraded" in marker else ""
        print(f"    - {marker}{flag}")
    if before and after:
        print(
            f"  loudness    : {before.input_i:.2f} -> {after.input_i:.2f} LUFS "
            f"(target {audio.platform_loudness_target(options.platform):g})"
        )
        print(f"  true peak   : {after.input_tp:.2f} dBTP")

    print("\nWhat to look at, in the order these have actually broken before:")
    print("  1. the caption face - is it the heavy display face, or a plain fallback?")
    print("  2. caption weight - real heavy, or a thin face with synthesised bold?")
    print("  3. emphasis - a couple of words per cue, or everything, or nothing?")
    print("  4. emoji - sharp at this size, or soft from being upscaled?")
    print("  5. the music bed - a track, or a two-tone drone? (a marker says which)")
    print("  6. captions against speech - still in sync at the end of the clip?")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
