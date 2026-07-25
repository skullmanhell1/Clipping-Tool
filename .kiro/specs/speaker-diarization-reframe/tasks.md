# Implementation Plan — Speaker Diarisation & Multi-Speaker Reframe

These are incremental, test-first coding steps. Execute them **one task at a time**,
in order — each task builds on the previous ones so there is never orphaned code.
The plan intentionally lands the shared data-model / options / config changes first
(they unblock the API + UI), then builds each subsystem's **pure planning / generation
functions with their unit / property tests before** wiring them into the ffmpeg
single-pass geometry stage and the pipeline. Every new capability defaults OFF, so at
any point an "all-off" run reproduces v0.7.0 output and `effects_applied` exactly.

Tasks marked with `*` are optional test sub-tasks (unit / property / integration
tests). Property tests use `hypothesis` with `@settings(max_examples=100)` and are
tagged `# Feature: speaker-diarization-reframe, Property N: <text>`, one property per
test, in the exact files named in the design's Testing Strategy
(`tests/test_diarization.py`, `tests/test_speaker_reframe.py`,
`tests/test_reframe_geometry.py`, `tests/test_pipeline_degradation.py`,
`tests/test_options_roundtrip.py`). ffmpeg integration tests reuse the existing helpers
(`make_video`, `requires_ffmpeg`, `probe_size`, `probe_duration`, `FakeWord`) and mock
the diariser / face detector / sampler (`FakeDiarizationBackend`, `FakeFaceDetector`,
spies on `_run` and `diarize_source`).

## Tasks

- [x] 1. Data-model, options, and config foundations
  - [x] 1.1 Extend `ProcessingOptions` in `worker/models.py`
    - Append the new fields with safe defaults: `diarization=False`, `speaker_reframe=False`, `reframe_layout="follow_active"`, `reframe_intensity="standard"`; keep all existing v0.7.0 fields/defaults unchanged.
    - Add the `_REFRAME_LAYOUTS = ("follow_active", "split_screen")` and `_REFRAME_INTENSITIES = ("subtle", "standard", "heavy")` known-value sets.
    - Extend `from_dict` bool coercion for `diarization`/`speaker_reframe` and validate `reframe_layout`/`reframe_intensity` against their known sets, falling back to the documented default (`follow_active` / `standard`) on unknown or malformed values without raising; leave the existing unknown-key filter intact.
    - _Requirements: 7.1, 7.5, 10.1, 16.1, 16.2, 16.3, 17.1, 17.3, 17.4_

  - [x] 1.2 Extend `ClipResult` effects markers in `worker/models.py`
    - Document/define the new `effects_applied` string markers used by later tasks (`diarization:transcript`, `diarization:model`, `diarization_degraded`, `speaker_reframe:follow_active`, `speaker_reframe:split_screen`, `speaker_reframe_substituted`, `faces_none`, `speaker_reframe_degraded`), preserving the existing `reframe` / static markers.
    - _Requirements: 4.2, 4.4, 7.5, 9.5, 14.1, 14.2, 14.3, 14.5_

  - [x] 1.3 Add diarisation and reframe-sampling settings in `config.py`
    - Add `diarization_max_speakers=2`, `diarization_pause_gap=0.9`, `reframe_sample_fps=5.0`, `reframe_sample_cap=120`, and `split_screen_max_regions=2`.
    - _Requirements: 2.4, 2.5, 10.2, 15.1, 15.2, 9.1, 9.4_

  - [x]* 1.4 Property test: new option fields round-trip and unknown values apply defaults → `tests/test_options_roundtrip.py`
    - **Property 25: New option fields round-trip and unknown values apply defaults** — for any options dict, `from_dict(to_dict(...))` preserves `diarization`, `speaker_reframe`, `reframe_layout`, `reframe_intensity` without loss, and any malformed/unrecognised `reframe_layout`/`reframe_intensity` value applies the documented default without raising.
    - _Requirements: 17.3, 17.4, 18.5_ · _Properties: P25_

  - [x]* 1.5 Unit tests: defaults OFF and existing fields untouched → `tests/test_options_roundtrip.py`
    - Assert `diarization` and `speaker_reframe` default disabled, `reframe_layout` defaults `follow_active`, `reframe_intensity` defaults `standard`, and every pre-existing v0.7.0 field keeps its current default; assert the two toggles are independent.
    - _Requirements: 16.1, 16.2, 16.3, 17.1_

- [x] 2. Speaker diarisation module (`worker/diarization.py`)
  - [x] 2.1 Implement the serialisable `Speaker_Turn` model and round-trip helpers
    - Add the frozen `Speaker_Turn` dataclass (`speaker_label`, `start`, `end`) with `to_dict`/`from_dict`, plus `turns_to_dicts(turns)` and `turns_from_dicts(data)` where malformed records are skipped and valid ones retained without raising.
    - _Requirements: 1.1, 3.1, 3.2, 3.3_

  - [x] 2.2 Implement the pure `segment_by_words` offline segmentation
    - Group Word_Timeline words into speech runs split on silence gaps > `pause_gap`; assign greedy/turn-taking speaker labels capped at `max_speakers` (default `settings.diarization_max_speakers`); bound every turn within `[0, duration]`, ensure `start <= end`, order by ascending `start`, merge adjacent same-label contiguous turns, guarantee non-overlapping `[start, end)`, and merge the least-represented speakers when a naive pass would exceed the cap; empty words → `[]`, single distinguishable speaker → one turn.
    - _Requirements: 1.2, 1.4, 1.5, 1.6, 1.7, 2.1, 2.2, 2.3, 2.4, 2.5, 4.3_

  - [x] 2.3 Implement `diarize_source` with the injectable `DiarizationBackend`
    - Add the `DiarizationBackend` protocol and `diarize_source(words, duration, *, backend=None, max_speakers=None, permissibility=False, notes=None)`: `permissibility=True` or no backend → pure `segment_by_words` recording `diarization:transcript`; backend present → `backend.assign(...)` aligned to Word_Timeline boundaries then normalised by the same ordering/bounding/merge/cap rules, recording `diarization:model`; backend raising → offline fallback appending `diarization_degraded`; never performs network access itself.
    - _Requirements: 1.3, 4.1, 4.2, 4.4, 15.1, 19.1, 19.3_

  - [x] 2.4 Implement the pure `slice_turns` and `rebase_turns`
    - Add `slice_turns(turns, start, end)` clipping source turns to `[start, end]` and rebasing to clip-relative 0-based coordinates bounded within `[0, end-start]`; add `rebase_turns(turns, keeps)` remapping clip-relative turns onto the tightened post-filler timeline (mirroring `filler.rebase_words`) so turns stay aligned to the rebased words.
    - _Requirements: 13.4, 13.5_

  - [x]* 2.5 Property test: speaker-turn structural well-formedness → `tests/test_diarization.py`
    - **Property 1: Speaker-turn structural well-formedness** — every produced turn has `start <= end`, lies within `[0, D]`, and the list is ordered by ascending `start`.
    - _Requirements: 1.1, 1.4, 1.5, 1.6_ · _Properties: P1_

  - [x]* 2.6 Property test: speaker-turns are non-overlapping → `tests/test_diarization.py`
    - **Property 2: Speaker-turns are non-overlapping** — the `[start, end)` intervals of any two distinct produced turns do not overlap.
    - _Requirements: 2.1_ · _Properties: P2_

  - [x]* 2.7 Property test: adjacent same-label contiguous turns are merged → `tests/test_diarization.py`
    - **Property 3: Adjacent same-label contiguous turns are merged** — no two adjacent produced turns share the same label while contiguous.
    - _Requirements: 1.7_ · _Properties: P3_

  - [x]* 2.8 Property test: empty timeline yields zero turns without failure → `tests/test_diarization.py`
    - **Property 4: Empty timeline yields zero turns without failure** — an empty Word_Timeline produces zero turns and does not raise.
    - _Requirements: 2.2_ · _Properties: P4_

  - [x]* 2.9 Property test: speaker cap is never exceeded → `tests/test_diarization.py`
    - **Property 5: Speaker cap is never exceeded** — distinct labels ≤ `M`, and least-represented speakers are merged rather than exceeding the cap.
    - _Requirements: 2.4, 2.5_ · _Properties: P5_

  - [x]* 2.10 Property test: speaker-turn serialisation round-trip → `tests/test_diarization.py`
    - **Property 6: Speaker-turn serialisation round-trip** — `turns_from_dicts(turns_to_dicts(t))` produces an equivalent list.
    - _Requirements: 3.2_ · _Properties: P6_

  - [x]* 2.11 Property test: malformed turn records discarded, valid ones retained → `tests/test_diarization.py`
    - **Property 7: Malformed turn records are discarded, valid ones retained** — parsing a mixed list keeps exactly the valid records and drops the malformed ones without raising.
    - _Requirements: 3.3_ · _Properties: P7_

  - [x]* 2.12 Property test: backend absence or failure degrades to offline segmentation → `tests/test_diarization.py`
    - **Property 8: Backend absence or failure degrades to offline segmentation** — with no backend the diariser returns the offline `segment_by_words` result; with a raising backend it returns the same result and records a degradation marker. Uses `FakeDiarizationBackend` (canned + raising variants).
    - _Requirements: 4.2, 4.4_ · _Properties: P8_

  - [x]* 2.13 Unit tests: backend alignment, single-speaker, marker selection → `tests/test_diarization.py`
    - Assert an injected backend's spans are aligned to word boundaries, single-speaker input yields one turn, and `diarization:model` vs `diarization:transcript` markers are selected correctly.
    - _Requirements: 1.3, 2.3, 4.1_

- [x] 3. Checkpoint — Ensure all tests pass, ask the user if questions arise.

- [x] 4. Multi-face detection, tracks, and face↔speaker association (`worker/effects/reframe.py`)
  - [x] 4.1 Implement `FaceBox`/`Face_Track` types and multi-face `detect_faces`
    - Add the frozen `FaceBox` and `Face_Track` (`center_at`, `presence`) dataclasses; implement `detect_faces(video, *, sample_fps=None, max_samples=None, detector=None)` sampling ≤ `max_samples` frames and returning **all** face boxes per sampled frame (not only the largest), with a lazy vision import that returns `[]` (never raises) on missing cv2 or an unopenable video, and CPU-only detection.
    - _Requirements: 5.1, 5.3, 5.4, 15.2, 15.3_

  - [x] 4.2 Implement the pure `build_face_tracks`
    - Group per-frame boxes into `Face_Track`s by IoU / nearest-centroid continuity, each with a stable `track_id`; no faces anywhere → `[]`; testable without cv2.
    - _Requirements: 5.2, 5.5_

  - [x] 4.3 Implement the pure `associate_faces`
    - Add the `Association` result type and `associate_faces(turns, tracks)`: assign at most one track per turn choosing the highest `presence` over the turn window; leave turns with no overlapping track unassociated and listed for degraded handling; keep distinct associated tracks ≤ distinct speaker labels; map turns sharing a label to the same track where a consistent best track exists.
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

  - [x]* 4.4 Property test: face boxes group into stable tracks → `tests/test_speaker_reframe.py`
    - **Property 9: Face boxes group into stable tracks** — `build_face_tracks` returns tracks each with a stable `track_id`; no faces in any frame → zero tracks.
    - _Requirements: 5.2, 5.5_ · _Properties: P9_

  - [x]* 4.5 Property test: association single-valued and cardinality-bounded → `tests/test_speaker_reframe.py`
    - **Property 10: Association is single-valued and cardinality-bounded** — each turn associates with ≤ one track, distinct associated tracks ≤ distinct labels, and turns sharing a label map to the same track when a consistent track exists.
    - _Requirements: 6.1, 6.4, 6.5_ · _Properties: P10_

  - [x]* 4.6 Property test: association picks the most-present track; gaps marked → `tests/test_speaker_reframe.py`
    - **Property 11: Association picks the most-present track; gaps are marked** — each associated turn's track maximises `presence` over its window, and turns with no overlapping track are left unassociated and recorded.
    - _Requirements: 6.2, 6.3_ · _Properties: P11_

  - [x]* 4.7 Unit tests: DI wiring and unassociated-turn handling → `tests/test_speaker_reframe.py`
    - Assert the injected `FakeFaceDetector`/sampler wiring works offline and a turn with no overlapping track is marked unassociated.
    - _Requirements: 6.3, 20.1_

- [x] 5. Speaker-aware reframe geometry and orchestration (`worker/effects/reframe.py`)
  - [x] 5.1 Implement the pure `intensity_params` mapping
    - Add `REFRAME_INTENSITY` and `intensity_params(intensity)` returning `(smoothing_alpha, transition_seconds)`; unknown → `standard`; monotonic subtle < standard < heavy in alpha and speed.
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 11.2_

  - [x] 5.2 Implement the pure `build_follow_active_path`
    - Produce the dense crop-centre path (reusing `Center`, `ema_smooth`, `resample_centers`): target the associated track's centre within a turn; hold the most recent valid centre in an unassociated turn; interpolate the centre over the intensity-derived transition on speaker change (ending no later than the next turn's stable window); smooth with the intensity alpha and clamp every centre so the crop window stays fully inside the source frame throughout, with every command time in `[0, duration]`.
    - _Requirements: 7.2, 8.1, 8.2, 8.4, 8.5, 10.5, 11.1, 11.3, 11.4_

  - [x] 5.3 Implement the pure `build_split_screen_layout`
    - Add the `Region` type and default 2-up builder (stacked for vertical, side-by-side for landscape): partition the target frame into non-overlapping tiles that exactly cover the frame; centre each tile's source crop on its track; when associated tracks > `max_regions` show the greatest-speaking-duration tracks up to capacity; return `[]` when fewer than two tracks are associated so the caller falls back to `follow_active`.
    - _Requirements: 7.3, 9.1, 9.2, 9.3, 9.4, 9.5_

  - [x] 5.4 Implement the pure `build_reframe_filter`
    - Return `(input_args, filter_string_or_filtergraph, applied_notes)` for a **single** ffmpeg pass: `follow_active` → `sendcmd` + `crop` + `scale` + `setsar` (as v0.7.0); `split_screen` → per-region `crop` → `scale` → `vstack`/`hstack` → `setsar` with `xfade`/`overlay` `enable=between(...)` for shown-speaker transitions; does not run ffmpeg.
    - _Requirements: 8.3, 9.6, 11.5, 13.1, 13.2_

  - [x] 5.5 Implement `apply_speaker_reframe` orchestration
    - Orchestrate `compute_crop_size` → `detect_faces` → `build_face_tracks` → `associate_faces` → build path/regions → `build_reframe_filter` → single `_run`; substitute `follow_active` for unknown layout or `split_screen` with < 2 tracks (recording `speaker_reframe_substituted`); raise `ReframeUnavailable` when the aspect is not narrower, no turns, no tracks, no usable geometry, or ffmpeg fails; accept injected `detector`/`sampler`.
    - _Requirements: 7.2, 7.3, 7.5, 9.5, 12.5, 13.1, 13.2, 14.4, 20.1_

  - [x]* 5.6 Property test: follow-active crop tracks the active speaker and holds on gaps → `tests/test_reframe_geometry.py`
    - **Property 12: Follow-active crop tracks the active speaker and holds on gaps** — within an associated turn the pre-smoothing target equals the track's centre; within an unassociated turn the centre holds the most recent valid centre.
    - _Requirements: 8.1, 8.4_ · _Properties: P12_

  - [x]* 5.7 Property test: crop windows within bounds and times within clip (master bounds) → `tests/test_reframe_geometry.py`
    - **Property 13: Crop windows stay within frame bounds and times within the clip** — every emitted crop window (including during transitions) lies fully within the source frame, and every command time lies within `[0, D]`.
    - _Requirements: 8.2, 8.5, 10.5, 11.3, 13.5, 20.6_ · _Properties: P13_

  - [x]* 5.8 Property test: intensity maps deterministically and monotonically → `tests/test_reframe_geometry.py`
    - **Property 14: Intensity maps deterministically and monotonically** — the mapping is deterministic, the transition is derived from intensity, and subtle → standard → heavy yields monotonically weaker smoothing and faster movement.
    - _Requirements: 10.2, 10.3, 10.4, 11.2_ · _Properties: P14_

  - [x]* 5.9 Property test: speaker changes transition smoothly and end before next stable window → `tests/test_reframe_geometry.py`
    - **Property 15: Speaker changes transition smoothly and end before the next stable window** — on a speaker change the centre is interpolated over the intensity-derived duration (never an instant jump) and ends no later than the next turn's stable window.
    - _Requirements: 11.1, 11.4_ · _Properties: P15_

  - [x]* 5.10 Property test: split-screen regions tile the target frame exactly → `tests/test_reframe_geometry.py`
    - **Property 16: Split-screen regions tile the target frame exactly** — for ≥ two tracks the regions are non-overlapping, their union exactly covers the full target frame with no uninitialised area, and each region's crop is centred on its track.
    - _Requirements: 9.1, 9.2, 9.3_ · _Properties: P16_

  - [x]* 5.11 Property test: split-screen shows the most-talkative speakers within capacity → `tests/test_reframe_geometry.py`
    - **Property 17: Split-screen shows the most-talkative speakers within capacity** — when tracks exceed capacity, the shown speakers are exactly those with the greatest total speaking duration up to the capacity.
    - _Requirements: 9.4_ · _Properties: P17_

  - [x]* 5.12 Property test: too few tracks fall back to follow-active → `tests/test_reframe_geometry.py`
    - **Property 18: Too few tracks fall back to follow-active** — a clip with fewer than two associated tracks falls back to `follow_active` and records a substitution marker.
    - _Requirements: 9.5_ · _Properties: P18_

  - [x]* 5.13 Property test: unknown layout applies the follow-active default → `tests/test_reframe_geometry.py`
    - **Property 19: Unknown layout applies the follow-active default** — any unrecognised `reframe_layout` applies `follow_active` and records a substitution.
    - _Requirements: 7.5_ · _Properties: P19_

  - [x]* 5.14 Property test: no geometry action when the target aspect is not narrower → `tests/test_reframe_geometry.py`
    - **Property 20: No geometry action when the target aspect is not narrower** — for a target aspect not narrower than the source, speaker-aware reframe takes no geometry action and leaves framing unchanged.
    - _Requirements: 12.5_ · _Properties: P20_

  - [x]* 5.15 Property test: filler rebasing keeps turns clip-relative and bounded → `tests/test_reframe_geometry.py`
    - **Property 21: Filler rebasing keeps turns clip-relative and bounded** — turns rebased onto the tightened post-filler timeline are bounded within the final clip duration and stay aligned to the rebased words.
    - _Requirements: 13.4_ · _Properties: P21_

  - [x]* 5.16 Unit tests: filter shape, tile arithmetic, transition, precedence dispatch → `tests/test_speaker_reframe.py`
    - Assert `follow_active` produces a valid `sendcmd`+`crop`+`scale` filter, `split_screen` 2-up tile arithmetic for a vertical target is correct, the shown-speaker transition uses the intensity duration, and precedence dispatch chooses speaker-aware when enabled+narrower, legacy reframe when speaker-aware off + reframe on, and static when both off.
    - _Requirements: 11.5, 12.1, 12.2, 12.4_

  - [x]* 5.17 ffmpeg integration tests: single-pass geometry outputs → `tests/test_speaker_reframe.py`
    - Using `make_video`/`requires_ffmpeg`/`probe_size` with a mocked detector/sampler returning canned boxes: render a 2–3s `follow_active` clip and assert output exists at the target resolution; render a `split_screen` clip (two canned tracks) and **spy on `_run` to assert a single ffmpeg invocation** at the target resolution; assert the geometry-prepared clip flows into `compositor.render_clip` with captions/emoji and no additional geometry pass.
    - _Requirements: 8.3, 9.6, 13.1, 13.2, 13.3, 15.5, 20.5_

- [x] 6. Checkpoint — Ensure all tests pass, ask the user if questions arise.

- [x] 7. Pipeline geometry-stage integration (`worker/pipeline.py`)
  - [x] 7.1 Add once-per-source diarisation and the geometry precedence ladder
    - Compute `need_diar = options.diarization or options.speaker_reframe` and call `diarization.diarize_source(...)` **once per source** (guarded so no diarisation/sampling occurs when disabled), auto-enabling the diarisation reframe needs internally **without mutating** `options`; per clip, `slice_turns` then `rebase_turns` when filler removed, and replace the `if options.reframe` block with the precedence ladder speaker-aware → single-speaker → static `crop_blur`, recording the layout marker on success and `speaker_reframe_degraded` on fallback (catching `ReframeUnavailable`/`FFmpegError`).
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 13.3, 13.4, 14.1, 14.2, 14.3, 14.4, 14.5, 15.1, 15.4, 16.4, 16.5_

  - [x]* 7.2 Property test: diarisation runs at most once per source; disabled means no work → `tests/test_pipeline_degradation.py`
    - **Property 22: Diarisation runs at most once per source; disabled means no work** — the diariser is invoked ≤ once per source, and when speaker-aware reframe and diarisation are disabled no diarisation and no face-detection sampling occur. Uses a spy on `diarize_source` and the sampler.
    - _Requirements: 15.1, 15.4_ · _Properties: P22_

  - [x]* 7.3 Property test: frame sampling is bounded → `tests/test_pipeline_degradation.py`
    - **Property 23: Frame sampling is bounded** — the number of frames sampled for face detection never exceeds the configured cap.
    - _Requirements: 15.2_ · _Properties: P23_

  - [x]* 7.4 Property test: the degradation chain always produces geometry and records the right marker → `tests/test_pipeline_degradation.py`
    - **Property 24: The degradation chain always produces geometry and records the right marker** — for zero turns, zero tracks, unusable geometry, or an ffmpeg failure the pipeline falls back speaker-aware → single-speaker → static and records the corresponding marker, while a successful run records the applied-layout marker. Uses `FakeFaceDetector` (empty/raising variants) and a forced `FFmpegError`.
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5_ · _Properties: P24_

  - [x]* 7.5 Property test: all-off reproduces v0.7.0 behaviour → `tests/test_pipeline_degradation.py`
    - **Property 26: All-off reproduces v0.7.0 behaviour** — with `diarization` and `speaker_reframe` both disabled the geometry decision, output, and `effects_applied` match pre-feature v0.7.0 behaviour.
    - _Requirements: 16.4, 17.2_ · _Properties: P26_

  - [x]* 7.6 Property test: reframe auto-enables diarisation without flipping the persisted toggle → `tests/test_pipeline_degradation.py`
    - **Property 27: Reframe auto-enables diarisation without flipping the persisted toggle** — when `speaker_reframe` is on and `diarization` is off, the pipeline computes the diarisation reframe needs while the persisted `diarization` value stays unchanged.
    - _Requirements: 16.5_ · _Properties: P27_

  - [x]* 7.7 Property test: permissibility forces offline, local, network-free diarisation → `tests/test_pipeline_degradation.py`
    - **Property 28: Permissibility forces offline, local, network-free diarisation** — with `permissibility_mode` on, diarisation uses only offline Word_Timeline segmentation (any external backend bypassed/degraded), reframe uses only local vision, and no external download/network call occurs. Asserted via a spy backend/downloader.
    - _Requirements: 19.1, 19.2, 19.3_ · _Properties: P28_

  - [x]* 7.8 ffmpeg integration tests: degradation and permissibility still produce clips → `tests/test_pipeline_degradation.py`
    - Using `make_video`/`requires_ffmpeg` with mocked detector/sampler: mock detector returning `[]` → clip still produced via single-speaker/static fallback with `faces_none`/`speaker_reframe_degraded` recorded; forced `FFmpegError` on the speaker-aware pass → fallback still yields a clip; `permissibility_mode` on with a spy backend/downloader → no backend/network call and a reframed clip is still produced.
    - _Requirements: 14.4, 14.6, 19.1, 19.4_

- [x] 8. API surface (`api/main.py`)
  - [x] 8.1 Extend `/api/info`, `OptionsModel`, and the `POST /api/upload` Form
    - Add `"reframe_layouts": ["follow_active", "split_screen"]` and `"reframe_intensities": ["subtle", "standard", "heavy"]` to the `effects` block (retaining all existing lists); add `diarization`, `speaker_reframe`, `reframe_layout`, `reframe_intensity` to `OptionsModel` with identical defaults and matching `Form(...)` parameters, threaded into `ProcessingOptions.from_dict`; leave existing fields untouched.
    - _Requirements: 7.4, 10.6, 17.5, 18.1, 18.2, 18.5_

  - [x]* 8.2 Unit tests: `/api/info` superset and option passthrough → `tests/test_api.py`
    - Assert `/api/info` advertises the new layout + intensity lists in addition to all existing values, and that the new upload Form fields reach `from_dict`.
    - _Requirements: 7.4, 10.6, 17.5, 18.1, 18.2_

- [x] 9. Frontend wiring
  - [x] 9.1 Extend `frontend/src/App.jsx` defaults and `toOptions`
    - Add the four new keys to `DEFAULT_SETTINGS` (`diarization`/`speaker_reframe` OFF, `reframe_layout="follow_active"`, `reframe_intensity="standard"`) and forward them in `toOptions`.
    - _Requirements: 18.3, 16.3_

  - [x] 9.2 Add controls to `frontend/src/components/SettingsPanel.jsx`
    - Add a **Speaker-aware reframe** toggle plus **Reframe layout** and **Reframe intensity** dropdowns (with a **Diarisation** toggle alongside) to the Effects block, populated from `/api/info`.
    - _Requirements: 18.4, 16.1_

- [x] 10. Checkpoint — Ensure all tests pass, ask the user if questions arise.

- [x] 11. Version, changelog, and README for the 0.8.0 release
  - [x] 11.1 Bump `VERSION` to `0.8.0` and document the release
    - Update `VERSION` from `0.7.0` to `0.8.0`; add a `## [0.8.0]` "Added — Speaker Diarisation & Multi-Speaker Reframe" section to `CHANGELOG.md` (transcript-first diarisation, multi-face follow-active and split-screen reframe layouts, intensity controls, graceful degradation, permissibility-aware offline operation); update `README.md` feature/options docs consistent with prior phases.
    - _Requirements: 15.6, 18.1_

## Notes

- Tasks marked with `*` are optional test sub-tasks (unit / property / integration) and can be skipped for a faster MVP; core implementation tasks are never optional.
- Each task references the specific requirement numbers it satisfies, and property-test tasks cite the design property (P1–P28) they implement.
- Ordering is test-first and dependency-safe: shared data-model / options / config land first (unblocking API + UI), then the diarisation module, then the reframe detection/association/geometry pure functions and their tests, all before the pipeline geometry-stage wiring — so no code is orphaned and an "all-off" run always reproduces v0.7.0.
- Every new capability defaults OFF; enabling speaker-aware reframe auto-enables the diarisation it needs internally without flipping the persisted diarisation toggle.
- Property tests use `hypothesis` (`@settings(max_examples=100)`), one property per test, tagged `# Feature: speaker-diarization-reframe, Property N: ...`, in the exact files named in the design's Testing Strategy (`tests/test_diarization.py`, `tests/test_speaker_reframe.py`, `tests/test_reframe_geometry.py`, `tests/test_pipeline_degradation.py`, `tests/test_options_roundtrip.py`).
- ffmpeg integration tests reuse `make_video`, `requires_ffmpeg`, `probe_size`, `probe_duration`, and `FakeWord`, and mock the diariser (`FakeDiarizationBackend`), face detector/sampler (`FakeFaceDetector`), and spy on `_run` / `diarize_source` so the suite stays fast, deterministic, offline, and CPU-only.
- All 28 design properties are covered by exactly one property-test sub-task: P1–P8 (`tests/test_diarization.py`), P9–P11 (`tests/test_speaker_reframe.py`), P12–P21 (`tests/test_reframe_geometry.py`), P22–P24 & P26–P28 (`tests/test_pipeline_degradation.py`), and P25 (`tests/test_options_roundtrip.py`).

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0,  "tasks": ["1.1", "1.3", "2.1"] },
    { "id": 1,  "tasks": ["1.2", "2.2", "1.4"] },
    { "id": 2,  "tasks": ["2.3", "2.5", "1.5", "8.1"] },
    { "id": 3,  "tasks": ["2.4", "2.6", "4.1", "8.2", "9.1"] },
    { "id": 4,  "tasks": ["2.7", "4.2", "9.2"] },
    { "id": 5,  "tasks": ["2.8", "4.3"] },
    { "id": 6,  "tasks": ["2.9", "5.1", "4.4"] },
    { "id": 7,  "tasks": ["2.10", "5.2", "4.5"] },
    { "id": 8,  "tasks": ["2.11", "5.3", "4.6"] },
    { "id": 9,  "tasks": ["2.12", "5.4", "4.7"] },
    { "id": 10, "tasks": ["2.13", "5.5"] },
    { "id": 11, "tasks": ["5.6", "7.1"] },
    { "id": 12, "tasks": ["5.7", "7.2"] },
    { "id": 13, "tasks": ["5.8", "7.3"] },
    { "id": 14, "tasks": ["5.9", "7.4"] },
    { "id": 15, "tasks": ["5.10", "7.5"] },
    { "id": 16, "tasks": ["5.11", "7.6"] },
    { "id": 17, "tasks": ["5.12", "7.7"] },
    { "id": 18, "tasks": ["5.13", "7.8", "5.16"] },
    { "id": 19, "tasks": ["5.14", "5.17"] },
    { "id": 20, "tasks": ["5.15"] },
    { "id": 21, "tasks": ["11.1"] }
  ]
}
```
