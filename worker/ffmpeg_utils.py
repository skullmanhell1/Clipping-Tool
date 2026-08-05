"""Thin FFmpeg/FFprobe helpers.

Wraps common FFmpeg operations (probing, cutting, aspect reformatting) behind
small, well-documented Python functions so the rest of the pipeline never shells
out directly. Binary paths come from :data:`config.settings`.

All functions raise :class:`FFmpegError` on failure with the captured stderr so
callers can surface actionable errors.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from config import settings


class FFmpegError(RuntimeError):
    """Raised when an ffmpeg/ffprobe invocation fails."""


# --------------------------------------------------------------------------- #
# H.264 output settings (O1, O2, O3)
# --------------------------------------------------------------------------- #
# Every encode in this repository spelled out ``-c:v libx264 -preset veryfast -crf 20``
# and nothing else, in seven places. Three flags that decide whether a platform will
# accept the file at all were missing everywhere:
#
#   O1  -pix_fmt yuv420p    Without it, ffmpeg keeps the source pixel format. A 4:2:2 or
#                           10-bit source therefore produced a 4:2:2/10-bit H.264 file,
#                           which Safari, many Android decoders and several upload
#                           pipelines refuse outright - and the failure appears at upload
#                           time, long after the render looked fine locally.
#   O2  -profile:v high     libx264 otherwise picks a profile from the input, and it can
#       -level 4.0          land above what older hardware decoders implement.
#   O3  -r <fps>            A variable-frame-rate source (every screen recording, most
#                           phone footage) has no single frame duration, so burned captions
#                           drift against speech as the effective rate wanders.
#
#: The pixel format every output is written in.
#:
#: Without this, libx264 preserves whatever the *source* used. A 10-bit source (phone
#: footage, OBS/NVENC, many screen recorders) therefore produced a ``yuv420p10le`` /
#: ``High 10`` clip, and a 4:2:2 source a ``High 4:2:2`` one. Neither plays in Windows
#: Media Player or Films & TV, QuickTime, or most browsers -- the file exists, has the
#: right duration, and simply will not open. 8-bit 4:2:0 is the format every player and
#: every social platform accepts.
OUTPUT_PIX_FMT = "yuv420p"

#: H.264 profile and level. ``high`` is 8-bit only, so it is the encoder-side guard that
#: matches OUTPUT_PIX_FMT; level 4.0 covers 1080p at the frame rates used here and keeps
#: older phones and smart TVs in scope.
OUTPUT_PROFILE = "high"
OUTPUT_LEVEL = "4.0"

#: Container/codec flags safe to apply to any encode, intermediate or final.
#:
#: Deliberately *not* configurable, unlike CRF and preset: there is no good reason to ship
#: a clip a player will refuse to open.
H264_COMPAT_ARGS: tuple[str, ...] = (
    "-pix_fmt",
    OUTPUT_PIX_FMT,
    "-profile:v",
    OUTPUT_PROFILE,
    "-level",
    OUTPUT_LEVEL,
)


def _compat_args(encoder) -> list[str]:
    """:data:`H264_COMPAT_ARGS`, adapted to ``encoder``'s requirements (O8).

    Built *from* the tuple rather than respelling it, and that is the whole point of this function
    existing. O8 first wrote these three flags out inline here, which left ``H264_COMPAT_ARGS``
    read by nothing but its own test - a second statement of the same contract, free to drift from
    what is actually emitted. Mutation testing found it: deleting ``-pix_fmt`` from the tuple
    changed no output at all, so the constant had quietly stopped being load-bearing while the test
    that iterates it still passed.

    Two encoders need an adaptation rather than the literal value:

    * VAAPI uploads frames to the device, so it needs ``nv12`` instead of the project default;
    * several VAAPI drivers reject ``-level`` outright, so it is dropped rather than passed.
    """
    pairs = zip(H264_COMPAT_ARGS[::2], H264_COMPAT_ARGS[1::2], strict=True)
    args: list[str] = []
    for flag, value in pairs:
        if flag == "-pix_fmt" and getattr(encoder, "pix_fmt", ""):
            value = encoder.pix_fmt
        elif flag == "-level" and not getattr(encoder, "accepts_level", True):
            continue
        args += [flag, value]
    return args


def escape_filter_path(path: str | Path) -> str:
    """Escape a filesystem path for use inside an ffmpeg filter argument.

    ``:`` separates filter options and ``\\``/``'`` are the escape characters, so an unescaped
    path in a directory containing a colon produces a filtergraph parse error rather than a
    wrong-looking result - and the error names the whole graph, not the path.

    Lives here because four modules had grown their own copy (captions, overlays, reframe and
    now audio) and they had *diverged*: one resolved the path and escaped backslashes, another
    did neither and rewrote backslashes as forward slashes. Two of those are defensible and the
    combination is not, since which one you got depended on which effect you enabled.

    Note for Windows hosts: ffmpeg also accepts ``C\\:/dir/file`` there. This project's
    deployment targets are Linux (Dockerfile, render.yaml), so the escaped form is used
    uniformly rather than branching on platform.
    """
    resolved = str(Path(path).resolve())
    return resolved.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def h264_args(*, normalise_fps: bool = False, vbv_cap: bool = False) -> list[str]:
    """The standard libx264 arguments for an encode.

    Centralised because these flags were duplicated at eight call sites across five
    modules (``cut_segment``, ``reformat_aspect``, captions burn-in, three reframe
    paths, filler-removal concat and the compositor). That duplication is why a missing
    ``-pix_fmt`` went unnoticed for so long: there was no single place where the output
    contract lived, so it could not be reviewed in one go.

    ``crf`` and ``preset`` come from settings rather than literals, so quality/speed is
    tunable without editing five files. The compatibility flags are not tunable.

    ``normalise_fps`` adds ``-r`` at :data:`config.settings.output_fps`, forcing constant
    frame rate. It is off by default because an intermediate that is about to be re-encoded
    gains nothing from being resampled twice, and on for anything a user receives.

    ``vbv_cap`` adds ``-maxrate``/``-bufsize`` (O4). ``-crf`` alone sets a *quality* target
    with no ceiling on bitrate, so a busy clip - confetti, fast pans, grain, a gameplay
    scene - can balloon well past a platform's file-size limit and be rejected on upload.
    Also off for intermediates: capping a file that is about to be re-encoded throws away
    quality the final pass could have used.

    The encoder itself is chosen by :func:`worker.video_encoders.resolve_encoder` (O8), which
    falls back to ``libx264`` whenever a configured hardware encoder is not usable on this
    machine - so the return value is always a working argv, and the quality flag always matches
    the encoder rather than being ``-crf`` regardless.
    """
    # O8: hardware encoding, when one is configured *and* proven to work on this machine.
    #
    # Routed through here rather than at the eight call sites for the same reason the flags were
    # centralised in the first place: an encoder swap that reached seven of the eight would produce
    # clips whose quality depended on which stage wrote them. `resolve_encoder` falls back to
    # libx264 whenever the request cannot be honoured, so this returns a working argv either way.
    from worker import video_encoders

    choice = video_encoders.resolve_encoder()
    encoder = choice.encoder

    args = ["-c:v", encoder.name]
    args += encoder.preset_args(str(settings.x264_preset))
    # Not `-crf`: every other encoder spells constant quality differently, and three of them use a
    # different scale. See worker/video_encoders.py for the table.
    args += encoder.quality_args(int(settings.x264_crf))
    args += _compat_args(encoder)
    if normalise_fps:
        args += ["-r", str(int(settings.output_fps))]
    if vbv_cap:
        # O7: the platform profile's ceiling when one is active, else the configured value.
        from worker import output_profiles

        maxrate = output_profiles.resolve_max_bitrate_kbps()
        # A two-second buffer, the usual pairing: large enough that a brief complex passage
        # is not visibly starved, small enough that the cap still means something.
        args += ["-maxrate", f"{maxrate}k", "-bufsize", f"{maxrate * 2}k"]
    return args


def mux_subtitle_tracks(
    video: str | Path,
    tracks: Sequence[tuple[str | Path, str]],
    dest: str | Path,
) -> Path:
    """Copy ``video`` to ``dest`` with each ``(subtitle_file, language)`` added as a track.

    The multi-track form exists for T10: a translated subtitle track is only *useful* alongside
    the original-language one, and adding them in two separate remuxes cannot label them
    correctly. ``-metadata:s:s:N`` addresses subtitle streams by their index in the **output**,
    so a second pass over a file that already has a subtitle stream would either re-label the
    first track or need to know how many the input already had. Supplying every track in one
    call makes the indices a property of this argument list instead of of the input file.

    ``tracks`` order is the output order, so the track a player selects by default is the first
    one given. Raises nothing when ``tracks`` is empty: it still produces ``dest`` as a plain
    remux, because a caller that asked for a copy should get one.
    """
    video, dest = Path(video), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = [settings.ffmpeg_binary, "-y", "-i", str(video)]
    for path, _lang in tracks:
        args += ["-i", str(Path(path))]
    args += ["-map", "0"]
    for index in range(len(tracks)):
        args += ["-map", str(index + 1)]
    args += ["-c", "copy", "-c:s", "mov_text"]
    for index, (_path, lang) in enumerate(tracks):
        args += [f"-metadata:s:s:{index}", f"language={lang or 'und'}"]
    args += ["-movflags", "+faststart", str(dest)]
    _run(args)
    return dest


def mux_soft_subtitles(
    video: str | Path,
    subtitles: str | Path,
    dest: str | Path,
    *,
    language: str = "eng",
) -> Path:
    """Copy ``video`` to ``dest`` with ``subtitles`` added as a selectable track (O12).

    Burned-in captions are the right default for short-form - the platforms autoplay muted and
    a viewer never enables a subtitle track - but they are a permanent, untranslatable,
    un-hideable decision baked into the pixels. A soft track lets the same file serve an
    audience that wants captions off, or wants them in another language, or is on a platform
    that renders its own.

    ``mov_text`` is the only subtitle codec MP4 carries, and it is *text only*: the karaoke
    fills, per-word highlights, glyphs and positioning of the ASS captions cannot survive in
    it. That is not a limitation of this function but of the container, and it is exactly why
    soft captions are an alternative to the burned ones rather than a replacement - which is
    also why ``both`` exists as a mode.

    Streams are copied, so this costs a remux rather than a re-encode: no generation loss, and
    a second or two on a clip-length file.

    A single-track shorthand for :func:`mux_subtitle_tracks`, kept because one track is the
    common case and because it is the published entry point for O12.
    """
    return mux_subtitle_tracks(video, [(subtitles, language)], dest)


def aac_args() -> list[str]:
    """The standard AAC arguments for an encode, including AU8's normalisation.

    ``-ar``/``-ac`` were set nowhere, so output sample rate and channel count were whatever
    the source happened to have: 44.1 kHz mono from a phone, 48 kHz 5.1 from a camera. Both
    are legal H.264/AAC and both cause trouble downstream - a mono clip plays out of one
    side on some players, and a surround layout is silently downmixed by whatever decoder
    gets it first, if at all.
    """
    return [
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        str(int(settings.output_sample_rate)),
        "-ac",
        str(int(settings.output_channels)),
    ]


# Common target aspect ratios keyed by the UI values, mapped to (w, h) at a
# canonical short-form resolution.
#
# These stay the 1080-class values they always were, because a great deal of code and a great
# many tests read them as the canonical mapping. O5/O9 scale them at the point of use via
# :func:`aspect_size`, which is the one place a resolution choice needs to exist.
ASPECT_PRESETS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
    "4:5": (1080, 1350),
}

#: The reference short side the values above are expressed at.
BASE_SHORT_SIDE = 1080

#: Selectable output resolutions, named by their short side (O9).
#:
#: 720 for a machine that cannot afford 1080 encodes, 1080 as the short-form consensus, 1440
#: and 2160 for sources that genuinely have the detail. Anything else is rejected rather than
#: rounded, because a resolution nobody chose is worse than an error.
OUTPUT_SHORT_SIDES: tuple[int, ...] = (720, 1080, 1440, 2160)


def aspect_size(aspect: str, short_side: int | None = None) -> tuple[int, int]:
    """The output ``(width, height)`` for ``aspect`` at the configured resolution (O5, O9).

    Resolution was fixed at the 1080-class values in :data:`ASPECT_PRESETS` with no way to ask
    for anything else - so a phone-shot 4K source was downscaled with no option to keep it, and
    a low-powered host had no way to trade quality for encode time.

    Both dimensions are forced **even**, because libx264's 4:2:0 chroma subsampling requires it
    and an odd dimension fails the encode outright rather than degrading.
    """
    if aspect not in ASPECT_PRESETS:
        raise ValueError(f"Unknown aspect '{aspect}'. Valid: {sorted(ASPECT_PRESETS)}")
    base_w, base_h = ASPECT_PRESETS[aspect]
    if short_side is None:
        # O7: an active platform profile supplies the resolution unless the operator has set
        # one explicitly. Imported here rather than at module scope because the profile table
        # reads this module's constants - and because ffmpeg_utils is imported by everything,
        # so keeping its import surface small is worth a local import.
        from worker import output_profiles

        short_side = output_profiles.resolve_short_side()
    if short_side not in OUTPUT_SHORT_SIDES:
        short_side = BASE_SHORT_SIDE
    if short_side == BASE_SHORT_SIDE:
        return base_w, base_h
    scale = short_side / float(min(base_w, base_h))
    width = int(round(base_w * scale))
    height = int(round(base_h * scale))
    return width - (width % 2), height - (height % 2)


@dataclass
class MediaInfo:
    """Basic probed metadata for a media file."""

    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    # O10: needed to validate a clip against a platform's accepted codecs before upload.
    # Defaulted and last so existing positional construction (several tests build MediaInfo
    # directly) keeps working.
    video_codec: str = ""
    audio_codec: str = ""
    size_bytes: int = 0


def _default_timeout(cmd: list[str]) -> float:
    """The configured ceiling for ``cmd``, chosen by which binary it invokes.

    ffprobe reads metadata and gets the short ceiling; anything else is treated as an
    encode and gets the long one. The comparison is on the *basename* so an absolute
    ``/usr/bin/ffprobe`` is classified the same as a bare ``ffprobe``.
    """
    if not cmd:
        return float(settings.ffmpeg_timeout_seconds)
    probe_name = Path(str(settings.ffprobe_binary)).name
    if Path(str(cmd[0])).name == probe_name:
        return float(settings.ffprobe_timeout_seconds)
    return float(settings.ffmpeg_timeout_seconds)


def _run(cmd: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run a command, returning the completed process or raising ``FFmpegError``.

    Every invocation is bounded. Jobs are processed by a thread pool with a single
    worker, so an ffmpeg that never exits would block the whole queue forever —
    silently, because a hung process produces neither output nor an exception. On
    expiry ``subprocess.run`` kills the child and reaps it, and the overrun is
    reported as an :class:`FFmpegError` so callers already handling failure need no
    change.

    Args:
        cmd: argv to execute.
        timeout: Ceiling in seconds. ``None`` uses :func:`_default_timeout`; a value
            ``<= 0`` means unbounded, which is the documented opt-out.

    Raises:
        FFmpegError: the binary is missing, the command failed, or it timed out.
    """
    limit = _default_timeout(cmd) if timeout is None else float(timeout)
    try:
        proc = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=limit if limit > 0 else None,
        )
    except FileNotFoundError as exc:  # binary missing
        raise FFmpegError(f"Binary not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        # The stderr captured so far is the only clue as to where it stalled, so it
        # is surfaced exactly like a non-zero exit is. It may be bytes or str
        # depending on where the timeout struck, hence the decode dance.
        raw = exc.stderr or ""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        tail = raw.strip().splitlines()[-15:]
        detail = (": " + "\n".join(tail)) if tail else ""
        raise FFmpegError(
            f"Command timed out after {limit:g}s ({' '.join(cmd[:2])} ...){detail}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or "").strip().splitlines()[-15:]
        raise FFmpegError(f"Command failed ({' '.join(cmd[:2])} ...): " + "\n".join(tail)) from exc
    return proc


def probe(path: str | Path) -> MediaInfo:
    """Return :class:`MediaInfo` for ``path`` via ffprobe.

    Args:
        path: Path to the media file.

    Raises:
        FFmpegError: if the file cannot be probed.
    """
    cmd = [
        settings.ffprobe_binary,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    proc = _run(cmd)
    data = json.loads(proc.stdout or "{}")

    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise FFmpegError(f"No video stream found in {path}")

    # Duration can live on the format or stream; prefer format.
    duration = float(fmt.get("duration") or video.get("duration") or 0.0)

    # fps is expressed as a fraction like "30000/1001".
    fps = 0.0
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/0"
    try:
        num, _, den = rate.partition("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0

    try:
        size_bytes = int(float(fmt.get("size") or 0))
    except (TypeError, ValueError):
        size_bytes = 0

    return MediaInfo(
        duration=duration,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=round(fps, 3),
        has_audio=audio is not None,
        video_codec=str(video.get("codec_name") or ""),
        audio_codec=str((audio or {}).get("codec_name") or ""),
        size_bytes=size_bytes,
    )


def cut_segment(
    source: str | Path,
    start: float,
    end: float,
    dest: str | Path,
    reencode: bool = True,
) -> Path:
    """Cut ``[start, end]`` (seconds) from ``source`` into ``dest``.

    Args:
        source: Input media path.
        start: Segment start in seconds.
        end: Segment end in seconds (must be > ``start``).
        dest: Output path (extension determines the container).
        reencode: When ``True`` (default) re-encode for frame-accurate cuts,
            which is what downstream captioning/reformatting needs. When
            ``False`` attempt a fast stream copy (keyframe-aligned, less exact).

    Returns:
        The ``dest`` path as a :class:`~pathlib.Path`.
    """
    if end <= start:
        raise ValueError(f"end ({end}) must be greater than start ({start})")

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    duration = end - start

    cmd = [
        settings.ffmpeg_binary,
        "-y",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(source),
        "-t",
        f"{duration:.3f}",
    ]
    if reencode:
        cmd += [*h264_args(), *aac_args()]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-movflags", "+faststart", str(dest)]

    _run(cmd)
    return dest


#: How the area around a fitted frame is filled (V11).
#:
#: The background used to be one hard-coded look: ``boxblur=40:1`` plus ``eq=brightness=-0.1``.
#: It suits talking-head footage and actively hurts other things - a blurred screen recording is
#: an unreadable smear, and gameplay footage becomes visual noise competing with the clip.
BACKGROUND_STYLES: tuple[str, ...] = ("blur", "mirror", "black", "color", "gradient")


#: The background styles that need a filter beyond the always-present set.
#:
#: ``gradient`` is built with ``geq``, which is a GPL-only filter: a build configured without
#: ``--enable-gpl`` has every other filter used here and not that one. Naming the dependency
#: rather than assuming it is what lets the style degrade instead of failing the render, and it
#: follows the pattern the stem engine already uses for ``alimiter``.
BACKGROUND_STYLE_FILTERS: dict[str, str] = {"gradient": "geq"}


def background_style_available(style: str) -> bool:
    """Whether ``style``'s required filter exists in the configured ffmpeg.

    Any failure to probe answers ``True``: a probe that cannot run is not evidence the filter is
    missing, and treating it as such would silently downgrade the look on every host where the
    capability system itself is unavailable.
    """
    required = BACKGROUND_STYLE_FILTERS.get(style)
    if not required:
        return True
    try:
        # get_report() rather than calling the prober directly: it caches per capability id for
        # the life of the process, so this costs one `ffmpeg -filters` invocation ever rather
        # than one per clip.
        from worker.engines.capabilities import get_report

        return bool(get_report().status(f"ffmpeg_filter:{required}").available)
    except Exception:
        return True


def resolve_background_style(style: str) -> str:
    """``style`` if it can be rendered here, else ``blur`` (V11).

    Degrading to the previous default is the right fallback: an unavailable filter should cost
    the *choice*, never the clip.
    """
    if style in BACKGROUND_STYLES and not background_style_available(style):
        return "blur"
    return style


def background_chain(style: str, tw: int, th: int, *, color: str = "0x0F172A") -> str:
    """The filter chain producing the background layer, given ``[bg]`` as its input.

    Each style ends with a single ``[bgb]`` output so the caller's overlay is unchanged.

    * ``blur`` - the original: cover, crop, blur, darken slightly.
    * ``mirror`` - cover and crop, then flip horizontally. Reads as intentional where a blur
      reads as a mistake, and it keeps the frame's own colour and motion.
    * ``black`` - true letterbox. The honest choice for a screen recording, where any
      derived background is a distraction from text the viewer is trying to read.
    * ``color`` - a flat brand colour.
    * ``gradient`` - a vertical darkening of the covered frame, so the fitted video sits in
      light and the edges fall away. Implemented with ``geq`` on luma only, so hue is
      untouched; a full RGB gradient would shift the source's colour.
    """
    cover = f"scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th}"
    if style == "mirror":
        return f"[bg]{cover},hflip[bgb];"
    if style == "black":
        # The source is discarded rather than blurred: nothing derived from it is wanted.
        return f"[bg]scale={tw}:{th},drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill[bgb];"
    if style == "color":
        return f"[bg]scale={tw}:{th}," f"drawbox=x=0:y=0:w=iw:h=ih:color={color}:t=fill[bgb];"
    if style == "gradient":
        return (
            f"[bg]{cover},boxblur=luma_radius=20:luma_power=1,"
            # Darkens with vertical position: full at the top, 45% at the bottom.
            f"geq=lum='p(X,Y)*(1-0.55*Y/H)':cb='p(X,Y)':cr='p(X,Y)'[bgb];"
        )
    # blur, and the fallback for an unknown style - the previous behaviour, so an unrecognised
    # value degrades to what shipped before rather than to no background at all.
    return f"[bg]{cover},boxblur=luma_radius=40:luma_power=1," f"eq=brightness=-0.1[bgb];"


def reformat_aspect(
    source: str | Path,
    dest: str | Path,
    aspect: str = "9:16",
    mode: str = "crop_blur",
    *,
    background: str = "blur",
    background_color: str = "0x0F172A",
) -> Path:
    """Reformat ``source`` to a target ``aspect`` ratio.

    Two strategies are supported:

    * ``crop_blur`` (default): the source is centre-cropped to fill the target
      frame; where the crop would leave empty bars, a scaled + blurred copy of
      the source is used as the background so the frame is always filled
      (Opus-Clip style). This is the recommended look for vertical clips.
    * ``pad``: the source is scaled to fit and letter/pillar-boxed with black.

    Args:
        source: Input clip path.
        dest: Output path.
        aspect: One of :data:`ASPECT_PRESETS` keys (e.g. ``"9:16"``).
        mode: ``"crop_blur"`` or ``"pad"``.

    Returns:
        The ``dest`` path.
    """
    # O5/O9: the size comes from the configured resolution rather than the 1080-class literal.
    tw, th = aspect_size(aspect)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if mode == "pad":
        # Scale to fit inside the target, then pad with black.
        vf = (
            f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
            f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
        )
    elif mode == "crop_blur":
        # Background per V11; foreground scaled to fit fully inside and overlaid centred.
        vf = (
            f"split=2[bg][fg];"
            # V11: resolved here rather than in background_chain, so that function stays a pure
            # string builder with no probe in it - the tests assert its output directly.
            f"{background_chain(resolve_background_style(background), tw, th, color=background_color)}"
            f"[fg]scale={tw}:{th}:force_original_aspect_ratio=decrease[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )
    else:
        raise ValueError(f"Unknown mode '{mode}'. Valid: 'crop_blur', 'pad'.")

    cmd = [
        settings.ffmpeg_binary,
        "-y",
        "-i",
        str(source),
        "-vf",
        vf,
        *h264_args(),
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    _run(cmd)
    return dest


#: Minimum fraction of a dimension that must be bar for a letterbox to be acted on.
#:
#: A couple of pixels of dark edge is normal in compressed footage and is not a letterbox.
#: Cropping it would be a change nobody asked for, applied to every clip.
MIN_LETTERBOX_FRACTION = 0.02


def detect_letterbox(
    source: str | Path,
    *,
    probe_seconds: float = 8.0,
    skip_seconds: float = 1.0,
) -> tuple[int, int, int, int] | None:
    """The content rectangle of ``source`` as ``(w, h, x, y)``, or ``None`` (V16).

    Source footage is very often already letterboxed - a 16:9 video exported inside a 1:1
    frame, a phone recording of a screen, anything re-uploaded from another platform. Reframing
    such a source *centres the crop on the bars*: the tracker sees a smaller subject inside a
    black border, and the output is a vertical clip with black bands baked into the middle of
    it. Detecting the real content first is what makes reframe usable on second-hand footage.

    Sampling starts at ``skip_seconds`` because the first frames are frequently a fade from
    black, and a fade looks exactly like a fully-letterboxed frame to ``cropdetect`` - probing
    from zero would report the whole frame as bar and crop everything.

    Returns ``None`` when there is nothing worth cropping, when the detection is implausible, or
    on any failure. Every caller treats that as "use the frame as-is", which is the previous
    behaviour.
    """
    try:
        info = probe(source)
    except Exception:
        return None
    if info.width <= 0 or info.height <= 0:
        return None

    command = [
        settings.ffmpeg_binary,
        "-nostdin",
        "-hide_banner",
        "-ss",
        f"{max(0.0, float(skip_seconds)):.3f}",
        "-t",
        f"{max(0.5, float(probe_seconds)):.3f}",
        "-i",
        str(source),
        # round=2 keeps the result even, which libx264's 4:2:0 subsampling requires anyway.
        "-vf",
        "cropdetect=limit=24:round=2:reset=0",
        "-an",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except Exception:
        return None

    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    matches = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", text)
    if not matches:
        return None
    # The last report is the most informed: cropdetect accumulates across the window with
    # reset=0, so earlier lines describe fewer frames.
    width, height, x, y = (int(v) for v in matches[-1])

    if width <= 0 or height <= 0:
        return None
    # Implausible results are ignored rather than trusted. cropdetect on a genuinely dark scene
    # can report a tiny rectangle, and cropping a clip down to a quarter of its frame because
    # somebody filmed at night would be far worse than leaving the bars.
    if width > info.width or height > info.height:
        return None
    if width * height < info.width * info.height * 0.25:
        return None

    trimmed_x = info.width - width
    trimmed_y = info.height - height
    if (
        trimmed_x < info.width * MIN_LETTERBOX_FRACTION
        and trimmed_y < info.height * MIN_LETTERBOX_FRACTION
    ):
        return None
    return width - (width % 2), height - (height % 2), x, y


def extract_audio(source: str | Path, dest: str | Path, sample_rate: int = 16000) -> Path:
    """Extract a mono WAV suitable for transcription/silence analysis.

    Args:
        source: Input media.
        dest: Output ``.wav`` path.
        sample_rate: Target sample rate (16 kHz is ideal for whisper).
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        settings.ffmpeg_binary,
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-c:a",
        "pcm_s16le",
        str(dest),
    ]
    _run(cmd)
    return dest


def generate_thumbnail(
    source: str | Path, dest: str | Path, at: float = 0.0, width: int = 640
) -> Path:
    """Write a single JPEG thumbnail from ``source`` at time ``at`` seconds."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        settings.ffmpeg_binary,
        "-y",
        "-ss",
        f"{max(at, 0):.3f}",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:-2",
        str(dest),
    ]
    _run(cmd)
    return dest
