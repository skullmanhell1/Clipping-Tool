"""Single-pass effect compositor.

Takes a geometry-prepared clip (already at the target aspect/resolution) and
applies the enabled "look" effects in **one efficient ffmpeg pass**:

    colour grade -> zoom/punch-in -> fades -> captions + hook title
    -> progress bar -> emoji overlays        (video)
    original audio (+ optional mood music, + optional fades)   (audio)

Streams that are not modified are stream-copied rather than re-encoded, and
when nothing at all is enabled the compositor returns ``None`` so the caller can
simply keep the input clip. This keeps frame-by-frame work strictly optional.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from config import settings
from worker import captions as cap
from worker.effects import audio, emoji, overlays
from worker.ffmpeg_utils import _run, probe
from worker.models import ProcessingOptions


@dataclass
class RenderResult:
    """Outcome of a compositor render."""

    path: Path
    effects_applied: list[str]


def render_clip(
    base_clip: str | Path,
    dest: str | Path,
    options: ProcessingOptions,
    words: list,
    temp_dir: str | Path,
    hook_text: str = "",
    llm_client=None,
    emoji_resolver=None,
) -> Optional[RenderResult]:
    """Apply enabled effects to ``base_clip`` -> ``dest`` in one ffmpeg pass.

    Returns a :class:`RenderResult`, or ``None`` when no effect (and no caption)
    is enabled (the caller should then use ``base_clip`` directly).
    """
    base_clip = Path(base_clip)
    dest = Path(dest)
    temp_dir = Path(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    info = probe(base_clip)
    width, height = info.width, info.height
    fps = info.fps or 30.0
    duration = info.duration
    applied: list[str] = []

    # --- captions + hook title (single combined ASS) ---------------------
    subtitles_filter: Optional[str] = None
    need_caps = options.captions and bool(words)
    need_hook = options.hook_title and bool(hook_text.strip())
    if need_caps or need_hook:
        ass_path = temp_dir / f"{base_clip.stem}.ass"
        cues = cap.words_to_cues(words) if need_caps else []
        cap.build_ass(
            cues, ass_path,
            video_width=width, video_height=height,
            template=options.caption_template,
            position=options.caption_position,
            hook_text=hook_text if need_hook else "",
        )
        subtitles_filter = cap.subtitles_filter(ass_path)
        if need_caps:
            applied.append("captions")
        if need_hook:
            applied.append("hook_title")

    # --- video look chain -------------------------------------------------
    chain = overlays.build_video_chain(
        duration=duration, fps=fps, width=width, height=height,
        color=options.color, zoom=options.zoom, transitions=options.transitions,
        fades=options.fades, progress_bar=options.progress_bar,
        subtitles=subtitles_filter,
    )
    if options.color:
        applied.append(f"color:{options.color}")
    if options.zoom:
        applied.append("zoom")
    if options.transitions:
        applied.append("transitions")
    if options.fades:
        applied.append("fades")
    if options.progress_bar:
        applied.append("progress_bar")

    # --- emoji overlays ---------------------------------------------------
    emoji_cues = []
    if options.emoji and options.emoji != "off":
        emoji_cues = emoji.plan_emoji(
            words, duration, intensity=options.emoji, mode=options.emoji_mode,
            client=llm_client,
        )

    # --- music bed --------------------------------------------------------
    music_path: Optional[Path] = None
    if options.music:
        music_path = audio.resolve_music(options.music, duration, temp_dir)

    # ---------------------------------------------------------------------
    # Assemble inputs + filter graph.
    # ---------------------------------------------------------------------
    inputs: list[str] = ["-i", str(base_clip)]
    graph_parts: list[str] = []

    # Video base label after the look chain.
    if chain:
        graph_parts.append(f"[0:v]{','.join(chain)}[vbase]")
        video_label = "vbase"
    else:
        video_label = "0:v"

    # Music occupies input index 1 (if present); emoji inputs follow.
    emoji_offset = 1
    if music_path is not None:
        emoji_offset = 2

    emoji_inputs: list[str] = []
    emoji_graph = ""
    if emoji_cues:
        emoji_inputs, emoji_graph = emoji.build_overlay(
            emoji_cues, base_label=video_label, out_label="vout",
            duration=duration, animate=options.emoji_animate,
            resolver=emoji_resolver, input_offset=emoji_offset,
        )
    if emoji_graph:
        graph_parts.append(emoji_graph)
        video_out = "vout"
        applied.append(f"emoji:{options.emoji}")
    else:
        video_out = video_label

    # Audio graph.
    audio_out = "0:a"
    audio_changed = False
    if not info.has_audio:
        # No audio track to work with; ignore music/fade on audio.
        music_path = None
    if music_path is not None:
        # Insert the music input at index 1 (before emoji inputs).
        inputs += ["-i", str(music_path)]
        graph_parts.append(
            audio.music_mix_filter("0:a", "1:a", "aout", options.music_volume,
                                   duration, fade=options.fades)
        )
        audio_out = "aout"
        audio_changed = True
        applied.append(f"music:{options.music}")
    elif options.fades and info.has_audio:
        out_start = max(0.0, duration - 0.4)
        graph_parts.append(
            f"[0:a]afade=t=in:st=0:d=0.400,afade=t=out:st={out_start:.3f}:d=0.400[aout]"
        )
        audio_out = "aout"
        audio_changed = True

    inputs += emoji_inputs

    video_changed = bool(chain) or bool(emoji_graph)
    if not video_changed and not audio_changed:
        return None  # nothing to do

    # ---------------------------------------------------------------------
    # Build and run the ffmpeg command.
    # ---------------------------------------------------------------------
    cmd = [settings.ffmpeg_binary, "-y", *inputs]
    if graph_parts:
        cmd += ["-filter_complex", ";".join(graph_parts)]

    # Map video.
    cmd += ["-map", f"[{video_out}]" if video_changed else "0:v"]
    # Map audio (only if the source has audio).
    if info.has_audio:
        cmd += ["-map", f"[{audio_out}]" if audio_changed else "0:a"]

    # Codecs: re-encode only the streams we changed.
    if video_changed:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    else:
        cmd += ["-c:v", "copy"]
    if info.has_audio:
        if audio_changed:
            cmd += ["-c:a", "aac", "-b:a", "128k"]
        else:
            cmd += ["-c:a", "copy"]

    cmd += ["-movflags", "+faststart", str(dest)]
    _run(cmd)
    return RenderResult(dest, applied)
