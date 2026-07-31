# Implementation Plan — Modern Face Detection & Detection Confidence

These are incremental, test-first coding steps. Execute them **one task at a time**, in
order — each task builds on the previous ones so there is never orphaned code. The plan
lands the **pure, dependency-free pieces first** (coordinate conversion, coverage
arithmetic, marker formatting, main-face selection), because those are where the real
risk lives and they are testable without OpenCV, MediaPipe, or ffmpeg. The backend
wiring, the sampler report, and the pipeline markers follow. The real-library
verification comes before the pipeline integration, so a coordinate-system mistake is
caught before it can reach a rendered clip.

Every new capability defaults to previously shipped behaviour: `face_detector` defaults
to `"haar"`, so at any point during this plan an unchanged configuration reproduces
v0.11.0 output.

Tasks marked with `*` are optional test sub-tasks (unit / property / integration tests).
Property tests use `hypothesis` with `@settings(max_examples=100)` and are tagged
`# Feature: face-detection-upgrade, Property N: <text>`, one property per test, in the
exact files named in the design's Testing Strategy (`tests/test_face_detection.py`,
`tests/test_face_detection_real_binary.py`, `tests/test_effects_reframe.py`,
`tests/test_reframe_geometry.py`, `tests/test_speaker_reframe.py`,
`tests/test_pipeline_degradation.py`, `tests/test_options_roundtrip.py`). ffmpeg and
image integration tests reuse the existing helpers (`make_video`, `png_asset`,
`requires_ffmpeg`, `probe_size`, `FakeWord`) and the existing `FakeFaceDetector`.

**Before starting, run the baseline and record it:** `pytest` must report
**1880 passed, 0 skipped, 0 warnings**. A drop at any point means something stopped
running.

## Tasks

- [x] 0. Settle the golden-parity question before writing code
  - [x] 0.1 Determine whether the parity/golden fixtures pin `effects_applied` exactly
    - Inspect the golden render and parity tests (`tests/test_output_compat.py`, `tests/test_render_output_quality.py`, and any `*_parity*` fixtures) to establish whether adding `face_detector:haar` to a default run breaks a frozen set.
    - Record the finding in the PR description. If they are pinned, choose **deliberately** between updating the goldens (recommended — preserves Requirement 3) and withholding the marker on the default backend (weakens Requirement 3). Do not decide this implicitly by discovering a red test.
    - _Requirements: 3.1, 3.3, 9.2, 9.5_

- [x] 1. Pure geometry, arithmetic, and selection (no cv2, no mediapipe, no ffmpeg)
  - [x] 1.1 Add the `Detection` record and `relative_box_to_pixels` to `worker/effects/reframe.py`
    - Add a frozen `Detection` dataclass (`x`, `y`, `w`, `h`, `score: Optional[float] = None`) and the pure `relative_box_to_pixels(rel_x, rel_y, rel_w, rel_h, *, width, height)` returning `Optional[tuple[int, int, int, int]]`.
    - Convert, **then** clamp to `[0, width] × [0, height]`, **then** return `None` for a degenerate box. Document why that order is fixed and why the function is not inlined.
    - _Requirements: 2.4, 2.5, 2.6_

  - [x] 1.2 Add `detection_coverage` and the marker formatters
    - Pure `detection_coverage(samples) -> float` returning `0.0` for an empty sample list, otherwise the fraction of samples with at least one detection, constrained to `[0.0, 1.0]`.
    - Pure marker builders producing `face_detector:{label}`, `face_detector_substituted:{requested}:{resolved}`, `reframe_low_confidence:{coverage:.2f}`, `reframe_sample_rate:{fps:.1f}` — fixed-precision formatting, never `str(float)`.
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 6.2, 6.6, 8.1_

  - [x] 1.3 Extend `pick_main_face` for optional Detection_Scores
    - Where any detection carries a score, rank on score and area together; where none do, preserve **exactly** the current largest-area behaviour; a single detection always wins regardless of score; zero detections still return `None`.
    - Keep the existing `list[tuple[int, int, int, int]]` call signature working, so no existing caller or test changes.
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 9.1_

  - [x] 1.4* Property test: relative→pixel conversion is bounded and non-degenerate → `tests/test_face_detection.py`
    - **Property 1** — for any relative box and any positive frame size, the result is `None` or lies within frame bounds with `w > 0 and h > 0`.
    - _Requirements: 2.4, 2.5, 2.6_ · _Properties: P1_

  - [x] 1.5* Property test: coverage is a bounded fraction → `tests/test_face_detection.py`
    - **Property 2** — coverage is in `[0, 1]`; `0.0` for an empty sample list; `1.0` when every sample has ≥ 1 detection.
    - _Requirements: 5.1, 5.2, 5.3, 5.4_ · _Properties: P2_

  - [x] 1.6* Property tests: main-face selection → `tests/test_face_detection.py`
    - **Property 3** — with no scores present, selection is exactly largest-area.
    - **Property 4** — at most one main face is selected, and a lone detection is always selected.
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 9.1_ · _Properties: P3, P4_

  - [x] 1.7* Property test: marker strings are deterministic → `tests/test_face_detection.py`
    - **Property 5** — for any coverage value the marker is stable across repeated formatting and carries a two-decimal representation.
    - _Requirements: 6.6_ · _Properties: P5_

- [x] 1b. Vendor the detector model — this is what unblocks the feature
  - [x] 1b.1 Confirm the model choice and licence before committing a binary
    - Decide between MediaPipe BlazeFace (`blaze_face_short_range.tflite`, ~230 KB, Apache-2.0) and OpenCV YuNet (`face_detection_yunet_*.onnx`, ~340 KB, MIT). Neither ships in its wheel — verified. Record the decision and the licence in the PR.
    - Prefer BlazeFace only if the licence permits redistribution as clearly as the OFL fonts already vendored do; otherwise prefer YuNet, which also removes the `mediapipe` API-churn risk entirely.
    - _Requirements: 12.1, 12.2_

  - [x] 1b.2 Commit the model and its licence under `assets/models/`
    - The model file plus `LICENSE-<model>.txt`, following the `assets/font-licenses/` precedent. Note `assets/fonts/` must contain nothing but font files because libass offers every entry to FreeType — `assets/models/` has no equivalent constraint, but keep the licence as a sibling for consistency.
    - Check `.gitignore` and `.dockerignore` do not exclude the path; `assets/emoji-*/` is excluded but `assets/emoji/` is not, and this must follow the latter.
    - _Requirements: 12.1, 12.2, 12.7_

  - [x] 1b.3 Add `scripts/fetch_models.py` with a `--check` mode
    - Modelled directly on `scripts/fetch_emoji.py`. A Model_Manifest (filename, SHA-256, source URL, licence id, backend served) and a `--check` that verifies the working tree with **no network access**, exiting non-zero and naming the offending file on a missing, truncated, or mismatched model.
    - The fetch path is for maintainers only and is never invoked from the render path.
    - _Requirements: 12.3, 12.4, 12.5, 12.10, 12.11_

  - [x] 1b.4 Wire verification into CI and the container
    - Add `python scripts/fetch_models.py --check` to the `backend` job in `.github/workflows/ci.yml`, beside the existing emoji check. Ensure the model is copied into the image, and extend `scripts/docker_smoke.sh` to assert it resolves **through the API** rather than by listing the filesystem — the emoji check's `/api/info` assertion is the pattern.
    - _Requirements: 12.6, 12.7, 12.8_

  - [x] 1b.5 Narrow the `mediapipe` pin and pin the API surface
    - Change `mediapipe>=0.10,<1.0` to a range whose every member exposes `mediapipe.tasks.python.vision.FaceDetector`, with a comment recording that `mediapipe.solutions` was removed and must not be depended upon.
    - _Requirements: 13.1, 13.2, 13.4_

  - [x] 1b.6* Test: the installed detector library exposes the API actually called → `tests/test_face_detection_real_binary.py`
    - Assert `mediapipe.tasks.python.vision.FaceDetector` (or the YuNet equivalent) is importable and that `mediapipe.solutions` is **not** relied upon. This is a drift pin: it fails loudly when a resolver upgrade moves the API out from under the backend, which is the failure this task exists to prevent.
    - _Requirements: 13.1, 13.3_

  - [x] 1b.7* Test: `--check` verifies offline and fails on corruption → `tests/test_face_detection.py`
    - Assert `--check` passes on the working tree with no network; assert a truncated copy in a temp dir fails and names the file. Mirror `scripts/fetch_emoji.py --check`'s existing coverage.
    - _Requirements: 12.4, 12.5_

- [x] 2. Backend resolution
  - [x] 2.1 Add `FACE_DETECTOR_BACKENDS`, `DEFAULT_FACE_DETECTOR_BACKEND`, and `resolve_detector`
    - `resolve_detector(backend, *, injected=None, cv2_module=None, min_score=None) -> tuple[Optional[Callable], str]`. The label is returned by the branch that succeeded, never inferred by the caller. An injected detector resolves to `"injected"`. Never raises; an unbuildable detector returns `(None, label)`.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.6, 3.1, 3.4_

  - [x] 2.2 Add `_mediapipe_detector(min_score, model_path)` returning `(detect, close)`
    - Lazy `import mediapipe` **inside** the function. Use `mediapipe.tasks.python.vision.FaceDetector` with `base_options.model_asset_path=model_path`. **Do not use `mediapipe.solutions` — it has been removed**, and there is therefore no `model_selection` argument; near/far range is a property of the vendored model file.
    - **First, establish empirically whether the tasks API's `bounding_box` is pixels or normalised** (task 3.1 does this). Route it through `relative_box_to_pixels` either way, which then clamps and validates rather than converting if the values are already absolute.
    - Drop detections below `min_score` and degenerate boxes. Apply no absolute minimum-size floor. A missing model file returns `None` rather than raising. Return a `close` for the native graph.
    - _Requirements: 2.1, 2.2, 2.3, 2.3a, 2.3b, 2.4, 2.7, 2.8, 2.9_

  - [x] 2.3 Wire the Haar → fallback path
    - MediaPipe requested but unimportable, unconstructible, **or its Vendored_Model absent or digest-mismatched** → build Haar and resolve to a substitution label naming both sides. All four causes share one marker; the log line names the specific cause. Leave `_default_haar_detector` byte-identical.
    - _Requirements: 1.3, 3.2, 4.1, 4.2, 4.2a_

  - [x] 2.4* Unit tests: resolution and substitution → `tests/test_face_detection.py`
    - Default resolves to `haar`; unknown value resolves to `haar` without raising; injected detector resolves to `injected` and is used; a stubbed import failure for mediapipe resolves to the substitution label; an unbuildable cascade yields `(None, ...)` rather than an exception.
    - _Requirements: 1.2, 1.4, 1.6, 3.2, 3.3, 3.4, 4.1, 4.2_

- [x] 3. Real-library verification — do this before pipeline integration
  - [x] 3.0 Establish the tasks API's coordinate system empirically
    - Load the Vendored_Model, run the real detector on a real image, and print `detections[0].bounding_box`. Determine whether `origin_x`/`origin_y`/`width`/`height` are absolute pixels or normalised. **Write this finding into the design document before implementing the conversion** — the first draft of this spec was wrong about the library's API twice, and both times the correction came from running it.
    - _Requirements: 2.4, 11.1_

  - [x] 3.1* Real MediaPipe test: detections are in pixels and in bounds → `tests/test_face_detection_real_binary.py`
    - Construct the actual `mediapipe` backend with **no monkeypatching of `mediapipe`**, loading the Vendored_Model. Run it on a real image built from the existing `png_asset` / ffmpeg fixtures. Assert at least one returned dimension exceeds `1` — the assertion that fails if normalised coordinates leak — and that every box lies within frame bounds.
    - **Not** guarded by an availability skip: `mediapipe` is a hard dependency, so a skip here would mean the dependency vanished, which is what the no-skips rule exists to surface.
    - _Requirements: 2.4, 2.5, 2.6, 11.1, 11.2, 11.4_

  - [x] 3.2* Real MediaPipe test: independent cross-check of the conversion → `tests/test_face_detection_real_binary.py`
    - Read the `relative_bounding_box` from MediaPipe directly, compute the expected pixel box **in the test**, sharing no code with `relative_box_to_pixels`, and compare. A cross-check that reuses the code under test verifies only self-consistency.
    - _Requirements: 11.1, 11.2_

  - [x] 3.3 Triage any new warning from importing mediapipe in a new place
    - If the import surfaces a further `pkg_resources`/protobuf deprecation, add a **targeted** `filterwarnings` ignore in `pyproject.toml` with a comment saying why it cannot be fixed. Never broaden the existing ignores and never relax `filterwarnings = error`.
    - _Requirements: 11.5_

- [x] 4. Sampler report
  - [x] 4.1 Add `Sample_Report` and `sample_face_report(...)`
    - Frozen `Sample_Report` (`samples`, `resolved_backend`, `effective_fps`, `requested_fps`) with a `coverage` property computing from the same sample set the crop path uses. Compute `effective_fps` from the sample count and the duration actually used.
    - _Requirements: 5.1, 5.5, 8.1, 8.3_

  - [x] 4.2 Keep `_sample_face_boxes` as a thin wrapper returning `report.samples`
    - Preserve the existing signature and return type exactly. `FRAME_SAMPLER` in `worker/pipeline.py:78-80` is patched by name and the existing reframe tests call this directly — an additive sibling, not a signature change.
    - _Requirements: 9.1, 9.2_

  - [x] 4.3 Make a per-frame detector exception a zero-detection frame
    - Wrap the per-frame `detector(frame)` call so one raising frame contributes zero detections and sampling continues. Deliberately not a degradation rung: one bad frame is not a broken backend, and the zero correctly lowers reported coverage.
    - _Requirements: 4.3, 4.6_

  - [x] 4.4 Release MediaPipe resources in a `finally`
    - Call the backend's `close` alongside the existing `cap.release()`, including when sampling raises.
    - _Requirements: 2.9_

  - [x] 4.5* Unit tests: the wrapper is unchanged and the report is consistent → `tests/test_effects_reframe.py`
    - Assert `_sample_face_boxes` returns what it returned before for an injected detector; assert `sample_face_report(...).samples` is that same value; assert a detector raising on one frame yields a zero-detection sample and does not abort sampling; assert `close` is called even when sampling raises.
    - _Requirements: 4.3, 4.6, 2.9, 9.1_

- [x] 5. Options, config, and API/UI surface
  - [x] 5.1 Add `face_detector` to `ProcessingOptions` in `worker/models.py`
    - `face_detector: str = "haar"`. Validate against `FACE_DETECTOR_BACKENDS` in `from_dict`, falling back to `"haar"` on unknown or malformed values without raising, matching the existing treatment of `reframe_layout` / `reframe_intensity`. Leave every pre-existing field and default unchanged.
    - _Requirements: 1.1, 1.2, 1.4, 10.1, 10.2, 10.6_

  - [x] 5.2 Add the three settings in `config.py` with `.env.example` entries
    - `face_detector_backend="haar"`, `face_detector_min_score=0.5`, `reframe_coverage_floor=0.35`. Document `0.35` as a starting value chosen as the point where the crop path is interpolated across more frames than it is anchored by — not a measured one; measuring it needs the labelled benchmark (`M4`/`S1`).
    - _Requirements: 2.8, 6.1, 10.4, 10.5_

  - [x] 5.3 Surface the option through the API and UI
    - `OptionsModel` in `api/main.py`, the `/api/upload` form fields (loose optional string, per the existing convention that an unrecognised value falls back to the documented default rather than 422-ing), `/api/info` domains, `App.jsx` `DEFAULT_SETTINGS` **and** `toOptions()`, and `SettingsPanel.jsx`.
    - _Requirements: 1.5, 10.3_

  - [x] 5.4 Verify the drift pins
    - `tests/test_config_documentation.py` for the three new settings. Check `tests/conftest.py` `EFFECTS_OFF` / `assert_effects_off_is_exhaustive()` — `face_detector` is a string rather than a default-on boolean effect so it should not need listing, but **verify rather than assume**.
    - _Requirements: 10.5, 10.6_

  - [x] 5.5* Property test: `face_detector` round-trips and unknown values default → `tests/test_options_roundtrip.py`
    - **Property 6** — for any options dict, `face_detector` survives `from_dict(asdict(...))`, and any unrecognised value resolves to `haar` without raising.
    - _Requirements: 10.2, 1.4_ · _Properties: P6_

- [x] 6. Pipeline integration and markers
  - [x] 6.1 Thread the resolved backend and coverage into `apply_reframe`
    - Record `face_detector:{resolved}` or the substitution marker; record `reframe_low_confidence:{coverage:.2f}` when coverage is below `reframe_coverage_floor` **and** at least one detection was found; record `reframe_sample_rate:{fps:.1f}` only when the cap bound. Leave `faces_none`, `reframe`, and the existing static fallback markers spelled exactly as they are.
    - _Requirements: 3.1, 3.2, 6.2, 6.3, 6.4, 8.1, 8.4, 9.5_

  - [x] 6.2 Thread the same markers through `apply_speaker_reframe`
    - Both geometry paths report identically; `speaker_reframe_degraded` is unchanged.
    - _Requirements: 6.5, 9.5_

  - [x] 6.3 Confirm the geometry stage is otherwise untouched
    - No change to the filter graph shape, the `sendcmd` script for a given crop path, or the ffmpeg pass count.
    - _Requirements: 9.3, 9.4_

  - [x] 6.4* Unit tests: markers on both geometry paths → `tests/test_speaker_reframe.py`, `tests/test_reframe_geometry.py`
    - Assert the resolved-backend marker names what ran for default, opt-in, injected, and substituted cases; assert `reframe_low_confidence` appears below the floor and not at or above it; assert `faces_none` and `reframe_low_confidence` are never both present; assert the sampling marker appears only when the cap bound.
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 6.2, 6.3, 6.4, 6.5, 8.1, 8.4, 11.3_

  - [x] 6.5* Integration tests: the five-rung degradation ladder → `tests/test_pipeline_degradation.py`
    - One test per rung: injected; mediapipe constructible; mediapipe unimportable → haar substitution; haar; nothing constructible → zero samples → Static_Reformat with the job still completing. Assert no rung raises out of the geometry stage.
    - _Requirements: 4.1, 4.2, 4.4, 4.5, 4.6_

  - [x] 6.6* Integration test: geometry output is unchanged on the default backend → `tests/test_reframe_geometry.py`
    - With `face_detector="haar"` and an injected detector, assert the produced `sendcmd` script and crop dimensions are identical to the current expected values.
    - _Requirements: 9.1, 9.3, 9.4_

- [ ] 7. Verification and close-out
  - [ ] 7.1 Full gate run
    - `ruff check .` clean · `pytest` at **1880 + new tests, 0 skipped, 0 warnings** · `cd frontend && npm run lint && npm run test:run && npm run build` · `scripts/docker_smoke.sh` builds and serves.
    - _Requirements: 11.4, 11.5_

  - [ ] 7.2 Eyeball the real output
    - Run `scripts/smoke_reel.py` on both backends over footage containing a profile turn and a two-shot. The suite cannot tell you the framing improved; only the pixels can. Attach both to the PR.
    - _Requirements: 2.1, 2.7_

  - [ ] 7.3 Add a mutation spec under `tests/mutations/`
    - Per the working agreement, one spec per batch. Highest-value mutations to attempt: return the relative box unconverted; drop the clamp; swap the substitution marker's two operands; return the requested backend label instead of the resolved one; invert the coverage comparison. Each should be **CAUGHT**; an ESCAPE is a real gap.
    - _Requirements: 3.3, 6.3, 2.4, 2.6_

  - [ ] 7.4 Record the measured coverage figures in the PR
    - Report Detection_Coverage for both backends on the same sources. This is the first quantitative statement anyone has made about this subsystem's accuracy, and it is what makes `reframe_coverage_floor` tunable rather than guessed.
    - _Requirements: 5.1, 6.1_
