# Requirements Document

## Introduction

This spec defines the **Audio Stem Inpainting Engine** — stem-aware audio repair for the
AI Video Clipper (self-hosted, CPU-first, currently **v0.8.0**).

It is the **second concrete engine built on the approved
[`av-engines-foundation`](../av-engines-foundation/requirements.md) spec**, after the
[`kinetic-typography`](../kinetic-typography/requirements.md) engine. The foundation
defines the shared AV engine contracts (`AV_Engine`, `Engine_Context`, `Engine_Result`,
`Capability_Report`, `Time_Base`, `Engine_Workspace`, options digest and seed derivation);
this spec binds a second real engine to them and adds no new foundation abstractions.
Kinetic typography is a **compose-stage, filter-only** engine that produces no media. This
engine is the opposite kind of engine: it runs at the **audio stage** and it **produces
replacement clip media**. Nothing in this spec redefines, widens, or contradicts either
sibling spec.

### Scope — what "stem separation / stem inpainting" means in this tool

"Stem inpainting" here is one engine with three concrete, in-scope capabilities:

1. **Stem separation.** Decompose the clip's existing audio into a small, documented,
   canonical **Stem_Set** — `vocals`, `music`, `other` — using an optional local ML
   backend.
2. **Stem-level gain and mute.** Re-mix those stems with per-stem gains, so an operator can
   suppress background music while keeping speech (or the reverse) without touching the
   speech track's level.
3. **Inpainting proper — seam repair.** Repair audio discontinuities in the clip, first and
   foremost the **seams left by the existing filler-word removal**:
   `worker/effects/filler.py` `apply_keep_intervals` concatenates the kept intervals with
   `atrim` + `concat`, so every interior keep boundary is a hard waveform join that can
   click or pop. Repairing those joins is the strongest justification for this engine and
   is a first-class requirement (Group C), not a side effect of separation.

Explicitly **out of scope**: adding any new audio content (that is the existing music-bed
feature), loudness targets and mastering, noise reduction as a standalone user feature,
speech enhancement/de-reverb, source-level stem caching for reuse across clips (see
Requirement 2 for the rejected alternative), and any GPU-only execution path.

### Relationship to the existing audio effects — repair the mix, do not replace the mixer

The Clipper already has two audio-touching subsystems, and this engine is deliberately
additive to both:

- `worker/effects/audio.py` owns **added** audio: `available_moods`, `find_user_track`,
  `synth_bed_filter`, `synthesize_bed`, `resolve_music` and the compositor-side
  `music_mix_filter` (an `amix` snippet mixing a volume-scaled bed under the untouched
  speech). This engine never resolves, synthesises, or mixes a music bed, and it never
  changes `ProcessingOptions.music` or `music_volume`. It runs **before** the compositor,
  so the music bed is mixed on top of this engine's repaired audio — the bed is never
  separated, suppressed, or repaired.
- `worker/effects/filler.py` owns **removal**: `plan_keep_intervals` produces a
  `FillerPlan` of `Interval` keeps, `apply_keep_intervals` concatenates them into a
  tightened clip, and `rebase_words` remaps the Word_Timeline onto that tightened
  timeline. This engine neither re-plans nor re-cuts anything: it consumes the keep
  boundaries as the seam positions it must repair, and it operates on the already
  tightened clip with the already rebased Word_Timeline.

The Pipeline order in `worker/pipeline.py` `run_pipeline` is unchanged (cut → filler
removal → geometry → compositor → thumbnail); this engine occupies the foundation's
`audio` stage, i.e. after filler removal and before geometry, which is exactly where the
seams exist and where the rebased Word_Timeline is authoritative.

All established Clipper design values are hard constraints: **default OFF**, **CPU-only**,
**no mandatory network access and no engine-triggered model download**, **graceful
degradation mandatory including a dependency-free fallback**, **bounded declared cost**,
**deterministic planning**, **storage-backend neutral**.

### New vs. existing modules

Modules marked **(NEW)** are introduced by this spec; every other path is verified existing
code this engine integrates with.

- **(NEW)** `worker/engines/stems.py` — the `Stem_Inpainting_Engine` (`AV_Engine`
  subclass), `Stem_Options`, the pure planner (`Stem_Plan`, seam derivation, gain
  resolution), the stem-mix and seam-repair filter emitters, and the injectable
  `Separator_Backend` adapters (ML backend and dependency-free ffmpeg backend).
- Foundation modules this engine depends on (introduced by `av-engines-foundation`, not by
  this spec): `worker/engines/base.py`, `worker/engines/registry.py`,
  `worker/engines/capabilities.py`, `worker/engines/timebase.py`,
  `worker/engines/artifacts.py`, `worker/engines/host.py`.
- Existing integration points: `worker/effects/filler.py` (`Interval`, `FillerPlan`,
  `plan_keep_intervals`, `apply_keep_intervals`, `rebase_words`),
  `worker/effects/audio.py` (`resolve_music`, `music_mix_filter`),
  `worker/effects/compositor.py` (`render_clip`, `RenderResult`), `worker/pipeline.py`
  (`run_pipeline`), `worker/models.py` (`ProcessingOptions`, `effective_options`,
  `ClipResult.effects_applied`), `worker/ffmpeg_utils.py` (`probe`, `MediaInfo`,
  `FFmpegError`), `worker/transcribe.py` (`Word`), `config.py` (`settings.ffmpeg_binary`,
  `settings.temp_dir`), `runtime_config.py` (`auto_delete_temp`),
  `storage_backends/base.py` (`BaseStorage`, `normalize_key`),
  `storage_backends/retention.py` (`cleanup_temp`), `api/main.py` (`OptionsModel`,
  `/api/upload`, `/api/info`), `frontend/src/App.jsx` (`toOptions`),
  `frontend/src/components/SettingsPanel.jsx`, `tests/conftest.py`, `tests/fakes.py`.

## Foundation contracts inlined

The following contracts are **pinned by `av-engines-foundation`** and are inlined here
verbatim. This engine binds to them exactly; it MUST NOT rename, widen, or re-invent them.

**Base class** — `AV_Engine` in `worker/engines/base.py`, with the abstract methods
`resolve_options(options)`, `plan(ctx)`, `run(ctx)` and the ClassVar contract:

```python
class Stem_Inpainting_Engine(AV_Engine):             # (NEW) worker/engines/stems.py
    engine_id = "stem_inpainting"                    # snake_case, stable
    stage = Engine_Stage.AUDIO                       # per clip, after filler removal
    priority = 20                                    # ordering key (registry Req 2.5)
    required_capabilities = ("binary:ffmpeg",)       # only the always-present baseline
    optional_capabilities = (                        # every heavy dependency is OPTIONAL
        "python_pkg:demucs",
        "model:htdemucs",
        "ffmpeg_filter:acrossfade",
        "ffmpeg_filter:afade",
    )
    requires_network = False                          # never fetches anything at run time
    requires_model_download = True                    # declared: needs a local model file
    time_budget_s = 90.0                              # declared per-clip budget
    max_media_passes = 2                              # extract clip audio, then remux
    produces_media = True                             # MAY return Engine_Result.media
```

**Stage** — `Engine_Stage.AUDIO`: the Engine_Host invokes the engine once per clip after
filler removal and before geometry, and takes `Engine_Result.media` as the clip media for
the next stage when the engine succeeded (`raw = out.media or raw`).

**Context** — `Engine_Context` supplies `source_path`, the current `clip_path`,
clip-relative bounds (`duration == clip_end - clip_start`), the shared `Time_Base`, the
rebased clip-relative `words` Word_Timeline, the resolved `options`, `options_digest`,
`seed` (via `ctx.rng()`), `workspace`, `capabilities`, `permissibility`, `deadline` /
`ctx.remaining()`, `notes`, and `deps`.

**Result** — `Engine_Result` with `Engine_Status` in {`applied`, `skipped`, `degraded`,
`failed`}, plus `markers`, `artifacts`, `media`, `plan`, `detail`, `elapsed_s`, and the
convenience constructors `Engine_Result.skipped/degraded/failed`.

**Feature flag** — `AV_Engine.flag_field()` resolves to
`ProcessingOptions.stem_inpainting_enabled` (`engine_id` + `FLAG_SUFFIX`), **default OFF**.

**Markers** — namespace `engine:stem_inpainting:<detail>` via `base.marker`, including the
foundation taxonomy `unavailable:<cap>`, `degraded:<cap>`, `failed`, `timeout`,
`permissibility_blocked`, `artifact_failed`.

**Capabilities** — `Capability_Id` probing through `Capability_Report`
(`status`/`available`/`first_missing`/`missing`), using the `python_pkg:<name>`,
`binary:<name>`, `ffmpeg_filter:<name>` and `model:<name>` kinds, where a `model:<name>`
capability reports unavailable when the model is absent locally and downloading is
disabled; graceful degradation is mandatory.

**Timing** — `Time_Base` (`seconds_to_frame`, `frame_to_seconds`, `seconds_to_sample`,
`sample_to_seconds`, `snap`) and `Timeline_Segment` / `normalize_segments` for every
interval; all timestamps are floats in clip-relative seconds.

**Artifacts** — `Engine_Workspace` (`path`, `artifact`) for scratch audio — extracted WAV,
per-stem WAVs, the re-mixed WAV, and the replacement clip media — allocated as
`<temp_dir>/engines/<job>/<clip>/stem_inpainting__<digest>`.

**Options** — `Engine_Options` protocol (`parse`/`to_dict`) with the coercion helpers
(`coerce_bool`, `coerce_int`, `coerce_float`, `coerce_choice`, `coerce_str`),
`dump_options`, the 16-char lowercase-hex `options_digest`, and `derive_seed`.

## Glossary

Foundation terms (**AV_Engine**, **Engine_Id**, **Engine_Registry**, **Engine_Stage**,
**Engine_Context**, **Engine_Result**, **Engine_Status**, **Engine_Host**,
**Engine_Artifact**, **Engine_Workspace**, **Engine_Options**, **Options_Digest**,
**Capability_Id**, **Capability_Probe**, **Capability_Report**, **Time_Base**,
**Timeline_Segment**, **Segment_List**, **Feature_Flag**, **Permissibility_Mode**,
**Degraded_Mode**, **Pipeline**, **Compositor**, **Word_Timeline**, **Processing_Options**,
**Effects_Applied**, **Info_Endpoint**, **Storage_Backend**, **Retention_Manager**) keep
the definitions given in `av-engines-foundation/requirements.md` and are not redefined
here.

Terms specific to this engine:

- **Stem_Engine**: **(NEW)** The `Stem_Inpainting_Engine` in `worker/engines/stems.py`;
  Engine_Id `stem_inpainting`, Engine_Stage `audio`, `produces_media` true.
- **Stem_Name**: **(NEW)** A member of the canonical Stem_Set. Allowed values: `vocals`,
  `music`, `other`.
- **Stem_Set**: **(NEW)** The canonical, documented decomposition of the clip audio into
  exactly the three Stem_Names above.
- **Backend_Stem**: **(NEW)** A stem as named by a Separator_Backend (for a four-stem ML
  backend: `vocals`, `drums`, `bass`, `other`), mapped onto the Stem_Set by the documented
  Stem_Mapping.
- **Stem_Mapping**: **(NEW)** The fixed mapping from Backend_Stems to Stem_Names:
  `vocals` → `vocals`; `drums` and `bass` → `music`; `other` → `other`.
- **Separator_Backend**: **(NEW)** An injectable adapter that turns one audio file into
  per-Stem_Name audio files. Allowed values of the resolved backend identifier: `ml`
  (local ML separation) and `ffmpeg` (dependency-free spectral/band approximation).
- **Stem_Gain**: **(NEW)** A linear multiplier in `[0.0, 4.0]` applied to one Stem_Name
  during re-mixing, where `1.0` is unchanged and `0.0` is muted.
- **Mix_Preset**: **(NEW)** A named Stem_Gain bundle. Allowed values: `custom`,
  `speech_focus` (music suppressed, vocals unchanged), `music_focus` (vocals suppressed),
  `clean_speech` (music and other suppressed).
- **Seam**: **(NEW)** A clip-relative timestamp at which the clip's audio has a known
  waveform discontinuity, i.e. an interior keep boundary produced by
  `filler.apply_keep_intervals`.
- **Seam_Note**: **(NEW)** The Engine_Context `notes` entry `filler_seam:<seconds>` through
  which the Engine_Host publishes one Seam, using the foundation's existing free-form
  `notes` tuple convention.
- **Repair_Mode**: **(NEW)** How a Seam is repaired. Allowed values: `off`, `crossfade`
  (equal-power fade across the join), `spectral` (stem-aware reconstruction across the
  join, ML backend only).
- **Repair_Window**: **(NEW)** The symmetric duration in milliseconds around a Seam within
  which repair may alter samples; range `[2, 120]`.
- **Stem_Options**: **(NEW)** The engine's Engine_Options dataclass (Mix_Preset, per-Stem
  Stem_Gains, Repair_Mode, Repair_Window, declick flag, backend selection, model name).
- **Stem_Plan**: **(NEW)** The serialisable output of the engine's pure `plan(ctx)` step:
  the resolved backend identifier, the resolved Stem_Gains, the normalised Seam list, the
  per-Seam Repair_Window segments, and the resolved audio format (sample rate, channels).
- **Replacement_Media**: **(NEW)** The clip media file the Stem_Engine returns in
  `Engine_Result.media`, carrying the untouched video stream and the repaired audio stream.
- **Audio_Format**: **(NEW)** The probed sample rate and channel count of the incoming clip
  audio, which the Replacement_Media must preserve.
- **Fixed_Environment**: **(NEW)** One installed set of separation dependencies — the same
  Separator_Backend package version, the same model file content, the same thread-count and
  inference settings, on the same platform — within which byte-level reproducibility is
  claimed.
- **Degraded_With_Media**: **(NEW)** The outcome in which the Stem_Engine returns
  Engine_Status `degraded` and `Engine_Result.media` is set, because a reduced-fidelity
  Replacement_Media was produced and is to be used as the clip media.
- **Degraded_Without_Media**: **(NEW)** The outcome in which the Stem_Engine returns
  Engine_Status `degraded` and `Engine_Result.media` is unset, because the engine could not
  produce Replacement_Media, so the media produced by the preceding stage is used.
- **No_Media_Outcome**: **(NEW)** Any Stem_Engine outcome that carries no
  `Engine_Result.media`: Engine_Status `skipped`, Engine_Status `failed`, or
  Degraded_Without_Media.

## Requirements

---

## Group A — Foundation Binding, Stage Choice, and Media Replacement

### Requirement 1: Bind to the AV engine contract

**User Story:** As a developer, I want stem inpainting to be an ordinary AV_Engine, so that it needs no bespoke Pipeline wiring.

#### Acceptance Criteria

1. THE Stem_Engine SHALL subclass the foundation `AV_Engine` and SHALL declare Engine_Id `stem_inpainting`, Engine_Stage `audio`, and an integer priority.
2. THE Stem_Engine SHALL implement `resolve_options`, `plan`, and `run` with the foundation signatures, accepting exactly one Engine_Context on `run` and returning exactly one Engine_Result.
3. THE Stem_Engine SHALL treat the Engine_Context as read-only, and THE Engine_Host SHALL observe that the Processing_Options instance it passed is unchanged after every Stem_Engine invocation.
4. THE `worker/engines/stems.py` module SHALL import successfully in an environment where the separation package, the separation model, and ffmpeg are all absent.
5. THE Stem_Engine SHALL declare `required_capabilities` containing only `binary:ffmpeg`, and SHALL declare the separation package, the separation model, and every non-baseline ffmpeg filter it uses as optional Capability_Ids.
6. THE Stem_Engine SHALL declare `requires_network` false, `requires_model_download` true, `produces_media` true, `max_media_passes` as 2, and a positive per-clip `time_budget_s`.
7. THE Stem_Engine SHALL register itself with the Engine_Registry under Engine_Id `stem_inpainting` exactly once per process.
8. THE Stem_Engine SHALL resolve its Feature_Flag through `AV_Engine.flag_field()` to the Processing_Options field `stem_inpainting_enabled`, defaulting to disabled.
9. THE Stem_Engine SHALL expose `plan(ctx)` as a pure function that returns a Stem_Plan without invoking ffmpeg, without importing the separation package, without touching the network, and without reading the model file.

### Requirement 2: Engine stage selection — per clip at the audio stage

**User Story:** As an operator, I want the engine to run at the stage where its inputs are correct and its total cost is bounded, so that repairs land on the right timeline without re-processing whole sources.

#### Acceptance Criteria

1. THE Stem_Engine SHALL declare Engine_Stage `audio`, so THE Engine_Host SHALL invoke the Stem_Engine once per clip after filler removal and before the geometry stage.
2. THE Stem_Engine SHALL operate on the clip media referenced by `Engine_Context.clip_path` and SHALL treat `Engine_Context.source_path` as provenance only.
3. THE Stem_Engine SHALL derive every timestamp it uses from the clip-relative bounds `[0, duration]` and the rebased Word_Timeline supplied in the Engine_Context, so no source-relative timestamp reaches the audio processing.
4. THE Stem_Engine SHALL NOT declare Engine_Stage `source`, because a source-stage result is computed before the per-clip cut and filler removal and therefore cannot know the Seam positions that filler concatenation creates on the tightened clip timeline.
5. WHERE a job produces clips whose total duration is less than the source duration, THE Stem_Engine SHALL separate only the clip audio, so the total separated audio duration for the job is at most the total clip duration.
6. FOR any clip set derived from one source, THE Engine_Host SHALL invoke the Stem_Engine exactly once per enabled clip, and THE Stem_Engine SHALL perform at most `max_media_passes` media passes per invocation (bounded-cost invariant).
7. THE Stem_Engine SHALL write no artifact outside the per-clip Engine_Workspace it was given, so no cross-clip or cross-job state is shared.
8. FOR any two clips of the same source with equal clip audio, equal Stem_Options, and equal Seam lists, THE Stem_Engine SHALL produce equal Stem_Plans (determinism property).

### Requirement 3: Replacement media contract

**User Story:** As an operator, I want a repaired clip to flow into the rest of the Pipeline exactly like an unrepaired one, so that enabling the engine cannot break clip production.

#### Acceptance Criteria

1. WHEN the Stem_Engine applies, THE Stem_Engine SHALL write Replacement_Media inside its Engine_Workspace and SHALL return that path as `Engine_Result.media` with Engine_Status `applied`.
2. THE Stem_Engine SHALL produce Replacement_Media whose video stream is copied from the incoming clip media without re-encoding.
3. WHEN the Stem_Engine returns Engine_Status `applied` with media, THE Engine_Host SHALL pass the Replacement_Media to the geometry stage as the current clip media.
4. WHEN the Stem_Engine returns Engine_Status `skipped`, Engine_Status `failed`, or Degraded_Without_Media, THE Stem_Engine SHALL return no media and THE Engine_Host SHALL pass the media produced by the preceding stage to the geometry stage unchanged.
5. IF the Stem_Engine cannot write valid Replacement_Media, THEN THE Stem_Engine SHALL return Engine_Status `failed` with no media, so the preceding stage's media is used.
6. THE Stem_Engine SHALL leave the incoming clip media file byte-identical, writing only new files inside its Engine_Workspace.
7. WHEN the Stem_Engine applies, THE Engine_Host SHALL record the marker `engine:stem_inpainting:applied:<backend>` in `ClipResult.effects_applied`.
8. FOR every No_Media_Outcome, THE Pipeline SHALL write the same clip bytes as an all-engines-disabled run of the same input (media-untouched invariant).
9. FOR every combination of Stem_Options, capability availability, and forced failures, THE Pipeline SHALL produce the same number of ClipResults as an all-engines-disabled run of the same input (clip-count invariant).
10. WHEN the Stem_Engine returns Degraded_With_Media, THE Engine_Host SHALL pass that Replacement_Media to the geometry stage as the current clip media exactly as it does for Engine_Status `applied`, and THE audio-integrity invariants of Requirement 17 SHALL apply to that Replacement_Media unchanged.
11. FOR every Stem_Engine outcome, `Engine_Result.media` SHALL be set exactly when the outcome is Engine_Status `applied` or Degraded_With_Media, and THE media handed to the geometry stage SHALL be byte-identical to the media produced by the preceding stage exactly when the outcome is a No_Media_Outcome (media-presence invariant).

---

## Group B — Stem Separation and Stem-Level Mixing

### Requirement 4: Stem separation into a documented stem set

**User Story:** As a creator, I want the clip's audio split into named stems, so that I can act on speech and background independently.

#### Acceptance Criteria

1. THE Stem_Engine SHALL decompose the clip audio into exactly the Stem_Set `vocals`, `music`, `other`.
2. THE Stem_Engine SHALL apply the fixed Stem_Mapping when a Separator_Backend reports Backend_Stems other than the Stem_Set, mapping `vocals` to `vocals`, `drums` and `bass` to `music`, and `other` to `other`.
3. IF a Separator_Backend omits a Backend_Stem, THEN THE Stem_Engine SHALL treat the missing Stem_Name as digital silence of the clip's duration and SHALL record the marker `engine:stem_inpainting:stem_missing:<stem_name>`.
4. THE Stem_Engine SHALL extract the clip audio to an intermediate uncompressed WAV artifact in its Engine_Workspace before separation, at the probed Audio_Format.
5. THE Stem_Engine SHALL accept an injected Separator_Backend through `Engine_Context.deps` so separation is testable without the ML package.
6. FOR every produced stem, THE Stem_Engine SHALL produce audio whose duration, sample rate, and channel count equal the incoming clip Audio_Format values (format-preservation invariant).
7. FOR every clip audio input, summing all Stem_Set stems at unit gain SHALL reconstruct the incoming clip audio within the documented per-sample amplitude tolerance (additive-decomposition property).
8. WHEN the incoming clip media has no audio stream, THE Stem_Engine SHALL return Engine_Status `skipped` and SHALL record no marker.
9. THE Stem_Engine SHALL iterate Stem_Names in sorted order wherever stem output is assembled, so the result does not depend on backend mapping iteration order.

### Requirement 5: Stem-level gain and mute

**User Story:** As a creator, I want to suppress background music while keeping speech, so that spoken clips stay intelligible without re-recording.

#### Acceptance Criteria

1. THE Stem_Options SHALL expose one Stem_Gain per Stem_Name, each defaulting to `1.0`.
2. WHEN a Mix_Preset other than `custom` is resolved, THE Stem_Engine SHALL apply that preset's documented Stem_Gain bundle and SHALL ignore the individual Stem_Gain fields.
3. WHEN Mix_Preset `custom` is resolved, THE Stem_Engine SHALL apply the individual Stem_Gain fields.
4. IF a Stem_Gain value is non-numeric, negative, non-finite, or greater than the documented maximum, THEN THE Stem_Engine SHALL substitute the documented default and SHALL continue.
5. THE Stem_Engine SHALL re-mix the gained stems into a single audio stream at the incoming Audio_Format.
6. WHEN every resolved Stem_Gain equals `1.0` AND the resolved Repair_Mode is `off`, THE Stem_Engine SHALL return Engine_Status `skipped`, SHALL return no media, and SHALL perform no separation and no media pass (no-op property).
7. WHEN a Stem_Gain of `0.0` is resolved for a Stem_Name, THE Stem_Engine SHALL exclude that stem from the re-mix entirely.
8. WHEN the Stem_Engine applies a Mix_Preset, THE Engine_Host SHALL record the marker `engine:stem_inpainting:mix:<mix_preset>`.
9. FOR every clip audio input and every resolved Stem_Gain set, THE re-mixed audio SHALL introduce no sample whose absolute amplitude exceeds full scale (no-clipping invariant).
10. FOR every clip audio input, THE Stem_Engine SHALL produce re-mixed audio containing only content derived from that clip's own audio (no-added-audio invariant).

---

## Group C — Seam Inpainting

### Requirement 6: Seam discovery from filler removal

**User Story:** As a creator using filler-word removal, I want the engine to know exactly where the cuts are, so that repairs land on the real joins and nowhere else.

#### Acceptance Criteria

1. WHEN filler removal has applied a keep plan to a clip, THE Engine_Host SHALL publish each interior keep boundary as a Seam_Note `filler_seam:<seconds>` in the Engine_Context `notes` tuple, in clip-relative seconds on the tightened timeline.
2. THE Engine_Host SHALL derive each Seam_Note value from the cumulative durations of the `FillerPlan` keeps returned by `filler.plan_keep_intervals`, using the same rounding as `filler.rebase_words`.
3. THE Engine_Host SHALL publish no Seam_Note for the clip start boundary and no Seam_Note for the clip end boundary.
4. THE Stem_Engine SHALL derive its Seam list only from Seam_Notes, and SHALL NOT infer additional Seams from waveform analysis or from Word_Timeline gaps.
5. WHEN the Engine_Context contains no Seam_Note, THE Stem_Engine SHALL plan an empty Seam list and SHALL apply no seam repair.
6. IF a Seam_Note value is malformed, non-finite, negative, or outside the clip bounds `[0, duration]`, THEN THE Stem_Engine SHALL discard that Seam_Note and SHALL retain the remaining valid Seam_Notes.
7. THE Stem_Engine SHALL convert each Seam into a Repair_Window segment centred on the Seam and SHALL normalise the resulting Segment_List with the foundation `normalize_segments`, clamped to `[0, duration]`.
8. FOR every Seam_Note list, THE planned Repair_Window Segment_List SHALL be sorted, non-overlapping, and contained in `[0, duration]` (normalisation invariant).
9. FOR every filler keep plan with N keeps where N is at least 1, THE Engine_Host SHALL publish exactly N minus 1 Seam_Notes (seam-count property).

### Requirement 7: Seam repair

**User Story:** As a creator, I want the clicks and pops at filler cuts smoothed out, so that tightened clips sound like they were never cut.

#### Acceptance Criteria

1. THE Stem_Options SHALL expose a Repair_Mode with allowed values `off`, `crossfade`, and `spectral`, and a Repair_Window in milliseconds.
2. WHEN Repair_Mode `crossfade` is resolved, THE Stem_Engine SHALL apply an equal-power fade across each Seam within that Seam's Repair_Window.
3. WHERE Repair_Mode `spectral` is resolved AND the ML Separator_Backend is available, THE Stem_Engine SHALL reconstruct each Seam's Repair_Window per Stem_Name before re-mixing.
4. WHERE Repair_Mode `spectral` is resolved AND the ML Separator_Backend is unavailable, THE Stem_Engine SHALL apply Repair_Mode `crossfade` instead, SHALL record the marker `engine:stem_inpainting:degraded:python_pkg:demucs`, and SHALL return Degraded_With_Media carrying the crossfade-repaired Replacement_Media.
5. THE Stem_Engine SHALL alter samples only inside the planned Repair_Window segments, leaving every sample outside those segments unchanged apart from the resolved Stem_Gains.
6. IF a Repair_Window value is non-numeric or outside the documented range, THEN THE Stem_Engine SHALL clamp that value into the documented range and SHALL continue.
7. WHEN two Repair_Windows overlap after normalisation, THE Stem_Engine SHALL repair the merged window once rather than repairing the same samples twice.
8. WHEN the Stem_Engine repairs at least one Seam, THE Engine_Host SHALL record the marker `engine:stem_inpainting:repair:<repair_mode>:<seam_count>`.
9. FOR every clip and every Repair_Mode, THE repaired audio duration SHALL equal the incoming clip audio duration (duration-preservation invariant).
10. FOR every clip with an empty Seam list and every Stem_Gain set equal to `1.0`, THE Stem_Engine SHALL leave the audio unchanged (repair no-op property).
11. FOR every clip, applying the Stem_Engine to its own Replacement_Media with the same Stem_Options and an empty Seam list SHALL leave that media's audio unchanged (idempotence-on-repaired-output property).

### Requirement 8: Coexistence with existing audio features

**User Story:** As a creator who also uses music beds and filler removal, I want stem repair to fit between them cleanly, so that no feature undoes another.

#### Acceptance Criteria

1. THE Stem_Engine SHALL run after `filler.apply_keep_intervals` has produced the tightened clip, so it repairs the concatenated result rather than the pre-cut audio.
2. THE Stem_Engine SHALL NOT call `filler.plan_keep_intervals`, `filler.apply_keep_intervals`, or `filler.rebase_words`, and SHALL NOT alter the Word_Timeline.
3. THE Stem_Engine SHALL run before the Compositor, so the existing `audio.music_mix_filter` bed is mixed on top of the repaired audio and is never separated or suppressed.
4. THE Stem_Engine SHALL leave `ProcessingOptions.music` and `ProcessingOptions.music_volume` unchanged and SHALL leave the existing `music:<mood>` and `filler_removal` Effects_Applied markers unchanged in meaning and spelling.
5. WHILE `ProcessingOptions.filler_removal` is disabled, THE Stem_Engine SHALL still apply resolved Stem_Gains when its Feature_Flag is enabled, with an empty Seam list.
6. WHEN the Stem_Engine applies, THE Compositor SHALL perform the same number of ffmpeg passes per clip as it performs with the Stem_Engine disabled.
7. FOR every clip, THE Effects_Applied list SHALL contain the existing filler and music markers unchanged alongside any `engine:stem_inpainting:*` markers (marker-coexistence invariant).

---

## Group D — Options, Determinism, and Artifacts

### Requirement 9: Options resolution and round-trip

**User Story:** As an operator, I want stem settings to serialise and reload exactly, so that a saved job reproduces the same repair.

#### Acceptance Criteria

1. THE Stem_Options SHALL be a dataclass whose fields are limited to JSON-serialisable scalar values, covering Mix_Preset, one Stem_Gain per Stem_Name, Repair_Mode, Repair_Window, a declick flag, a backend selection, and a model name.
2. THE Stem_Options SHALL provide a parse operation from a possibly partial mapping and a serialise operation to a mapping, following the foundation `Engine_Options` protocol.
3. THE Stem_Engine SHALL validate the Mix_Preset, Repair_Mode, and backend selection fields against their declared value sets and SHALL substitute the documented default for any unrecognised value, following the existing `ProcessingOptions.from_dict` convention.
4. FOR every valid Stem_Options value, serialising then parsing then serialising again SHALL produce an identical mapping (round-trip property).
5. FOR every mapping of arbitrary values, THE Stem_Options parse operation SHALL return a Stem_Options value without raising, applying the documented default for each unrecognised or malformed field (totality property).
6. FOR every Processing_Options value, resolving Stem_Options twice SHALL produce equal Stem_Options (idempotence property).
7. FOR every pair of equal Stem_Options values, THE Options_Digest SHALL be equal, and FOR every pair differing in at least one field value THE Options_Digest SHALL differ.
8. THE Stem_Engine SHALL round-trip every new Processing_Options field through `from_dict` and `dataclasses.asdict` without loss, consistent with `tests/test_options_roundtrip.py`.

### Requirement 10: Determinism, stated honestly

**User Story:** As a developer, I want a truthful reproducibility guarantee, so that tests assert what the engine can actually promise.

#### Acceptance Criteria

1. FOR the same clip audio, the same Seam_Note list, the same rebased Word_Timeline, and the same Stem_Options, THE Stem_Engine `plan` operation SHALL produce equal Stem_Plans (planning-determinism property).
2. THE Stem_Engine SHALL derive every random choice from `Engine_Context.seed` through `ctx.rng()`, and SHALL use no other randomness source.
3. WHERE the ML Separator_Backend is used, THE Stem_Engine SHALL configure that backend for CPU execution with a pinned thread count and a seeded initial state before inference.
4. WHILE running inside one Fixed_Environment, THE Stem_Engine SHALL produce byte-identical Replacement_Media audio for equal clip audio, equal Stem_Options, and equal seeds (environment-scoped reproducibility property).
5. THE Stem_Engine SHALL NOT claim byte-identical separation output across different Separator_Backend package versions, model file contents, thread counts, or platforms.
6. FOR equal inputs across differing environments, THE Stem_Engine SHALL guarantee only equal Stem_Plans, an equal Stem_Set, equal Audio_Format values, equal output duration, and a documented amplitude tolerance on the re-mixed audio (cross-environment guarantee).
7. THE Stem_Engine SHALL record the resolved backend identifier and model name in the Stem_Plan so a reproduced run can be compared against the environment that produced it.
8. WHERE the dependency-free ffmpeg Separator_Backend is used, THE Stem_Engine SHALL produce byte-identical Replacement_Media audio for equal inputs using the same ffmpeg build (fallback-determinism property).

### Requirement 11: Workspace, artifacts, and cleanup

**User Story:** As an operator with limited disk, I want stem intermediates bounded and cleaned up, so that enabling the engine cannot fill the disk.

#### Acceptance Criteria

1. THE Stem_Engine SHALL write the extracted WAV, every per-stem WAV, the re-mixed WAV, and the Replacement_Media inside the Engine_Workspace supplied in the Engine_Context.
2. THE Stem_Engine SHALL declare each intermediate audio file as a transient Engine_Artifact with a documented media type.
3. WHERE the resolved Stem_Options request retained stems, THE Stem_Engine SHALL declare the per-stem WAVs as durable Engine_Artifacts so the Engine_Host persists them through the active Storage_Backend under keys normalised with `storage_backends.base.normalize_key`.
4. WHEN the Stem_Engine finishes, THE Stem_Engine SHALL delete the extracted WAV and per-stem WAVs it no longer needs, retaining only the Replacement_Media and any declared durable artifacts.
5. WHEN the Engine_Host deletes the Engine_Workspace for the clip, THE Pipeline SHALL already hold the Replacement_Media it needs for the geometry stage.
6. IF writing or deleting a workspace file raises an operating-system error, THEN THE Stem_Engine SHALL record the error detail in the Engine_Result and SHALL continue producing the clip.
7. FOR every job, clip, and Stem_Options combination, THE Stem_Engine SHALL keep total workspace bytes bounded by the documented multiple of the clip audio size (bounded-disk invariant).
8. FOR every completed job with `auto_delete_temp` enabled, THE Clipper SHALL leave no `stem_inpainting__*` workspace directory beneath `settings.temp_dir` (cleanup invariant).

---

## Group E — Capabilities, Degradation, Failure Isolation, Cost, and Permissibility

### Requirement 12: Capability declaration and probing

**User Story:** As an operator on a minimal install, I want to know whether real separation is possible here, so that I am not surprised by a silent downgrade.

#### Acceptance Criteria

1. THE Stem_Engine SHALL declare the separation Python package as the Capability_Id `python_pkg:<package name>` and the separation model as the Capability_Id `model:<model name>`.
2. THE Stem_Engine SHALL declare every ffmpeg filter it uses beyond the always-present baseline as an `ffmpeg_filter:<name>` Capability_Id.
3. THE Stem_Engine SHALL register a model locator for its `model:<model name>` Capability_Id that reports the model available only when the model file is present in the configured local model directory.
4. WHEN the separation model is absent locally AND downloading is disabled, THE Capability_Report SHALL report `model:<model name>` unavailable, and THE Stem_Engine SHALL continue on its fallback path.
5. THE Stem_Engine SHALL perform no network access during capability probing, planning, or running.
6. THE Stem_Engine SHALL NOT trigger a model download from inside `run`, and IF the resolved Separator_Backend would fetch a model over the network, THEN THE Stem_Engine SHALL treat that model as unavailable and SHALL degrade along the Requirement 13 fallback path, returning Degraded_With_Media when the fallback produces Replacement_Media.
7. THE Stem_Engine SHALL accept an injected Capability_Report so tests can declare any Capability_Id available or unavailable without installing the separation package or model.
8. THE Info_Endpoint SHALL advertise the availability of the separation package Capability_Id and the separation model Capability_Id.

### Requirement 13: Degradation ladder with a dependency-free fallback

**User Story:** As a creator on a CPU-only box with no ML model, I want stem inpainting to still do something useful, so that a missing model never blocks my clips.

#### Acceptance Criteria

1. WHEN the separation package and the separation model are both available, THE Stem_Engine SHALL use the `ml` Separator_Backend and SHALL record the marker `engine:stem_inpainting:applied:ml`.
2. WHEN the separation package or the separation model is unavailable, THE Stem_Engine SHALL use the dependency-free `ffmpeg` Separator_Backend, SHALL record the marker `engine:stem_inpainting:degraded:<capability_id>` for the missing capability, and SHALL return Degraded_With_Media, that is Engine_Status `degraded` with Replacement_Media, when the reduced-fidelity result is produced.
3. THE dependency-free `ffmpeg` Separator_Backend SHALL approximate the Stem_Set using ffmpeg filters only, and SHALL apply Repair_Mode `crossfade` seam repair without any model.
4. WHEN the resolved Stem_Options request only seam repair, THE Stem_Engine SHALL complete without the separation package and without the separation model.
5. IF the ffmpeg filters required by the fallback path are unavailable, THEN THE Stem_Engine SHALL return Degraded_Without_Media, that is Engine_Status `degraded` with no media, and SHALL record `engine:stem_inpainting:unavailable:<capability_id>`.
6. IF `binary:ffmpeg` is unavailable, THEN THE Engine_Host SHALL skip the engine body and SHALL record `engine:stem_inpainting:unavailable:binary:ffmpeg`.
7. THE Engine_Host SHALL record exactly one degradation marker per missing Capability_Id per clip.
8. FOR every combination of available and unavailable Capability_Ids, THE Pipeline SHALL produce the same number of clips as an all-engines-disabled run of the same input (invariant under degradation).

### Requirement 14: Failure isolation

**User Story:** As an operator, I want a broken separator to cost me only the repair, so that a batch is never lost.

#### Acceptance Criteria

1. IF the Stem_Engine raises any exception during `plan` or `run`, THEN THE Engine_Host SHALL catch it, SHALL return Engine_Status `failed`, and SHALL record `engine:stem_inpainting:failed`.
2. IF the Separator_Backend raises, returns a non-audio file, or returns audio of the wrong duration, THEN THE Stem_Engine SHALL return Engine_Status `failed` with no media.
3. IF an ffmpeg invocation raises `worker.ffmpeg_utils.FFmpegError`, THEN THE Engine_Host SHALL treat the outcome as Engine_Status `failed` for the Stem_Engine and SHALL continue the clip.
4. WHEN the Stem_Engine returns Engine_Status `failed`, THE Pipeline SHALL continue with the clip media produced by the preceding stage and SHALL still write the clip and its thumbnail.
5. THE Engine_Host SHALL log the caught exception type and message for a failed Stem_Engine invocation.
6. FOR every forced failure point in the Stem_Engine, THE Pipeline SHALL produce the same clip count and the same clip durations as an all-engines-disabled run of the same input (failure-isolation invariant).

### Requirement 15: Bounded cost on CPU

**User Story:** As an operator on a modest CPU box, I want separation cost capped per clip, so that enabling the engine cannot make a job run indefinitely.

#### Acceptance Criteria

1. THE Stem_Engine SHALL declare a positive per-clip `time_budget_s` and a `max_media_passes` value of 2.
2. THE Stem_Engine SHALL execute CPU-only by default and SHALL require no GPU.
3. THE Stem_Engine SHALL check `Engine_Context.remaining()` before starting audio extraction, before starting separation, before starting seam repair, and before starting the remux.
4. THE Stem_Engine SHALL pass a subprocess timeout derived from `Engine_Context.remaining()` to every ffmpeg invocation it makes.
5. IF the remaining budget is insufficient for separation but sufficient for seam repair, THEN THE Stem_Engine SHALL skip separation, SHALL apply Repair_Mode `crossfade` seam repair only, SHALL return Degraded_With_Media carrying the crossfade-repaired Replacement_Media, and SHALL record `engine:stem_inpainting:degraded:budget`.
6. IF the budget is exhausted during separation, THEN THE Stem_Engine SHALL abandon separation, SHALL discard every partial artifact, SHALL return Degraded_Without_Media, that is Engine_Status `degraded` with no media, and THE Engine_Host SHALL record `engine:stem_inpainting:timeout`.
7. WHEN the budget is exhausted at any point, THE Stem_Engine SHALL leave the preceding stage's clip media untouched and SHALL leave no partial Replacement_Media on disk.
8. WHILE the Stem_Engine Feature_Flag is disabled, THE Engine_Host SHALL allocate no Engine_Workspace, SHALL probe none of the Stem_Engine's exclusive Capability_Ids, and SHALL perform no additional media pass.
9. FOR every clip, THE Stem_Engine SHALL perform at most `max_media_passes` ffmpeg media passes regardless of the Seam count and the Stem_Gain values.

### Requirement 16: Permissibility and offline operation

**User Story:** As an operator with a permissibility preference, I want the engine to stay local and add no audio, so that repairs never introduce external content.

#### Acceptance Criteria

1. THE Stem_Engine SHALL declare `requires_network` false, because it never fetches a model, a package, or an asset at run time.
2. WHERE Permissibility_Mode is enabled, THE Engine_Host SHALL run the Stem_Engine, because the engine declares no external network requirement.
3. WHERE Permissibility_Mode is enabled AND the resolved Separator_Backend declares an external network requirement, THE Engine_Host SHALL return Degraded_Without_Media, that is Engine_Status `degraded` with no media, for the Stem_Engine and SHALL record `engine:stem_inpainting:permissibility_blocked`.
4. WHILE Permissibility_Mode is enabled, THE Stem_Engine SHALL produce audio derived only from the incoming clip audio, adding no synthesised bed, no external sample, and no downloaded content.
5. THE Stem_Engine SHALL declare `requires_model_download` true so the Info_Endpoint can advertise that the engine needs an operator-provisioned local model for full fidelity.
6. WHEN the separation model is absent, THE Stem_Engine SHALL produce its documented fallback result with no network access.
7. FOR every enabled configuration of the Stem_Engine, THE Pipeline SHALL complete with no outbound network connection (offline invariant).

---

## Group F — Audio Integrity, Surface, Testability, and Compatibility

### Requirement 17: Audio integrity invariants

**User Story:** As a creator, I want repaired clips to line up exactly with the video and captions, so that nothing drifts or desyncs.

#### Acceptance Criteria

1. THE Stem_Engine SHALL produce Replacement_Media whose audio duration equals the incoming clip audio duration within the documented tolerance of one audio frame.
2. THE Stem_Engine SHALL produce Replacement_Media whose sample rate and channel count equal the incoming clip Audio_Format values.
3. THE Stem_Engine SHALL produce Replacement_Media whose video duration and frame count equal those of the incoming clip media.
4. THE Stem_Engine SHALL preserve the audio start timestamp of the incoming clip so no audio-video offset is introduced.
5. IF the probed Audio_Format is missing, zero, or negative for the sample rate or the channel count, THEN THE Stem_Engine SHALL return Degraded_Without_Media, that is Engine_Status `degraded` with no media, and SHALL record `engine:stem_inpainting:degraded:audio_format`.
6. FOR every clip and every Stem_Options value, THE Replacement_Media duration SHALL equal the incoming clip duration, so downstream geometry, caption, and emoji timings computed from the rebased Word_Timeline remain valid (timeline-preservation invariant).
7. FOR every clip and every Stem_Options value, THE Replacement_Media SHALL contain exactly one audio stream and exactly one video stream (stream-count invariant).

### Requirement 18: API and UI surface

**User Story:** As a creator, I want to see and set the stem options my install can actually run, so that I do not enable something that silently degrades.

#### Acceptance Criteria

1. THE `OptionsModel` and the `/api/upload` Form fields in `api/main.py` SHALL accept `stem_inpainting_enabled` and every Stem_Options field.
2. THE Info_Endpoint SHALL advertise Engine_Id `stem_inpainting`, its default-disabled Feature_Flag, its current availability, the allowed Mix_Preset values, the allowed Repair_Mode values, and the Stem_Set.
3. THE frontend defaults in `frontend/src/App.jsx` SHALL include `stem_inpainting_enabled` as disabled, and `toOptions` SHALL forward every Stem_Options field.
4. THE `frontend/src/components/SettingsPanel.jsx` SHALL present the Stem_Engine toggle, the Mix_Preset choice, the per-Stem_Name gains, and the Repair_Mode choice.
5. WHEN the API receives an unrecognised value for a Stem_Options field, THE Clipper SHALL apply the documented default and SHALL still process the job.
6. THE Info_Endpoint SHALL continue to advertise all existing v0.8.0 option values, including the existing music moods from `audio.available_moods`, in addition to the Stem_Engine values.

### Requirement 19: Testability offline with injected dependencies

**User Story:** As a developer, I want the engine testable without the ML model, so that the suite stays fast, offline, and CPU-only.

#### Acceptance Criteria

1. THE Stem_Engine SHALL accept an injected Separator_Backend, an injected Capability_Report, and an injected command runner through `Engine_Context.deps` and its constructor.
2. THE Stem_Engine planner, Stem_Mapping, Stem_Gain resolution, Seam derivation, and Repair_Window normalisation SHALL be pure functions callable without ffmpeg, without the separation package, and without a network.
3. THE test doubles for the Separator_Backend and for Seam_Note fixtures SHALL live in the existing `tests/fakes.py` module.
4. THE ffmpeg-dependent behaviour SHALL be verifiable on tiny generated clips using the existing helpers in `tests/conftest.py` (`make_video`, `requires_ffmpeg`, `probe_duration`, `FakeWord`).
5. THE test suite SHALL verify the seam-repair path using a fake Separator_Backend that returns synthetic stems, so no model file is required.
6. FOR all valid Stem_Options, Seam_Note lists, and Word_Timelines, THE Stem_Engine SHALL satisfy the declared round-trip, idempotence, determinism, totality, and preservation invariants under property-based tests.
7. THE test suite SHALL run the Stem_Engine tests without any outbound network connection.

### Requirement 20: Backward compatibility

**User Story:** As an operator upgrading from v0.8.0, I want nothing to change until I enable stem inpainting, so that the upgrade is risk-free.

#### Acceptance Criteria

1. WHEN `stem_inpainting_enabled` is disabled, THE Pipeline SHALL produce clips, Effects_Applied, and metadata identical to a run without this engine registered, for the same input and options.
2. THE Processing_Options SHALL retain all existing v0.8.0 fields and their current default values, adding only `stem_inpainting_enabled` and the Stem_Options fields, all defaulting to the documented safe values.
3. THE Stem_Engine SHALL preserve the existing Pipeline stage order in `worker/pipeline.py` and SHALL add no new stage.
4. THE existing `ClipResult.effects_applied` marker values documented in `worker/models.py` SHALL retain their current meanings and spellings.
5. THE Stem_Engine SHALL add no new mandatory dependency to `requirements.txt`, keeping the separation package optional.
6. THE Stem_Engine SHALL depend only on the foundation contracts inlined above, requiring no change to `av-engines-foundation` and no change to `kinetic-typography`.
