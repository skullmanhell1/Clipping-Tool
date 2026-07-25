# Requirements Document

## Introduction

This spec defines the **Tier 1 Creator Output Upgrade** — a bundle of three related
capabilities that raise the visual quality and editorial power of clips produced by
the AI Video Clipper (self-hosted, v0.6.0, Phases 1–5 merged). The three features are
independent enough to ship incrementally but share the same core constraints, so they
are specified together:

1. **Animated captions** — trendy caption presets with per-word animation, keyword
   highlighting, in-caption emoji, and per-preset fonts/colors/positions. This
   *extends* the existing caption system (three static ASS templates —
   `karaoke` / `boxed` / `minimal` — with `bottom` / `center` / `top` positions).
2. **B-roll / image & clip overlay auto-insertion** — contextually insert relevant
   images or short b-roll clips over key phrases in a clip, timed to the transcript,
   individually toggleable, with graceful fallback when no asset is found.
3. **Prompt / visual clip finding** — let the user describe what they want in natural
   language and select clips using visual/scene cues in addition to the transcript,
   augmenting the existing `worker/selection.py` highlight selection.

All three features MUST preserve the product's core design values: individual
toggleability, graceful degradation when a key/asset/dependency is missing,
single-pass per-clip ffmpeg efficiency, transcript-timeline synchronisation (including
after filler removal rebases word timings), self-hosted "bring your own key"
operation, and explicit copyright/licensing/permissibility controls (including a
music/audio-permissibility preference under which added audio and downloaded assets
stay optional and off by default).

## Glossary

- **Clipper**: The overall AI Video Clipper application (self-hosted, ffmpeg-based, CPU-first).
- **Pipeline**: The per-source processing flow in `worker/pipeline.py` (probe → transcribe → selection → per-clip: cut → filler removal → geometry → compositor → thumbnail).
- **Compositor**: The single-pass effect renderer in `worker/effects/compositor.py` that applies all enabled look effects in one ffmpeg pass.
- **Caption_Engine**: The subtitle subsystem in `worker/captions.py` that builds ASS/libass cues from the word timeline and returns a `subtitles=` filter string.
- **Caption_Preset**: A named, declarative caption style bundle (animation style, font, colors, position, highlight rules, emoji policy) selectable by the user.
- **Word_Timeline**: The clip-relative word list (`start`/`end`/`text`/`probability`) produced by `slice_words`, and rebased by filler removal via `rebase_words`.
- **Broll_Engine**: The new subsystem that plans and composites image/video overlays ("b-roll") over key phrases within a clip.
- **Broll_Cue**: A planned overlay occurrence — an asset, a `[start, end]` clip-relative window, a placement, and a source phrase/keyword.
- **Asset_Provider**: A pluggable source of overlay assets. Types: **Local_Provider** (a user-supplied library folder) and **External_Provider** (a stock/API source requiring a user-supplied API key).
- **Visual_Selector**: The new selection augmentation that scores candidate moments using visual/scene cues (e.g. sampled keyframes) in addition to transcript text.
- **Selection_Prompt**: A free-text user description of the moments to find (e.g. "find every moment where the speaker laughs").
- **Processing_Options**: The user-selected options record (`worker/models.py` `ProcessingOptions`, mirrored by `OptionsModel`, upload Form fields, `App.jsx` `toOptions`, and `SettingsPanel.jsx`).
- **Info_Endpoint**: The `/api/info` endpoint that advertises available option values to the UI.
- **Degraded_Mode**: Operation when an optional dependency (LLM/API key, asset, network, model, ffmpeg feature) is unavailable; features no-op cleanly and the Pipeline still produces clips.
- **BYOK**: "Bring your own key" — the self-hosted model where the operator supplies their own provider API keys and there are no per-clip fees.
- **Permissibility_Mode**: A user setting that keeps all added audio and all externally-downloaded assets disabled unless explicitly enabled (supports the operator's stated music/audio-permissibility preference).
- **Asset_Sourcing_Mode**: A setting controlling where overlay assets may come from: `off`, `local_only` (no external download), or `local_then_external`.

## Requirements

---

## Feature A — Animated Captions

### Requirement 1: Selectable animated caption presets

**User Story:** As a creator, I want to choose from multiple trendy animated caption presets, so that my clips match current short-form styles beyond the three existing static templates.

#### Acceptance Criteria

1. THE Caption_Engine SHALL support the three existing templates (`karaoke`, `boxed`, `minimal`) as valid Caption_Preset values with unchanged behaviour.
2. THE Caption_Engine SHALL provide additional animated Caption_Presets that each define an animation style, a default font, a color scheme, and a default caption position.
3. WHEN a user selects a valid Caption_Preset, THE Caption_Engine SHALL render captions using that preset's animation style, font, colors, and highlight rules, and SHALL produce a successfully rendered output for that clip.
4. THE Info_Endpoint SHALL advertise the list of available Caption_Preset values to the UI.
5. IF a requested Caption_Preset value is unknown, THEN THE Caption_Engine SHALL fall back to the `karaoke` template and record the substitution in the applied-effects list.

### Requirement 2: Per-word animation

**User Story:** As a creator, I want per-word caption animation (pop/scale/typewriter), so that captions feel dynamic and hold viewer attention.

#### Acceptance Criteria

1. WHERE a Caption_Preset specifies a per-word animation style, THE Caption_Engine SHALL animate each word using that style timed to the word's `start` time in the Word_Timeline.
2. THE Caption_Engine SHALL support at least the `pop`/`scale`, `typewriter`, and `karaoke-fill` per-word animation styles.
3. THE Caption_Engine SHALL render per-word animation using libass ASS tags only, without using the ffmpeg `drawtext` filter.
4. WHEN a clip's Word_Timeline is empty, THE Caption_Engine SHALL produce no caption events and SHALL NOT fail the Pipeline.
5. FOR every rendered caption word, THE Caption_Engine SHALL constrain the animation timing to within the clip duration bounds `[0, end - start]`.

### Requirement 3: Keyword highlighting

**User Story:** As a creator, I want important words automatically emphasized, so that key points stand out without manual editing.

#### Acceptance Criteria

1. WHERE keyword highlighting is enabled for a Caption_Preset, THE Caption_Engine SHALL apply a distinct color and/or scale to words identified as important.
2. THE Caption_Engine SHALL identify important words using a deterministic offline rule set by default.
3. WHERE keyword highlighting is enabled for the Caption_Preset AND the user enables AI-assisted highlighting AND an LLM client is available, THE Caption_Engine SHALL request important-word selections from the LLM client and merge them with the deterministic rule set.
4. IF AI-assisted highlighting is requested but no LLM client is available, THEN THE Caption_Engine SHALL use the deterministic rule set only.
5. WHEN highlighting a word, THE Caption_Engine SHALL preserve that word's spoken timing in the Word_Timeline.
6. WHERE keyword highlighting is disabled for the Caption_Preset, THE Caption_Engine SHALL skip all LLM processing for highlighting.

### Requirement 4: In-caption emoji

**User Story:** As a creator, I want emoji embedded inside caption text, so that captions carry emotional cues inline with words.

#### Acceptance Criteria

1. WHERE a Caption_Preset enables in-caption emoji, THE Caption_Engine SHALL insert emoji glyphs inline within the relevant caption cue text.
2. THE Caption_Engine SHALL keep in-caption emoji functionally independent of the existing overlay-based emoji effect controlled by the `emoji` option.
3. IF an in-caption emoji glyph cannot be rendered by the active font, THEN THE Caption_Engine SHALL render the caption without that emoji glyph and SHALL retain the surrounding words.
4. WHERE Permissibility_Mode restricts externally-downloaded assets, THE Caption_Engine SHALL render in-caption emoji only from locally available glyphs.

### Requirement 5: Per-preset fonts, colors, and positions

**User Story:** As a creator, I want each preset to control fonts, colors, and position, so that presets are visually distinct and predictable.

#### Acceptance Criteria

1. THE Caption_Engine SHALL apply each Caption_Preset's declared font, color scheme, and default position when the user does not override them.
2. WHERE the user overrides the caption position (`bottom` / `center` / `top`), THE Caption_Engine SHALL apply the overridden position instead of the preset default.
3. IF a Caption_Preset's declared font is unavailable on the host, THEN THE Caption_Engine SHALL render captions using a fallback font, SHALL continue rendering the clip, and SHALL surface a font-substitution notification to the user.
4. THE Caption_Engine SHALL produce a valid ASS file that libass parses without error for every supported Caption_Preset and position combination.

### Requirement 6: Caption preset definition round-trip

**User Story:** As an operator, I want caption preset definitions to serialize and reload exactly, so that saved settings profiles reproduce the same caption look.

#### Acceptance Criteria

1. THE Clipper SHALL represent each Caption_Preset as a serializable definition (animation style, font, colors, position, highlight rules, emoji policy).
2. FOR every Caption_Preset definition, serializing the definition and then parsing the serialized form SHALL produce an equivalent definition (round-trip property).
3. WHEN a saved settings profile is applied, THE Clipper SHALL restore the previously selected Caption_Preset and its overrides.
4. IF a serialized Caption_Preset definition is malformed, THEN THE Clipper SHALL reject the definition and SHALL fall back to the `karaoke` template.

---

## Feature B — B-roll / Image & Clip Overlay Auto-Insertion

### Requirement 7: Contextual overlay planning

**User Story:** As a creator, I want relevant images or short b-roll clips inserted over key phrases, so that my clips are more visually engaging.

#### Acceptance Criteria

1. WHERE b-roll overlays are enabled, THE Broll_Engine SHALL identify key phrases or keywords from the Word_Timeline and plan one Broll_Cue per selected phrase.
2. WHEN planning a Broll_Cue, THE Broll_Engine SHALL time the cue's `[start, end]` window to the source phrase's timing in the Word_Timeline.
3. THE Broll_Engine SHALL bound every Broll_Cue window within the clip duration `[0, end - start]`.
4. THE Broll_Engine SHALL limit the number and total on-screen duration of Broll_Cues per clip according to a user-selectable intensity setting.
5. WHEN b-roll overlays are disabled, THE Broll_Engine SHALL clear any existing planned Broll_Cues and SHALL plan zero new Broll_Cues.

### Requirement 8: Asset sourcing and modes

**User Story:** As an operator, I want to control where overlay assets come from, so that I can stay fully local or use an external provider with my own key.

#### Acceptance Criteria

1. THE Broll_Engine SHALL support an Asset_Sourcing_Mode with the values `off`, `local_only`, and `local_then_external`.
2. WHERE Asset_Sourcing_Mode is `local_only`, THE Broll_Engine SHALL resolve assets only from the user-provided Local_Provider library folder and SHALL NOT perform any external download.
3. WHERE Asset_Sourcing_Mode is `local_then_external` and an External_Provider API key is configured, THE Broll_Engine SHALL query the External_Provider only after the Local_Provider yields no match.
4. IF an External_Provider is selected but no API key is configured, THEN THE Broll_Engine SHALL operate as if Asset_Sourcing_Mode were `local_only`.
5. WHERE Asset_Sourcing_Mode is `off`, THE Broll_Engine SHALL disable all asset sourcing regardless of any configured API key.
6. WHERE Permissibility_Mode restricts externally-downloaded assets, THE Broll_Engine SHALL force Asset_Sourcing_Mode to `local_only` regardless of other settings.
7. THE Info_Endpoint SHALL advertise the available Asset_Sourcing_Mode values to the UI.

### Requirement 9: Graceful fallback when no asset is found

**User Story:** As a creator, I want the clip to render normally when no matching asset exists, so that a missing asset never breaks output.

#### Acceptance Criteria

1. IF the Broll_Engine finds no asset for a planned Broll_Cue, THEN THE Broll_Engine SHALL drop that cue and SHALL continue rendering the clip.
2. IF an asset fails to download or decode, THEN THE Broll_Engine SHALL drop the affected Broll_Cue and SHALL retain all other cues.
3. WHEN all Broll_Cues are dropped, THE Compositor SHALL enter a b-roll-disabled rendering mode and SHALL render the clip identically to a clip with b-roll overlays disabled.
4. THE Broll_Engine SHALL record in the clip's applied-effects list only the b-roll cues that were actually composited.

### Requirement 10: Composition with existing effects in a single pass

**User Story:** As an operator, I want b-roll overlays composed efficiently alongside captions and emoji, so that clip rendering stays fast on CPU.

#### Acceptance Criteria

1. THE Compositor SHALL composite b-roll overlays within the existing single ffmpeg pass together with captions, look effects, and emoji.
2. THE Compositor SHALL apply b-roll overlays in a defined layer order relative to captions and emoji so that captions remain legible.
3. WHEN both b-roll overlays and emoji overlays are enabled, THE Compositor SHALL assign distinct ffmpeg input indices to each overlay asset without index collisions.
4. FOR every Broll_Cue regardless of asset type, THE Compositor SHALL bound the overlay's on-screen presence to the cue's `[start, end]` timing window.
5. IF a Broll_Cue has a zero-length window (`start` equals `end`), THEN THE Compositor SHALL display the overlay for a minimum of one frame.
6. IF the single-pass filtergraph cannot be constructed for the b-roll overlays, THEN THE Compositor SHALL render the clip without b-roll overlays rather than fail the clip.

### Requirement 11: Timeline synchronisation after filler removal

**User Story:** As a creator, I want b-roll timing to stay aligned with speech even when filler words are removed, so that overlays land on the right phrases.

#### Acceptance Criteria

1. WHERE filler removal is enabled, THE Broll_Engine SHALL plan Broll_Cues using the rebased Word_Timeline produced after filler removal.
2. WHEN filler removal rebases word timings, THE Broll_Engine SHALL NOT place any Broll_Cue in a removed time interval.
3. FOR every composited Broll_Cue, THE Broll_Engine SHALL keep the cue window within the final clip duration after filler removal.

### Requirement 12: Licensing and rights controls for overlays

**User Story:** As an operator, I want licensing information tracked for overlay assets, so that I can respect copyright and rights obligations.

#### Acceptance Criteria

1. WHEN an asset is sourced from an External_Provider, THE Broll_Engine SHALL record the asset's provider, source identifier, and license/attribution metadata alongside the produced clip.
2. THE Broll_Engine SHALL treat Local_Provider assets as operator-supplied and SHALL record their source path.
3. WHERE an External_Provider requires attribution, THE Clipper SHALL make the recorded attribution available for display or export with the clip.
4. THE Clipper SHALL keep all external asset downloading disabled by default until the operator explicitly enables it.

---

## Feature C — Prompt / Visual Clip Finding

### Requirement 13: Natural-language selection prompt

**User Story:** As a creator, I want to describe what moments to find in plain language, so that selection matches my intent instead of a generic "best moments" heuristic.

#### Acceptance Criteria

1. THE Processing_Options SHALL include a Selection_Prompt free-text field.
2. WHERE a Selection_Prompt is provided and an LLM client is available, THE Visual_Selector SHALL bias moment selection toward moments matching the Selection_Prompt.
3. IF a Selection_Prompt is provided but no LLM client is available, THEN THE Visual_Selector SHALL fall back to the existing deterministic segmentation and SHALL still produce clips.
4. THE Visual_Selector SHALL continue to honour the existing `topic`, `vibe`, `clip_length`, `num_clips`, and process-range settings.

### Requirement 14: Visual/scene cue augmentation

**User Story:** As a creator, I want selection to use visual cues, not just the transcript, so that I can find moments that are defined by what is seen rather than said.

#### Acceptance Criteria

1. WHERE visual selection is enabled, THE Visual_Selector SHALL derive visual cues from sampled keyframes of the source video in addition to the transcript.
2. THE Visual_Selector SHALL combine transcript-based scores and visual-cue scores into a single ranking for candidate moments.
3. WHEN the source video has no audio track, THE Visual_Selector SHALL rank candidate moments using visual cues, and SHALL still incorporate transcript text from any other available source (such as embedded captions) when present.
4. THE Visual_Selector SHALL return candidate moments in the existing `ClipCandidate` shape (`start`, `end`, `score`, `reason`, `title`, `text`) so downstream Pipeline stages are unchanged.
5. THE Visual_Selector SHALL snap each candidate's start and end to natural boundaries as the existing selection does.

### Requirement 15: Cost, performance, and degraded behaviour of visual selection

**User Story:** As an operator, I want visual selection to be bounded in cost and to degrade gracefully, so that it stays viable on CPU and without extra dependencies.

#### Acceptance Criteria

1. THE Visual_Selector SHALL cap the number of sampled keyframes per source according to a configurable limit.
2. IF keyframe sampling fails, THEN THE Visual_Selector SHALL fall back to transcript-only selection and SHALL still produce clips.
5. IF a catastrophic failure prevents both visual and transcript-based selection, THEN THE Visual_Selector SHALL report the failure and MAY produce zero clips.
3. WHERE visual selection requires a provider that is not configured, THE Visual_Selector SHALL operate in Degraded_Mode using transcript-only selection.
4. WHEN visual selection is disabled, THE Visual_Selector SHALL behave identically to the existing `select_moments` transcript-based selection.

---

## Non-Functional Requirements

### Requirement 16: Individual toggleability and defaults

**User Story:** As an operator, I want every new capability individually toggleable and off by default where it adds cost or rights exposure, so that the tool stays predictable and safe.

#### Acceptance Criteria

1. THE Processing_Options SHALL expose an independent toggle or mode value for animated caption presets, keyword highlighting, in-caption emoji, b-roll overlays, Asset_Sourcing_Mode, Selection_Prompt, and visual selection.
2. THE Clipper SHALL default b-roll overlays, external asset downloading, and any added audio to disabled.
3. WHEN a new capability's toggle is disabled, THE Pipeline SHALL produce output identical to the pre-feature behaviour for that capability.
4. THE Processing_Options record SHALL round-trip each new field through `from_dict` and `to_dict` without loss.
5. IF serialization or deserialization of a Processing_Options field fails, THEN THE Clipper SHALL handle the failure gracefully by applying the documented default and SHALL surface an error message rather than crash.

### Requirement 17: Single-pass CPU performance

**User Story:** As an operator, I want the new visual features to stay within the single-pass compositor model, so that CPU render time does not grow disproportionately.

#### Acceptance Criteria

1. THE Compositor SHALL apply animated captions, in-caption emoji, and b-roll overlays within the existing single ffmpeg pass per clip.
2. THE Compositor SHALL re-encode only the streams it modifies and SHALL stream-copy unmodified streams.
3. WHEN no new visual feature is enabled for a clip, THE Compositor SHALL add no additional ffmpeg visual-processing passes beyond the current pipeline.
4. THE Visual_Selector SHALL perform keyframe sampling at most once per source video.
5. WHERE a non-visual optimization (such as audio normalization or metadata update) is enabled, THE Compositor MAY perform an additional pass for that optimization even when no visual feature is enabled.

### Requirement 18: Graceful degradation without keys, assets, or dependencies

**User Story:** As an operator, I want every feature to no-op cleanly when a dependency is missing, so that the tool always produces clips.

#### Acceptance Criteria

1. IF an LLM client, External_Provider key, network, model, or required ffmpeg feature is unavailable, THEN THE affected feature SHALL enter Degraded_Mode and THE Pipeline SHALL still produce clips.
2. WHEN a feature enters Degraded_Mode, THE Clipper SHALL record the degradation in the clip's applied-effects list or job status.
3. THE Clipper SHALL NOT require any external network access to produce a clip when all external-download and provider features are disabled.
4. THE Pipeline SHALL produce clips both when all dependencies operate normally and when any optional dependency is in Degraded_Mode.

### Requirement 19: Permissibility and audio/asset controls

**User Story:** As an operator with a music/audio-permissibility preference, I want all added audio and all downloaded assets to remain optional and off by default, so that the tool respects my constraints.

#### Acceptance Criteria

1. WHERE Permissibility_Mode is enabled, THE Clipper SHALL disable all added audio and SHALL force Asset_Sourcing_Mode to `local_only`.
2. THE Broll_Engine SHALL support a `local_only` Asset_Sourcing_Mode that performs no external download.
3. WHEN Permissibility_Mode is enabled, THE Clipper SHALL NOT add any music or externally-downloaded asset to any clip, regardless of the audio's source.
4. WHERE Permissibility_Mode is disabled AND the user enables music, THE Clipper SHALL allow music from local sources regardless of the active Asset_Sourcing_Mode.

### Requirement 20: Licensing and rights obligations

**User Story:** As an operator, I want licensing and attribution tracked for third-party assets, so that published clips meet rights requirements.

#### Acceptance Criteria

1. WHEN a clip includes an External_Provider asset, THE Clipper SHALL retain that asset's license and attribution metadata with the clip record.
2. THE Clipper SHALL treat all External_Provider usage as opt-in and disabled by default.
3. IF an asset's license is unknown, THEN THE Broll_Engine SHALL treat the asset as unusable and SHALL drop the corresponding Broll_Cue, regardless of the external-usage settings.

### Requirement 21: Testability with mocked providers and ffmpeg integration

**User Story:** As a developer, I want the new features testable with mocked providers and ffmpeg on tiny clips, so that the suite stays fast and deterministic.

#### Acceptance Criteria

1. THE Caption_Engine, Broll_Engine, and Visual_Selector SHALL accept dependency-injected clients/resolvers so tests can supply mock LLM clients and mock Asset_Providers.
2. THE Caption_Engine SHALL expose caption planning and ASS generation as pure functions testable without invoking ffmpeg.
3. THE Broll_Engine SHALL expose Broll_Cue planning as a pure function testable without invoking ffmpeg or downloading assets.
4. THE new ffmpeg-composited outputs SHALL be verifiable via ffprobe on tiny generated clips using the existing test helpers (`make_video`, `requires_ffmpeg`, `probe_size`, `FakeWord`).
5. FOR all valid Word_Timelines, caption and b-roll cue planning SHALL produce windows bounded within the clip duration (property-based test).

### Requirement 22: Backward compatibility

**User Story:** As an operator upgrading from v0.6.0, I want existing options and outputs to keep working, so that the upgrade is non-breaking.

#### Acceptance Criteria

1. THE Processing_Options SHALL retain all existing fields and their current default values.
2. WHEN a request omits every new option, THE Pipeline SHALL behave identically to v0.6.0.
3. THE Info_Endpoint SHALL continue to advertise all existing option values in addition to the new ones.
4. IF a new option value is unrecognised, THEN THE Clipper SHALL ignore the unknown value and SHALL apply the documented default.
