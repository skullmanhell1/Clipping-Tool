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
from typing import TYPE_CHECKING, Callable, Optional, Sequence

from config import settings
from worker import captions as cap
from worker.effects import audio, broll, caption_presets, emoji, overlays
from worker.ffmpeg_utils import _run, h264_args, probe
from worker.models import ProcessingOptions

if TYPE_CHECKING:  # pragma: no cover - annotation only, no runtime import added
    from worker.engines.base import Compose_Contribution


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


#: Req 3.2 — the engine whose Compose_Contribution takes over the Subtitle_Slot.
#: Spelled as a literal rather than imported from ``worker.engines.kinetic`` so the
#: compositor keeps its v0.8.0 import surface (no engine module is imported here).
KINETIC_ENGINE_ID = "kinetic_typography"


def _kinetic_subtitle_path(
    contributions: Sequence["Compose_Contribution"],
) -> Optional[Path]:
    """The ``kinetic_typography`` ASS path when that engine owns the captions.

    ``None`` — the value that keeps the whole v0.8.0 caption ladder on its normal
    path — unless a contribution from :data:`KINETIC_ENGINE_ID` carries a
    non-``None`` ``subtitle_path``. That single check *is* the caption-ownership
    decision (Reqs 3.2, 3.9): only an ``applied`` Engine_Result carries a
    contribution at all, because the foundation's ``skipped``/``degraded``/
    ``failed`` constructors leave ``contribution`` as ``None`` — so
    ``skipped``/``degraded``/``failed``/``timeout`` all fall through to the
    existing preset/legacy caption path (Reqs 3.5, 3.6, 13.2, 14.2).
    """
    for contribution in contributions:
        if (
            getattr(contribution, "engine_id", "") == KINETIC_ENGINE_ID
            and getattr(contribution, "subtitle_path", None) is not None
        ):
            return Path(contribution.subtitle_path)
    return None


def _ordered_contributions(
    contributions: Optional[Sequence["Compose_Contribution"]],
) -> list["Compose_Contribution"]:
    """Compose_Contributions in deterministic ``(z_order, engine_id)`` order.

    ``None``/empty yields ``[]``, which is what keeps the whole engine layer of
    :func:`render_clip` inert on an all-off run (Reqs 1.5, 23.3).
    """
    if not contributions:
        return []
    ordered = [c for c in contributions if c is not None]
    ordered.sort(key=lambda c: (getattr(c, "z_order", 0), getattr(c, "engine_id", "")))
    return ordered


def _input_order_contributions(
    contributions: Optional[Sequence["Compose_Contribution"]],
) -> list["Compose_Contribution"]:
    """Compose_Contributions in **registry** order — the order they arrived in.

    ``Engine_Host.run_stage`` builds ``Stage_Outcome.contributions`` by walking the
    registry, so the sequence handed to :func:`render_clip` is already in registry
    ``(priority, engine_id)`` order — exactly the order the host reserved the
    ffmpeg input indices in (``Engine_Context.first_input_index``). Emitting the
    extra ``-i`` inputs in this order is what makes an engine's
    ``[first_input_index:v]`` filter label point at its own input.

    The two orderings **intentionally decouple**: inputs follow registry order
    because that is what the index reservation is computed from and an index must
    be knowable before ``run()``, while filters follow ``(z_order, engine_id)``
    (:func:`_ordered_contributions`) because that is a *layering* decision made
    after the fact. Sorting the inputs by ``z_order`` would let a z-order change
    silently invalidate every filter label an engine had already written.
    """
    if not contributions:
        return []
    return [c for c in contributions if c is not None]


def _engine_input_args(
    contributions: Sequence["Compose_Contribution"],
) -> list[str]:
    """The ``-i`` argv fragment for the reserved engine input block.

    ``[]`` when no engine contributed an input, which is what keeps the whole
    index accounting below byte-identical to v0.8.0 on an all-off run.
    """
    args: list[str] = []
    for contribution in contributions:
        for item in contribution.inputs:
            if item.loop:
                args += ["-loop", "1"]
            if item.duration > 0:
                args += ["-t", f"{item.duration:.3f}"]
            args += ["-i", str(item.path)]
    return args


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
    engine_contributions: Optional[Sequence["Compose_Contribution"]] = None,
) -> Optional[RenderResult]:
    """Apply enabled effects to ``base_clip`` -> ``dest`` in one ffmpeg pass.

    Returns a :class:`RenderResult`, or ``None`` when no effect (and no caption)
    is enabled (the caller should then use ``base_clip`` directly).

    ``engine_contributions`` is the advanced-AV-engine seam (Reqs 1.5, 23.3).
    When it is ``None`` or empty — which is the case for every v0.8.0 caller and
    for any run with no enabled engine — every code path below, including the
    input-index accounting and the "return ``None`` when nothing changed"
    contract, is byte-identical to v0.8.0: the reserved engine input block is
    empty, so ``broll_offset``/``emoji_offset`` and the music input label keep
    exactly the values they always had.

    When contributions are present their extra ffmpeg inputs form **one
    contiguous block immediately after the base clip** (index 0), emitted in
    registry ``(priority, engine_id)`` order so each input lands on exactly the
    index the host published as ``Engine_Context.first_input_index``; music,
    b-roll and emoji inputs shift after that block. Their filters go into the same
    ``-filter_complex``, ordered by ``(z_order, engine_id)`` and inserted **below**
    the caption layer so captions stay on top, and a contribution's
    ``subtitle_path`` is handed to the existing libass slot. It is still exactly
    **one** ffmpeg pass — an engine never invokes ffmpeg itself.

    A ``kinetic_typography`` contribution carrying a ``subtitle_path`` additionally
    takes **ownership** of the captions: the compositor then generates no ASS of
    its own for that clip, so caption text is drawn by exactly one producer
    (Reqs 3.2, 3.9). Every other outcome — flag off, ``skipped``, ``degraded``,
    ``failed``, host-abandoned ``timeout`` — carries no contribution and therefore
    leaves the v0.8.0 preset/legacy ladder untouched (Reqs 3.1, 3.5, 3.6).
    """
    contributions = _ordered_contributions(engine_contributions)
    # Inputs follow registry order, filters follow (z_order, engine_id): the two
    # orderings decouple deliberately (see :func:`_input_order_contributions`).
    engine_input_args = _engine_input_args(
        _input_order_contributions(engine_contributions)
    )
    engine_input_count = engine_input_args.count("-i")
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

    # Caption ownership (Reqs 3.2, 3.9). ``None`` on every v0.8.0 / all-off run,
    # so this whole branch is inert and the ladder below is byte-for-byte the
    # v0.8.0 one.
    kinetic_ass = _kinetic_subtitle_path(contributions)
    engine_owns_captions = kinetic_ass is not None

    if engine_owns_captions:
        # Req 3.2 — the compositor's own ASS generation is suppressed *entirely*:
        # no ``words_to_cues``, no ``plan_keywords`` (hence no duplicate LLM
        # call), no ``build_ass``. ``subtitles_filter`` stays ``None`` because the
        # engine's ASS reaches the single Subtitle_Slot through the
        # ``caption_chain`` loop below, which already appends
        # ``cap.subtitles_filter(contribution.subtitle_path)`` for exactly this
        # path — so the graph carries exactly one libass ``subtitles`` filter
        # (Req 2.6) in exactly one ffmpeg pass (Req 2.5).
        #
        # The v0.8.0 marker spellings that apply are still recorded, unchanged in
        # meaning (Reqs 3.7, 3.8): under-reporting versus an identical v0.8.0 run
        # would be a behaviour change of its own. ``caption_preset:<name>`` is
        # kept because the Base_Preset really did supply the caption look the
        # engine inherited, and ``keyword_highlight`` / ``caption_emoji`` are
        # recorded on exactly the condition under which the engine's planner
        # performs them (``Kinetic_Options.from_processing_options``: the option
        # is on *and* the preset enables the feature). The
        # ``engine:kinetic_typography:*`` markers are appended by the Engine_Host,
        # not here (Req 3.7). The emission order mirrors the v0.8.0 block below
        # (look markers first, then ``captions`` / ``hook_title``), so a diff of
        # ``effects_applied`` against an identical v0.8.0 run differs in no
        # position either.
        if need_caps:
            preset, _substituted = caption_presets.resolve_preset(
                options.caption_preset
            )
            applied.append(f"caption_preset:{preset.name}")
            if options.caption_keyword_highlight and preset.highlight_keywords:
                applied.append("keyword_highlight")
            if options.caption_emoji and preset.emoji_inline:
                applied.append("caption_emoji")
            applied.append("captions")
        if need_hook:
            # Req 3.3 — the engine re-emitted the hook title into its own ASS.
            applied.append("hook_title")
    elif need_caps or need_hook:
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
    # Engine compose contributions render *below* the caption layer (Req 23.3),
    # so they sit above the look chain and any b-roll but under captions/progress
    # and the emoji layer. Empty unless an engine actually contributed, in which
    # case ``caption_chain`` is exactly what it has always been.
    caption_chain: list[str] = []
    for contribution in contributions:
        caption_chain.extend(contribution.video_filters)
        if contribution.subtitle_path:
            caption_chain.append(cap.subtitles_filter(contribution.subtitle_path))
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
    # A15: the bed carries its own provenance, because "there is a music input" and "music
    # is playing" are different claims. The synthesised fallback is a two-tone drone, and
    # reporting it as music:<mood> - indistinguishable from a real track - is what made the
    # limitation invisible.
    music_bed: Optional[audio.MusicBed] = None
    if options.music:
        music_bed = audio.resolve_music_bed(options.music, duration, temp_dir)
    if not info.has_audio:
        # No audio track to work with; ignore music/fade on audio.
        music_bed = None
    music_path: Optional[Path] = None if music_bed is None else music_bed.path

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
    #     idx 1..N     : reserved engine input block (N == engine_input_count,
    #                    0 on every v0.8.0 / all-off run — Reqs 1.5, 23.3)
    #     music_index  : music (when present)
    #     broll_offset : b-roll inputs (contiguous)
    #     emoji_offset : emoji inputs (after b-roll)
    #
    # The engine block sits immediately after the base clip because the host must
    # publish each engine's ``first_input_index`` BEFORE the engine runs, and it
    # knows nothing about the music/b-roll/emoji decisions taken here. Everything
    # downstream of the block is therefore shifted by ``engine_input_count``; with
    # no contribution that shift is 0 and every index below is unchanged.
    # ---------------------------------------------------------------------
    music_index = 1 + engine_input_count
    broll_offset = (2 if music_path is not None else 1) + engine_input_count

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
    # Base clip first, then the reserved engine block (empty unless an engine
    # contributed an input), then music / b-roll / emoji below.
    inputs: list[str] = ["-i", str(base_clip), *engine_input_args]
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
            # A8: the real target width. build_overlay assumed 1080, so the emoji was
            # sized for a frame the output might not have.
            frame_width=width,
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
        # Music follows the engine block and precedes the b-roll/emoji inputs, so
        # its index is 1 on every run without an engine contribution (i.e. the
        # label is byte-identically ``1:a`` for every v0.8.0 caller).
        inputs += ["-i", str(music_path)]
        graph_parts.append(
            audio.music_mix_filter("0:a", f"{music_index}:a", "aout",
                                   options.music_volume, duration,
                                   fade=options.fades)
        )
        audio_out = "aout"
        audio_changed = True
        applied.append(f"music:{options.music}")
        if music_bed is not None and music_bed.synthesised:
            # A15: a labelled last resort, not a track. Recorded next to the music marker
            # so a clip's own record says which of the two it got.
            applied.append("music_degraded:synthesised")
    elif options.fades and info.has_audio:
        out_start = max(0.0, duration - 0.4)
        graph_parts.append(
            f"[0:a]afade=t=in:st=0:d=0.400,afade=t=out:st={out_start:.3f}:d=0.400[aout]"
        )
        audio_out = "aout"
        audio_changed = True

    # Engine audio contributions chain onto whatever audio label the compositor
    # produced (Req 23.3). Skipped entirely when the source has no audio track,
    # exactly like the music/fade paths above.
    engine_audio = [f for c in contributions for f in c.audio_filters]
    if engine_audio and info.has_audio:
        graph_parts.append(f"[{audio_out}]{','.join(engine_audio)}[aeng]")
        audio_out = "aeng"
        audio_changed = True

    # Inputs are ordered base -> engines -> music -> b-roll -> emoji (Req 10.3);
    # the engine block was emitted with the base clip above so its indices are
    # fixed and knowable before any engine ran.
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
        # The clip a user receives, so the frame rate is normalised too (O1-O3).
        cmd += h264_args(normalise_fps=True)
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
