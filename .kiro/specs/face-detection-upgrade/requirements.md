# Requirements Document

## Introduction

This spec defines **Modern Face Detection & Detection Confidence** — an incremental
enhancement to the AI Video Clipper (self-hosted, CPU-first, currently **v0.11.0**).

Face-tracking auto-reframe (`worker/effects/reframe.py`) is enabled by default
(`ProcessingOptions.reframe = True`) and is therefore on the critical path for almost
every clip the tool produces. Its detector is still the one extracted from the v0.7.0
single-speaker path: OpenCV's `haarcascade_frontalface_default.xml`, invoked with
`scaleFactor=1.1, minNeighbors=5, minSize=(60, 60)` (`worker/effects/reframe.py:531`).
`worker/thumbnail.py:21` already describes it in-source as "the 2001-era Haar cascade
V2 exists to replace".

Three defects follow from that detector, and one from the absence of reporting:

1. **It is frontal-only.** A speaker turning toward an interlocutor, tilting their
   head, or looking down leaves the detector's operating range. `track_faces` then
   "holds the last known centre through frames with no detection", so the crop freezes
   on a position that is no longer correct.
2. **`minSize=(60, 60)` is an absolute pixel floor.** On a 1080-high frame a face must
   occupy ≥ 5.6% of frame height to be detected at all. Wide two-shots and stage or
   interview framing therefore yield zero detections and fall back to Static_Reformat.
3. **It false-positives on texture**, and `pick_main_face` selects the box with the
   largest area. A spurious detection larger than the real face silently captures the
   crop path for as long as it persists.
4. **Nothing reports any of this.** There is a `speaker_reframe_degraded` marker for
   when reframe is *unavailable*, but no marker distinguishes "tracked the subject
   throughout" from "found a face in 12% of sampled frames and interpolated the rest".
   This is the sole remaining silent-quality failure in a codebase whose stated rule is
   that "an absent feature with no explanation is indistinguishable from a broken one".

This feature adds three cooperating capabilities:

1. **A modern detector backend.** MediaPipe Face Detection (BlazeFace) offered as a
   selectable Face_Detector_Backend beside the existing Haar cascade, reached through
   the `detector` injection point `_sample_face_boxes` already exposes, with its model
   file **vendored into the repository** (see below).

### Model provenance — verified, and not what was originally assumed

An earlier draft of this spec asserted that MediaPipe's BlazeFace weights ship inside
the `mediapipe` wheel, so the backend needed no checkpoint. **That was measured and is
false for the version this project actually installs.** On `mediapipe 0.10.35` — what
`mediapipe>=0.10,<1.0` resolves to at the time of writing:

- the legacy `mediapipe.solutions` namespace, which did bundle
  `face_detection_short_range.tflite`, **has been removed**. The module exposes only
  `Image`, `ImageFormat`, and `tasks`.
- the wheel contains **zero** `.tflite`, `.task`, or `.binarypb` files.
- the current API, `mediapipe.tasks.python.vision.FaceDetector`, requires
  `base_options.model_asset_path` — an explicit path to a model file the caller must
  supply.

The same is true of the alternative: OpenCV 4.11 exposes `cv2.FaceDetectorYN` (YuNet),
but ships no ONNX model in the wheel either.

So **every modern face detector available here needs a model file, and none of them
provide one.** Left there, this feature would join the eight items already blocked on
"model weights CI cannot have", because the no-skips rule forbids a test that downloads
a checkpoint.

It is not blocked, because this repository already solved this exact problem for a
different asset class. Emoji artwork is **vendored into the repository**, licensed,
verified by `scripts/fetch_emoji.py --check` with no network access, and asserted in CI
and in `scripts/docker_smoke.sh`. A ~230 KB Apache-2.0 licensed model file is a smaller
version of the same problem, and this spec requires the same treatment (Requirement 12).

That is the difference between this feature and `V3`/`V7`: an active-speaker or body
model is tens to hundreds of megabytes with a research licence, and cannot reasonably be
vendored. A single BlazeFace detector graph can.
2. **Detection confidence as first-class output.** A measured Detection_Coverage per
   clip, surfaced as an Effects_Applied marker so a caller can tell a well-framed clip
   from a guessed one without watching it.
3. **Confidence-aware face choice.** Where a backend supplies per-detection scores,
   Main_Face selection weighs score alongside area instead of ranking on area alone.

The feature MUST preserve the product's established design values, which are treated
as hard constraints throughout this document:

- **Default to previously shipped behaviour.** The Haar cascade remains the default
  backend so an unchanged configuration reproduces v0.11.0 output and `effects_applied`
  byte-for-byte, and the existing parity/golden renders continue to detect *accidental*
  change. Selecting MediaPipe is an explicit opt-in.
- **The resolved value is what gets reported**, never the requested one. The marker
  names the backend that actually ran, which is the guard that would have caught the
  `font_substituted:Arial` defect.
- **CPU-only**, no GPU, and **no network access at detection time or at test time** —
  which is why the model is vendored rather than fetched.
- **Graceful degradation is mandatory.** An unavailable, unloadable, or failing backend
  falls back along Haar → Static_Reformat. The job is never failed and every
  degradation is recorded in Effects_Applied.
- **Dependency injection at the boundary** so every path is testable offline, and a
  **real-library test** for anything that reads another library's output.
- **Single ffmpeg pass** — detection feeds the existing geometry stage and changes
  neither the filter graph nor the pass count.

### Out of scope

Explicitly excluded, with reasons, so the boundary is not rediscovered:

- **Active-speaker detection** (`V3`) and **subject/body detection** (`V7`) — both
  require model checkpoints that cannot be vendored, and the no-skips rule forbids a
  test that needs one. The seams remain as they are.
- **Vertical reframing for 9:16 output.** `compute_crop_size` returns `ch = src_h`
  whenever the target is narrower than the source, so `max_y` is 0 and the crop pans
  horizontally only. This is geometric, not a detector limitation, and no detector
  change affects it.
- **Automatic split-screen layout selection.** Deferred; it depends on this spec
  landing first, because the decision is only as good as the detections beneath it.
- **Raising `reframe_sample_cap`'s default.** The cap's interaction with clip length is
  *reported* here (Requirement 8) but its default is not changed, because that would
  alter timing and output for existing configurations.

## Glossary

- **Clipper**: The overall AI Video Clipper application (self-hosted, ffmpeg-based, CPU-first).
- **Pipeline**: The per-source flow in `worker/pipeline.py` (probe → transcribe → selection → per clip: cut → filler removal → **geometry** → compositor → thumbnail).
- **Face_Detector**: The callable `frame -> list[(x, y, w, h)]` that `_sample_face_boxes` invokes on each sampled frame; already dependency-injectable.
- **Face_Detector_Backend**: A named, selectable implementation of Face_Detector. This spec defines two: `haar` (the existing OpenCV cascade) and `mediapipe` (BlazeFace).
- **Detection**: One face found in one sampled frame, as an absolute-pixel box `(x, y, w, h)` optionally carrying a Detection_Score.
- **Detection_Score**: A backend-supplied confidence in `[0.0, 1.0]` for one Detection. Haar supplies none.
- **Detection_Coverage**: The fraction of sampled frames in which at least one Detection was found, in `[0.0, 1.0]`.
- **Coverage_Floor**: The configurable Detection_Coverage below which a clip's framing is reported as low-confidence.
- **Main_Face**: The single Detection per frame that the single-speaker path follows (`pick_main_face` today, by largest area).
- **Face_Box**: The `FaceBox` record (`t`, `x`, `y`, `w`, `h`) used by the multi-face tracking path.
- **Face_Track**: A Face_Box path persisted across sampled frames with a stable track identifier.
- **Sampler**: The bounded frame sampler in `_sample_face_boxes`, sampling at `reframe_sample_fps` capped at `reframe_sample_cap` frames per clip.
- **Single_Speaker_Reframe**: The existing face-tracking auto-reframe (`apply_reframe`) following one Main_Face.
- **Speaker_Reframe**: The speaker-aware geometry subsystem (`apply_speaker_reframe`) using Face_Tracks.
- **Static_Reformat**: The existing blurred-background centre-crop reformat (`reformat_aspect(..., mode="crop_blur")`), the terminal rung of the geometry ladder.
- **Processing_Options**: The user options record (`worker/models.py` `ProcessingOptions`, mirrored by `OptionsModel`, upload Form fields, `App.jsx` defaults/`toOptions`, and `SettingsPanel.jsx`).
- **Info_Endpoint**: The `/api/info` endpoint advertising available option values to the UI.
- **Effects_Applied**: The free-form `ClipResult.effects_applied` string markers recording which enhancements ran and how they degraded.
- **Degraded_Mode**: Operation when an optional dependency (MediaPipe, OpenCV, a cascade file) is unavailable; the feature falls back along the degradation chain and the Pipeline still produces clips.
- **Relative_Bounding_Box**: MediaPipe's native detection geometry, normalised to `[0.0, 1.0]` of frame width/height — *not* absolute pixels.
- **Vendored_Model**: The BlazeFace detector model file committed into the repository under `assets/models/`, with its licence, verified without network access — the same treatment `assets/emoji/` already receives.
- **Model_Manifest**: The record describing each Vendored_Model (filename, SHA-256, source URL, licence, backend it serves), used to verify the vendored file has not been substituted or truncated.

## Requirements

---

## Group A — Detector backends

### Requirement 1: Selectable detector backend

**User Story:** As an operator, I want to choose which face detector runs, so that I can adopt a better one without being forced onto it and without invalidating my existing output.

#### Acceptance Criteria

1. THE Clipper SHALL expose a Face_Detector_Backend selection accepting at least the values `haar` and `mediapipe`.
2. THE Clipper SHALL default the Face_Detector_Backend selection to `haar`.
3. WHEN the Face_Detector_Backend is `haar`, THE Face_Detector SHALL be behaviourally identical to the v0.11.0 cascade detector, including its `scaleFactor`, `minNeighbors`, and `minSize` arguments.
4. IF the Face_Detector_Backend value is unrecognised or malformed, THEN THE Clipper SHALL resolve to `haar` and SHALL NOT raise.
5. THE Info_Endpoint SHALL advertise the available Face_Detector_Backend values.
6. THE Clipper SHALL accept an injected Face_Detector that overrides backend selection entirely, so tests need neither backend installed.

### Requirement 2: MediaPipe detector backend

**User Story:** As a creator clipping podcast footage, I want a detector that finds faces in profile and at a distance, so that the crop follows the speaker instead of freezing.

#### Acceptance Criteria

1. WHERE the Face_Detector_Backend is `mediapipe`, THE Face_Detector SHALL detect faces using MediaPipe Face Detection via `mediapipe.tasks.python.vision.FaceDetector`, loading the Vendored_Model from disk.
2. THE Face_Detector SHALL import `mediapipe` lazily, inside the function that constructs the backend, and SHALL NOT import it at module scope.
3. THE Face_Detector SHALL NOT perform any network access, and SHALL NOT download any model file, at detection time, at construction time, or during any test.
3a. IF the Vendored_Model is absent from disk, THEN THE Face_Detector SHALL treat the `mediapipe` backend as unavailable and SHALL fall back per Requirement 4.
3b. THE Face_Detector SHALL NOT depend on the removed `mediapipe.solutions` namespace.
4. THE Face_Detector SHALL convert every MediaPipe Relative_Bounding_Box into an absolute-pixel box using the sampled frame's own width and height.
5. FOR every Detection returned by the `mediapipe` backend, THE Face_Detector SHALL ensure `w` and `h` are greater than zero.
6. THE Face_Detector SHALL clamp every converted Detection to the frame bounds, so no returned box extends outside `[0, width] × [0, height]`.
7. THE Face_Detector SHALL apply no absolute minimum-size floor to MediaPipe Detections.
8. THE Face_Detector SHALL expose a configurable minimum Detection_Score below which a MediaPipe Detection is discarded.
9. THE Face_Detector SHALL release any MediaPipe resource it opened once sampling for a clip has finished, including when sampling raises.

### Requirement 3: Backend resolution is reported, not assumed

**User Story:** As an operator diagnosing a clip, I want to know which detector actually ran, so that a silent substitution cannot be mistaken for the backend I asked for.

#### Acceptance Criteria

1. THE Clipper SHALL record in Effects_Applied the Face_Detector_Backend that actually produced the Detections for a clip.
2. WHEN the requested Face_Detector_Backend is unavailable AND a fallback backend runs instead, THE Clipper SHALL record the substitution naming both the requested and the resolved backend.
3. THE Clipper SHALL NOT record a Face_Detector_Backend marker naming a backend that did not run.
4. WHERE an injected Face_Detector is supplied, THE Clipper SHALL record that the detector was injected rather than naming a built-in backend.

### Requirement 4: Backend degradation chain

**User Story:** As a creator, I want a missing or broken detector to cost me framing quality and nothing else, so that a clip is still produced.

#### Acceptance Criteria

1. IF the `mediapipe` backend is requested AND `mediapipe` cannot be imported, THEN THE Face_Detector SHALL fall back to `haar` and SHALL record the substitution.
2. IF the `mediapipe` backend is requested AND its construction raises, THEN THE Face_Detector SHALL fall back to `haar` and SHALL record the substitution.
2a. IF the `mediapipe` backend is requested AND the Vendored_Model file is absent or fails Model_Manifest verification, THEN THE Face_Detector SHALL fall back to `haar` and SHALL record the substitution.
3. IF a Face_Detector raises while processing one sampled frame, THEN THE Sampler SHALL treat that frame as having zero Detections and SHALL continue sampling the remaining frames.
4. IF `cv2` cannot be imported, OR the video cannot be opened, OR no backend can be constructed, THEN THE Sampler SHALL return zero samples and SHALL NOT raise.
5. WHEN the Sampler returns zero samples, THE Pipeline SHALL fall back along the existing geometry ladder to Static_Reformat and SHALL NOT fail the job.
6. THE Face_Detector SHALL NOT allow a backend failure to propagate as an exception out of the geometry stage.

---

## Group B — Detection confidence

### Requirement 5: Detection coverage is measured

**User Story:** As a developer, I want the proportion of frames in which a face was actually found, so that framing quality is a measured quantity rather than an assumption.

#### Acceptance Criteria

1. THE Clipper SHALL compute Detection_Coverage as the fraction of sampled frames containing at least one Detection.
2. THE Clipper SHALL constrain Detection_Coverage to the closed interval `[0.0, 1.0]`.
3. WHEN zero frames were sampled, THE Clipper SHALL treat Detection_Coverage as `0.0` and SHALL NOT divide by zero.
4. WHEN every sampled frame contains at least one Detection, THE Clipper SHALL compute a Detection_Coverage of `1.0`.
5. THE Clipper SHALL compute Detection_Coverage from the same sample set the crop path was derived from, not from a second sampling pass.

### Requirement 6: Low-confidence framing is reported

**User Story:** As a creator reviewing a batch, I want to be told which clips were framed on sparse detections, so that I can review those and trust the rest.

#### Acceptance Criteria

1. THE Clipper SHALL expose a configurable Coverage_Floor.
2. WHEN Detection_Coverage is below the Coverage_Floor AND at least one Detection was found, THE Clipper SHALL record a low-confidence marker in Effects_Applied carrying the measured Detection_Coverage.
3. WHEN Detection_Coverage is at or above the Coverage_Floor, THE Clipper SHALL NOT record the low-confidence marker.
4. WHEN zero Detections were found anywhere, THE Clipper SHALL record the existing no-faces degradation rather than the low-confidence marker, so the two conditions remain distinguishable.
5. THE Clipper SHALL record the low-confidence marker for both Single_Speaker_Reframe and Speaker_Reframe paths.
6. THE Clipper SHALL format the recorded Detection_Coverage deterministically, so the same coverage produces the same marker string on every run and on every platform.

### Requirement 7: Confidence-aware main-face selection

**User Story:** As a creator, I want the crop to follow a face rather than a bookshelf, so that a large false positive cannot capture the framing.

#### Acceptance Criteria

1. WHERE Detection_Scores are available for a frame's Detections, THE Clipper SHALL select the Main_Face using both Detection_Score and box area.
2. WHERE no Detection_Scores are available for a frame's Detections, THE Clipper SHALL select the Main_Face by largest area, preserving current behaviour exactly.
3. WHEN a frame contains exactly one Detection, THE Clipper SHALL select that Detection as the Main_Face regardless of its Detection_Score.
4. THE Clipper SHALL select at most one Main_Face per sampled frame.
5. WHEN a frame contains zero Detections, THE Clipper SHALL select no Main_Face and SHALL leave the existing hold-last-centre behaviour unchanged.

### Requirement 8: Sampling coverage is observable

**User Story:** As an operator, I want to know when the sampling cap has reduced the effective sample rate, so that a long clip's framing quality is not silently worse than a short one's.

#### Acceptance Criteria

1. WHEN `reframe_sample_cap` causes the Sampler's effective rate to fall below the configured `reframe_sample_fps`, THE Clipper SHALL record a marker naming the effective rate.
2. THE Clipper SHALL NOT change the default value of `reframe_sample_cap` or `reframe_sample_fps`.
3. THE Clipper SHALL compute the effective rate from the sample count and clip duration actually used.
4. WHEN the cap did not bind, THE Clipper SHALL NOT record the sampling marker.

---

## Group C — Integration and compatibility

### Requirement 9: Byte-identical default output

**User Story:** As a maintainer, I want an unchanged configuration to produce unchanged output, so that the parity and golden renders keep detecting accidental change.

#### Acceptance Criteria

1. WHERE the Face_Detector_Backend is the default `haar` AND the low-confidence condition is not met, THE Clipper SHALL produce the same rendered output as v0.11.0 for the same input.
2. WHERE the Face_Detector_Backend is the default `haar` AND the low-confidence condition is not met, THE Clipper SHALL produce the same `effects_applied` set as v0.11.0 for the same input.
3. THE Clipper SHALL NOT change the geometry stage's ffmpeg pass count.
4. THE Clipper SHALL NOT change the shape of the geometry stage's filter graph or `sendcmd` script for a given crop path.
5. THE Clipper SHALL keep every existing reframe-related Effects_Applied marker spelled exactly as it is today.

### Requirement 10: Options, config, and UI surface

**User Story:** As a creator, I want the detector choice available where every other setting lives, so that it is discoverable and persists in a profile.

#### Acceptance Criteria

1. THE Processing_Options SHALL carry the Face_Detector_Backend selection.
2. THE Processing_Options SHALL round-trip the Face_Detector_Backend selection through serialisation without loss.
3. THE Clipper SHALL surface the Face_Detector_Backend selection through the API options model, the upload form fields, and the settings UI.
4. THE Clipper SHALL define the Coverage_Floor and the MediaPipe minimum Detection_Score as configuration settings.
5. FOR every configuration setting this feature adds, THE Clipper SHALL provide a matching documented entry in `.env.example`.
6. THE Clipper SHALL keep every pre-existing Processing_Options field and default unchanged.

### Requirement 11: Verification against the real library

**User Story:** As a maintainer, I want the MediaPipe geometry conversion tested against MediaPipe itself, so that a coordinate-system mistake cannot pass a suite of mocks.

#### Acceptance Criteria

1. THE Clipper SHALL include a test that constructs the `mediapipe` backend and runs it against a real image, without mocking MediaPipe.
2. THE Clipper SHALL include a test asserting that a Detection returned by the real `mediapipe` backend is expressed in absolute pixels and lies within the frame bounds.
3. THE Clipper SHALL include a test asserting that the resolved backend recorded in Effects_Applied is the one that ran, for both the default and the opt-in backend.
4. THE Clipper SHALL NOT introduce any test that is skipped when its dependencies are present.
5. THE Clipper SHALL NOT introduce any new warning into the test run.


---

## Group D — Model vendoring

### Requirement 12: The detector model is vendored, licensed, and verified offline

**User Story:** As an operator, I want the detector's model committed alongside the code, so that a clip renders the same way on any host and a test never needs the network.

#### Acceptance Criteria

1. THE Clipper SHALL store the Vendored_Model inside the repository under `assets/models/`.
2. THE Clipper SHALL store the Vendored_Model's licence text alongside it, in the manner `assets/font-licenses/` already establishes for fonts.
3. THE Clipper SHALL provide a Model_Manifest recording, for each Vendored_Model, its filename, SHA-256 digest, source URL, licence identifier, and the Face_Detector_Backend it serves.
4. THE Clipper SHALL provide a verification mode that checks every Vendored_Model against the Model_Manifest using only the working tree, performing no network access.
5. WHEN the verification mode finds a missing, truncated, or digest-mismatched Vendored_Model, THE Clipper SHALL exit non-zero and SHALL name the offending file.
6. THE Clipper SHALL invoke the verification mode in CI.
7. THE Clipper SHALL include the Vendored_Model in the built container image.
8. THE Clipper SHALL assert the Vendored_Model's presence in the container smoke test, through the running application rather than by inspecting the filesystem alone.
9. THE Clipper SHALL resolve the Vendored_Model's location from a configurable setting, defaulting to the in-repository path.
10. THE Clipper SHALL NOT fetch, cache, or write any model file at render time.
11. WHERE a fetch helper is provided to obtain the Vendored_Model for a maintainer, THE Clipper SHALL keep it separate from the render path and SHALL NOT invoke it automatically.

### Requirement 13: Detector dependency pinning reflects the API actually used

**User Story:** As a maintainer, I want the dependency pin to match the API the code calls, so that a resolver upgrade cannot silently remove the namespace the detector depends on.

#### Acceptance Criteria

1. THE Clipper SHALL constrain the `mediapipe` dependency to a range whose every member exposes `mediapipe.tasks.python.vision.FaceDetector`.
2. THE Clipper SHALL document, at the pin, that the `mediapipe.solutions` namespace was removed and MUST NOT be depended upon.
3. THE Clipper SHALL include a test asserting that the installed `mediapipe` exposes the API the detector calls.
4. THE Clipper SHALL NOT rely on `mediapipe` supplying any model file.
