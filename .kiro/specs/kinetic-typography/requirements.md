# Requirements Document

## Introduction

This spec defines the **Kinetic Typography Engine** — animated caption rendering that goes
beyond the current static-per-cue ASS presets in the AI Video Clipper (self-hosted,
CPU-first, currently **v0.8.0**).

It is the **first concrete engine built on the approved
[`av-engines-foundation`](../av-engines-foundation/requirements.md) spec**. The foundation
defines the shared AV engine contracts (`AV_Engine`, `Engine_Context`, `Engine_Result`,
`Capability_Report`, `Time_Base`, `Engine_Workspace`, options digest and seed derivation);
this spec binds a real engine to them and adds no new foundation abstractions. The sibling
**audio stem separation** engine is explicitly **out of scope** here.

### Relationship to the existing caption system — extend the look, supersede the render

The Clipper already has a two-layer caption system:

- `worker/captions.py` builds the ASS file: `slice_words` → `words_to_cues` → `build_ass`,
  with per-word spans from `build_word_span`, style lines from `_preset_style_line`, font
  probing via `font_available`, and the ffmpeg filter string from `subtitles_filter`.
- `worker/effects/caption_presets.py` holds the Tier 1 `CaptionPreset` registry
  (`BUILTIN_PRESETS`: `karaoke`, `boxed`, `minimal`, `pop`, `typewriter`, `hormozi`), the
  `VALID_ANIMATIONS` vocabulary (`none`, `pop`, `typewriter`, `karaoke_fill`), the
  `VALID_POSITIONS` set, and the keyword planner `plan_keywords`.
- `worker/effects/compositor.py` `render_clip` decides between the legacy
  `caption_template` path and the preset path, writes one combined ASS (captions plus the
  optional hook title), and hands it to the single libass `subtitles` filter slot.

This engine's relationship to that system is deliberate and threefold:

1. **It EXTENDS the preset vocabulary.** A `CaptionPreset` remains the source of truth for
   the caption *look* (font, `CaptionColors`, `border_style`, default position). Kinetic
   typography adds a richer *motion* vocabulary and a *reveal* dimension on top of a
   chosen base preset. It does not redefine colours, fonts, or positions.
2. **It SUPERSEDES caption event rendering while enabled.** Because the compositor exposes
   exactly one libass subtitle slot, this engine — when enabled and applied — owns that
   slot for the clip and renders the dialogue events itself (including the hook title
   event, so nothing is lost). The compositor's own preset/legacy caption path is
   suppressed for that clip only. The two never render simultaneously.
3. **It CONTRADICTS nothing in Tier 1.** Every Tier 1 Feature A acceptance criterion
   (`tier1-creator-output-upgrade` Requirements 1–6) continues to hold whenever this
   engine is disabled — which is the default — and the Tier 1 animation values
   (`pop`, `typewriter`, `karaoke_fill`, `none`) remain valid, unchanged, and reachable.

All established Clipper design values are hard constraints: **default OFF**,
**CPU-only**, **no extra ffmpeg pass**, **fully local / no network / no model download**,
**graceful degradation mandatory**, **deterministic output**, **storage-backend neutral**.

### New vs. existing modules

Modules marked **(NEW)** are introduced by this spec; every other path is verified
existing code this engine integrates with.

- **(NEW)** `worker/engines/kinetic.py` — the `Kinetic_Typography_Engine` (`AV_Engine`
  subclass), `Kinetic_Options`, the pure planner, and the pure ASS emitter.
- Foundation modules this engine depends on (introduced by `av-engines-foundation`, not by
  this spec): `worker/engines/base.py`, `worker/engines/registry.py`,
  `worker/engines/capabilities.py`, `worker/engines/timebase.py`,
  `worker/engines/artifacts.py`, `worker/engines/host.py`.
- Existing integration points: `worker/captions.py` (`Cue`, `words_to_cues`,
  `font_available`, `subtitles_filter`, `_escape`, `_ass_timestamp`),
  `worker/effects/caption_presets.py` (`CaptionPreset`, `CaptionColors`,
  `BUILTIN_PRESETS`, `VALID_ANIMATIONS`, `VALID_POSITIONS`, `resolve_preset`,
  `plan_keywords`), `worker/effects/compositor.py` (`render_clip`, `RenderResult`),
  `worker/models.py` (`ProcessingOptions`, `effective_options`,
  `ClipResult.effects_applied`), `worker/transcribe.py` (`Word`), `api/main.py`
  (`OptionsModel`, `/api/upload`, `/api/info`), `frontend/src/App.jsx` (`toOptions`),
  `frontend/src/components/SettingsPanel.jsx`, `tests/conftest.py`, `tests/fakes.py`.

## Foundation contracts inlined

The following contracts are **pinned by `av-engines-foundation`** and are inlined here
verbatim. This engine binds to them exactly; it MUST NOT rename, widen, or re-invent them.

**Base class** — `AV_Engine` in `worker/engines/base.py`, with the abstract methods
`resolve_options(options)`, `plan(ctx)`, `run(ctx)` and the ClassVar contract:

```python
class Kinetic_Typography_Engine(AV_Engine):          # (NEW) worker/engines/kinetic.py
    engine_id = "kinetic_typography"                 # snake_case, stable
    stage = Engine_Stage.COMPOSE                     # contributes to the ONE compose pass
    priority = 50                                    # ordering key (registry Req 2.5)
    required_capabilities = ("ffmpeg_filter:subtitles",)
    optional_capabilities = ("font:<configured family>",)
    requires_network = False                          # fully local
    requires_model_download = False                   # fully local
    time_budget_s = 5.0                               # declared per-clip budget
    max_media_passes = 0                              # emits filters only, never runs ffmpeg
    produces_media = False                            # never returns Engine_Result.media
```

**Stage** — `Engine_Stage.COMPOSE`: the engine returns a `Compose_Contribution`
(`engine_id`, `inputs`, `video_filters`, `audio_filters`, `subtitle_path`, `z_order`) and
**never invokes ffmpeg itself**, preserving the single-pass compositor.

**Context** — `Engine_Context` supplies clip-relative bounds (`duration == clip_end -
clip_start`), the shared `Time_Base`, the rebased clip-relative `words` Word_Timeline, the
resolved `options`, `options_digest`, `seed` (via `ctx.rng()`), `workspace`,
`capabilities`, `permissibility`, `deadline` / `ctx.remaining()`, and `notes`.

**Result** — `Engine_Result` with `Engine_Status` in {`applied`, `skipped`, `degraded`,
`failed`}, plus `markers`, `artifacts`, `contribution`, `plan`, `detail`, `elapsed_s`, and
the convenience constructors `Engine_Result.skipped/degraded/failed`.

**Feature flag** — `AV_Engine.flag_field()` resolves to
`ProcessingOptions.kinetic_typography_enabled` (`engine_id` + `FLAG_SUFFIX`), **default
OFF**.

**Markers** — namespace `engine:kinetic_typography:<detail>` via `base.marker`, including
the foundation taxonomy `unavailable:<cap>`, `degraded:<cap>`, `failed`, `timeout`,
`permissibility_blocked`, `artifact_failed`.

**Capabilities** — `Capability_Id` probing through `Capability_Report`
(`status`/`available`/`first_missing`/`missing`), using the `font:<name>` and
`ffmpeg_filter:<name>` kinds; graceful degradation is mandatory.

**Timing** — `Time_Base` (`seconds_to_frame`, `frame_to_seconds`, `snap`) and
`Timeline_Segment` / `normalize_segments` for every interval; all timestamps are floats in
clip-relative seconds.

**Artifacts** — `Engine_Workspace` (`path`, `artifact`) for scratch output, i.e. the
generated `.ass` file, allocated as
`<temp_dir>/engines/<job>/<clip>/kinetic_typography__<digest>`.

**Options** — `Engine_Options` protocol (`parse`/`to_dict`) with the coercion helpers
(`coerce_bool`, `coerce_int`, `coerce_float`, `coerce_choice`, `coerce_str`),
`dump_options`, the 16-char lowercase-hex `options_digest`, and `derive_seed`.

## Glossary

Foundation terms (**AV_Engine**, **Engine_Id**, **Engine_Registry**, **Engine_Stage**,
**Engine_Context**, **Engine_Result**, **Engine_Status**, **Engine_Host**,
**Capability_Id**, **Capability_Report**, **Time_Base**, **Timeline_Segment**,
**Engine_Workspace**, **Engine_Artifact**, **Engine_Options**, **Options_Digest**,
**Feature_Flag**, **Permissibility_Mode**, **Degraded_Mode**, **Pipeline**,
**Compositor**, **Word_Timeline**, **Processing_Options**, **Effects_Applied**,
**Info_Endpoint**, **Storage_Backend**) keep the definitions given in
`av-engines-foundation/requirements.md` and are not redefined here.

Terms specific to this engine:

- **Kinetic_Engine**: **(NEW)** The `Kinetic_Typography_Engine` in
  `worker/engines/kinetic.py`; Engine_Id `kinetic_typography`, Engine_Stage `compose`.
- **Caption_Preset**: The existing `worker.effects.caption_presets.CaptionPreset` record
  (name, animation, font, font_size, `CaptionColors`, position, highlight policy,
  emoji policy, border_style).
- **Base_Preset**: The Caption_Preset whose look (font, colours, border_style, default
  position) the Kinetic_Engine renders with, resolved through the existing
  `caption_presets.resolve_preset`.
- **Kinetic_Style**: **(NEW)** A named per-word motion style in the engine's animation
  vocabulary, realised purely as ASS override tags. Allowed values: `none`,
  `karaoke_fill`, `pop`, `typewriter`, `bounce`, `slide_up`, `highlight_sweep`.
- **Reveal_Mode**: **(NEW)** How much of a cue is on screen at once. Allowed values:
  `cumulative` (the whole cue is visible and the active word is emphasised) and
  `word_by_word` (only words up to the active word are visible).
- **Kinetic_Options**: **(NEW)** The engine's Engine_Options dataclass (Kinetic_Style,
  Reveal_Mode, Base_Preset name, font override, font size, position, layout bounds,
  Safe_Area insets, motion duration, highlight policy, Word_Confidence floor).
- **Kinetic_Plan**: **(NEW)** The serialisable output of the engine's pure `plan(ctx)`
  step: an ordered list of Kinetic_Cues plus the resolved layout and style parameters.
- **Kinetic_Cue**: **(NEW)** One planned on-screen caption group: a Timeline_Segment, an
  ordered list of Kinetic_Words, and the assigned Text_Line breaks.
- **Kinetic_Word**: **(NEW)** One planned word: escaped text, clip-relative
  `[start, end)`, frame-snapped motion offsets, an emphasis flag, and a
  `timing_synthesised` flag.
- **Text_Line**: **(NEW)** One rendered line within a Kinetic_Cue, produced by the layout
  step and joined in the ASS event with an explicit `\N` break.
- **Safe_Area**: **(NEW)** The inset rectangle, expressed as a percentage of `PlayResX` /
  `PlayResY`, inside which all caption glyph boxes must fall.
- **Display_Width**: **(NEW)** The layout cost of a text run in character units, counting
  wide (East Asian fullwidth/wide) characters as 2 and other characters as 1.
- **Word_Confidence**: The existing `worker.transcribe.Word.probability` value.
- **ASS_Override_Tag**: A libass in-line override such as `\t`, `\fscx`, `\alpha`,
  `\kf`, `\move`, `\c`, `\fad`, `\N`, emitted inside an ASS `Dialogue:` event.
- **Subtitle_Slot**: The single libass `subtitles` filter position in
  `worker/effects/compositor.py`, fed today by `captions.subtitles_filter` and expressed
  in a `Compose_Contribution` as `subtitle_path`.
- **Caption_Layer**: The compositor's existing z-order position for captions: above the
  look chain and b-roll overlays, below the emoji overlays.

## Requirements

---

## Group A — Foundation Binding, Composition, and Caption Ownership

### Requirement 1: Bind to the AV engine contract

**User Story:** As a developer, I want kinetic typography to be an ordinary AV_Engine, so that it needs no bespoke Pipeline wiring.

#### Acceptance Criteria

1. THE Kinetic_Engine SHALL subclass the foundation `AV_Engine` and SHALL declare Engine_Id `kinetic_typography`, Engine_Stage `compose`, and an integer priority.
2. THE Kinetic_Engine SHALL implement `resolve_options`, `plan`, and `run` with the foundation signatures, accepting exactly one Engine_Context on `run` and returning exactly one Engine_Result.
3. THE Kinetic_Engine SHALL treat the Engine_Context as read-only, and THE Engine_Host SHALL observe that the Processing_Options instance it passed is unchanged after every Kinetic_Engine invocation.
4. THE Kinetic_Engine module SHALL import successfully in an environment where ffmpeg, libass, and every optional font are absent.
5. THE Kinetic_Engine SHALL declare `required_capabilities` containing `ffmpeg_filter:subtitles`, SHALL declare the configured font family as an optional Capability_Id, and SHALL declare `requires_network` and `requires_model_download` as false.
6. THE Kinetic_Engine SHALL declare `max_media_passes` as 0, `produces_media` as false, and a per-clip `time_budget_s` value.
7. THE Kinetic_Engine SHALL register itself with the Engine_Registry under Engine_Id `kinetic_typography` exactly once per process.
8. THE Kinetic_Engine SHALL resolve its Feature_Flag through `AV_Engine.flag_field()` to the Processing_Options field `kinetic_typography_enabled`.

### Requirement 2: Contribute to the single compositor pass

**User Story:** As an operator on a modest CPU box, I want animated captions to cost no extra render pass, so that enabling them does not slow my batches.

#### Acceptance Criteria

1. WHEN the Kinetic_Engine applies, THE Kinetic_Engine SHALL return its work as a `Compose_Contribution` whose `subtitle_path` points at the generated ASS file.
2. THE Kinetic_Engine SHALL invoke no ffmpeg process, and SHALL create no subprocess, during `plan` or `run`.
3. THE Kinetic_Engine SHALL set the `Compose_Contribution` `z_order` to the value that places the Subtitle_Slot in the existing Caption_Layer, above the look chain and b-roll overlays and below the emoji overlays.
4. THE Kinetic_Engine SHALL return an empty `inputs` tuple, an empty `audio_filters` tuple, and no `video_filters` other than those required to feed the Subtitle_Slot.
5. WHEN the Kinetic_Engine applies, THE Compositor SHALL perform the same number of ffmpeg passes per clip as it performs with the Kinetic_Engine disabled.
6. FOR every clip on which the Kinetic_Engine applies, THE Compositor SHALL feed exactly one libass `subtitles` filter instance.

### Requirement 3: Caption ownership and mutual exclusion

**User Story:** As a creator, I want animated captions to replace — never double up with — the existing caption render, so that text is never drawn twice.

#### Acceptance Criteria

1. WHILE the Kinetic_Engine Feature_Flag is disabled, THE Compositor SHALL render captions through its existing preset and legacy `caption_template` paths with unchanged behaviour.
2. WHEN the Kinetic_Engine returns Engine_Status `applied` for a clip, THE Compositor SHALL suppress its own caption ASS generation for that clip and SHALL use the Kinetic_Engine `subtitle_path` for the Subtitle_Slot.
3. WHERE the Processing_Options `hook_title` field is enabled and hook text is non-empty, THE Kinetic_Engine SHALL emit the hook title as an additional ASS event in its own ASS file using the existing `Hook` style definition, so no hook title is lost when the engine owns the Subtitle_Slot.
4. WHEN the Processing_Options `captions` field is disabled, THE Kinetic_Engine SHALL return Engine_Status `skipped`.
5. WHEN the rebased Word_Timeline for a clip is empty, THE Kinetic_Engine SHALL return Engine_Status `skipped` and THE Compositor SHALL fall back to its existing caption behaviour for that clip.
6. WHEN the Kinetic_Engine returns Engine_Status `degraded` or `failed`, THE Compositor SHALL render captions through its existing preset or legacy path so the clip still carries captions.
7. WHEN the Kinetic_Engine applies, THE Engine_Host SHALL record the markers `engine:kinetic_typography:style:<kinetic_style>` and `engine:kinetic_typography:supersedes_captions` in `ClipResult.effects_applied`.
8. THE Kinetic_Engine SHALL leave the existing Effects_Applied marker spellings documented in `worker/models.py` (`captions`, `hook_title`, `caption_preset:<name>`, `keyword_highlight`, `caption_emoji`, `font_substituted:<name>`) unchanged in meaning.
9. FOR every clip and every Kinetic_Options value, THE Compositor SHALL render caption text through exactly one of the Kinetic_Engine path and the existing caption path, never both (mutual-exclusion invariant).

---

## Group B — Animation Vocabulary and Per-Word Timing

### Requirement 4: Kinetic style vocabulary as ASS override tags

**User Story:** As a creator, I want a richer animated caption vocabulary, so that my clips match current short-form motion styles.

#### Acceptance Criteria

1. THE Kinetic_Engine SHALL support the Kinetic_Style values `none`, `karaoke_fill`, `pop`, `typewriter`, `bounce`, `slide_up`, and `highlight_sweep`.
2. THE Kinetic_Engine SHALL render every Kinetic_Style using ASS_Override_Tags interpreted by libass, and SHALL use no ffmpeg `drawtext` filter and no image overlay.
3. THE Kinetic_Engine SHALL preserve the tag semantics already implemented in `worker.captions.build_word_span` for the Kinetic_Style values shared with the existing vocabulary: `pop` as an `\fscx`/`\fscy` scale ramp driven by `\t`, `typewriter` as an `\alpha` reveal driven by `\t`, `karaoke_fill` as a `\kf` fill whose value is the word duration in centiseconds, and `none` as the plain escaped word.
4. THE Kinetic_Engine SHALL render `bounce` as a two-stage `\t` scale sequence that overshoots and then settles at 100 percent scale.
5. THE Kinetic_Engine SHALL render `slide_up` as a positional entry using `\move` or a `\t`-driven origin offset that ends at the resolved caption position.
6. THE Kinetic_Engine SHALL render `highlight_sweep` as a per-word `\t`-driven colour transition between the Base_Preset `CaptionColors.highlight` and `CaptionColors.primary` values.
7. THE Kinetic_Engine SHALL escape every word's text with the existing ASS escaping rules in `worker.captions._escape` before emitting it.
8. IF the requested Kinetic_Style is unknown, empty, or not a string, THEN THE Kinetic_Engine SHALL apply the documented default Kinetic_Style and SHALL record the marker `engine:kinetic_typography:style_substituted`.
9. THE Kinetic_Engine SHALL support the Reveal_Mode values `cumulative` and `word_by_word`, and SHALL apply the selected Reveal_Mode independently of the selected Kinetic_Style.
10. FOR every Kinetic_Style, every Reveal_Mode, and every non-empty Word_Timeline, THE Kinetic_Engine SHALL emit an ASS file in which every `Dialogue:` line has balanced override braces and a recognised style name (well-formedness invariant).
11. FOR every Kinetic_Style, every Reveal_Mode, and every non-empty Word_Timeline, THE Kinetic_Engine SHALL emit an ASS file whose visible text, with all ASS_Override_Tags removed, contains every escaped word of the Word_Timeline in Word_Timeline order (text-preservation property).

### Requirement 5: Per-word timing from the rebased Word_Timeline

**User Story:** As a creator, I want each word to animate exactly when it is spoken, so that captions feel locked to the audio.

#### Acceptance Criteria

1. THE Kinetic_Engine SHALL derive every word's motion timing from the clip-relative rebased Word_Timeline carried on the Engine_Context.
2. THE Kinetic_Engine SHALL group words into Kinetic_Cues using the existing `worker.captions.words_to_cues` grouping rules, subject to the layout bounds in Requirement 7.
3. THE Kinetic_Engine SHALL express every per-word motion offset relative to its Kinetic_Cue start, in milliseconds, as libass `\t` timings require.
4. THE Kinetic_Engine SHALL snap every Kinetic_Cue boundary to a frame boundary using the Engine_Context `Time_Base.snap` operation.
5. THE Kinetic_Engine SHALL normalise its Kinetic_Cue intervals with the foundation `normalize_segments` operation against the clip duration.
6. THE Kinetic_Engine SHALL clamp every emitted ASS timestamp to the clip-relative range `[0, ctx.duration]`.
7. FOR every Word_Timeline and every Kinetic_Options value, THE Kinetic_Engine SHALL emit Kinetic_Cue intervals that are sorted by start time, mutually non-overlapping, and contained in `[0, ctx.duration]` (timeline invariant).
8. FOR every Word_Timeline, THE Kinetic_Engine SHALL emit each word's motion start no earlier than that word's Kinetic_Cue start and no later than that word's own end time (per-word timing bound).
9. WHERE the Base_Preset enables keyword highlighting and highlighting is requested, THE Kinetic_Engine SHALL select emphasised words with the existing `worker.effects.caption_presets.plan_keywords` planner and SHALL leave every word's spoken timing unchanged.

### Requirement 6: Missing, degenerate, and low-confidence word timings

**User Story:** As a creator whose transcript is imperfect, I want captions to stay watchable when word timings are poor, so that a weak transcript never produces broken text.

#### Acceptance Criteria

1. IF a word's `end` value is missing, non-numeric, or earlier than its `start` value, THEN THE Kinetic_Engine SHALL synthesise that word's interval by distributing its Kinetic_Cue span evenly across the cue's words and SHALL mark that Kinetic_Word as `timing_synthesised`.
2. IF a word's duration is zero after normalisation, THEN THE Kinetic_Engine SHALL assign the documented minimum on-screen duration to that Kinetic_Word.
3. IF the proportion of Kinetic_Words with synthesised timings in a clip exceeds the documented threshold, THEN THE Kinetic_Engine SHALL fall back to cue-level animation, SHALL return Engine_Status `degraded`, and SHALL record the marker `engine:kinetic_typography:degraded:word_timings`.
4. WHILE cue-level animation is in force, THE Kinetic_Engine SHALL animate each Kinetic_Cue as a single unit using a `\fad` entry and exit rather than per-word motion.
5. WHERE a word's Word_Confidence is below the configured Word_Confidence floor, THE Kinetic_Engine SHALL render that word without emphasis while retaining the word's text and timing.
6. WHEN a word's text is empty or contains only whitespace, THE Kinetic_Engine SHALL omit that word from the emitted ASS events and SHALL retain the remaining words of its Kinetic_Cue.
7. FOR every Word_Timeline containing malformed, inverted, zero-length, or empty-text words, THE Kinetic_Engine SHALL return an Engine_Result without raising (totality property).

---

## Group C — Text Layout, Internationalisation, and Fonts

### Requirement 7: Layout, safe area, and line breaking

**User Story:** As a creator publishing to vertical feeds, I want captions to stay inside the safe area and wrap predictably, so that text is never clipped or hidden by platform UI.

#### Acceptance Criteria

1. THE Kinetic_Engine SHALL emit an ASS header declaring `PlayResX` and `PlayResY` equal to the clip's probed width and height.
2. THE Kinetic_Engine SHALL apply a configurable Safe_Area inset, expressed as a percentage of `PlayResX` and `PlayResY`, through the emitted style's `MarginL`, `MarginR`, and `MarginV` values.
3. THE Kinetic_Engine SHALL support the caption positions `bottom`, `center`, and `top`, using the same ASS alignment values the existing caption system uses (`2`, `5`, and `8` respectively).
4. WHERE the Kinetic_Options position value is empty, THE Kinetic_Engine SHALL use the Base_Preset `position` value.
5. THE Kinetic_Engine SHALL break each Kinetic_Cue into at most the configured maximum number of Text_Lines, joined in the ASS event with explicit `\N` breaks, because the emitted header sets `WrapStyle: 2` and libass performs no automatic wrapping.
6. THE Kinetic_Engine SHALL place at most the configured maximum Display_Width of text on each Text_Line, except where a single word exceeds that maximum.
7. IF a Kinetic_Cue cannot fit within the configured maximum number of Text_Lines, THEN THE Kinetic_Engine SHALL split that Kinetic_Cue into additional Kinetic_Cues at word boundaries and SHALL divide the original interval between the resulting cues in proportion to their word timings.
8. THE Kinetic_Engine SHALL keep every word intact within one Text_Line, so no word is split across a `\N` break.
9. FOR every Word_Timeline and every Kinetic_Options value, THE Kinetic_Engine SHALL emit ASS events whose Text_Line count per event is at most the configured maximum (layout invariant).
10. FOR every Word_Timeline and every Kinetic_Options value, THE Kinetic_Engine SHALL emit style margins that place the caption text box inside the Safe_Area rectangle (safe-area invariant).

### Requirement 8: Wide scripts, bidirectional text, emoji, and long words

**User Story:** As a creator working in more than one language, I want captions to lay out sensibly for CJK, right-to-left, emoji, and very long tokens, so that non-Latin content is not mangled.

#### Acceptance Criteria

1. THE Kinetic_Engine SHALL compute Display_Width by counting East Asian fullwidth and wide characters as 2 units and all other characters as 1 unit.
2. WHERE a Kinetic_Cue contains no inter-word spacing characters, as is typical for Chinese and Japanese text, THE Kinetic_Engine SHALL break Text_Lines at Word_Timeline word boundaries rather than at space characters.
3. THE Kinetic_Engine SHALL emit right-to-left word text in Word_Timeline order without inserting explicit directional override characters, leaving bidirectional reordering to libass.
4. THE Kinetic_Engine SHALL join Latin-script words in a Text_Line with a single space character and SHALL join words of a space-free script without an inserted space.
5. WHERE a single word's Display_Width exceeds the configured maximum line width, THE Kinetic_Engine SHALL place that word alone on its own Text_Line without splitting the word.
6. WHERE the Base_Preset enables in-caption emoji and in-caption emoji is requested, THE Kinetic_Engine SHALL append the inline glyph selected by the existing `worker.captions.caption_emoji_glyph` helper to the corresponding word span.
7. IF an in-caption emoji glyph is unavailable in the active font, THEN THE Kinetic_Engine SHALL omit that glyph and SHALL retain the surrounding words.
8. THE Kinetic_Engine SHALL write the emitted ASS file as UTF-8 text.
9. FOR every Word_Timeline containing wide-script, right-to-left, combining-mark, or emoji characters, THE Kinetic_Engine SHALL emit an ASS file whose tag-stripped visible text contains every word's escaped text (internationalisation text-preservation property).
10. FOR every Word_Timeline, THE Kinetic_Engine SHALL emit Text_Lines whose Display_Width is at most the configured maximum, except for lines holding exactly one word (line-width invariant).

### Requirement 9: Font availability probing and fallback ladder

**User Story:** As an operator on a minimal install, I want a missing font to degrade the look and nothing else, so that clips always render.

#### Acceptance Criteria

1. THE Kinetic_Engine SHALL probe the resolved font family as the Capability_Id `font:<family>` through the Engine_Context `Capability_Report`.
2. THE Capability_Report SHALL resolve the `font:<family>` Capability_Id using the existing `worker.captions.font_available` helper.
3. WHEN the resolved font family is unavailable, THE Kinetic_Engine SHALL descend a documented fallback ladder of the Kinetic_Options font override, then the Base_Preset `font`, then the documented fallback family `Arial`.
4. WHEN the Kinetic_Engine substitutes a font family, THE Kinetic_Engine SHALL return Engine_Status `degraded` and SHALL record the marker `engine:kinetic_typography:degraded:font:<requested_family>`.
5. WHEN a font family is substituted, THE Kinetic_Engine SHALL still emit the requested Kinetic_Style and Reveal_Mode.
6. THE Kinetic_Engine SHALL attempt no font download and no network access while resolving the fallback ladder.
7. FOR every Kinetic_Options value and every Capability_Report font availability combination, THE Kinetic_Engine SHALL emit exactly one `Fontname` value in its `Style:` line, and that value SHALL be a member of the documented fallback ladder (font-ladder invariant).
8. THE Kinetic_Engine SHALL record at most one font-substitution marker per clip.

---

## Group D — Options, Determinism, and Artifacts

### Requirement 10: Kinetic options resolution and round-trip

**User Story:** As an operator, I want animated caption settings to serialise and reload exactly, so that saved profiles reproduce the same look.

#### Acceptance Criteria

1. THE Kinetic_Options SHALL be a dataclass whose fields are limited to JSON-serialisable scalar, list, and mapping values, satisfying the foundation `Engine_Options` protocol.
2. THE Kinetic_Options SHALL expose fields for Kinetic_Style, Reveal_Mode, Base_Preset name, font override, font size, position, maximum Text_Lines, maximum line Display_Width, Safe_Area inset percentages, motion duration in milliseconds, keyword-highlight enablement, in-caption emoji enablement, and the Word_Confidence floor.
3. THE Kinetic_Engine `resolve_options` operation SHALL project Processing_Options onto Kinetic_Options using the foundation coercion helpers, validating Kinetic_Style, Reveal_Mode, and position against their declared known-value sets and substituting the documented default for any other value.
4. THE Kinetic_Engine `resolve_options` operation SHALL derive the Base_Preset through the existing `worker.effects.caption_presets.resolve_preset` helper and SHALL inherit that preset's font, colours, border style, and default position.
5. FOR every mapping of arbitrary values, THE Kinetic_Options parse operation SHALL return a Kinetic_Options value without raising, applying the documented default for each unrecognised or malformed field (totality property).
6. WHEN a mapping contains keys that are not Kinetic_Options fields, THE Kinetic_Options parse operation SHALL ignore those keys.
7. FOR every valid Kinetic_Options value, serialising then parsing then serialising again SHALL produce an identical mapping (round-trip property).
8. FOR every Processing_Options value, THE Kinetic_Engine `resolve_options` operation SHALL be idempotent, so resolving twice yields equal Kinetic_Options (idempotence property).
9. THE Kinetic_Engine `resolve_options` operation SHALL leave the supplied Processing_Options instance unmodified.
10. THE Kinetic_Engine SHALL resolve enablement from options already normalised by `worker.models.effective_options`.

### Requirement 11: Deterministic, byte-identical ASS output

**User Story:** As a developer, I want the same clip and settings to produce the same subtitle file, so that regressions are detectable and reruns are predictable.

#### Acceptance Criteria

1. THE Kinetic_Engine `plan` operation SHALL be a pure function callable without ffmpeg, without a network, and without a model download.
2. THE Kinetic_Engine `plan` operation SHALL return a JSON-serialisable Kinetic_Plan mapping.
3. THE Kinetic_Engine SHALL derive every random choice from `Engine_Context.rng()`, seeded from the Engine_Context `seed`.
4. THE Kinetic_Engine SHALL iterate every mapping it uses to build output in sorted key order.
5. THE Kinetic_Engine SHALL exclude wall-clock time, absolute host paths, locale-dependent formatting, process identifiers, and iteration-order-dependent values from the emitted ASS content.
6. THE Kinetic_Engine SHALL format every emitted timestamp with the existing `worker.captions._ass_timestamp` centisecond format.
7. FOR the same clip bounds, the same Word_Timeline, the same Kinetic_Options, the same Time_Base, and the same seed, THE Kinetic_Engine SHALL emit byte-identical ASS file content across repeated invocations (determinism property).
8. FOR the same inputs and seed, THE Kinetic_Engine `plan` operation SHALL return equal Kinetic_Plan values across repeated invocations (planning determinism property).
9. FOR every Kinetic_Options value, THE Engine_Host SHALL compute an Options_Digest that is equal for equal option values and different for option values differing in at least one field (digest determinism property).
10. FOR every Kinetic_Plan, serialising then parsing the Kinetic_Plan SHALL produce an equivalent Kinetic_Plan (plan round-trip property).

### Requirement 12: Workspace and artifact handling

**User Story:** As an operator with limited disk, I want generated subtitle files cleaned up like every other temp file, so that disk usage stays bounded.

#### Acceptance Criteria

1. THE Kinetic_Engine SHALL write its generated ASS file inside the Engine_Workspace supplied on the Engine_Context, using the workspace `path` operation.
2. THE Kinetic_Engine SHALL declare the generated ASS file as a transient Engine_Artifact with media type `subtitle` and with the durable flag set to false by default.
3. THE Kinetic_Engine SHALL write no file outside its Engine_Workspace.
4. THE Kinetic_Engine SHALL return the generated ASS file both as an Engine_Artifact and as the `Compose_Contribution` `subtitle_path`.
5. IF writing the ASS file raises an operating-system error, THEN THE Kinetic_Engine SHALL return Engine_Status `failed` with the error summary as the result detail.
6. FOR every job identifier, clip identifier, and Options_Digest, THE Kinetic_Engine SHALL produce an ASS file path that resolves inside the Pipeline `temp_dir` (containment invariant).
7. WHERE an operator requests a durable subtitle artifact, THE Engine_Host SHALL persist the ASS file through the active Storage_Backend using the foundation `artifact_key` and SHALL record the resulting storage key in the Engine_Result.

---

## Group E — Degradation, Failure Isolation, Cost, and Permissibility

### Requirement 13: Graceful degradation when a capability is unavailable

**User Story:** As a creator, I want clips to keep rendering when animated captions cannot run, so that a missing dependency never blocks output.

#### Acceptance Criteria

1. WHEN the Capability_Report reports `ffmpeg_filter:subtitles` unavailable, THE Engine_Host SHALL skip the Kinetic_Engine's work, SHALL return Engine_Status `degraded`, and SHALL record the marker `engine:kinetic_typography:unavailable:ffmpeg_filter:subtitles`.
2. WHEN the Kinetic_Engine degrades for any reason, THE Pipeline SHALL still write the clip file and SHALL still return a ClipResult for that clip.
3. THE Engine_Host SHALL record exactly one degradation marker per degraded Kinetic_Engine invocation per clip.
4. WHILE the Kinetic_Engine Feature_Flag is disabled, THE Engine_Host SHALL allocate no Engine_Workspace for the Kinetic_Engine and SHALL probe no font Capability_Id on its behalf.
5. FOR every combination of available and unavailable Capability_Ids, THE Pipeline SHALL produce the same number of clips as a Kinetic_Engine-disabled run of the same input (invariant under degradation).

### Requirement 14: Failure isolation and bounded budget

**User Story:** As an operator, I want a broken caption render to spoil only the captions, so that a whole batch is never lost.

#### Acceptance Criteria

1. IF the Kinetic_Engine raises any exception during `run`, THEN THE Engine_Host SHALL catch that exception, SHALL return Engine_Status `failed`, and SHALL record the marker `engine:kinetic_typography:failed`.
2. WHEN the Kinetic_Engine returns Engine_Status `failed`, THE Compositor SHALL continue building the clip using the remaining enabled effects.
3. IF the Kinetic_Engine exceeds its declared per-clip time budget, THEN THE Engine_Host SHALL abandon the contribution, SHALL record the marker `engine:kinetic_typography:timeout`, and SHALL continue the clip.
4. IF the Engine_Context `remaining` value reaches zero during planning, THEN THE Kinetic_Engine SHALL stop planning and SHALL return Engine_Status `degraded` with the marker `engine:kinetic_typography:degraded:budget`.
5. THE Engine_Host SHALL log the caught exception type and message for a failed Kinetic_Engine invocation.
6. FOR every Word_Timeline and every Kinetic_Options value, THE Pipeline SHALL produce the same clip count whether the Kinetic_Engine applies, degrades, or fails (failure-isolation invariant).

### Requirement 15: Fully local, offline, permissibility-safe operation

**User Story:** As an operator with a permissibility preference, I want animated captions to stay entirely local, so that no external call or download happens.

#### Acceptance Criteria

1. THE Kinetic_Engine SHALL complete `resolve_options`, `plan`, and `run` with no external network access.
2. THE Kinetic_Engine SHALL require no model download and SHALL declare `requires_model_download` as false.
3. WHERE Permissibility_Mode is enabled, THE Kinetic_Engine SHALL run normally, because it declares no external network requirement and adds no audio.
4. WHERE Permissibility_Mode is enabled, THE Kinetic_Engine SHALL render in-caption emoji only from locally available font glyphs and SHALL attempt no asset download.
5. THE Kinetic_Engine SHALL execute on CPU only and SHALL require no GPU.
6. FOR every Kinetic_Options value, THE Kinetic_Engine SHALL perform zero network operations and zero subprocess invocations (locality invariant).

### Requirement 16: Bounded, declared cost

**User Story:** As an operator, I want animated captions to have a declared, bounded cost, so that enabling them cannot make a job run indefinitely.

#### Acceptance Criteria

1. THE Kinetic_Engine SHALL declare a per-clip time budget and SHALL declare zero additional media passes.
2. THE Kinetic_Engine SHALL reuse the Time_Base supplied on the Engine_Context rather than probing the clip media itself.
3. THE Kinetic_Engine SHALL emit at most one ASS file per clip invocation.
4. FOR every Word_Timeline, THE Kinetic_Engine SHALL emit an ASS event count that is at most the Kinetic_Cue count plus one hook event (output-size bound).
5. WHEN the Kinetic_Engine Feature_Flag is disabled, THE Pipeline SHALL perform no additional work attributable to the Kinetic_Engine.

---

## Group F — Surface, Testability, and Compatibility

### Requirement 17: API and UI surface

**User Story:** As a creator, I want to select an animated caption style in the UI and see which ones my install can render, so that I do not enable something that silently degrades.

#### Acceptance Criteria

1. THE Processing_Options SHALL expose the Feature_Flag field `kinetic_typography_enabled`, defaulting to disabled, together with the Kinetic_Options fields.
2. THE Info_Endpoint SHALL advertise the Engine_Id `kinetic_typography`, its default-enabled value, its current availability, the supported Kinetic_Style values, and the supported Reveal_Mode values.
3. THE Info_Endpoint SHALL continue to advertise all existing v0.8.0 option values, including the existing Caption_Preset list and `VALID_ANIMATIONS` values.
4. THE `OptionsModel` and the `/api/upload` Form fields in `api/main.py` SHALL accept the Kinetic_Engine Feature_Flag and every Kinetic_Options field.
5. THE frontend defaults in `frontend/src/App.jsx` SHALL include the Kinetic_Engine Feature_Flag at its disabled default, and `toOptions` SHALL forward every Kinetic_Options field.
6. THE `SettingsPanel.jsx` component SHALL present the Kinetic_Engine Feature_Flag, Kinetic_Style, and Reveal_Mode controls.
7. IF the API receives an unrecognised value for a Kinetic_Options field, THEN THE Clipper SHALL apply the documented default and SHALL still process the job.
8. FOR every Kinetic_Options field, THE Processing_Options SHALL round-trip that field through `from_dict` and `dataclasses.asdict` without loss (options round-trip property).

### Requirement 18: Testability offline with injected dependencies

**User Story:** As a developer, I want the engine testable offline with fakes, so that the suite stays fast, deterministic, and CPU-only.

#### Acceptance Criteria

1. THE Kinetic_Engine SHALL accept dependency-injected collaborators for font availability probing, keyword planning, and the ASS writer.
2. THE Kinetic_Engine `plan`, layout, Display_Width, and ASS-emission operations SHALL be pure functions callable without ffmpeg and without a network.
3. THE Kinetic_Engine tests SHALL construct Engine_Contexts using the foundation test doubles in `tests/fakes.py`, including a fake Capability_Report and a fake Engine_Workspace.
4. THE Kinetic_Engine tests SHALL build Word_Timelines using the existing `FakeWord` helper in `tests/conftest.py`.
5. THE libass-dependent behaviour SHALL be verified on tiny generated clips using the existing `make_video`, `requires_ffmpeg`, `probe_size`, and `probe_duration` helpers in `tests/conftest.py`.
6. THE Kinetic_Engine tests SHALL verify that a generated ASS file is parsed by libass without error for every Kinetic_Style and position combination.
7. FOR all valid Kinetic_Options values, Word_Timelines, and Time_Base values, THE Kinetic_Engine SHALL satisfy the declared round-trip, idempotence, determinism, totality, and bounds properties under property-based tests.

### Requirement 19: Backward compatibility

**User Story:** As an operator upgrading from v0.8.0, I want nothing to change until I enable animated captions, so that the upgrade is risk-free.

#### Acceptance Criteria

1. WHEN the Kinetic_Engine Feature_Flag is disabled, THE Pipeline SHALL produce clips, ASS content, `ClipResult.effects_applied`, and metadata identical to v0.8.0 for the same input and options.
2. THE Kinetic_Engine SHALL preserve every existing `worker.effects.caption_presets` value, including `BUILTIN_PRESETS`, `VALID_ANIMATIONS`, `VALID_POSITIONS`, and `FALLBACK_PRESET_NAME`.
3. THE Kinetic_Engine SHALL preserve the existing behaviour of `worker.captions.build_ass`, `build_word_span`, `words_to_cues`, and `subtitles_filter` for callers that do not use the Kinetic_Engine.
4. THE Kinetic_Engine SHALL retain every Tier 1 Feature A behaviour described in `tier1-creator-output-upgrade` Requirements 1 through 6 whenever the Kinetic_Engine Feature_Flag is disabled.
5. THE Kinetic_Engine SHALL depend only on the foundation contracts inlined in this document and SHALL require no modification to `av-engines-foundation`.
6. FOR every existing Processing_Options value with the Kinetic_Engine Feature_Flag disabled, THE Compositor SHALL emit the same ffmpeg filter graph it emits at v0.8.0 (compatibility invariant).
