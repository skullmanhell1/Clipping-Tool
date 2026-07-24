"""Face-aware vertical reframing.

When enabled (and the target aspect is narrower than the source), this replaces
the static centre-crop / blurred-background reformat with a crop window that
*follows the main speaker*:

1. Sample frames across the clip and detect faces (MediaPipe if available,
   otherwise OpenCV's Haar cascade).
2. Pick the dominant face per sample (largest / most persistent).
3. Smooth the crop-centre path (EMA + linear resample) so the "camera" glides
   instead of jumping.
4. Apply the moving crop in a single ffmpeg pass via the ``sendcmd`` +
   ``crop`` filters, then scale to the target resolution.

The heavy vision dependency (``cv2``) is imported lazily, and every failure
mode (no cv2, no faces, ffmpeg error) degrades gracefully so the caller can
fall back to the static reformat.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from config import settings
from worker.ffmpeg_utils import ASPECT_PRESETS, FFmpegError, _run, probe


class ReframeUnavailable(RuntimeError):
    """Raised when face-tracking reframe cannot be produced (use fallback)."""


@dataclass
class Center:
    """A crop-centre sample: time ``t`` (s) and centre ``(cx, cy)`` in px."""

    t: float
    cx: float
    cy: float


# --------------------------------------------------------------------------- #
# Pure geometry / smoothing helpers (no ffmpeg or cv2 needed — unit-testable)
# --------------------------------------------------------------------------- #
def compute_crop_size(
    src_w: int, src_h: int, aspect_w: int, aspect_h: int
) -> tuple[int, int]:
    """Return the largest ``(w, h)`` of the target aspect that fits the source.

    Dimensions are rounded to even numbers (encoder-friendly).
    """
    if src_w <= 0 or src_h <= 0:
        raise ValueError("source dimensions must be positive")
    target = aspect_w / aspect_h
    source = src_w / src_h
    if target <= source:
        # Target is narrower (or equal): full height, reduced width.
        ch = src_h
        cw = int(round(ch * target))
    else:
        # Target is wider: full width, reduced height.
        cw = src_w
        ch = int(round(cw / target))
    cw = min(src_w, cw - (cw % 2))
    ch = min(src_h, ch - (ch % 2))
    return max(2, cw), max(2, ch)


def pick_main_face(faces: list[tuple[int, int, int, int]]) -> Optional[tuple[float, float]]:
    """Return the centre ``(cx, cy)`` of the largest face box, or ``None``.

    ``faces`` is a list of ``(x, y, w, h)`` rectangles.
    """
    if not faces:
        return None
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    return (x + w / 2.0, y + h / 2.0)


def ema_smooth(values: list[float], alpha: float = 0.35) -> list[float]:
    """Exponential moving-average smoothing of a 1-D sequence."""
    if not values:
        return []
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def smooth_centers(samples: list[Center], alpha: float = 0.35) -> list[Center]:
    """Smooth the x/y paths of ``samples`` independently with an EMA."""
    if not samples:
        return []
    xs = ema_smooth([s.cx for s in samples], alpha)
    ys = ema_smooth([s.cy for s in samples], alpha)
    return [Center(s.t, x, y) for s, x, y in zip(samples, xs, ys)]


def resample_centers(
    samples: list[Center], fps: float, duration: float
) -> list[Center]:
    """Linearly resample ``samples`` onto a uniform ``fps`` grid over ``duration``.

    Produces the dense, evenly-spaced centres used to emit smooth crop
    commands. Values are held/clamped at the ends.
    """
    if not samples:
        return []
    if len(samples) == 1:
        s = samples[0]
        return [Center(0.0, s.cx, s.cy)]

    fps = max(1.0, float(fps))
    n = max(1, int(round(duration * fps)))
    out: list[Center] = []
    j = 0
    for i in range(n + 1):
        t = i / fps
        while j < len(samples) - 2 and samples[j + 1].t < t:
            j += 1
        a, b = samples[j], samples[j + 1]
        span = (b.t - a.t) or 1e-6
        frac = max(0.0, min(1.0, (t - a.t) / span))
        cx = a.cx + (b.cx - a.cx) * frac
        cy = a.cy + (b.cy - a.cy) * frac
        out.append(Center(round(t, 3), cx, cy))
    return out


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def build_sendcmd(
    centers: list[Center],
    crop_w: int,
    crop_h: int,
    src_w: int,
    src_h: int,
) -> str:
    """Return ``sendcmd`` script text setting the crop x/y over time.

    Each line: ``<t> crop x <X>, crop y <Y>;`` where X/Y are the top-left of the
    crop window, derived from the (clamped) centre so the window stays inside
    the frame.
    """
    max_x = max(0, src_w - crop_w)
    max_y = max(0, src_h - crop_h)
    lines: list[str] = []
    for c in centers:
        x = int(round(_clamp(c.cx - crop_w / 2.0, 0, max_x)))
        y = int(round(_clamp(c.cy - crop_h / 2.0, 0, max_y)))
        lines.append(f"{c.t:.3f} crop x {x}, crop y {y};")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Face detection (lazy cv2) + ffmpeg application
# --------------------------------------------------------------------------- #
def track_faces(video: str | Path, sample_fps: float = 5.0) -> list[Center]:
    """Sample frames and return the main-face centre path (``Center`` samples).

    Uses OpenCV's Haar cascade. Returns ``[]`` when cv2 is unavailable, the
    video cannot be opened, or no faces are found anywhere (caller falls back).
    """
    try:
        import cv2  # type: ignore
    except Exception:
        return []

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return []

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    step = max(1, int(round(fps / max(0.5, sample_fps))))

    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    detector = cv2.CascadeClassifier(cascade_path)
    if detector.empty():
        cap.release()
        return []

    samples: list[Center] = []
    last_center: Optional[tuple[float, float]] = None
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % step == 0:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                              minSize=(60, 60))
            center = pick_main_face([tuple(f) for f in faces])
            if center is None:
                center = last_center
            if center is not None:
                t = idx / fps
                samples.append(Center(round(t, 3), center[0], center[1]))
                last_center = center
        idx += 1
    cap.release()

    if frame_count and not samples:
        return []
    return samples


def apply_reframe(
    video: str | Path,
    dest: str | Path,
    aspect: str = "9:16",
    sample_fps: float = 5.0,
    command_fps: float = 12.0,
    smoothing: float = 0.35,
) -> Path:
    """Reframe ``video`` to ``aspect`` following the main face; write ``dest``.

    Raises :class:`ReframeUnavailable` when no usable face path is found or the
    aspect is not narrower than the source (nothing to track) so the caller can
    fall back to the static reformat.
    """
    if aspect not in ASPECT_PRESETS:
        raise ReframeUnavailable(f"Unknown aspect '{aspect}'")

    dest = Path(dest)
    info = probe(video)
    tw, th = ASPECT_PRESETS[aspect]
    aw, ah = _aspect_ratio_parts(aspect)

    crop_w, crop_h = compute_crop_size(info.width, info.height, aw, ah)
    if crop_w >= info.width and crop_h >= info.height:
        # Target isn't a tighter crop than the source; nothing to follow.
        raise ReframeUnavailable("target aspect is not narrower than source")

    samples = track_faces(video, sample_fps=sample_fps)
    if not samples:
        raise ReframeUnavailable("no faces detected")

    smoothed = smooth_centers(samples, alpha=smoothing)
    dense = resample_centers(smoothed, command_fps, info.duration)
    script = build_sendcmd(dense, crop_w, crop_h, info.width, info.height)

    cmd_file = dest.with_suffix(".reframe.cmd")
    cmd_file.parent.mkdir(parents=True, exist_ok=True)
    cmd_file.write_text(script, encoding="utf-8")

    # Initial crop position (first command); sendcmd updates x/y over time.
    first = dense[0]
    x0 = int(round(_clamp(first.cx - crop_w / 2.0, 0, info.width - crop_w)))
    y0 = int(round(_clamp(first.cy - crop_h / 2.0, 0, info.height - crop_h)))

    escaped = str(cmd_file.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    vf = (
        f"sendcmd=f='{escaped}',"
        f"crop={crop_w}:{crop_h}:{x0}:{y0},"
        f"scale={tw}:{th},setsar=1"
    )
    cmd = [
        settings.ffmpeg_binary, "-y", "-i", str(video),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "copy", "-movflags", "+faststart",
        str(dest),
    ]
    try:
        _run(cmd)
    except FFmpegError as exc:
        raise ReframeUnavailable(f"ffmpeg reframe failed: {exc}") from exc
    finally:
        cmd_file.unlink(missing_ok=True)
    return dest


def _aspect_ratio_parts(aspect: str) -> tuple[int, int]:
    """Parse ``"9:16"`` -> ``(9, 16)``."""
    a, _, b = aspect.partition(":")
    return int(a), int(b)
