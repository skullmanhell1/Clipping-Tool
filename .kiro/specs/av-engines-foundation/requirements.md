# Requirements Document

## Introduction

This spec defines the **Advanced AV Engines Foundation** — the shared contracts and
cross-cutting guarantees that every future "advanced AV engine" in the AI Video Clipper
(self-hosted, CPU-first, currently **v0.8.0**) is built on. It is an
**infrastructure/contracts spec**, not a user-facing feature: on its own it adds no new
visible output. Its deliverable is a small, named, testable set of abstractions that
sibling engine specs *inline* rather than re-invent.

Today each optional capability was bolted onto the Pipeline individually. Captions
(`worker/captions.py`), b-roll (`worker/effects/broll.py`), emoji
(`worker/effects/emoji.py`), music (`worker/effects/audio.py`), filler removal
(`worker/effects/filler.py`), reframe/diarisation (`worker/effects/reframe.py`,
`worker/diarization.py`) and visual selection (`worker/visual_selection.py`) each
re-implement — in slightly different ways — the same five concerns:

1. an availability check for an optional heavy dependency (`llm_available()`,
   `font_available()`, the lazy `cv2` import, ffmpeg/ffprobe presence);
2. a default-OFF toggle plus enum validation on `ProcessingOptions`
   (`worker/models.py` `from_dict`);
3. a fallback ladder that records a marker in `ClipResult.effects_applied`;
4. scratch files written into the per-job `temp_dir` handed down by
   `worker/pipeline.py` `run_pipeline`;
5. clip-relative timing derived from the Word_Timeline and rebased after filler removal.

Two engines are queued to land **on top of** this foundation and are explicitly **out of
scope** here:

- **Audio stem separation / stem inpainting** (music/voice/SFX stem isolation and repair).
- **Kinetic typography** (animated caption rendering beyond the current ASS presets).

This document therefore specifies only the shared layer: the **AV_Engine** abstraction and
**Engine_Registry**, **Capability_Probe** based degradation, deterministic
**Engine_Options** serialisation, shared **Time_Base**/segment primitives, the
**Engine_Workspace** artifact lifecycle over the existing storage backends, and failure
isolation. All of the Clipper's established design values are treated as hard constraints:

- **Individually toggleable and default OFF** — an all-off run reproduces v0.8.0 output
  and `effects_applied` exactly.
- **CPU-only by default**, no GPU required; heavy work is optional and bounded.
- **Graceful degradation is mandatory** — a missing model, binary, ffmpeg filter, or key
  degrades that engine only; the job still produces clips and records the degradation.
- **BYOK / self-hosted / offline-friendly** — no mandatory network access; every heavy
  dependency is injectable for testing.
- **Storage-backend neutral** — identical behaviour on the local filesystem and S3
  (`storage_backends/base.py`).
- **Determinism** — the same input plus the same options yields the same engine decisions.

### New vs. existing modules

Modules marked **(NEW)** do not exist yet and are introduced by this spec; all other paths
are existing code this foundation must integrate with.

- **(NEW)** `worker/engines/__init__.py` — engines package.
- **(NEW)** `worker/engines/base.py` — `AV_Engine` abstract base, `Engine_Context`, `Engine_Result`.
- **(NEW)** `worker/engines/registry.py` — registration, discovery, deterministic ordering.
- **(NEW)** `worker/engines/capabilities.py` — `Capability_Probe` / `Capability_Report`.
- **(NEW)** `worker/engines/timebase.py` — `Time_Base` and Timeline_Segment primitives.
- **(NEW)** `worker/engines/artifacts.py` — `Engine_Workspace` allocation and cleanup.
- Existing integration points: `worker/pipeline.py`, `worker/models.py`
  (`ProcessingOptions`, `ClipResult.effects_applied`), `worker/effects/compositor.py`,
  `worker/ffmpeg_utils.py` (`probe`, `MediaInfo`, `FFmpegError`), `config.py` `settings`,
  `runtime_config.py`, `storage_backends/base.py`, `storage_backends/retention.py`,
  `api/main.py` (`/api/info`), `frontend/src/components/SettingsPanel.jsx`.

## Glossary

- **Clipper**: The overall AI Video Clipper application (self-hosted, ffmpeg-based, CPU-first).
- **Pipeline**: The per-source flow in `worker/pipeline.py` (probe → transcribe → selection → per clip: cut → filler removal → geometry → compositor → thumbnail).
- **Compositor**: The single-pass effect renderer in `worker/effects/compositor.py` returning a `RenderResult`.
- **Word_Timeline**: The clip-relative word list (`start`/`end`/`text`/`probability`) from `worker.transcribe.Word`, rebased by filler removal via `rebase_words`.
- **Processing_Options**: The user options record (`worker/models.py` `ProcessingOptions`, mirrored by `OptionsModel`, the `/api/upload` Form fields, `App.jsx` `toOptions`, and `SettingsPanel.jsx`).
- **Effects_Applied**: The free-form marker list `ClipResult.effects_applied` recording which optional enhancements ran and how they degraded.
- **Info_Endpoint**: The `/api/info` endpoint in `api/main.py` advertising available option values to the UI.
- **Storage_Backend**: The active `BaseStorage` implementation (`storage_backends/base.py`, local or S3) addressed by POSIX-style keys normalised with `normalize_key`.
- **Retention_Manager**: The temp/clip cleanup layer in `storage_backends/retention.py` (`cleanup_temp`, `cleanup_expired`, `RetentionSweeper`).
- **Permissibility_Mode**: The existing `ProcessingOptions.permissibility_mode` setting that forbids added audio and external asset downloads.
- **Degraded_Mode**: Operation when an optional dependency is unavailable; the capability no-ops or falls back cleanly and the Pipeline still produces clips.
- **AV_Engine**: **(NEW)** The abstract base class in `worker/engines/base.py` that every advanced AV engine implements (audio stem separation, kinetic typography, and future engines).
- **Engine_Id**: A stable, lowercase, snake_case string uniquely identifying one AV_Engine (e.g. `stem_separation`, `kinetic_typography`).
- **Engine_Registry**: **(NEW)** The registry in `worker/engines/registry.py` that maps Engine_Ids to AV_Engine instances and yields them in a deterministic order.
- **Engine_Stage**: The Pipeline point at which an AV_Engine runs. Allowed values: `source` (once per source), `audio` (per clip, before geometry), `geometry`, `compose` (Compositor filter contribution), `post` (after the clip is written).
- **Engine_Context**: **(NEW)** The immutable per-invocation record handed to an AV_Engine: source/clip media paths, `Time_Base`, clip-relative `[start, end)` bounds, Word_Timeline, resolved Engine_Options, `Engine_Workspace`, `Capability_Report`, and injected dependencies.
- **Engine_Result**: **(NEW)** The immutable record an AV_Engine returns: `engine_id`, `Engine_Status`, produced Engine_Artifacts, Compositor contributions, and Effects_Applied markers.
- **Engine_Status**: One of `applied`, `skipped`, `degraded`, `failed`.
- **Capability_Id**: A stable string naming one optional dependency (e.g. `ffmpeg_filter:atempo`, `python_pkg:demucs`, `binary:ffprobe`, `font:Impact`, `llm`).
- **Capability_Probe**: **(NEW)** A pure-ish callable that reports whether one Capability_Id is usable in the current environment, without raising.
- **Capability_Report**: **(NEW)** The immutable, serialisable mapping of Capability_Id to availability plus a short detail string, produced once per process and injected into Engine_Contexts.
- **Engine_Options**: The per-engine, serialisable options record (a dataclass) resolved from Processing_Options, with documented defaults for unknown or malformed values.
- **Options_Digest**: A short, stable, content-derived string identifying a resolved Engine_Options value, used for caching and reproducibility assertions.
- **Feature_Flag**: The per-engine boolean toggle on Processing_Options (`<engine>_enabled` style), defaulting to disabled.
- **Time_Base**: **(NEW)** The shared timing record (`fps`, `sample_rate`, rounding rule) derived from `worker.ffmpeg_utils.probe` / `MediaInfo`, used by every engine to convert seconds to frames or samples.
- **Timeline_Segment**: **(NEW)** A half-open clip-relative interval record `{start, end}` with `start <= end`, expressed in seconds.
- **Segment_List**: An ordered list of Timeline_Segments that is sorted, non-overlapping, and clamped to the clip bounds after normalisation.
- **Engine_Workspace**: **(NEW)** The per-job, per-clip, per-engine scratch directory allocated beneath the Pipeline's `temp_dir` in which an engine writes intermediate artifacts.
- **Engine_Artifact**: A file produced by an AV_Engine, either transient (inside the Engine_Workspace) or durable (persisted through the Storage_Backend under a normalised key).
- **Engine_Host**: The Pipeline-side coordinator that resolves options, probes capabilities, allocates workspaces, invokes engines by Engine_Stage, isolates failures, and merges Engine_Results.

## Requirements

---

## Group A — Engine Contract, Registry, and Invocation

### Requirement 1: Common AV engine abstraction

**User Story:** As a developer adding a new AV engine, I want one base abstraction to implement, so that my engine plugs into the Pipeline without bespoke wiring.

#### Acceptance Criteria

1. THE AV_Engine SHALL define an abstract interface exposing an Engine_Id, an Engine_Stage, a declared list of required Capability_Ids, a declared list of optional Capability_Ids, an options-resolution operation, and a run operation.
2. THE AV_Engine SHALL accept exactly one Engine_Context argument on its run operation and SHALL return exactly one Engine_Result.
3. THE AV_Engine SHALL treat the Engine_Context as read-only, and THE Engine_Host SHALL observe that the Processing_Options instance it passed is unchanged after every engine invocation.
4. THE AV_Engine module SHALL import successfully in an environment where every optional heavy dependency is absent.
5. WHERE an AV_Engine contributes to the Compositor, THE Engine_Result SHALL carry that contribution as filter-graph fragments and input files rather than by invoking ffmpeg directly, so the existing single-pass composition in `worker/effects/compositor.py` is preserved.
6. THE Engine_Result SHALL be a serialisable record whose Engine_Status is one of `applied`, `skipped`, `degraded`, or `failed`.

### Requirement 2: Engine registry and discovery

**User Story:** As a developer, I want engines discoverable through a registry, so that adding an engine does not require editing the Pipeline.

#### Acceptance Criteria

1. THE Engine_Registry SHALL provide a registration operation that associates one Engine_Id with one AV_Engine instance.
2. THE Engine_Registry SHALL provide a lookup operation that returns the AV_Engine registered for a given Engine_Id.
3. IF a registration uses an Engine_Id that is already registered, THEN THE Engine_Registry SHALL raise a registration error naming the conflicting Engine_Id.
4. WHEN asked for the engines of a given Engine_Stage, THE Engine_Registry SHALL return only the engines declaring that Engine_Stage.
5. FOR any set of registrations, THE Engine_Registry SHALL return engines ordered by a declared integer priority and then by Engine_Id, so the returned order is identical regardless of registration order (determinism property).
6. WHEN no engine is registered, THE Engine_Registry SHALL return an empty engine list for every Engine_Stage.
7. THE Engine_Registry SHALL provide a reset operation that clears all registrations so tests start from a known empty state.

### Requirement 3: Engine invocation and result merging

**User Story:** As an operator, I want engine outcomes recorded consistently on each clip, so that I can tell what ran and what degraded.

#### Acceptance Criteria

1. WHEN the Pipeline processes a clip, THE Engine_Host SHALL invoke each enabled AV_Engine for the current Engine_Stage in the Engine_Registry order.
2. THE Engine_Host SHALL merge the Effects_Applied markers of every Engine_Result into `ClipResult.effects_applied`, preserving the Engine_Registry invocation order.
3. THE Engine_Host SHALL namespace every engine-produced marker as `engine:<engine_id>:<detail>` so markers are attributable to one engine.
4. WHEN an Engine_Result has Engine_Status `skipped`, THE Engine_Host SHALL add no marker for that engine.
5. WHERE an AV_Engine declares Engine_Stage `source`, THE Engine_Host SHALL invoke that engine at most once per source per `run_pipeline` call regardless of the number of clips produced.
6. FOR any set of Engine_Results, THE Engine_Host SHALL produce a merged marker list containing each engine's markers at most once (no duplication under merge).

### Requirement 4: Enabled-engine resolution

**User Story:** As an operator, I want only the engines I enabled to run, so that cost and behaviour stay predictable.

#### Acceptance Criteria

1. THE Engine_Host SHALL treat an AV_Engine as enabled when the engine's Feature_Flag on the resolved Processing_Options is true.
2. WHILE an AV_Engine is disabled, THE Engine_Host SHALL skip that engine without allocating an Engine_Workspace and without probing that engine's exclusive Capability_Ids.
3. WHEN every AV_Engine is disabled, THE Pipeline SHALL produce clips and Effects_Applied identical to the v0.8.0 result for the same input and options.
4. THE Engine_Host SHALL resolve enablement using the existing `effective_options` normalisation in `worker/models.py` so Permissibility_Mode downgrades are applied before any engine runs.

---

## Group B — Capability Probing, Degradation, and Failure Isolation

### Requirement 5: Capability probing for optional dependencies

**User Story:** As an operator on a minimal install, I want the tool to detect which heavy dependencies are present, so that features that cannot run are reported instead of crashing.

#### Acceptance Criteria

1. THE Capability_Probe layer SHALL support probing at least these Capability_Id kinds: an importable Python package, an executable binary on the configured path, a named ffmpeg filter, an installed font, and a configured provider key.
2. WHEN a Capability_Id is probed, THE Capability_Probe SHALL return an availability boolean together with a short human-readable detail string.
3. IF probing a Capability_Id raises an underlying error, THEN THE Capability_Probe SHALL report that Capability_Id as unavailable with the error summary as the detail string.
4. THE Capability_Probe SHALL probe an ffmpeg filter using the `settings.ffmpeg_binary` configured in `config.py` rather than a hard-coded binary name.
5. THE Capability_Probe layer SHALL reuse the existing availability helpers `worker.llm_client.llm_available` and `worker.captions.font_available` for the `llm` and font Capability_Ids.
6. THE Capability_Probe SHALL complete every probe without any external network access.
7. THE Capability_Probe SHALL accept an injected prober so tests can declare any Capability_Id available or unavailable without installing dependencies.

### Requirement 6: Capability report caching and stability

**User Story:** As an operator, I want dependency detection to cost almost nothing per clip, so that batches stay fast.

#### Acceptance Criteria

1. THE Capability_Report SHALL cache each probed Capability_Id result for the lifetime of the worker process.
2. WHEN the same Capability_Id is requested repeatedly within one process, THE Capability_Report SHALL invoke the underlying Capability_Probe at most once (idempotence property).
3. FOR any set of Capability_Ids, requesting the report twice SHALL yield equal availability values (determinism property).
4. THE Capability_Report SHALL expose a serialisable mapping of Capability_Id to availability and detail suitable for the Info_Endpoint.
5. THE Capability_Report SHALL provide an invalidation operation that clears the cache so tests can re-probe with different injected probers.

### Requirement 7: Graceful degradation when a capability is unavailable

**User Story:** As a creator, I want clips to keep rendering when an advanced engine cannot run, so that a missing model never blocks my output.

#### Acceptance Criteria

1. WHEN an enabled AV_Engine declares a required Capability_Id that the Capability_Report reports unavailable, THE Engine_Host SHALL skip that engine's work, SHALL return an Engine_Result with Engine_Status `degraded`, and SHALL record the marker `engine:<engine_id>:unavailable:<capability_id>`.
2. WHERE an AV_Engine declares an optional Capability_Id that is unavailable, THE AV_Engine SHALL produce its reduced-fidelity output and SHALL record the marker `engine:<engine_id>:degraded:<capability_id>`.
3. WHEN an enabled AV_Engine degrades for any reason, THE Pipeline SHALL still write the clip file and SHALL still return a ClipResult for that clip.
4. THE Engine_Host SHALL record exactly one degradation marker per degraded engine per clip.
5. FOR every combination of available and unavailable Capability_Ids, THE Pipeline SHALL produce the same number of clips as an all-engines-disabled run of the same input (invariant under degradation).

### Requirement 8: Failure isolation between engines

**User Story:** As an operator, I want one broken engine to spoil only its own contribution, so that a whole batch is never lost.

#### Acceptance Criteria

1. IF an AV_Engine raises any exception during its run operation, THEN THE Engine_Host SHALL catch that exception, SHALL return an Engine_Result with Engine_Status `failed`, and SHALL record the marker `engine:<engine_id>:failed`.
2. WHEN one AV_Engine returns Engine_Status `failed`, THE Engine_Host SHALL still invoke the remaining engines for that Engine_Stage.
3. WHEN an AV_Engine returns Engine_Status `failed`, THE Pipeline SHALL use the clip media produced by the preceding stage so the clip is still written.
4. IF an AV_Engine raises `worker.ffmpeg_utils.FFmpegError`, THEN THE Engine_Host SHALL treat the outcome as Engine_Status `failed` for that engine and SHALL continue the clip.
5. THE Engine_Host SHALL log the caught exception type and message for every failed engine.
6. IF an AV_Engine exceeds its declared per-clip time budget, THEN THE Engine_Host SHALL abandon that engine's contribution, SHALL record the marker `engine:<engine_id>:timeout`, and SHALL continue the clip.
7. FOR any subset of engines forced to raise, THE Pipeline SHALL produce the same clip count as the all-engines-disabled run of the same input (failure-isolation invariant).

---

## Group C — Options, Feature Flags, and Determinism

### Requirement 9: Per-engine feature flags default OFF

**User Story:** As an operator upgrading from v0.8.0, I want every new engine off until I enable it, so that upgrading changes nothing by default.

#### Acceptance Criteria

1. THE Processing_Options SHALL expose one independent Feature_Flag per registered AV_Engine.
2. THE Clipper SHALL default every engine Feature_Flag to disabled.
3. THE Processing_Options SHALL retain all existing v0.8.0 fields and their current default values.
4. WHEN a request omits every engine option, THE Pipeline SHALL produce output and Effects_Applied identical to v0.8.0 for the same input.
5. WHERE Permissibility_Mode is enabled, THE Engine_Host SHALL apply the documented safe value for every engine option that would otherwise permit added audio or an external download.
6. THE Engine_Host SHALL leave the caller's persisted Feature_Flag values unchanged when an engine internally requires another engine's output.

### Requirement 10: Engine options serialisation round-trip

**User Story:** As an operator, I want engine settings to serialise and reload exactly, so that saved jobs and cached results reproduce the same run.

#### Acceptance Criteria

1. THE Engine_Options record SHALL be a dataclass whose fields are limited to JSON-serialisable scalar, list, and mapping values.
2. THE Engine_Options SHALL provide a parse operation from a possibly partial mapping and a serialise operation to a mapping.
3. FOR every valid Engine_Options value, serialising then parsing then serialising again SHALL produce an identical mapping (round-trip property).
4. FOR every mapping of arbitrary values, THE Engine_Options parse operation SHALL return an Engine_Options value without raising, applying the documented default for each unrecognised or malformed field.
5. WHEN a mapping contains keys that are not Engine_Options fields, THE Engine_Options parse operation SHALL ignore those keys.
6. FOR every valid Engine_Options value, resolving that value from Processing_Options twice SHALL produce equal Engine_Options (idempotence property).
7. THE Engine_Options resolution SHALL follow the existing `ProcessingOptions.from_dict` convention of validating enum-like string fields against a declared known-value set and substituting the documented default.

### Requirement 11: Options digest determinism

**User Story:** As a developer, I want a stable identifier for a resolved engine configuration, so that caching and reproducibility can be asserted.

#### Acceptance Criteria

1. THE Engine_Host SHALL compute an Options_Digest from the serialised Engine_Options of each engine invocation.
2. FOR any two equal Engine_Options values, THE Engine_Host SHALL compute equal Options_Digests (determinism property).
3. FOR any two Engine_Options mappings differing only in key insertion order, THE Engine_Host SHALL compute equal Options_Digests (order insensitivity property).
4. FOR any two Engine_Options values differing in at least one field value, THE Engine_Host SHALL compute different Options_Digests.
5. THE Options_Digest SHALL be a lowercase hexadecimal string of fixed length that is stable across worker processes and across Python invocations.
6. THE Engine_Host SHALL include the Options_Digest in the Engine_Workspace directory name so concurrent runs with different options use distinct directories.

### Requirement 12: Reproducibility of engine decisions

**User Story:** As a creator, I want the same source and settings to produce the same result, so that re-running a job is predictable.

#### Acceptance Criteria

1. FOR the same source media, the same clip bounds, the same Word_Timeline, and the same Engine_Options, THE AV_Engine SHALL produce equal planning output (segment lists, cue lists, parameter values) across repeated invocations (determinism property).
2. THE AV_Engine SHALL derive every random choice from a seed contained in the Engine_Context.
3. WHEN the Engine_Context seed is unchanged, THE AV_Engine SHALL produce equal planning output for equal inputs.
4. THE Engine_Host SHALL derive the Engine_Context seed from the source identity and the Options_Digest so the seed is reproducible without being stored.
5. THE AV_Engine SHALL expose its planning step as a pure function that is callable without ffmpeg, without a network, and without a model download.
6. WHERE an AV_Engine iterates over a mapping to build output, THE AV_Engine SHALL iterate in sorted key order so output does not depend on mapping insertion order.

---

## Group D — Time-Base and Timeline Primitives

### Requirement 13: Shared time-base primitive

**User Story:** As a developer of two engines that must line up, I want one shared time-base, so that our outputs do not drift against each other.

#### Acceptance Criteria

1. THE Time_Base SHALL record the frame rate, the audio sample rate, and the rounding rule used to convert seconds to frames and to samples.
2. THE Engine_Host SHALL construct the Time_Base for a clip from the probed `MediaInfo` returned by `worker.ffmpeg_utils.probe`.
3. IF the probed frame rate is missing, zero, or negative, THEN THE Time_Base SHALL use the documented fallback frame rate and SHALL record the substitution in the Engine_Context.
4. THE Time_Base SHALL provide seconds-to-frame, frame-to-seconds, seconds-to-sample, and sample-to-seconds conversions.
5. FOR every frame index within the clip, converting that index to seconds and back to a frame index SHALL return the original index (round-trip property).
6. FOR every timestamp in seconds within the clip, THE Time_Base SHALL convert that timestamp to a frame index whose reconstructed timestamp differs from the input by less than one frame duration.
7. THE Engine_Host SHALL pass one identical Time_Base value to every AV_Engine invoked for the same clip.

### Requirement 14: Timeline segment normalisation invariants

**User Story:** As a developer, I want one segment normaliser, so that every engine agrees on what a valid timeline looks like.

#### Acceptance Criteria

1. THE Timeline_Segment SHALL be a serialisable record with `start` and `end` in clip-relative seconds satisfying `start <= end`.
2. THE Time_Base module SHALL provide a normalisation operation that clamps a Segment_List to the clip bounds `[0, duration]`, sorts it by start time, drops zero-length segments, and merges overlapping or touching segments.
3. FOR every input Segment_List, THE normalisation operation SHALL return a Segment_List that is sorted by start time and free of overlaps (invariant property).
4. FOR every input Segment_List, normalising twice SHALL equal normalising once (idempotence property).
5. FOR every input Segment_List, THE normalisation operation SHALL return segments whose total duration is less than or equal to the clip duration (invariant property).
6. FOR every Segment_List, serialising then parsing SHALL produce an equivalent Segment_List (round-trip property).
7. IF a serialised Timeline_Segment record is malformed or has `end` before `start`, THEN THE normalisation operation SHALL discard that record and SHALL retain the remaining valid records.

### Requirement 15: Composition without timeline drift

**User Story:** As a creator using several engines at once, I want captions, audio, and overlays to stay in sync, so that clips do not look mistimed.

#### Acceptance Criteria

1. THE Engine_Context SHALL carry clip-relative bounds `[0, end - start]` and the clip-relative Word_Timeline for the clip currently being processed.
2. WHEN filler removal has rebased the Word_Timeline, THE Engine_Host SHALL pass the rebased Word_Timeline to every subsequent AV_Engine.
3. THE Time_Base module SHALL provide a snap operation that aligns a timestamp to the nearest frame boundary of the Time_Base.
4. FOR every timestamp, applying the snap operation twice SHALL equal applying it once (idempotence property).
5. FOR every Segment_List produced by any two AV_Engines for the same clip, THE Engine_Host SHALL observe segment bounds within `[0, end - start]` (invariant under composition).
6. FOR any two AV_Engines whose contributions do not overlap in time, THE Engine_Host SHALL produce identical merged Effects_Applied markers and identical produced-artifact sets regardless of the order in which those two engines run (confluence property).
7. THE Engine_Host SHALL express every engine timestamp in seconds as a float so no engine converts through a private frame convention.

---

## Group E — Artifact Lifecycle and Storage Integration

### Requirement 16: Engine workspace allocation

**User Story:** As an operator, I want engine scratch files kept apart, so that two engines or two concurrent jobs never collide.

#### Acceptance Criteria

1. WHEN an enabled AV_Engine is invoked, THE Engine_Host SHALL allocate an Engine_Workspace directory beneath the `temp_dir` passed to `run_pipeline` in `worker/pipeline.py`.
2. THE Engine_Workspace path SHALL include the job identifier, the clip identifier, the Engine_Id, and the Options_Digest so distinct invocations use distinct directories.
3. THE Engine_Host SHALL create the Engine_Workspace directory before invoking the engine, including any missing parent directories.
4. THE AV_Engine SHALL write every intermediate artifact inside the Engine_Workspace it was given.
5. FOR every generated job, clip, engine, and options combination, THE Engine_Host SHALL produce an Engine_Workspace path that resolves inside the Pipeline `temp_dir` (containment invariant).
6. THE Engine_Host SHALL sanitise every path component of the Engine_Workspace so a hostile Engine_Id or clip identifier resolves inside the Pipeline `temp_dir`.
7. WHEN two AV_Engines run for the same clip, THE Engine_Host SHALL allocate two different Engine_Workspace directories.

### Requirement 17: Artifact cleanup and retention integration

**User Story:** As an operator with limited disk, I want engine intermediates cleaned up like every other temp file, so that disk usage stays bounded.

#### Acceptance Criteria

1. WHEN a clip finishes processing, THE Engine_Host SHALL delete the Engine_Workspace directories allocated for that clip.
2. WHERE the `auto_delete_temp` runtime setting in `runtime_config.py` is enabled, THE Clipper SHALL remove all Engine_Workspace content for a job through the existing `storage_backends.retention.cleanup_temp` path.
3. WHILE the `auto_delete_temp` runtime setting is disabled, THE Clipper SHALL retain Engine_Workspace content until the Retention_Manager sweep removes it.
4. IF deleting an Engine_Workspace raises an operating-system error, THEN THE Engine_Host SHALL log the error and SHALL continue processing the remaining clips.
5. WHEN an AV_Engine returns Engine_Status `failed` or `degraded`, THE Engine_Host SHALL delete that engine's Engine_Workspace on the same schedule as a successful engine.
6. FOR every completed job with `auto_delete_temp` enabled, THE Clipper SHALL leave no Engine_Workspace directory for that job beneath `settings.temp_dir` (cleanup invariant).
7. WHERE an AV_Engine declares an artifact durable, THE Engine_Host SHALL persist that artifact before the Engine_Workspace is deleted.

### Requirement 18: Storage-backend neutrality for durable artifacts

**User Story:** As an operator running on S3, I want engine artifacts stored the same way as clips, so that behaviour does not depend on the backend.

#### Acceptance Criteria

1. THE Engine_Host SHALL persist every durable Engine_Artifact through the active Storage_Backend interface defined in `storage_backends/base.py`.
2. THE Engine_Host SHALL address durable Engine_Artifacts by a POSIX-style key normalised with `storage_backends.base.normalize_key`.
3. THE Engine_Host SHALL produce the same durable-artifact keys when the active Storage_Backend is `local` and when it is `s3`.
4. FOR every Engine_Id, clip identifier, and artifact name, THE Engine_Host SHALL produce a storage key that contains no `.` or `..` segment and no leading slash (key-safety invariant).
5. WHEN a durable Engine_Artifact is persisted, THE Engine_Host SHALL record its storage key in the Engine_Result.
6. IF persisting a durable Engine_Artifact raises an error, THEN THE Engine_Host SHALL record the marker `engine:<engine_id>:artifact_failed` and SHALL continue producing the clip.

---

## Group F — Cross-Cutting: Cost, Surface, Permissibility, Testability, Compatibility

### Requirement 19: Bounded CPU-first cost

**User Story:** As an operator on a modest CPU box, I want engine cost bounded and declared, so that enabling an engine cannot make a job run indefinitely.

#### Acceptance Criteria

1. THE AV_Engine SHALL declare a per-clip time budget and a maximum number of media passes it performs.
2. THE Engine_Host SHALL invoke AV_Engines using CPU-only execution by default, requiring no GPU.
3. WHERE an AV_Engine declares Engine_Stage `source`, THE Engine_Host SHALL reuse that engine's single source-level result for every clip derived from that source.
4. THE Engine_Host SHALL invoke `worker.ffmpeg_utils.probe` at most once per media file per clip and SHALL share the resulting Time_Base with every engine for that clip.
5. WHEN every AV_Engine is disabled, THE Engine_Host SHALL perform no capability probe, no workspace allocation, and no additional media pass.

### Requirement 20: API and UI surface for engines

**User Story:** As a creator, I want to see which advanced engines my install can actually run, so that I do not enable something that silently degrades.

#### Acceptance Criteria

1. THE Info_Endpoint SHALL advertise each registered Engine_Id together with that engine's enabled-by-default value and current availability.
2. THE Info_Endpoint SHALL advertise the serialisable Capability_Report mapping of Capability_Id to availability.
3. THE `OptionsModel` and the `/api/upload` Form fields in `api/main.py` SHALL accept each engine Feature_Flag and each engine's option values.
4. THE frontend defaults in `frontend/src/App.jsx` SHALL include each engine Feature_Flag at its documented default, and `toOptions` SHALL forward each engine option.
5. WHEN the API receives an unrecognised value for an engine option, THE Clipper SHALL apply the documented default and SHALL still process the job.
6. THE Info_Endpoint SHALL continue to advertise all existing v0.8.0 option values in addition to the engine values.

### Requirement 21: Permissibility and offline operation

**User Story:** As an operator with a permissibility preference, I want engines to stay local and offline, so that no external call or download happens without my consent.

#### Acceptance Criteria

1. THE AV_Engine SHALL declare whether it requires an external network call or a model download.
2. WHERE Permissibility_Mode is enabled, THE Engine_Host SHALL run only AV_Engines that declare no external network requirement.
3. WHERE Permissibility_Mode is enabled AND an enabled AV_Engine declares an external network requirement, THE Engine_Host SHALL return Engine_Status `degraded` for that engine and SHALL record the marker `engine:<engine_id>:permissibility_blocked`.
4. WHEN every enabled AV_Engine is local, THE Pipeline SHALL produce clips with no external network access.
5. THE Capability_Probe layer SHALL report a model-dependent Capability_Id as unavailable when the model is absent locally and downloading is disabled.

### Requirement 22: Testability with injected dependencies

**User Story:** As a developer, I want the foundation testable offline with fakes, so that the suite stays fast, deterministic, and CPU-only.

#### Acceptance Criteria

1. THE AV_Engine, Engine_Registry, Capability_Probe layer, and Engine_Host SHALL accept dependency-injected collaborators so tests can supply fakes.
2. THE Engine_Registry SHALL support constructing an isolated registry instance so tests avoid mutating shared global state.
3. THE Time_Base, Timeline_Segment normalisation, Engine_Options resolution, and Options_Digest operations SHALL be pure functions callable without ffmpeg, OpenCV, or a network.
4. THE Engine_Host SHALL support a fake AV_Engine whose Engine_Status and raised exception are controllable so failure isolation is testable without a real engine.
5. THE ffmpeg-dependent behaviour SHALL be verifiable on tiny generated clips using the existing test helpers in `tests/conftest.py` (`make_video`, `requires_ffmpeg`, `probe_size`, `probe_duration`, `FakeWord`).
6. THE test doubles for engines, capability probers, and workspaces SHALL live in the existing `tests/fakes.py` module so sibling engine specs reuse them.
7. FOR all valid Engine_Options, Word_Timelines, and Segment_Lists, THE foundation SHALL satisfy the declared round-trip, idempotence, determinism, and bounds invariants under property-based tests.

### Requirement 23: Backward compatibility and non-invasive integration

**User Story:** As an operator upgrading from v0.8.0, I want the foundation to change nothing observable until an engine is enabled, so that the upgrade is risk-free.

#### Acceptance Criteria

1. WHEN no AV_Engine is registered, THE Pipeline SHALL produce clips, Effects_Applied, and metadata identical to v0.8.0 for the same input and options.
2. THE Engine_Host SHALL preserve the existing Pipeline stage order (cut → filler removal → geometry → compositor → thumbnail) and SHALL add engine invocation points within that order.
3. THE Engine_Host SHALL preserve the existing single-pass Compositor behaviour, so an all-off run still performs the same number of ffmpeg passes per clip as v0.8.0.
4. THE Processing_Options SHALL round-trip each engine field through `from_dict` and `dataclasses.asdict` without loss, consistent with the existing checks in `tests/test_options_roundtrip.py`.
5. THE existing `ClipResult.effects_applied` marker values documented in `worker/models.py` SHALL retain their current meanings and spellings.
6. THE foundation SHALL keep the `AV_Engine`, `Engine_Context`, `Engine_Result`, `Capability_Report`, `Time_Base`, and Engine_Workspace contracts stable so the audio stem separation engine and the kinetic typography engine can depend on them without modification.
