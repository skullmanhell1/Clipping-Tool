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

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional

from config import settings
from worker import captions as cap
from worker.effects import audio, broll, caption_presets, emoji, overlays
from worker.ffmpeg_utils import _run, probe
from worker.models import ProcessingOptions


@dataclass
class RenderResult:
    """Outcome of a compositor render."""

    path: Path
    effects_applied: list[str]
    # Provenance for composited b-roll assets (shape:
    # ``{provider, source_id, license, attribution, keyword, path}``). Empty
    # unless b-roll cues were actually composited; the pipeline surfaces these
    # onto ``ClipResult.broll_assets`` (Reqs 9.4, 12.1, 12.2).
    broll_records: list[dict] = field(default_factory=list)


def render_clip(
    base_clip: str | Path,
    dest: str | Path,
    options: ProcessingOptions,
    words: list,
    temp_dir: str | Path,
    hook_text: str = "",
    llm_client=None,
    emoji_resolver=None,
    broll_resolver: Optional[Callable[[], list]] = None,
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

        # Feature A — decide whether the animated caption-preset system drives
        # the captions, or the legacy ``caption_template`` path does. The preset
        # system engages only when a non-default preset / animation override /
        # keyword highlighting / in-caption emoji is requested. Otherwise the
        # legacy path renders byte-for-byte as in v0.6.0 (the default "karaoke"
        # preset maps to the legacy karaoke_fill look), so an all-default run is
        # behaviourally identical and existing caption/compositor tests still
        # pass unchanged.
        use_preset = need_caps and (
            options.caption_preset != "karaoke"
            or bool(options.caption_animation)
            or options.caption_keyword_highlight
            or options.caption_emoji
        )

        if use_preset:
            preset, substituted = caption_presets.resolve_preset(
                options.caption_preset
            )
            # An explicit animation override wins over the preset default.
            if options.caption_animation:
                preset = replace(preset, animation=options.caption_animation)
            # In-caption emoji is gated on the explicit option (default OFF), so
            # a preset with built-in emoji (e.g. hormozi) only emits glyphs when
            # the user turned in-caption emoji on.
            preset = replace(preset, emoji_inline=bool(options.caption_emoji))

            # Keyword highlighting: compute indices only when enabled. When
            # disabled we pass ``None`` and make NO llm call (Req 3.6).
            keyword_indices = None
            if options.caption_keyword_highlight:
                flat_words = [w for cue in cues for w in cue.words]
                keyword_indices = caption_presets.plan_keywords(
                    flat_words,
                    use_ai=options.caption_keyword_ai,
                    client=llm_client,
                )

            notes: list[str] = []
            cap.build_ass(
                cues, ass_path,
                video_width=width, video_height=height,
                preset=preset,
                keyword_indices=keyword_indices,
                position=options.caption_position or None,
                hook_text=hook_text if need_hook else "",
                clip_duration=duration,
                permissibility=options.permissibility_mode,
                notes=notes,
            )
            applied.append(f"caption_preset:{preset.name}")
            if substituted:
                applied.append("caption_preset_substituted")
            if keyword_indices is not None:
                applied.append("keyword_highlight")
            if options.caption_emoji:
                applied.append("caption_emoji")
            for note in notes:  # e.g. font_substituted:<name>
                if note not in applied:
                    applied.append(note)
        else:
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
    # The "look" (colour/zoom/fades) is kept separate from the caption layer
    # (subtitles + progress bar) so b-roll overlays can be composited *between*
    # them — below the captions so text stays legible (Req 10.2). When no b-roll
    # is present the two are re-joined into the single chain the compositor has
    # always built, preserving byte-for-byte behaviour.
    look_chain = overlays.build_video_chain(
        duration=duration, fps=fps, width=width, height=height,
        color=options.color, zoom=options.zoom, transitions=options.transitions,
        fades=options.fades, progress_bar=False, subtitles=None,
    )
    caption_chain: list[str] = []
    if subtitles_filter:
        caption_chain.append(subtitles_filter)
    if options.progress_bar:
        caption_chain.append(overlays.progress_bar_filter(duration, width, height))

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
    if not info.has_audio:
        # No audio track to work with; ignore music/fade on audio.
        music_path = None

    # --- b-roll cues (already resolved via the injected resolver) ---------
    broll_cues: list = []
    if broll_resolver is not None:
        try:
            broll_cues = [
                c for c in (broll_resolver() or [])
                if getattr(c, "asset", None) is not None
            ]
        except Exception:
            broll_cues = []

    # ---------------------------------------------------------------------
    # Input index accounting (Req 10.3), explicit and collision-free:
    #     idx 0        : base clip
    #     idx 1        : music (when present)
    #     broll_offset : b-roll inputs (contiguous)
    #     emoji_offset : emoji inputs (after b-roll)
    # ---------------------------------------------------------------------
    broll_offset = 2 if music_path is not None else 1

    # Build the b-roll overlay graph (below captions). Any failure degrades to
    # a b-roll-disabled render rather than failing the clip (Reqs 10.6, 9.3).
    broll_input_args: list[str] = []
    broll_graph = ""
    broll_notes: list[str] = []
    broll_records: list[dict] = []
    if broll_cues:
        broll_base = "vlook" if look_chain else "0:v"
        try:
            broll_input_args, broll_graph, broll_notes = broll.build_broll_overlay(
                broll_cues, base_label=broll_base, out_label="vbroll",
                width=width, height=height, fps=fps, input_offset=broll_offset,
            )
        except Exception:
            broll_input_args, broll_graph, broll_notes = [], "", []
            if "broll_degraded" not in applied:
                applied.append("broll_degraded")
        if broll_graph:
            broll_records = [broll.broll_asset_record(c) for c in broll_cues]

    num_broll_inputs = broll_input_args.count("-i")
    emoji_offset = broll_offset + num_broll_inputs

    # ---------------------------------------------------------------------
    # Assemble the video filter graph (bottom -> top):
    #     look chain -> b-roll overlays -> captions/progress -> emoji
    # ---------------------------------------------------------------------
    inputs: list[str] = ["-i", str(base_clip)]
    graph_parts: list[str] = []
    video_label = "0:v"

    if broll_graph:
        # Split so b-roll sits below the caption layer.
        if look_chain:
            graph_parts.append(f"[0:v]{','.join(look_chain)}[vlook]")
        graph_parts.append(broll_graph)
        video_label = "vbroll"
        if caption_chain:
            graph_parts.append(f"[{video_label}]{','.join(caption_chain)}[vbase]")
            video_label = "vbase"
    else:
        # No b-roll: rebuild the single combined look+caption chain exactly as
        # the compositor always has.
        full_chain = look_chain + caption_chain
        if full_chain:
            graph_parts.append(f"[0:v]{','.join(full_chain)}[vbase]")
            video_label = "vbase"

    # Emoji overlays sit on top of the caption layer.
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

    # Record composited b-roll (only the cues actually in the graph, Req 9.4).
    if broll_graph:
        applied.extend(broll_notes)

    # Audio graph.
    audio_out = "0:a"
    audio_changed = False
    if music_path is not None:
        # Insert the music input at index 1 (before b-roll/emoji inputs).
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

    # Inputs are ordered base -> music -> b-roll -> emoji (Req 10.3).
    inputs += broll_input_args
    inputs += emoji_inputs

    video_changed = bool(look_chain) or bool(caption_chain) or bool(broll_graph) or bool(emoji_graph)
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
    return RenderResult(dest, applied, broll_records)
