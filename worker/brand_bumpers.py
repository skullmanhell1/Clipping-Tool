"""Optional intro/outro bumper composition for brand profiles."""

from __future__ import annotations

from pathlib import Path

from config import settings
from worker.ffmpeg_utils import FFmpegError, _run, aac_args, h264_args, probe


def _video_chain(label: str, width: int, height: int, fps: int) -> str:
    return (
        f"[{label}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},"
        f"format=yuv420p,setpts=PTS-STARTPTS[v{label}]"
    )


def _audio_chain(label: str, audio_label: str) -> str:
    return (
        f"[{audio_label}:a]aresample=48000,"
        "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
        f"asetpts=PTS-STARTPTS[a{label}]"
    )


def apply_bumpers(
    video: str | Path,
    dest: str | Path,
    *,
    intro: str | Path | None = None,
    outro: str | Path | None = None,
    width: int,
    height: int,
    fps: int = 30,
) -> Path:
    """Prepend/append brand videos and normalize all segments to one stream contract.

    Missing bumpers are rejected before ffmpeg is called. A source without audio receives a silent
    stereo track so a bumper with audio can still be concatenated without producing an invalid
    ``concat`` graph. The main clip remains the middle segment and its existing pixels are not
    filtered beyond the required output normalization.
    """
    video = Path(video)
    dest = Path(dest)
    segments = [Path(path) for path in (intro, video, outro) if path]
    if len(segments) == 1:
        raise FFmpegError("no brand bumper was supplied")
    if any(not path.is_file() for path in segments):
        raise FFmpegError("a configured brand bumper does not exist")
    infos = [probe(path) for path in segments]
    target_fps = max(1, int(fps))
    args = [settings.ffmpeg_binary, "-y"]
    real_input_indices: list[int] = []
    audio_input_indices: list[int] = []
    input_index = 0
    for path, info in zip(segments, infos):
        args += ["-i", str(path)]
        real_input_indices.append(input_index)
        input_index += 1
        if info.has_audio:
            audio_input_indices.append(real_input_indices[-1])
        else:
            duration = max(0.05, float(info.duration))
            args += [
                "-f",
                "lavfi",
                "-t",
                f"{duration:.3f}",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
            ]
            audio_input_indices.append(input_index)
            input_index += 1

    filters: list[str] = []
    concat_inputs: list[str] = []
    for index, (video_index, audio_index) in enumerate(
        zip(real_input_indices, audio_input_indices)
    ):
        label = str(index)
        filters.append(_video_chain(str(video_index), width, height, target_fps))
        filters.append(_audio_chain(label, str(audio_index)))
        concat_inputs.extend([f"[v{label}]", f"[a{label}]"])
    filters.append("".join(concat_inputs) + f"concat=n={len(segments)}:v=1:a=1[vout][aout]")
    dest.parent.mkdir(parents=True, exist_ok=True)
    args += ["-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]"]
    args += h264_args(normalise_fps=True, delivered_fps=target_fps)
    args += aac_args()
    args += ["-shortest", "-movflags", "+faststart", str(dest)]
    _run(args)
    if not dest.is_file() or dest.stat().st_size <= 0:
        raise FFmpegError("brand bumper composition produced no output")
    return dest
