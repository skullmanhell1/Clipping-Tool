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

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from config import settings
from worker import branding, caption_contrast
from worker import captions as cap
from worker.effects import audio, broll, caption_presets, emoji, overlays
from worker.ffmpeg_utils import _run, aac_args, h264_args, probe
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
    contributions: Sequence[Compose_Contribution],
) -> Path | None:
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
        subtitle_path = getattr(contribution, "subtitle_path", None)
        if (
            getattr(contribution, "engine_id", "") == KINETIC_ENGINE_ID
            and subtitle_path is not None
        ):
            return Path(subtitle_path)
    return None


def _ordered_contributions(
    contributions: Sequence[Compose_Contribution] | None,
) -> list[Compose_Contribution]:
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
    contributions: Sequence[Compose_Contribution] | None,
) -> list[Compose_Contribution]:
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
    contributions: Sequence[Compose_Contribution],
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


#: Envelope resolution used for beat detection (V19).
#:
#: Much finer than the 1 s window used for clip scoring: a bump has to land on the transient to
#: read as one, and a second of slack would put it anywhere in the bar.
BEAT_ENVELOPE_WINDOW_S = 0.1


def _beat_times(base_clip: str | Path, options: ProcessingOptions) -> tuple[float, ...]:
    """Audio accents to bump the zoom on, or ``()`` (V19).

    Returns ``()`` unless beat sync is enabled *and* a zoom is actually running, because the
    bump is a multiplier on the zoom expression - with no zoom there is nothing to multiply, and
    measuring the envelope would be a decode spent on an effect that cannot appear.
    """
    if not getattr(settings, "beat_sync_zoom", False):
        return ()
    if not (options.zoom or options.transitions):
        return ()
    from worker import audio_features

    envelope = audio_features.energy_envelope(base_clip, window=BEAT_ENVELOPE_WINDOW_S)
    if not envelope:
        return ()
    return tuple(
        audio_features.detect_onsets(
            envelope, rise_db=float(getattr(settings, "beat_sync_rise_db", 6.0))
        )
    )


class Filter_Graph:
    """Owns ffmpeg input registration, label bookkeeping and segment order.

    Before this existed, all three were expressed by hand in :func:`render_clip`: layering was
    append-order into two Python lists, labels were string literals reassigned as the function
    progressed (``video_label = "vbase"``), and input indices were derived arithmetically from
    ``engine_input_count`` and whether music was present. A mis-ordered append or a stale label
    is not an error — the graph still parses, ffmpeg still encodes, and the only symptom is in
    the pixels.

    Three responsibilities, and the third is the one that was most fragile:

    **Ordering.** Segments are emitted in the order they are added, joined with ``;``. A phase
    says what it wants layered on top; it never sees the list.

    **Labels.** The graph tracks the current video and audio label. A phase declares its
    *output* label and receives the current one as input, so the two can never disagree — which
    is what a hand-maintained ``video_label`` variable could do silently.

    **Input indices.** Inputs are registered in argv order and each registration returns the
    index of its first input. Nothing counts ``"-i"`` occurrences in a fragment any more. The
    previous accounting was three interacting offsets (music shifts b-roll, b-roll shifts emoji),
    and it was fragile enough that the brand logo is drawn with ffmpeg's ``movie=`` source filter
    purely to avoid perturbing it.

    Note what this class deliberately does **not** own: whether a stream was changed enough to
    need re-encoding. That decision stays in :func:`render_clip`, because "a segment was added"
    and "the audio must be re-encoded" are different questions — loudness normalisation and the
    true-peak limiter both add segments and neither is a reason to re-encode on its own, since
    both are gated on something else having already changed the audio.
    """

    def __init__(self, base_clip: str | Path) -> None:
        #: argv fragments in ffmpeg order. The base clip is always index 0.
        self._input_args: list[str] = ["-i", str(base_clip)]
        self._next_index = 1
        self._segments: list[str] = []
        self.video_label = "0:v"
        self.audio_label = "0:a"

    # -- inputs ------------------------------------------------------------
    @property
    def next_input_index(self) -> int:
        """The index the next registered input will occupy.

        Read *before* registering, by builders that need to reference their own inputs inside a
        filter fragment they are about to produce.
        """
        return self._next_index

    def add_inputs(self, args: Sequence[str]) -> int:
        """Register a block of ffmpeg input arguments; return its first input's index.

        Registration order is argv order, so an index handed out here is the index ffmpeg will
        assign. That equivalence is the whole point: it used to be maintained by arithmetic in a
        different part of the function from where the inputs were appended.
        """
        first = self._next_index
        self._input_args.extend(str(arg) for arg in args)
        self._next_index += list(args).count("-i")
        return first

    @property
    def input_args(self) -> list[str]:
        return list(self._input_args)

    # -- filters -----------------------------------------------------------
    def chain_video(self, filters: Sequence[str], out_label: str) -> None:
        """Apply ``filters`` to the current video label, producing ``out_label``."""
        if not filters:
            return
        self._segments.append(f"[{self.video_label}]{','.join(filters)}[{out_label}]")
        self.video_label = out_label

    def chain_audio(self, filters: Sequence[str], out_label: str) -> None:
        """Apply ``filters`` to the current audio label, producing ``out_label``."""
        if not filters:
            return
        self._segments.append(f"[{self.audio_label}]{','.join(filters)}[{out_label}]")
        self.audio_label = out_label

    def add_video_segment(self, segment: str, out_label: str) -> None:
        """Add a pre-built segment that leaves the video on ``out_label``.

        For the overlay builders (b-roll, emoji, brand logo), which emit their own multi-step
        fragments because they wire up their own inputs. They are given the current label and
        told which one to produce, so the graph still owns the hand-off.
        """
        if not segment:
            return
        self._segments.append(segment)
        self.video_label = out_label

    def add_audio_segment(self, segment: str, out_label: str) -> None:
        if not segment:
            return
        self._segments.append(segment)
        self.audio_label = out_label

    def serialise(self) -> str:
        """The ``-filter_complex`` value, or an empty string when nothing was added."""
        return ";".join(self._segments)

    def __bool__(self) -> bool:
        return bool(self._segments)


def render_clip(
    base_clip: str | Path,
    dest: str | Path,
    options: ProcessingOptions,
    words: list,
    temp_dir: str | Path,
    hook_text: str = "",
    llm_client=None,
    emoji_resolver=None,
    broll_resolver: Callable[[], list] | None = None,
    engine_contributions: Sequence[Compose_Contribution] | None = None,
    music_select_key: str = "",
) -> RenderResult | None:
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
    engine_input_args = _engine_input_args(_input_order_contributions(engine_contributions))
    base_clip = Path(base_clip)
    dest = Path(dest)
    temp_dir = Path(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    # The graph owns inputs, labels and segment order from here on. The base clip is index 0 and
    # the reserved engine block follows it immediately, because the host publishes each engine's
    # ``first_input_index`` before the engine runs and knows nothing about the music, b-roll or
    # emoji decisions taken later in this function.
    graph = Filter_Graph(base_clip)
    graph.add_inputs(engine_input_args)

    info = probe(base_clip)
    width, height = info.width, info.height
    fps = info.fps or 30.0
    duration = info.duration
    applied: list[str] = []

    # --- captions + hook title (single combined ASS) ---------------------
    subtitles_filter: str | None = None
    # C19: the highlighted word indices, hoisted out of the preset branch below so the emoji
    # planner can read them. `None` means "no highlighting ran", which is distinct from an empty
    # set ("highlighting ran and chose nothing") - the planner treats only the latter as a decision.
    keyword_indices: set[int] | None = None
    # O12: in `soft` mode the captions are delivered as a selectable track by the pipeline
    # instead of being burned in here. The hook title is unaffected - it is a title card, not a
    # caption, and there is no soft equivalent of one.
    burn_captions = str(getattr(settings, "caption_mode", "burned") or "burned") != "soft"
    need_caps = options.captions and bool(words) and burn_captions
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
            preset, _substituted = caption_presets.resolve_preset(options.caption_preset)
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
            preset, substituted = caption_presets.resolve_preset(options.caption_preset)
            # An explicit animation override wins over the preset default.
            if options.caption_animation:
                preset = replace(preset, animation=options.caption_animation)
            # In-caption emoji is gated on the explicit option (default OFF), so
            # a preset with built-in emoji (e.g. hormozi) only emits glyphs when
            # the user turned in-caption emoji on.
            preset = replace(preset, emoji_inline=bool(options.caption_emoji))

            # U6: the brand kit's typography overrides the preset's. A preset is a *look* - how
            # captions animate, where they sit - and the kit is an *identity*, so choosing the
            # hormozi preset with a brand font should give hormozi's animation in the brand's
            # typeface. Inert when no kit is configured.
            preset, brand_markers = branding.apply_brand(preset, options)
            applied.extend(brand_markers)

            # C20: choose the outline/box colour from the footage behind the caption. After the
            # brand kit deliberately - the kit sets the *fill*, and this reacts to whatever fill is
            # in force by adjusting only the legibility layer around it. Inert unless enabled.
            preset, contrast_markers = caption_contrast.choose_for_clip(
                base_clip,
                preset,
                duration=duration,
                video_width=width,
                video_height=height,
                position=options.caption_position or None,
            )
            applied.extend(contrast_markers)

            # Keyword highlighting: compute indices only when enabled. When
            # disabled we pass ``None`` and make NO llm call (Req 3.6).
            if options.caption_keyword_highlight:
                flat_words = [w for cue in cues for w in cue.words]
                keyword_indices = caption_presets.plan_keywords(
                    flat_words,
                    use_ai=options.caption_keyword_ai,
                    client=llm_client,
                )

            notes: list[str] = []
            cap.build_ass(
                cues,
                ass_path,
                video_width=width,
                video_height=height,
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
                cues,
                ass_path,
                video_width=width,
                video_height=height,
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
        duration=duration,
        fps=fps,
        width=width,
        height=height,
        color=options.color,
        zoom=options.zoom,
        transitions=options.transitions,
        fades=options.fades,
        progress_bar=False,
        subtitles=None,
        # V9: which opening treatment `transitions` means. Default `punch_in` is what shipped.
        transition_style=str(getattr(settings, "transition_style", "punch_in")),
        # V18: an optional 3D LUT after the preset. Empty (the default) changes nothing.
        color_lut=str(getattr(settings, "color_lut", "") or ""),
        # V19: eased Ken Burns, and scale bumps on real audio accents.
        zoom_ease=bool(getattr(settings, "zoom_ease", False)),
        beats=_beat_times(base_clip, options),
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

    # V14: the closing call-to-action, above the captions so it is never occluded by a long
    # final cue. Its own ASS, so it is independent of whether captions ran at all and of whether
    # an engine took ownership of them.
    end_card_path = cap.write_end_card_ass(
        temp_dir / f"{base_clip.stem}.endcard.ass",
        duration,
        video_width=width,
        video_height=height,
        # U6: a brand kit's standing CTA is the end card. Without this the CTA was regenerated
        # per clip by the LLM, so a creator with one standing ask got a different wording on
        # every clip. The global END_CARD_TEXT setting remains the fallback.
        text=branding.end_card_text(options) or None,
    )
    if end_card_path is not None:
        caption_chain.append(cap.subtitles_filter(end_card_path))
        applied.append("end_card")

    if options.progress_bar:
        # V13: position/style/colour/thickness come from settings; the defaults are exactly the
        # hard-coded values this replaces, so an unconfigured install renders the same bar.
        caption_chain.append(
            overlays.progress_bar_filter(
                duration,
                width,
                height,
                thickness=int(getattr(settings, "progress_bar_thickness", 12)),
                color=str(getattr(settings, "progress_bar_color", "0x22D3EE")),
                position=str(getattr(settings, "progress_bar_position", "bottom")),
                style=str(getattr(settings, "progress_bar_style", "bar")),
            )
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
            words,
            duration,
            intensity=options.emoji,
            mode=options.emoji_mode,
            client=llm_client,
            # C19: the words the captions actually highlight, so the emoji lands on the word the
            # viewer is already being pointed at. `None` when keyword highlighting is off, which
            # leaves the A11 salience ranking as the only opinion - the pre-C19 behaviour.
            keyword_indices=keyword_indices,
        )

    # --- music bed --------------------------------------------------------
    # A15: the bed carries its own provenance, because "there is a music input" and "music
    # is playing" are different claims. The synthesised fallback is a two-tone drone, and
    # reporting it as music:<mood> - indistinguishable from a real track - is what made the
    # limitation invisible.
    music_bed: audio.MusicBed | None = None
    if options.music:
        # A17: `music_select_key` picks among several tracks for the mood. Supplied by the caller
        # rather than derived here, because the only keys that work are ones this function cannot
        # see: the clip's temp filename carries a `uuid4`, so keying on anything local would give
        # a different bed on every re-run and break the M1 golden renders.
        music_bed = audio.resolve_music_bed(
            options.music, duration, temp_dir, select_key=music_select_key
        )
    if not info.has_audio:
        # No audio track to work with; ignore music/fade on audio.
        music_bed = None
    music_path: Path | None = None if music_bed is None else music_bed.path

    # --- b-roll cues (already resolved via the injected resolver) ---------
    # A22: filled in once the overlay graph is built, so only windows that are actually on screen
    # duck the bed.
    broll_duck_windows: list[tuple[float, float]] = []
    broll_duck_amount = float(getattr(settings, "broll_duck", 0.0) or 0.0)
    broll_cues: list = []
    if broll_resolver is not None:
        try:
            broll_cues = [
                c for c in (broll_resolver() or []) if getattr(c, "asset", None) is not None
            ]
        except Exception:
            broll_cues = []

    # ---------------------------------------------------------------------
    # Input registration (Req 10.3). Order is argv order, and the graph hands out the index
    # each block lands on:
    #     idx 0        : base clip
    #     idx 1..N     : reserved engine input block (empty on every v0.8.0 / all-off
    #                    run, so nothing below shifts — Reqs 1.5, 23.3)
    #     music_index  : music (when present)
    #     broll_offset : b-roll inputs (contiguous)
    #     emoji_offset : emoji inputs (after b-roll)
    #
    # The engine block sits immediately after the base clip because the host must publish each
    # engine's ``first_input_index`` BEFORE the engine runs, and it knows nothing about the
    # music/b-roll/emoji decisions taken here.
    #
    # Music is *registered* here rather than in the audio section further down, even though its
    # filter is not added until then. Registration and filtering are separate concerns and the
    # graph is what keeps them consistent: the b-roll offset depends on whether a music input
    # exists, so the two used to be linked by the arithmetic
    # ``(2 if music_path is not None else 1) + <engine input count>`` — correct, and silently
    # wrong the moment anything else claimed an index.
    # ---------------------------------------------------------------------
    music_index = graph.next_input_index if music_path is not None else -1
    if music_path is not None:
        graph.add_inputs(["-i", str(music_path)])
    broll_offset = graph.next_input_index

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
                broll_cues,
                base_label=broll_base,
                out_label="vbroll",
                width=width,
                height=height,
                fps=fps,
                input_offset=broll_offset,
                # A22: motion on stills. Off by default - it cover-crops them into a fixed box,
                # which is a visible change to the shipped look.
                ken_burns=bool(getattr(settings, "broll_ken_burns", False)),
                zoom=float(getattr(settings, "broll_ken_burns_zoom", 0.0) or 0.0),
            )
        except Exception:
            broll_input_args, broll_graph, broll_notes = [], "", []
            if "broll_degraded" not in applied:
                applied.append("broll_degraded")
        if broll_graph:
            broll_records = [broll.broll_asset_record(c) for c in broll_cues]
            # A22: the windows the audio side ducks under. Taken from the cues that actually
            # produced a graph, so a failed overlay does not leave an unexplained dip in the bed.
            broll_duck_windows = [
                (max(0.0, float(c.start)), max(0.0, float(c.end)))
                for c in broll_cues
                if float(c.end) > float(c.start)
            ]

    graph.add_inputs(broll_input_args)
    emoji_offset = graph.next_input_index

    # ---------------------------------------------------------------------
    # Assemble the video filter graph (bottom -> top):
    #     look chain -> b-roll overlays -> captions/progress -> emoji -> logo
    # ---------------------------------------------------------------------
    if broll_graph:
        # Split, so b-roll sits *below* the caption layer and text stays legible (Req 10.2).
        graph.chain_video(look_chain, "vlook")
        graph.add_video_segment(broll_graph, "vbroll")
        graph.chain_video(caption_chain, "vbase")
    else:
        # No b-roll: one combined look+caption chain, which is what the compositor has always
        # emitted when nothing needs to be composited between the two.
        graph.chain_video(look_chain + caption_chain, "vbase")

    # Emoji overlays sit on top of the caption layer.
    emoji_inputs: list[str] = []
    emoji_graph = ""
    if emoji_cues:
        emoji_inputs, emoji_graph = emoji.build_overlay(
            emoji_cues,
            base_label=graph.video_label,
            out_label="vout",
            duration=duration,
            animate=options.emoji_animate,
            # A8: the real target width. build_overlay assumed 1080, so the emoji was
            # sized for a frame the output might not have.
            frame_width=width,
            resolver=emoji_resolver,
            input_offset=emoji_offset,
            # C19: `caption` sits the glyph just clear of the caption block, which only makes
            # sense now that the emoji lands on the word the caption highlights.
            placement=str(getattr(settings, "emoji_placement", "spread") or "spread"),
            caption_position=options.caption_position or "bottom",
        )
    graph.add_inputs(emoji_inputs)
    if emoji_graph:
        graph.add_video_segment(emoji_graph, "vout")
        applied.append(f"emoji:{options.emoji}")
        # A13: record the artwork set, and record it *separately* when a glyph came from the
        # vendored Noto fallback instead. The fallback keeps the overlay rather than dropping it,
        # which is the right trade, but it means the rendered look is not the one that was asked
        # for - and a cosmetic difference nobody is told about is a bug report later.
        emoji_style = emoji.resolve_style()
        if emoji_style.name != emoji.DEFAULT_STYLE:
            applied.append(f"emoji_style:{emoji_style.name}")
            wanted = emoji.style_assets_dir(emoji_style).resolve()
            used = [Path(emoji_inputs[i + 1]) for i, a in enumerate(emoji_inputs) if a == "-i"]
            if any(path.resolve().parent != wanted for path in used):
                applied.append(f"emoji_style_degraded:{emoji_style.name}")

    # U6: the brand logo, on top of everything - captions and emoji included. A watermark that
    # an emoji overlay could cover is not a watermark.
    #
    # Read with the `movie` source filter rather than a second ffmpeg input. The input indices
    # here are load-bearing: engine contributions, music, b-roll and emoji each compute offsets
    # from them, and that accounting is what keeps the v0.8.0 parity guarantee. Adding an input
    # for a watermark would put all of those at risk to save nothing.
    logo_graph = branding.logo_filter(
        options, width, height, base_label=graph.video_label, out_label="vbrand"
    )
    if logo_graph:
        graph.add_video_segment(logo_graph, "vbrand")
        applied.append("brand_logo")

    # Record composited b-roll (only the cues actually in the graph, Req 9.4).
    if broll_graph:
        applied.extend(broll_notes)

    # Audio graph. `audio_changed` stays a local decision rather than something the graph
    # infers: it means "re-encode the audio", and the last two stages below add segments without
    # being a reason to re-encode on their own — both are gated on it already being true.
    audio_changed = False

    # AU4/AU5: clean the speech *first*, before anything is mixed into it.
    #
    # Position matters: de-noising after the music mix would attack the bed as well as the room
    # tone, and a de-esser keyed on a signal that already has music in it is keying on the wrong
    # spectrum. Both filters are off by default, so this adds nothing to an unconfigured graph.
    #
    # One honest limitation. Loudness normalisation measures the *source file* (its two-pass
    # measurement runs before this graph exists), so heavy de-noising shifts the integrated
    # loudness slightly away from what was measured. The true-peak limiter at the end of the
    # chain is what keeps that safe; the residual error is well under a LU, which is below the
    # threshold any platform normalises against.
    repair = audio.speech_repair_chain()
    if repair and info.has_audio:
        graph.chain_audio(repair, "aclean")
        audio_changed = True
        if audio.denoise_filter():
            applied.append("speech_denoise")
        if audio.deesser_filter():
            applied.append("deesser")

    speech_label = graph.audio_label
    if music_path is not None:
        # The input was registered with the others further up, so `music_index` is the index
        # ffmpeg will give it - 1 on every run without an engine contribution, which is what
        # makes the label byte-identically ``1:a`` for every v0.8.0 caller.
        graph.add_audio_segment(
            audio.music_mix_filter(
                speech_label,
                f"{music_index}:a",
                "aout",
                options.music_volume,
                duration,
                fade=options.fades,
                duck=options.music_duck,
                # A22: dip the bed under each b-roll window, so a visual
                # accent has an audible one. Only the windows that actually
                # composited - a cue whose asset failed to resolve is not on
                # screen, and ducking under nothing is a hole in the bed.
                broll_windows=broll_duck_windows,
                broll_duck=broll_duck_amount,
            ),
            "aout",
        )
        audio_changed = True
        applied.append(f"music:{options.music}")
        if options.music_duck:
            applied.append("music_ducked")
        if broll_duck_windows and broll_duck_amount > 0.0:
            applied.append(f"broll_ducked:{len(broll_duck_windows)}")
        if music_bed is not None and music_bed.synthesised:
            # A15: a labelled last resort, not a track. Recorded next to the music marker
            # so a clip's own record says which of the two it got.
            applied.append("music_degraded:synthesised")
        elif music_bed is not None and music_bed.track_count > 1:
            # A17: which of the mood's tracks this clip drew. The point of A17 is that two clips
            # in a batch do not share a bed, and nothing in a clip record would otherwise show
            # whether that happened - the path is not in the record, only the marker is.
            applied.append(f"music_track:{music_bed.track_index}/{music_bed.track_count}")
    elif options.fades and info.has_audio:
        out_start = max(0.0, duration - 0.4)
        graph.chain_audio(
            ["afade=t=in:st=0:d=0.400", f"afade=t=out:st={out_start:.3f}:d=0.400"],
            "aout",
        )
        audio_changed = True

    # Engine audio contributions chain onto whatever audio label the compositor
    # produced (Req 23.3). Skipped entirely when the source has no audio track,
    # exactly like the music/fade paths above.
    engine_audio = [f for c in contributions for f in c.audio_filters]
    if engine_audio and info.has_audio:
        graph.chain_audio(engine_audio, "aeng")
        audio_changed = True

    # --- loudness normalisation (AU1) -------------------------------------
    # Last in the audio chain, so it measures and corrects what will actually be delivered
    # rather than an intermediate stage of it. Only applied when something else already
    # changed the audio: a clip with every effect off is stream-copied, and re-encoding it
    # purely to adjust loudness would trade a generation of quality for a gain the platform
    # would otherwise apply itself.
    #
    # The measurement pass runs on the *source*, because the mix does not exist yet - both
    # happen in this one ffmpeg invocation. A bed at 0.12 with ducking moves integrated
    # loudness by a fraction of a LU, well inside loudnorm's own tolerance, and paying for a
    # second full encode to measure the finished mix would cost more than it corrects (O6).
    if options.loudness_normalise and info.has_audio and audio_changed:
        stats = audio.measure_loudness(base_clip)
        if stats is None:
            # No audio to measure, an ffmpeg without loudnorm, or an unparsable report.
            # Render at the source's own level rather than failing the clip.
            applied.append("loudness_degraded:unmeasurable")
        else:
            target = audio.platform_loudness_target(options.platform)
            graph.chain_audio([audio.loudnorm_filter(stats, target)], "aloud")
            applied.append(f"loudness:{target:g}lufs")

    # --- true-peak limiting (AU3) -----------------------------------------
    # Last, after everything that can raise a level. loudnorm targets a true-peak ceiling but
    # only on the path where it runs; with normalisation off or unmeasurable, nothing
    # constrained the output and a hot source plus a music bed sums past full scale (measured:
    # +5.5 dBFS). No marker is recorded, deliberately - this is a safety stage that is
    # inaudible when it does not engage, and "applied" markers describe choices, not guards.
    if settings.true_peak_limit_enabled and info.has_audio and audio_changed:
        graph.chain_audio([audio.true_peak_limit_filter()], "apeak")

    # Inputs were registered base -> engines -> music -> b-roll -> emoji (Req 10.3) as each
    # was decided, so the graph already holds them in argv order.
    video_changed = (
        bool(look_chain)
        or bool(caption_chain)
        or bool(broll_graph)
        or bool(emoji_graph)
        or bool(logo_graph)
    )
    if not video_changed and not audio_changed:
        return None  # nothing to do

    # ---------------------------------------------------------------------
    # Build and run the ffmpeg command.
    # ---------------------------------------------------------------------
    cmd = [settings.ffmpeg_binary, "-y", *graph.input_args]
    if graph:
        cmd += ["-filter_complex", graph.serialise()]

    # Map video.
    cmd += ["-map", f"[{graph.video_label}]" if video_changed else "0:v"]
    # Map audio (only if the source has audio).
    if info.has_audio:
        cmd += ["-map", f"[{graph.audio_label}]" if audio_changed else "0:a"]

    # Codecs: re-encode only the streams we changed.
    if video_changed:
        # The clip a user receives: frame rate normalised (O3) and a VBV ceiling (O4).
        cmd += h264_args(normalise_fps=True, vbv_cap=True)
    else:
        cmd += ["-c:v", "copy"]
    if info.has_audio:
        if audio_changed:
            cmd += aac_args()
        else:
            cmd += ["-c:a", "copy"]

    cmd += ["-movflags", "+faststart", str(dest)]
    _run(cmd)
    return RenderResult(dest, applied, broll_records)
