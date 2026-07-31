# Face detection upgrade — close-out report

Branch `spec/face-detection-upgrade`, PR #83. All 52 tasks in `tasks.md` are complete;
nothing was deferred.

This file exists because the sandbox's git remote is a push-only gateway proxy — there is
no authenticated API path from here to edit a pull request body. It is the report that
task 7.4 asks to be recorded "in the PR"; paste it into the PR description if you want it
there verbatim.

## Result

`haar` remains the default. Every new setting defaults to the previously shipped
behaviour, so the parity goldens still detect accidental change.

Gates, all green:

| gate | result |
| --- | --- |
| `ruff check .` | clean |
| `pytest` | **1994 passed, 0 skipped, 0 warnings** (baseline 1911) |
| `scripts/fetch_models.py --check` | clean |
| `scripts/fetch_emoji.py --check` | clean, 326 emoji |
| `scripts/mutate.py --spec tests/mutations/face_detection.json` | 23 caught, 0 escaped, 1 declared equivalent, 0 stale |
| frontend `lint` / `test:run` / `build` | clean, 98 tests |
| `npm audit --audit-level=critical` | exit 0 |
| `scripts/docker_smoke.sh` | exit 0 |

## Measured detection coverage (task 7.4)

First quantitative statement about this subsystem. Same synthetic three-act source
(`storage/temp/faces_src.mp4`), 35 samples at `sample_fps` 5.0, uncapped:

| backend | overall | act 1 frontal | act 2 profile turn | act 3 two-shot |
| --- | --- | --- | --- | --- |
| `haar` | 0.886 | 1.00 | **0.60** | 1.00 |
| `mediapipe` | 0.971 | 1.00 | **0.90** | 1.00 |

The profile turn is the whole story: it is where `haar` drops a third of its frames, and
where a viewer sees the crop jump back to centre. `haar` also averages 1.32 faces per
detecting frame against mediapipe's 1.00, i.e. it reports phantom faces on the two-shot,
which is the other half of the framing instability.

**Caveat, stated plainly:** these faces are drawn, not photographed. Read the numbers as a
floor-setting aid for `reframe_coverage_floor` and as a relative ranking, not as
real-world accuracy.

## Task 3.0 — the measured MediaPipe API answer

The design document assumed a *relative* bounding box. Measured against the pinned
`mediapipe` 0.10.35 tasks API before any code was written:

- `detection.bounding_box` carries **absolute pixels as `int`** — `origin_x=225
  origin_y=165 width=191 height=191` on a 640×480 frame.
- `relative_bounding_box` **does not exist** on that object.
- The score lives at `detection.categories[0].score`.
- `dir(mediapipe)` is `['Image', 'ImageFormat', 'tasks']`.

`relative_box_to_pixels` was still written dual-mode (all four values ≤ 1.0 → scale;
otherwise clamp only) because this library has already moved this API once. Operation
order is fixed: convert → clamp → *then* test degeneracy, so a box that clamps to
sub-pixel width is rejected rather than rounded to zero.

## Model

Vendored rather than downloaded at runtime, per the spec.

- `assets/models/blaze_face_short_range.tflite`, 229 746 bytes
- sha256 `b4578f35940bf5a1a655214a1cce5cab13eba73c1297cd78e1a04c2380b0152f`
- Apache-2.0, `assets/models/LICENSE-blazeface.txt`. Licence confirmed from the installed
  wheel: `mediapipe-0.10.35.dist-info/licenses/LICENSE` plus `License: Apache 2.0` in the
  wheel metadata. The YuNet fallback was not needed.
- Source: `https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite`

The manifest lives in `worker/face_models.py` and `scripts/fetch_models.py` imports it, so
there is one definition of the digest rather than a copy in `scripts/`. Per clip,
`resolve_model` checks size only; the full sha256 is CI's job via `--check`.

## Defect found on the way

`_as_detection` now reads `x`/`y`/`w`/`h` attributes *before* falling back to tuple
unpacking. `FaceBox` has a leading `t` field, so unpacking it positionally shifted every
coordinate — which meant **coverage on the speaker-reframe path was always 0.0**. That was
pre-existing and silent.

## Mutation testing (task 7.3)

`tests/mutations/face_detection.json`, 24 mutations: 23 caught, 0 escaped.

Two mutations were **fixed rather than declared equivalent**:

- An unreachable post-`int` `box_w <= 0` guard that silently rescued
  `test_degeneracy_before_clamping`. Removed; the float check already covers it.
- A `len(items) == 1` fast path in `pick_main_face` that `max` already handles
  identically — an unobservable second statement. Removed.

One mutation is **declared equivalent**, `accept_non_finite_values`: the clamp incidentally
neutralises NaN and ±inf, because NaN comparisons are always False, so `min(width, nan)`
returns `width` and the box collapses. The explicit guard stays anyway — correctness should
not rest on the operand order of `min`/`max`.

Selection uses `score * sqrt(area)`, not `score * area`. Area-weighting makes confidence
nearly irrelevant: a 400×400 box at 0.10 confidence would beat an 80×80 box at 0.95 by 2:1.
My own test caught that.

## Task 7.2 — eyeballing the real output, and its limitation

`compositor.render_clip` **does not run the geometry stage** (zero references to
`apply_reframe`), so `smoke_reel.py --face-detector` is inert for the reel it produces. The
flag is still wired for when that changes, but the real backend comparison had to be driven
through `rf.apply_reframe(..., backend=..., notes=...)` directly, which is what
`smoke_reel.py`'s coverage-report mode does.

Artefacts: `storage/temp/reel_haar.mp4`, `reel_mediapipe.mp4`, `reframe_haar.mp4`,
`reframe_mediapipe.mp4`, `faces_src.mp4`. The two `reframe_*.mp4` files are the pair worth
watching.

## Task 0 — no goldens were updated, and why

No golden or parity fixture pins `effects_applied`, so the new markers break nothing and
Requirement 3 is preserved: **the marker is never withheld**, including on the default
backend. Withholding it on `haar` to keep output byte-identical was considered and rejected
— it would weaken Requirement 3. Evidence:

- `evaluation/golden_render.py` compares frame hashes and luma only.
- `tests/test_stems_parity.py` compares before/after *within one run*.
- The three frozen `notes == [...]` assertions (`test_reframe_geometry.py:511`,
  `test_speaker_reframe.py:397,448`) are on `build_reframe_filter`, a pure filter builder
  that performs no detection.
- The `test_engines_base.py` marker tuples are one-directional: they assert existing
  markers are still present, not that the set is closed.

## Other decisions that look odd without context

- **Markers ride on a `notes` out-parameter** of `apply_reframe` / `apply_speaker_reframe`,
  appended only *after* the render succeeds, at all three exit points. Changing the return
  type from `Path` would have touched 48 tests.
- **`resolve_detector` returns `(detector, label)`** with the label taken from the branch
  that succeeded; `close` is attached as an attribute on the callable so the tuple stays a
  2-tuple. An injected detector labels as `"injected"`. All four MediaPipe failure causes
  share the marker `"substituted:mediapipe:haar"`, and the log names the specific cause.
- **`apply_reframe` now calls `track_faces_report`**, so two existing tests that patched
  `rf.track_faces` were re-pointed. Requirement 5.5 forbids a second sampling pass, so
  sampling twice was not an option.
- **`_sample_face_boxes` keeps its signature**; `sample_face_report` is a sibling.
- **`capped` uses a 0.05 tolerance**, not a bare `<`.
- **The `face_detector` domain is duplicated** as a literal `ProcessingOptions._FACE_DETECTORS`
  to avoid dragging `reframe`/`config` into the API layer, and pinned by
  `test_the_option_domain_matches_the_detector_modules_domain`.
- **`/api/info` advertises `face_detectors` as `{name, available}`** objects rather than
  bare strings, so `docker_smoke.sh` can assert real usability through the API inside the
  built image.
- **No `filterwarnings` entry was added** (task 3.3). Verified under `-W error` that
  importing mediapipe in a new place raises no Python warning; the absl/XNNPACK noise is
  C++ stderr.
- **Task 5.4 was verified twice**, before and after adding the option:
  `assert_effects_off_is_exhaustive()` passes without a `face_detector` entry.

## Test-suite bugs fixed in passing

- A `cv2.VideoCapture` subclass crashed the interpreter at teardown *after* reporting
  "16 passed". Replaced with a plain stand-in.
- A leaked `open()` produced `PytestUnraisableExceptionWarning`.
- Unclosed real MediaPipe detectors raised `TypeError: 'NoneType' object is not callable`
  at shutdown. Tests now release them.

## Out of scope, per the spec

Not attempted: V3 active-speaker detection, V7 body detection, vertical reframing for 9:16,
automatic split-screen layout selection.

## Environment note

The sandbox was recycled mid-run and lost the static ffmpeg, libGL, and node from `PATH`.
Two `test_publishers.py` Whop failures were caused entirely by that and passed again after
`bash scripts/setup_dev_env.sh`. No code was involved.
