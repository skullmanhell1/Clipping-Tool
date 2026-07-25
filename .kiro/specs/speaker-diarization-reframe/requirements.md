# Requirements Document

## Introduction

This spec defines **Speaker Diarisation & Multi-Speaker Reframe** — an incremental
enhancement to the AI Video Clipper (self-hosted, CPU-first, currently **v0.7.0**).

Today the tool's face-tracking auto-reframe (`worker/effects/reframe.py`) follows a
single dominant speaker: it samples frames, picks the largest face, smooths a crop
path, and applies a moving crop in one ffmpeg pass, degrading to a blurred-background
static reformat when no face is found or OpenCV is unavailable. This feature adds two
cooperating capabilities on top of that foundation:

1. **Speaker diarisation** — determine *who* is speaking *when* across a clip by
   segmenting the audio/transcript into ordered **speaker turns**, reusing the
   existing Whisper word-level timestamps (`worker/transcribe.py` `Word`) as the
   primary signal. Diarisation is optional, injectable, and offline-capable.
2. **Speaker-aware reframe** — for interviews, podcasts, and multi-person footage,
   detect each on-screen face, associate the active speaking face with the current
   speaker turn, and either (a) dynamically re-crop to follow whoever is speaking
   (*follow-active* layout) or (b) render a **split-screen / stacked** layout showing
   multiple speakers, with smooth transitions on speaker changes.

The feature MUST preserve the product's established design values, which are treated
as hard constraints throughout this document:

- **Individually toggleable and default OFF** — an all-off run reproduces v0.7.0
  output and `effects_applied` exactly.
- **CPU-only by default**, no GPU required; frame-by-frame vision work stays optional,
  bounded, and once-per-source where possible.
- **Graceful degradation is mandatory** — if diarisation is unavailable, faces cannot
  be detected, or a dependency is missing, the feature falls back to the existing
  single-speaker auto-reframe, which in turn falls back to the blurred-background
  static reformat. The job is never failed; degradation is recorded in
  `effects_applied`.
- **BYOK / self-hosted / offline-friendly** — no mandatory external network calls; any
  external/model dependency is optional and dependency-injected for testability.
- **Single ffmpeg pass** — reframe replaces the geometry-stage crop and interoperates
  with captions, emoji, b-roll, music, progress bar, filler-word removal, etc.
- **Permissibility mode** is honoured (local-only, no external download).

## Glossary

- **Clipper**: The overall AI Video Clipper application (self-hosted, ffmpeg-based, CPU-first).
- **Pipeline**: The per-source flow in `worker/pipeline.py` (probe → transcribe → selection → per clip: cut → filler removal → **geometry** → compositor → thumbnail).
- **Word_Timeline**: The clip-relative word list (`start`/`end`/`text`/`probability`) from `worker.transcribe.Word`, rebased by filler removal via `rebase_words`.
- **Speaker_Diariser**: The new subsystem that segments a source into ordered Speaker_Turns using the Word_Timeline (and optionally an injected diarisation model).
- **Speaker_Turn**: An ordered record `{speaker_label, start, end}` (source-relative, then clip-relative after slicing) attributing a contiguous time window to one speaker.
- **Speaker_Label**: A stable identifier for a distinct speaker within a source (e.g. `S1`, `S2`).
- **Face_Detector**: The pluggable on-screen face detector (lazy-imported OpenCV Haar cascade today, MediaPipe-ready), extended to return **all** detected faces per sampled frame, not only the dominant one.
- **Face_Track**: A face box path persisted across sampled frames and assigned a stable track identifier.
- **Face_Speaker_Associator**: The component that maps each active Speaker_Turn to a Face_Track (the "active speaking face").
- **Speaker_Reframe**: The new geometry subsystem producing a speaker-aware crop/layout; extends `worker/effects/reframe.py`.
- **Reframe_Layout**: The user-selectable output layout: `follow_active` (dynamic single-face crop that follows the current speaker) or `split_screen` (multi-speaker stacked/side-by-side composite).
- **Single_Speaker_Reframe**: The existing v0.7.0 face-tracking auto-reframe (`apply_reframe`) that follows one dominant face.
- **Static_Reformat**: The existing blurred-background centre-crop reformat (`reformat_aspect(..., mode="crop_blur")`).
- **Compositor**: The single-pass effect renderer in `worker/effects/compositor.py`.
- **Sampler**: The bounded frame sampler used by face detection (sampling at a capped rate / capped count per clip).
- **Processing_Options**: The user options record (`worker/models.py` `ProcessingOptions`, mirrored by `OptionsModel`, upload Form fields, `App.jsx` defaults/`toOptions`, and `SettingsPanel.jsx`).
- **Info_Endpoint**: The `/api/info` endpoint advertising available option values to the UI.
- **Effects_Applied**: The free-form `ClipResult.effects_applied` string markers recording which enhancements ran and how they degraded.
- **Degraded_Mode**: Operation when an optional dependency (diarisation model, OpenCV/MediaPipe, ffmpeg feature) is unavailable; the feature falls back along the degradation chain and the Pipeline still produces clips.
- **Permissibility_Mode**: The existing `permissibility_mode` setting that forces local-only sourcing, disables added music, and blocks external downloads.
- **BYOK**: "Bring your own key" — the self-hosted model in which the operator supplies any external model/provider credentials.

## Requirements

---

## Group A — Speaker Diarisation

### Requirement 1: Speaker-turn segmentation

**User Story:** As a creator with multi-person footage, I want the clip segmented into speaker turns, so that the tool knows who is speaking when.

#### Acceptance Criteria

1. WHERE speaker diarisation is enabled, THE Speaker_Diariser SHALL segment the source into an ordered list of Speaker_Turns, each carrying a Speaker_Label, a `start`, and an `end`.
2. THE Speaker_Diariser SHALL derive Speaker_Turns from the Word_Timeline produced by transcription.
3. WHERE an injected diarisation model is available, THE Speaker_Diariser SHALL use the model's speaker assignments and align each assignment to Word_Timeline boundaries.
4. THE Speaker_Diariser SHALL order all produced Speaker_Turns by ascending `start` time.
5. FOR every produced Speaker_Turn, THE Speaker_Diariser SHALL ensure `start` is less than or equal to `end`.
6. THE Speaker_Diariser SHALL bound every Speaker_Turn within the source duration `[0, source_duration]`.
7. WHEN two adjacent Speaker_Turns share the same Speaker_Label AND are contiguous, THE Speaker_Diariser SHALL merge them into a single Speaker_Turn.

### Requirement 2: Speaker-turn coverage and non-overlap

**User Story:** As a developer consuming diarisation output, I want speaker turns to be a clean, non-overlapping timeline, so that downstream association is unambiguous.

#### Acceptance Criteria

1. FOR any two distinct Speaker_Turns produced for one source, THE Speaker_Diariser SHALL ensure the turns' `[start, end)` intervals do not overlap.
2. WHEN the Word_Timeline is empty, THE Speaker_Diariser SHALL produce zero Speaker_Turns and SHALL NOT fail the Pipeline.
3. WHEN diarisation cannot distinguish more than one speaker, THE Speaker_Diariser SHALL produce a single Speaker_Turn spanning the spoken range with one Speaker_Label.
4. THE Speaker_Diariser SHALL cap the number of distinct Speaker_Labels per source at a configurable maximum.
5. IF the number of detected speakers exceeds the configured maximum, THEN THE Speaker_Diariser SHALL merge the least-represented speakers into existing Speaker_Labels rather than exceed the maximum.

### Requirement 3: Speaker-turn model serialisation round-trip

**User Story:** As an operator, I want speaker-turn data to serialise and reload exactly, so that clip metadata and cached diarisation reproduce the same turns.

#### Acceptance Criteria

1. THE Clipper SHALL represent each Speaker_Turn as a serialisable record (`speaker_label`, `start`, `end`).
2. FOR every list of Speaker_Turns, serialising the list and then parsing the serialised form SHALL produce an equivalent list of Speaker_Turns (round-trip property).
3. IF a serialised Speaker_Turn record is malformed, THEN THE Clipper SHALL discard that record and SHALL retain the remaining valid records.

### Requirement 4: Diariser injection and offline operation

**User Story:** As a developer, I want the diariser dependency-injected, so that tests run offline and CPU-only and no external call is mandatory.

#### Acceptance Criteria

1. THE Speaker_Diariser SHALL accept a dependency-injected diarisation backend so tests can supply a mock backend.
2. WHEN no diarisation backend is injected or configured, THE Speaker_Diariser SHALL segment speaker turns using only the offline Word_Timeline signal.
3. THE Speaker_Diariser SHALL NOT require any external network access to produce Speaker_Turns.
4. IF an injected diarisation backend raises an error, THEN THE Speaker_Diariser SHALL fall back to the offline Word_Timeline segmentation and SHALL record the degradation in Effects_Applied.

---

## Group B — Face Detection and Face↔Speaker Association

### Requirement 5: Multi-face detection

**User Story:** As a creator, I want every on-screen face detected, so that the tool can show or follow more than one person.

#### Acceptance Criteria

1. WHERE speaker-aware reframe is enabled, THE Face_Detector SHALL return all detected face boxes for each sampled frame, not only the largest.
2. THE Face_Detector SHALL group per-frame face boxes into Face_Tracks, each with a stable track identifier across sampled frames.
3. THE Face_Detector SHALL lazily import the vision dependency (OpenCV, MediaPipe-ready) so that importing the module never fails when the dependency is absent.
4. IF the vision dependency is unavailable OR the video cannot be opened, THEN THE Face_Detector SHALL return zero Face_Tracks and SHALL NOT raise to the Pipeline.
5. WHEN no faces are detected in any sampled frame, THE Face_Detector SHALL return zero Face_Tracks.

### Requirement 6: Face↔speaker association

**User Story:** As a creator, I want the active speaking face identified per speaker turn, so that the reframe follows or arranges the right person.

#### Acceptance Criteria

1. WHERE speaker-aware reframe is enabled AND Speaker_Turns and Face_Tracks are both present, THE Face_Speaker_Associator SHALL assign at most one active Face_Track to each Speaker_Turn.
2. THE Face_Speaker_Associator SHALL associate a Speaker_Turn with the Face_Track most consistently present during that turn's time window.
3. WHEN a Speaker_Turn has no overlapping Face_Track, THE Face_Speaker_Associator SHALL leave that turn unassociated and SHALL mark it for degraded handling.
4. THE Face_Speaker_Associator SHALL keep the number of distinct associated Face_Tracks less than or equal to the number of distinct Speaker_Labels.
5. WHEN multiple Speaker_Turns share one Speaker_Label, THE Face_Speaker_Associator SHALL associate them with the same Face_Track where a consistent track exists.

---

## Group C — Speaker-Aware Reframe Layouts

### Requirement 7: Layout selection

**User Story:** As a creator, I want to choose between following the active speaker and a split-screen layout, so that the output matches my content type.

#### Acceptance Criteria

1. THE Processing_Options SHALL expose a Reframe_Layout value with the values `follow_active` and `split_screen`.
2. WHEN speaker-aware reframe is enabled AND Reframe_Layout is `follow_active`, THE Speaker_Reframe SHALL produce a single dynamic crop that follows the active speaking face across Speaker_Turns.
3. WHEN speaker-aware reframe is enabled AND Reframe_Layout is `split_screen`, THE Speaker_Reframe SHALL produce a composite layout that arranges multiple associated Face_Tracks within the target aspect.
4. THE Info_Endpoint SHALL advertise the available Reframe_Layout values to the UI.
5. IF a requested Reframe_Layout value is unknown, THEN THE Clipper SHALL apply the `follow_active` default and SHALL record the substitution in Effects_Applied.

### Requirement 8: Follow-active-speaker crop

**User Story:** As a creator, I want the crop to move to whoever is speaking, so that viewers always see the active speaker.

#### Acceptance Criteria

1. WHILE the current playback time falls within a Speaker_Turn associated with a Face_Track, THE Speaker_Reframe SHALL centre the crop window on that Face_Track's position.
2. THE Speaker_Reframe SHALL keep the crop window fully within the source frame bounds at every time.
3. THE Speaker_Reframe SHALL apply the moving crop within the existing single ffmpeg geometry pass, scaled to the target aspect resolution.
4. WHEN a Speaker_Turn is unassociated with any Face_Track, THE Speaker_Reframe SHALL hold the most recent valid crop centre for that turn's duration.
5. FOR every emitted crop command, THE Speaker_Reframe SHALL bound the command time within the clip duration `[0, end - start]`.

### Requirement 9: Split-screen / stacked layout

**User Story:** As a creator making interview clips, I want multiple speakers shown at once, so that reactions and dialogue stay visible.

#### Acceptance Criteria

1. WHERE Reframe_Layout is `split_screen`, THE Speaker_Reframe SHALL partition the target frame into one region per shown speaker without overlapping regions.
2. THE Speaker_Reframe SHALL crop each region to centre its associated Face_Track.
3. THE Speaker_Reframe SHALL cover the full target frame with the composed regions, leaving no uninitialised area.
4. WHERE the number of associated Face_Tracks exceeds the layout's region capacity, THE Speaker_Reframe SHALL show the speakers with the greatest speaking duration up to the region capacity.
5. IF fewer than two Face_Tracks are associated, THEN THE Speaker_Reframe SHALL fall back to the `follow_active` layout for that clip and SHALL record the substitution in Effects_Applied.
6. THE Speaker_Reframe SHALL compose the split-screen layout within the existing single ffmpeg geometry pass.

### Requirement 10: Smoothing and intensity controls

**User Story:** As a creator, I want to control how aggressively the camera moves, so that the motion suits calm or energetic edits.

#### Acceptance Criteria

1. THE Processing_Options SHALL expose a reframe smoothing/intensity value with the values `subtle`, `standard`, and `heavy`.
2. THE Speaker_Reframe SHALL map each intensity value to a deterministic smoothing factor and transition duration.
3. WHERE intensity is `subtle`, THE Speaker_Reframe SHALL apply stronger smoothing and slower crop movement than `standard`.
4. WHERE intensity is `heavy`, THE Speaker_Reframe SHALL apply weaker smoothing and faster crop movement than `standard`.
5. FOR every intensity value, THE Speaker_Reframe SHALL keep the smoothed crop path within the source frame bounds.
6. THE Info_Endpoint SHALL advertise the available reframe intensity values to the UI.

### Requirement 11: Smooth transitions on speaker change

**User Story:** As a creator, I want smooth transitions when the speaker changes, so that cuts between speakers do not feel jarring.

#### Acceptance Criteria

1. WHEN the active speaker changes between consecutive Speaker_Turns in `follow_active` layout, THE Speaker_Reframe SHALL transition the crop centre over a bounded transition duration rather than jumping instantaneously.
2. THE Speaker_Reframe SHALL derive the transition duration from the selected reframe intensity value.
3. FOR every transition, THE Speaker_Reframe SHALL keep the interpolated crop window within the source frame bounds throughout the transition.
4. THE Speaker_Reframe SHALL constrain each transition to end no later than the start of the next Speaker_Turn's stable window.
5. WHERE Reframe_Layout is `split_screen` AND the set of shown speakers changes, THE Speaker_Reframe SHALL transition region contents over the same bounded transition duration.

---

## Group D — Interaction, Precedence, and Composition

### Requirement 12: Precedence over the existing single-speaker reframe

**User Story:** As an operator, I want the new speaker-aware reframe and the existing single-speaker reframe to have a clear, predictable precedence, so that geometry behaviour is unambiguous.

#### Acceptance Criteria

1. WHEN speaker-aware reframe is enabled AND the target aspect is narrower than the source, THE Pipeline SHALL use the Speaker_Reframe geometry instead of the Single_Speaker_Reframe for that clip.
2. WHEN speaker-aware reframe is disabled AND the existing `reframe` option is enabled, THE Pipeline SHALL use the Single_Speaker_Reframe exactly as in v0.7.0.
3. IF speaker-aware reframe is enabled but produces no usable geometry, THEN THE Pipeline SHALL fall back to the Single_Speaker_Reframe.
4. WHEN both speaker-aware reframe and the existing `reframe` option are disabled, THE Pipeline SHALL use the Static_Reformat as in v0.7.0.
5. WHERE the target aspect is not narrower than the source, THE Speaker_Reframe SHALL take no geometry action and SHALL leave clip framing unchanged.

### Requirement 13: Interoperation with other effects in a single pass

**User Story:** As an operator, I want speaker-aware reframe to run at the geometry stage and interoperate with the compositor, so that captions, emoji, b-roll, music, and other effects still apply correctly.

#### Acceptance Criteria

1. THE Speaker_Reframe SHALL produce a geometry-prepared clip at the target aspect/resolution that the Compositor consumes unchanged.
2. THE Speaker_Reframe SHALL apply its crop/composite in a single ffmpeg pass at the geometry stage.
3. WHEN speaker-aware reframe runs, THE Compositor SHALL apply captions, emoji, b-roll, progress bar, and music to the reframed clip without additional geometry passes.
4. WHERE filler removal is enabled, THE Speaker_Reframe SHALL operate on the trimmed clip and SHALL use the rebased Word_Timeline for speaker-turn timing.
5. THE Speaker_Reframe SHALL keep all Speaker_Turn timing clip-relative and bounded within the final clip duration after filler removal.

---

## Group E — Graceful Degradation

### Requirement 14: Degradation chain and failure isolation

**User Story:** As an operator, I want speaker-aware reframe to never fail a job, so that clips are always produced even when diarisation or vision is unavailable.

#### Acceptance Criteria

1. IF diarisation produces zero Speaker_Turns, THEN THE Pipeline SHALL fall back to the Single_Speaker_Reframe and SHALL record the degradation in Effects_Applied.
2. IF face detection produces zero Face_Tracks, THEN THE Pipeline SHALL fall back to the Single_Speaker_Reframe and SHALL record the degradation in Effects_Applied.
3. IF the Single_Speaker_Reframe fallback also cannot produce geometry, THEN THE Pipeline SHALL apply the Static_Reformat and SHALL record the degradation in Effects_Applied.
4. IF the ffmpeg geometry command for speaker-aware reframe fails, THEN THE Pipeline SHALL fall back along the degradation chain rather than fail the clip.
5. WHEN speaker-aware reframe succeeds, THE Clipper SHALL record a marker identifying the applied Reframe_Layout in Effects_Applied.
6. THE Pipeline SHALL produce clips both when all dependencies operate normally and when any optional dependency is in Degraded_Mode.

---

## Group F — Performance and Sampling Discipline

### Requirement 15: Bounded, CPU-only, once-per-source cost

**User Story:** As an operator on CPU-only hardware, I want the vision and diarisation work bounded, so that render time stays viable.

#### Acceptance Criteria

1. THE Speaker_Diariser SHALL compute Speaker_Turns at most once per source video.
2. THE Sampler SHALL cap the number of frames sampled for face detection per clip at a configurable limit.
3. THE Face_Detector SHALL perform face detection on CPU without requiring a GPU.
4. WHEN speaker-aware reframe is disabled for a clip, THE Pipeline SHALL perform no diarisation and no face-detection sampling for that clip.
5. THE Speaker_Reframe SHALL add no ffmpeg passes beyond the existing single geometry pass per clip.
6. THE design document SHALL document the expected render-time cost of enabling speaker-aware reframe relative to the Single_Speaker_Reframe.

---

## Group G — Cross-Cutting: Toggles, Compatibility, Surface, Permissibility, Testability

### Requirement 16: Individual toggleability and defaults

**User Story:** As an operator, I want every new capability individually toggleable and off by default, so that behaviour stays predictable.

#### Acceptance Criteria

1. THE Processing_Options SHALL expose an independent toggle for speaker diarisation and an independent toggle for speaker-aware reframe.
2. THE Clipper SHALL default speaker diarisation and speaker-aware reframe to disabled.
3. THE Clipper SHALL default Reframe_Layout to `follow_active` and the reframe intensity to `standard`.
4. WHEN a new toggle is disabled, THE Pipeline SHALL produce output identical to the pre-feature behaviour for that capability.
5. WHERE speaker-aware reframe is enabled AND speaker diarisation is disabled, THE Pipeline SHALL enable the diarisation needed by reframe internally without altering the persisted diarisation toggle.

### Requirement 17: Backward compatibility and options round-trip

**User Story:** As an operator upgrading from v0.7.0, I want existing options and outputs to keep working, so that the upgrade is non-breaking.

#### Acceptance Criteria

1. THE Processing_Options SHALL retain all existing v0.7.0 fields and their current default values.
2. WHEN a request omits every new option, THE Pipeline SHALL produce output and Effects_Applied identical to v0.7.0.
3. THE Processing_Options record SHALL round-trip each new field through `from_dict` and `to_dict` without loss.
4. IF a new enum-like option value is unrecognised, THEN THE Processing_Options SHALL apply the documented default rather than raise.
5. THE Info_Endpoint SHALL continue to advertise all existing option values in addition to the new ones.

### Requirement 18: API and UI surface

**User Story:** As a creator, I want the new controls exposed in the API and UI, so that I can configure the feature.

#### Acceptance Criteria

1. THE Info_Endpoint SHALL advertise the available Reframe_Layout values and reframe intensity values.
2. THE `OptionsModel` and the `/api/upload` Form fields SHALL accept the speaker-diarisation toggle, the speaker-aware-reframe toggle, the Reframe_Layout value, and the reframe intensity value.
3. THE frontend defaults (`App.jsx`) SHALL include the new fields with the documented default values, and `toOptions` SHALL forward them.
4. THE `SettingsPanel.jsx` SHALL provide controls for the speaker-aware-reframe toggle, the Reframe_Layout selector, and the reframe intensity selector.
5. WHEN the API receives an unknown value for a new option, THE Clipper SHALL apply the documented default and SHALL still process the job.

### Requirement 19: Permissibility mode behaviour

**User Story:** As an operator with a permissibility preference, I want speaker-aware reframe to stay fully local, so that no external call is made.

#### Acceptance Criteria

1. WHERE Permissibility_Mode is enabled, THE Speaker_Diariser SHALL use only local/offline diarisation and SHALL NOT perform any external download or network call.
2. WHERE Permissibility_Mode is enabled, THE Speaker_Reframe SHALL operate using only locally available vision dependencies.
3. WHEN Permissibility_Mode is enabled AND an external diarisation backend would otherwise be used, THE Speaker_Diariser SHALL operate in Degraded_Mode using the offline Word_Timeline segmentation.
4. THE Clipper SHALL produce reframed clips under Permissibility_Mode without any external network access.

### Requirement 20: Testability with injected dependencies

**User Story:** As a developer, I want the feature testable offline with mocked diariser, face detector, and sampler, so that the suite stays fast, deterministic, and CPU-only.

#### Acceptance Criteria

1. THE Speaker_Diariser, Face_Detector, Face_Speaker_Associator, and Speaker_Reframe SHALL accept dependency-injected backends so tests can supply mocks.
2. THE Speaker_Diariser SHALL expose speaker-turn segmentation as a pure function testable without invoking ffmpeg, OpenCV, or a network.
3. THE Face_Speaker_Associator SHALL expose association as a pure function testable with synthetic Speaker_Turns and Face_Tracks.
4. THE Speaker_Reframe SHALL expose crop-path and layout-geometry computation as pure functions testable without invoking ffmpeg.
5. THE ffmpeg-produced geometry outputs SHALL be verifiable via ffprobe on tiny generated clips using the existing test helpers (`make_video`, `requires_ffmpeg`, `probe_size`, `probe_duration`, `FakeWord`).
6. FOR all valid Word_Timelines and Face_Track sets, speaker-turn segmentation and crop-path computation SHALL produce windows bounded within the clip duration and crop windows within the frame bounds (property-based test).
