# Session Handoff

Rewritten after the `audio-stem-inpainting` spec completed. The previous version of this file
described a session that ended mid-epic and is no longer accurate in any part; it has been
replaced rather than amended.

## 1. Status

**All five specs are complete — 388/388 tasks, nothing open.**

| Spec | Tasks |
| --- | --- |
| `av-engines-foundation` | 91/91 |
| `kinetic-typography` | 76/76 |
| `tier1-creator-output-upgrade` | 69/69 |
| `speaker-diarization-reframe` | 66/66 |
| `audio-stem-inpainting` | 86/86 |

Two AV engines are registered and advertised by `/api/info`, both **default-off**:
`kinetic_typography` (COMPOSE) and `stem_inpainting` (AUDIO).

## 2. Open PR

| PR | Contents |
| --- | --- |
| [#42](https://github.com/skullmanhell1/Clipping-Tool/pull/42) | `audio-stem-inpainting` epics 9-20, plus two approved foundation changes. Its description understates the contents — it was written when the branch only held epics 9-11. |

## 3. Test baselines

Run with `PYENV_VERSION=3.11.15 python3 -m pytest tests/ -q`.

| Environment | Result |
| --- | --- |
| Full `requirements-dev.txt` + ffmpeg + fonts (what CI has) | **598 passed, 0 failed, 0 skipped** |
| The same, plus `libGL` so `cv2`/`mediapipe` import (what Docker has) | **598 passed, 0 failed, 0 skipped** |
| No ffmpeg on `PATH` (a bare developer checkout) | **509 passed, 89 skipped, 0 failed** |

The 89 skips are all the `requires_ffmpeg` gate. **A skip is not a pass** — anything touching a
filtergraph needs ffmpeg on `PATH` before you can claim it works. There is no
`/projects/sandbox/.ffbin` in this repo; get a static build and put it on `PATH`.

## 4. Superseded decisions

These were carried by the previous handoff and are now **resolved**; do not act on them again.

* **(a) Widen the host media gate — DONE.** `worker/engines/host.py` now admits `degraded` as
  well as `applied` via `_MEDIA_BEARING_STATUSES`. Req 8.3 still holds because it is carried by
  `media is None`, not by status. No Pipeline change was needed.
* **(b) Additive `notes` parameter — DONE.** `Engine_Host.run_stage(notes=...)`, threaded into
  `_build_context` and appended after the host's own synthesised notes. Both contract pins were
  updated to match.
* **(c) A dedicated bugfix spec for kinetic P12 — NOT RECOMMENDED, see below.**

## 5. Kinetic P12: could not be reproduced

The previous handoff described `test_p12_malformed_timings_degrade_instead_of_raising` as a
known-red defect and asked for its own bugfix spec. **It does not reproduce.**

Evidence:

* 4000 fresh Hypothesis examples with the example database disabled — clean.
* A deterministic sweep of 1020 cases (5 degenerate timeline shapes x 17 fps values, including
  the `14.0` the old analysis named, x 3 sample rates x 4 durations) — clean.
* The predicate the old analysis blamed (`later_start > cue_start`, `worker/engines/kinetic.py`
  ~line 1639) is **still present**, so this is not a silent fix — the diagnosis appears to have
  been wrong.

Also note the old handoff attributed the failure to ffmpeg being on `PATH`. That is definitely
wrong: P12 is a pure planner property with no ffmpeg dependency. The likeliest explanation for
the original red run is a stale counterexample in a local `.hypothesis` database.

**Recommendation:** keep the property test as the guard, do not open the spec. Absence of a
counterexample is not proof of absence, so if it ever reappears, capture the counterexample
*and* the generator versions before analysing.

## 6. What is actually left

No spec work. Everything below is unplanned.

### Small and mechanical
* No `VERSION`/`CHANGELOG` entry existed for the stem engine — addressed in this pass (0.9.0).
* `README.md` claimed a Redis + RQ queue that does not exist — addressed in this pass.

### Real product gaps, highest impact first
1. **`review_required` is a dead end.** Every publisher can return it, but there is no
   approve/retry/resume endpoint and `PublishManager` only picks up `queued`/`scheduled`. The
   README describes an approval flow with no server-side path.
2. **Job state is in-memory only.** `JobStore` is a dict; history/campaigns/profiles are
   durable. After a restart `/api/history` lists clips whose ZIP download 404s and whose
   `PATCH` silently updates 0 rows.
3. **No ffmpeg invocation outside the stem engine carries a timeout.** `worker/ffmpeg_utils._run`
   passes none, so a wedged ffmpeg hangs a job thread forever. The stem engine threads an
   explicit timeout through every call; the rest of the pipeline does not.
4. **`allow_origins=["*"]` with `allow_credentials=True`** (`api/main.py`) is a combination
   browsers reject outright.
5. **Diarisation invents speakers.** `segment_by_words` labels speech runs **round-robin**, so a
   monologue with pauses over `diarization_pause_gap` (0.9 s) alternates `S1`/`S2` — and that
   drives visible `follow_active`/`split_screen` framing changes.
6. `merge_scores` hard-codes `weight=0.5` with at most 12 keyframes per source, so most
   candidates score `visual_score = 0` and brightness dominates the ranking.
7. Uploads have no size/type validation, and `shutil.copyfileobj` runs synchronously inside an
   `async def`, blocking the event loop.
8. Per-platform `min_interval_seconds` are dead — `max(..., publish_default_interval_seconds)`
   with a 30 s default overrides every one of them.
9. `visual_selection.sample_keyframes` leaks its `mkdtemp` directory; `disk_usage()` walks every
   file on every `/api/storage` poll.

### Test / CI infrastructure
* **CI installs `opencv-python` but not `libgl1`**, so `import cv2` fails there with
  `libGL.so.1: cannot open shared object file`. The Dockerfile installs it (with a comment
  saying opencv needs it); the workflow does not. Addressed in this pass. Note the suite passes
  either way, because the vision tests inject their detectors — so this was costing CI
  *coverage*, not correctness.
* The frontend has no tests of any kind, and `npm run lint` fails: `eslint` is scripted but not
  in `devDependencies`.
* The `deploy` job uses `secrets` inside step-level `if:` expressions, which does not evaluate
  as intended.
* There is no pytest configuration at all — no registered markers, no coverage gate.
