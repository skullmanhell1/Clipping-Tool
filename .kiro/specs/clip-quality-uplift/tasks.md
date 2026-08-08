# Implementation Plan — Clip Quality Uplift

These are incremental, test-first coding steps. Execute them **one task at a time**, in
order — each builds on the previous ones so there is never orphaned code.

The plan is ordered by a hard dependency, not by convenience. **Group 1 is a gate.** It
adds no production code: it adds the Benchmark_Dataset and the committed Baseline_Report.
Nothing in Groups 3–4 (selection ranking) may begin until Group 1's report is committed,
because `.kiro/steering/working-agreement.md` states that clip-selection quality work
without the harness cannot be judged — and the harness has a dataset-shaped hole in it.

Within the code groups, **pure arithmetic lands before any wiring**: pitch estimation,
reaction proxying, blending, span snapping, span hygiene, and silence planning are all
pure and testable with no ffmpeg, no Whisper, and no network. The real-program
verification comes before pipeline integration in each group, so a units or
coordinate-system mistake is caught before it can reach a rendered clip.

**Default discipline.** Every new setting defaults to previously shipped behaviour, with
exactly two deliberate exceptions — the detector default (task 8) and interior silence
removal (task 9) — each of which lands in its **own commit with the re-frozen fixtures and
a message naming what moved**. At every other point in this plan, an unchanged
configuration reproduces v0.11.0 output.

Tasks marked with `*` are optional test sub-tasks (unit / property / integration).
Property tests use `hypothesis` with `@settings(max_examples=100)`, one property per test,
tagged `# Feature: clip-quality-uplift, Property N: <text>`, in the files named in the
design's Testing Strategy. ffmpeg and audio integration tests reuse the existing helpers
(`make_video`, `requires_ffmpeg`, `probe_size`, `FakeWord`, `FakeFaceDetector`).

**Before starting, run the baseline and record it:** `pytest` must report
**2619 passed, 0 skipped, 0 warnings**; `cd frontend && npm run test:run` must report
**141 passed**. A drop at any point means something stopped running, and a skip is not a
pass.

> The figure above was **2030** until it was corrected against `d309f36` plus the caption-timing
> wiring change. Anyone who ran the stated baseline and saw 2596 would have concluded the instruction
> was untrustworthy and stopped reading, which is what a stale number in a "before you start" step
> actually costs.

## Status — measured, because the checkboxes below were never maintained

**Every box in this file is unticked and a lot of it is done.** The list below was derived by looking
for each task's artefacts in the tree, not by reading the checkboxes. Where the two disagree, this
section is right.

| Group | State | Evidence |
| --- | --- | --- |
| 1 — the benchmark gate (S1/M4) | **not started** | `eval/labels/` holds only `.gitkeep`. Still gates groups 3.6, 4, 5.4, 8, 10.5. |
| 2.1, 2.2 — pitch | **done** | `worker/pitch_features.py`: `f0_track`, `source_median_f0`, `pitch_in_window`, `describe`. |
| 2.3 — reaction proxy | **not started** | No `worker/reaction_features.py`. Pure DSP and needs no checkpoint, so it is buildable now. |
| 2.4, 2.5 — pitch tests | **done** | `tests/test_pitch_features.py`, 20 tests. |
| 3.1, 3.2, 3.4 — pitch wiring | **done** | `selection.py` memoises one track per source and calls `pitch_features.annotate_candidates`, behind `selection_pitch_feature`. |
| 3.3, 3.6 — reaction wiring/default | **blocked** | Follows 2.3, then group 1. |
| 4 — blending (S18) | **not started** | No `blend_scores`, no `selection_opinion_weight`. Task 4.6 ships inert at `1.0`, so the mechanism could land before group 1; the *default* cannot. |
| 5 — hook disqualification (S19) | **not started** | `hook_score.py` still zeroes on `promptness <= 0.0` alone. |
| 6.1, 6.5, 6.6 — span hygiene (C23) | **done** | `worker/word_spans.py`, wired into `build_ass` and `plan_kinetic`; `MIN_WORD_SPAN_SECONDS`; `word_spans_repaired:N`. |
| 6.2, 6.3, 6.4, 6.8 — onset snapping (T11) | **refused, measured** | The cached envelope is 1 reading/second; on a source with 20 bursts at 2.5/s `detect_onsets` found **zero**. R7.8 forbids the finer pass that would fix it, so the requirement contradicts itself. Reasoning in `word_spans.py`'s docstring. |
| 7 — intermediate fidelity (O6) | **not started** | No `x264_crf_intermediate`. Now *measurable*: `evaluation/fidelity.py` (M9) exists, so 7.5 is answerable. |
| 8 — detector default | **blocked** | Needs group 1's footage. Note `face_detector_backend` is currently read by nothing, so the setting does not work either — see `scripts/check_wired.py`. |
| 9 — interior dead air (AU10) | **not started** | Buildable now; `plan_keep_intervals` already merges multiple keep sources into one re-encode. |
| 10 — end snapping (S20) | **not started** | `snap_end` absent. Gate on `mean_best_iou` per 10.5. |
| 11 — assets (A14/A21) | **partial** | `assets/music/` and `assets/broll/` exist; no `scripts/fetch_music.py`. Licensing is the real work. |
| 12 — configuration contract | **ongoing** | Enforced continuously by `tests/test_config_documentation.py`. Note it proves a setting is *documented*, not *read*. |
| 13 — make the plan true (M8) | **partial** | Appendix B now carries a measured status section; the body is still quoted against v0.10.0. |
| 14 — close-out | **open** | Depends on group 1. |

**Group 6 carries a lesson worth more than the group.** C23 landed complete and tested and was called
by nothing for as long as it existed — as were C24 and C25 from the sibling spec. Task 6.5 says "apply
hygiene on every caption path"; that step is what was missed, and no test could see it. There is now a
gate: `scripts/check_wired.py`, run in CI and by `tests/test_check_wired.py`.

## Tasks

- [ ] 1. THE GATE — build the benchmark and measure what we have (S1/M4)
  - [ ] 1.1 Write the labelling protocol document
    - Create `eval/LABELLING.md` recording the instruction from the design verbatim: mark spans you would actually post, watching at normal speed once, marking the span you would post rather than the span containing the good bit. Record per-source provenance requirements and how a second labeller extends the set consistently.
    - Do **not** extend `evaluation/dataset.py`'s format. Its docstring already argues down to `start`/`end`/`note` and explicitly refuses per-moment rank and anything derivable; that reasoning stands.
    - _Requirements: 1.7_

  - [ ] 1.2 Label at least 15 sources
    - One JSON file per source in `eval/labels/`, ≥ 2 moments each, spanning at minimum single-speaker, two-person conversational, and audience-reaction footage. Media is **not** committed (R1.5); `source` may be relative so the dataset stays portable.
    - Verify every file loads via `Dataset` without `DatasetError` and that no moment has `end <= start`.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.8_

  - [ ] 1.3 Confirm absent media degrades rather than aborts
    - Run the harness with one source's media deliberately missing. `run_selector` records errors per source rather than aborting — confirm that, and confirm the aggregate is computed over the sources that did run.
    - _Requirements: 1.6_

  - [ ] 1.4 Run and commit the Baseline_Report
    - `python scripts/eval_selection.py validate` then `run`, covering the Selector and the `uniform`, `random`, and `longest` baselines. Commit as `eval/baseline-v0.11.0.json` with the model identity, configuration, and code revision recorded in it.
    - Record the Primary_Metric (F1 @ IoU 0.5, pooled) **and** the Boundary_Metric (`mean_best_iou`) for every path. The second is what distinguishes "right moment, bad boundaries" from "wrong place" and it is the metric tasks 10 and 6 are judged on.
    - _Requirements: 2.1, 2.2, 2.3, 2.6_

  - [ ] 1.5 Record what is and is not reproducible
    - The `ai` path is a network call with a sampling temperature and is **not** reproducible; `uniform` and `longest` are deterministic and `random` is seeded. State this in the report, state the run count for the LLM path, and state that single-run `compare` output for that path must not be read as a regression signal.
    - Confirm re-running on an unchanged transcript cache reproduces the deterministic paths byte-identically.
    - _Requirements: 2.4, 2.5_

  - [ ] 1.6 State plainly whether we beat `longest`
    - Write the answer into the report and into the PR description. If a weighted mean of seven heuristics cannot beat the longest silence-delimited segment, the conclusion is that the approach is wrong — not that the weights need another pass. Do not soften this.
    - Do **not** add a CI gate on an absolute F1 threshold: on 15 sources it would either never fire or block unrelated work, and it would tempt someone to add sources until it passes.
    - _Requirements: 2.7, 2.8_

- [ ] 2. Pure measurement — pitch and reaction (no ffmpeg, no network, no checkpoints)
  - [ ] 2.1 Add `worker/pitch_features.py` with a pure autocorrelation F0 estimator
    - Frame-wise F0 over a mono sample buffer, returning `(time, f0_hz)` for **voiced frames only**. An unvoiced frame is excluded, never recorded as 0 Hz — recording it as zero would make silence read as enormous pitch variation.
    - Deliberately **not** `librosa`: it drags `numba`/`scipy` weight, and with `filterwarnings = error` every deprecation it emits becomes a suite failure that is not ours to fix. A bounded autocorrelation estimator is ~60 lines, ours, and deterministic across platforms.
    - _Requirements: 4.3, 4.5, 4.8_

  - [ ] 2.2 Add relative Pitch_Variation windowing
    - `source_median_variation(track)` and `variation_in_window(track, start, end)` returning a value plus a `reliable` flag, mirroring `audio_features.energy_in_window` / `source_median_energy` exactly — same shape, same vocabulary, so there is one definition of "departure from this speaker's norm".
    - Relative to the source's own median (R4.2) so a bass voice and a high voice are not scored differently for being themselves. Mark unreliable where the window has too few voiced frames.
    - _Requirements: 4.1, 4.2, 4.5_

  - [ ] 2.3 Add `worker/reaction_features.py` — the laughter/applause proxy
    - Detect Reaction_Events from broadband, high-spectral-flatness, fast-attack, sustained bursts that carry no F0 — combining the existing envelope with the new pitch track. Confidence in `[0.0, 1.0]`.
    - Name every public symbol so it cannot be mistaken for a trained classifier. The precedent is `music_degraded:synthesised`: a proxy is fine, a proxy labelled as a classifier is the `A15` defect in a new place.
    - _Requirements: 5.1, 5.5, 5.4_

  - [ ] 2.4* Property test: pitch is relative and deterministic → `tests/test_pitch_features.py`
    - **Property 1** — the relative measure is invariant when every F0 in the track is scaled by a constant factor (a higher-voiced speaker saying the same thing scores the same).
    - **Property 2** — identical input produces identical output, across repeated calls.
    - _Requirements: 4.2, 4.8_ · _Properties: P1, P2_

  - [ ] 2.5* Real-audio test: the estimator finds a known F0 → `tests/test_pitch_features.py`
    - Generate real audio with ffmpeg's `sine` source at several known frequencies. Assert the estimate is within tolerance. **Cross-check independently**: derive the expected F0 from the generator's own parameters, sharing no code with the estimator — a cross-check that reuses the code under test verifies only self-consistency.
    - Not guarded by an availability skip: ffmpeg is a hard dependency and a skip here would mean it vanished, which is what the no-skips rule exists to surface.
    - _Requirements: 17.1, 17.6, 17.7_

  - [ ] 2.6* Real-audio test: the reaction proxy separates noise bursts from speech → `tests/test_reaction_features.py`
    - Real synthesised noise bursts versus a real speech-like signal. Assert separation. **Report** the false-positive behaviour in the PR rather than asserting a rate — an asserted rate on synthetic input is a number about the fixture, not about the feature.
    - _Requirements: 5.6, 17.7_

- [ ] 3. Wire the new features into candidates (still no ranking change)
  - [ ] 3.1 Compute the pitch track once per source
    - One additional audio pass per job, maximum (R4.7), following `audio_features.energy_envelope`'s single-pass shape. Memoise through `intermediate_cache.memoise` as the silence detection already does.
    - _Requirements: 4.7_

  - [ ] 3.2 Attach Pitch_Variation and the reaction measure to `ClipCandidate.features`
    - Annotators only — **do not touch `score`** in this task. `worker/candidate_ranking` remains the only place a measurement becomes a score, and task 4 is where that changes.
    - _Requirements: 4.4, 5.2_

  - [ ] 3.3 Add the proxy marker to Effects_Applied
    - Record that reaction detection ran by proxy rather than by a trained classifier, naming what actually ran (R14.6).
    - _Requirements: 5.3, 5.4, 14.5, 14.6_

  - [ ] 3.4 Add pitch to the LLM delivery annotations
    - Extend `selection._segment_annotation` with a qualitative pitch tag (`animated` / `flat`), gated by the existing `selection_features_in_prompt`. **Words, not numbers**, and only *departures* from the speaker's norm — that existing design decision is right, and a stray float in a prompt invites the model to do arithmetic it cannot do.
    - _Requirements: 4.9_

  - [ ] 3.5* Unit tests: features are attached and the prompt is annotated → `tests/test_selection.py`
    - Assert both new features appear in `features` on both selection paths; assert `score` is unchanged by this task; assert the prompt gains a pitch tag only when the setting is on and only for departures.
    - _Requirements: 4.4, 5.2, 4.9_

  - [ ] 3.6 Decide the reaction default by measurement, once the benchmark exists
    - Enable reaction detection by default **only** if the Baseline_Report shows it does not reduce the Primary_Metric. This is the first feature in this project whose default is decided by the benchmark rather than by judgement, which is the entire point of Group 1 — do not shortcut it.
    - Report the measured false-positive behaviour on the benchmark footage. Overlapping speech, room noise, and music stings will trigger the proxy; the number is the deliverable, not a clean bill of health.
    - _Requirements: 5.6, 5.7_

- [ ] 4. Blending — the ranking finally uses the measurements (S18)
  **Do not start until task 1.4 is committed.**
  - [ ] 4.1 Add `blend_scores` to `worker/candidate_ranking.py`
    - `blend_scores(opinion, measured, *, opinion_weight) -> float`. **Branch explicitly on `opinion_weight == 1.0` and return `opinion` unchanged** — do not write `w*o + (1-w)*m*100` and rely on float luck at the boundary, because that default is what the golden fixtures depend on.
    - Reconcile the units here, once: `opinion` is 0–100, `measured` is 0–1. A units mismatch produces a plausible ordering that is wrong, which is the hardest defect class in this codebase to see.
    - _Requirements: 3.1, 3.2, 3.3_

  - [ ] 4.2 Source the measured half from `score_candidate`, not a new sum
    - Call the existing `score_candidate` for the measured component so the seven `selection_weight_*` settings become live on the LLM path, and add pitch and reaction as two more weighted components rather than as special cases.
    - **Do not write a fresh weighted sum over `c.features` in `selection.py`.** That would create a second definition of "how good does this look", and the two would drift the first time anyone tuned one of them.
    - _Requirements: 3.10_

  - [ ] 4.3 Treat unreliable features as neutral
    - An unmeasured or unreliable feature contributes the neutral 0.5 that `score_candidate` already uses, and never a penalty. A candidate must not be punished for audio the estimator could not read.
    - _Requirements: 3.5, 4.6_

  - [ ] 4.4 Replace the sort key, preserving `LLM_Opinion` on the record
    - `worker/selection.py:466` sorts on `c.score`. Compute the Blended_Score, sort on it, and keep `LLM_Opinion` on the candidate unmodified so the two are separately inspectable. The UI's displayed number stays the model's; the ordering becomes ours.
    - _Requirements: 3.1, 3.6, 3.7_

  - [ ] 4.5 Blend **before** deduplication and before the count cap
    - `deduplicate` is greedy over the score-sorted list and keeps the highest-scored member of an overlapping set. Blending after it would let dedup pick its survivors on the unblended order, and the blend would only reorder the leftovers. This is a silent defect: the output is still a sensible list of clips.
    - _Requirements: 3.8_

  - [ ] 4.6 Add `selection_opinion_weight` defaulting to `1.0`
    - `1.0` reproduces v0.11.0 ordering exactly (R3.4). The code lands inert; the value changes only by a measured decision in task 4.9.
    - _Requirements: 3.2, 3.4, 15.1, 15.2_

  - [ ] 4.7* Property tests: blending → `tests/test_candidate_ranking.py`
    - **Property 3** — `opinion_weight == 1.0` returns the opinion bit-identically for any inputs.
    - **Property 4** — the blend is monotonic non-decreasing in both `opinion` and `measured`.
    - _Requirements: 3.1, 3.3_ · _Properties: P3, P4_

  - [ ] 4.8* Unit test: dedup observes the blended order → `tests/test_selection.py`
    - Construct a case where blended and unblended orders select **different** survivors from an overlapping set, and assert the blended survivor wins. A test where both orders agree proves nothing about the ordering of the two stages.
    - _Requirements: 3.8_

  - [ ] 4.9 Choose the weight by measurement, and re-run the report
    - Sweep `selection_opinion_weight` against the Benchmark_Dataset. Adopt a new default **only** if it improves the Primary_Metric; record the swept figures and the weight used in the report either way. A null result is a result and gets written down.
    - _Requirements: 3.4, 3.9, 2.2_

- [ ] 5. Narrow the hook disqualification (S19)
  - [ ] 5.1 Make the zero conditional on the absence of *any* opening signal
    - `worker/hook_score.py:193` zeroes the score whenever `promptness <= 0.0`. The comment is right — dead air has no hook — and the rule is too broad by exactly one case: a clip opening on a laugh or a hard visual accent is not dead air, but `speech_promptness` only looks at word starts.
    - Disqualify only when there is no speech **and** no Reaction_Event **and** no Onset in the hook window. Keep the components reported separately so the cause of a zero stays inspectable.
    - _Requirements: 6.1, 6.2, 6.6_

  - [ ] 5.2 Keep the old rule reachable and default
    - A setting selects the strict rule, defaulting to v0.11.0 behaviour. Do **not** change `SPEECH_DEADLINE_S` or `HOOK_WINDOW_S` — they are calibrated against the rest of the hook arithmetic and moving them would confound this measurement.
    - _Requirements: 6.3, 6.4, 6.5, 15.1_

  - [ ] 5.3* Unit tests: the narrowed predicate → `tests/test_hook_score.py`
    - Zero retained for a genuinely empty window; **not** zero when a reaction or onset is present but speech is late; default configuration reproduces v0.11.0 for every existing fixture.
    - _Requirements: 6.1, 6.2, 6.4_

  - [ ] 5.4 Measure it, then decide the default
    - Re-run the benchmark. Flip the default only if the Primary_Metric does not fall.
    - _Requirements: 6.4_

- [ ] 6. Caption timing — snapping and hygiene (T11, C23)
  - [ ] 6.1 Add `span_hygiene` first, as a pure repair
    - Enforce monotonic non-decreasing starts, non-overlap, and a configurable minimum duration. **When extending to reach the minimum would overlap the next span, non-overlap wins** — overlapping `\kf` spans produce visibly wrong karaoke (two words lit, or a highlight jumping backwards), while a short span is merely fast.
    - Never extend a span past its containing cue's end. **A compliant sequence must be returned bit-identical**, which is what allows unconditional application on every path without moving any golden.
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.7_

  - [ ] 6.2 Add `snap_word_spans(words, onsets, *, max_shift_s)`
    - Starts only — the following word's start already defines where a word stops for rendering, and moving both ends independently is how you get crossing spans that hygiene then has to repair, hiding the cause.
    - Never move a start onto a time with no detected Onset (R7.3), so the worst case is "not moved", never "moved somewhere invented". Preserve order **by construction** — reject a candidate onset that would place this start at or before the previous snapped start; do not sort afterwards, which would silently reorder the transcript.
    - Return `(words, moved_count)`.
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_

  - [ ] 6.3 Reuse the existing envelope; do not add an audio pass
    - Take onsets from `audio_features.detect_onsets` over the envelope already computed for the source. Its guarantee — every returned time has a real transient at it — is precisely what makes snapping safe, and is why this must not become beat tracking, which would happily place a beat in silence.
    - _Requirements: 7.8_

  - [ ] 6.4 Apply as a rendering transform, in the right place in the order
    - Snap **before** `words_to_cues` so cue grouping sees corrected spans (R7.6). Apply to the spans handed to captions and **never write back to the cached transcript** — `T3` filtering already establishes that the cache stays raw ASR, and snapping into it would poison the selection prompt, the WER harness, the transcript editor endpoint, and the SRT sidecars with times derived from an effect.
    - Snap **before** `filler.rebase_words`, or map the onsets through the same keep list. Onsets are detected on the original timeline; snapping rebased words against original-timeline onsets would match words to transients belonging to removed audio.
    - _Requirements: 7.6, 7.7, 7.11_

  - [ ] 6.5 Apply hygiene on every caption path including kinetic
    - Both `worker/captions.py` and `worker/engines/kinetic.py`. A repair applied only to the main path leaves the kinetic path with the defect and the two diverge.
    - _Requirements: 8.6_

  - [ ] 6.6 Add the settings and the markers
    - `caption_onset_snap` (default **off**, R7.9), `caption_onset_snap_max_shift_s`, `caption_min_word_span_ms`. Record the moved-span count and whether hygiene altered anything.
    - _Requirements: 7.9, 7.10, 8.8, 15.1, 15.2_

  - [ ] 6.7* Property tests: snapping and hygiene → `tests/test_caption_timing.py`
    - **Property 5** — no span moves further than `max_shift_s`, and every moved start coincides with a supplied onset.
    - **Property 6** — hygiene output is always monotonic and non-overlapping, for arbitrary input.
    - **Property 7** — hygiene is idempotent, and returns already-valid input bit-identically.
    - _Requirements: 7.2, 7.3, 8.1, 8.2, 8.7, 17.3_ · _Properties: P5, P6, P7_

  - [ ] 6.8* Real-audio test: onsets → snapping, end to end → `tests/test_caption_timing_real.py`
    - Real ffmpeg-generated audio with known transient times, through the real `detect_onsets`, through real snapping. **No mocked onsets** — a mocked onset list makes this a test of the arithmetic, which task 6.7 already covers.
    - _Requirements: 17.2, 17.7_

  - [ ] 6.9 Measure whether it is worth enabling
    - Report the moved-span fraction on real footage. If snapping moves 2 % of spans it is not worth the setting; say so and leave it off. Attach rendered output — a moved-span count is not a correctness measure and only the pixels can judge karaoke.
    - _Requirements: 7.10, 17.10_

- [ ] 7. Intermediate render fidelity (O6, in part)
  - [ ] 7.1 Add `intermediate=` to `h264_args` and `x264_crf_intermediate`
    - The parameter belongs **in `ffmpeg_utils.h264_args`**, not as a conditional at the three call sites: these flags were once duplicated across seven call sites with three missing flags, which is why `tests/test_output_compat.py`'s drift pin fails if `libx264` or `-crf` is named elsewhere. Adding per-call overrides would reintroduce exactly that.
    - _Requirements: 12.2, 12.4_

  - [ ] 7.2 Route the cut and geometry passes through it; leave the compositor alone
    - `cut_segment`, the optional trim concat, and the geometry pass are Intermediate_Renders. The compositor is the Final_Render and its quality setting is **unchanged** (R12.3). Change no container, pixel format, profile, frame rate, or bitrate-ceiling behaviour, and do not change the pass count.
    - _Requirements: 12.1, 12.3, 12.5, 12.6_

  - [ ] 7.3 Add the intermediate size ceiling and its fallback
    - A near-lossless intermediate for a 3-minute 4K clip can be very large and a full disk fails the job. Fall back to the previous setting above a configurable ceiling and record that it happened.
    - _Requirements: 12.8, 14.5_

  - [ ] 7.4* Test: final args unchanged, intermediates changed → `tests/test_output_compat.py`
    - Assert the Final_Render's argument list is byte-identical to v0.11.0; assert intermediates use the new setting; assert the existing `libx264`/`-crf` drift pin still holds.
    - _Requirements: 12.3, 12.4, 12.6_

  - [ ] 7.5 Prove it actually helps, or revert it
    - Measure PSNR/SSIM of the Final_Render against a single-pass reference, plus file size and wall-clock delta. `evaluation/golden_render.py`'s perceptual hashing is the wrong tool — it detects *change*, it does not rank quality.
    - "Fewer generations is better" is true in general and could still be **invisible** here. If CRF 20 three times is already perceptually transparent on this footage, this costs render time for nothing and should not land.
    - _Requirements: 12.7, 12.9_

- [ ] 8. Detector default → `mediapipe` (V2 close-out) — **own commit**
  - [ ] 8.1 Measure both backends on real footage first
    - The CHANGELOG's figures (Haar 0.886 / 0.60 on a profile turn; BlazeFace 0.971 / 0.90) were measured on **a synthetic source** — which is exactly where Haar's frontal bias is least punished. Re-measure detection coverage for both backends on the Benchmark_Dataset footage from task 1.
    - _Requirements: 9.6_

  - [ ] 8.2 Flip the default, or record why not
    - If real-footage coverage favours `mediapipe`, set the default. **If it does not, leave the default at `haar` and record the finding** — this task is allowed to conclude "no".
    - Everything needed for this flip already exists: the model is vendored, licensed, manifest-verified, CI-checked, and asserted through `/api/info`; `resolve_detector` already returns the resolved label and already degrades `mediapipe → haar` with a substitution marker. The `face-detection-upgrade` spec built it all and deliberately did not flip it, to keep its own goldens valid.
    - _Requirements: 9.1, 9.3, 9.7_

  - [ ] 8.3 Re-freeze the affected fixtures in this same commit
    - Nothing else changes in this commit. Name in the message which fixtures moved and why. A default change bundled with behavioural work is how a golden gets re-frozen around a real regression — the `font_substituted:Arial` failure mode, where a fixture had the defect frozen in as correct.
    - _Requirements: 9.4, 9.5, 14.2_

  - [ ] 8.4 Confirm `haar` is still byte-identical and the surface is unchanged
    - Selecting `haar` must reproduce v0.11.0 exactly. The `face_detector` option field, the Info_Endpoint advertisement, and the settings UI keep their shape.
    - _Requirements: 9.2, 9.8_

  - [ ] 8.5* Test: the resolved marker names the new default → `tests/test_face_detection.py`, `tests/test_reframe_geometry.py`
    - Assert Effects_Applied records `mediapipe` for a default run and `haar` when selected — the resolved value, never the requested one.
    - _Requirements: 17.5, 14.6_

  - [ ] 8.6 Attach rendered output
    - `scripts/smoke_reel.py` on footage containing a profile turn and a two-shot, both backends. The suite cannot tell you the framing improved; only the pixels can.
    - _Requirements: 17.10_

- [ ] 9. Interior dead air (AU10) — **own commit**
  - [ ] 9.1 Plan interior silences as drop intervals
    - Reuse the per-source `detect_silences` result already memoised for `AU7`; no new detection pass. Intersect with the clip window and keep **only** silences lying wholly inside it — those straddling a boundary belong to `trim_edge_silence` and must not be handled twice.
    - _Requirements: 10.1_

  - [ ] 9.2 Merge into the single existing keep list
    - Interior silence becomes a third contributor to the keep list that filler removal and the `U4` transcript cut list already share, resolved by `plan_keep_intervals` into **one** `apply_keep_intervals` and **one** re-encode. Add no pass.
    - _Requirements: 10.2, 10.3_

  - [ ] 9.3 Retain a configurable pad at each seam
    - Removing a pause *entirely* butts speech against speech, which sounds worse than the pause did. This is the setting most likely to need tuning by ear rather than by metric; document it as provisional.
    - _Requirements: 10.6, 10.7, 15.2_

  - [ ] 9.4 Confirm seam fades apply at every new seam
    - `filler._seam_fades` puts a few-ms `afade` at each interior seam, deliberately **not** `acrossfade`, which would shift the timeline the rebased words depend on. Interior silence removal creates many more seams than filler removal typically does, so this stops being a nicety and becomes the thing preventing audible clicks.
    - _Requirements: 10.4_

  - [ ] 9.5 Rebase every consumer of the timeline
    - Words, emoji placements, and speaker turns, through the existing `rebase_words` / `rebase_turns` path.
    - _Requirements: 10.5_

  - [ ] 9.6 Add the duration guard and the refusal
    - Never reduce a clip below the minimum for its length preset. Above a configurable removed-fraction limit, **refuse and record it** rather than producing a clip the user cannot recognise as their own video — following the established `transcript_trim_refused:*` pattern: decline, label, carry on.
    - _Requirements: 10.8, 10.11, 14.5_

  - [ ] 9.7 Default it ON, conservatively, in this commit, with fixtures re-frozen
    - One of the two deliberate default changes in this plan. Record the removed duration in Effects_Applied. **Leave `filler_removal` off** — removing "um" is an editorial opinion about someone's speech; removing two seconds of nothing is not, and these are separate features.
    - _Requirements: 10.9, 10.10, 10.12, 14.2_

  - [ ] 9.8* Tests: one re-encode, seams, guards → `tests/test_filler.py`, `tests/test_pipeline_trim.py`
    - Assert filler ∪ transcript cuts ∪ interior silence resolve to exactly **one** `apply_keep_intervals` call; assert a seam fade at every interior seam; assert the min-duration guard and the refusal marker; assert words, emoji, **and** speaker turns each land on the tightened timeline — one test per consumer, because one rebased consumer does not imply three.
    - _Requirements: 10.2, 10.3, 10.4, 10.5, 10.8, 10.11, 17.4_

  - [ ] 9.9 Listen to it
    - Render before and after on conversational footage and attach both. Whether the edit sounds natural or chopped is not assertable.
    - _Requirements: 17.10_

- [ ] 10. End-boundary scene snapping (S20)
  - [ ] 10.1 Add `snap_end` beside `snap_start`
    - Same bounded per-candidate scan mechanism as `detect_cuts(path, start)` at the other boundary. **Do not call `scan_cuts` over the whole source** — that is a full decode.
    - Cap displacement by the same `scene_snap_max_shift_s`; respect the same minimum-duration guard; never extend past the source duration.
    - _Requirements: 11.1, 11.2, 11.4, 11.5, 11.6_

  - [ ] 10.2 Give sentence ends priority over shot boundaries
    - Ending mid-sentence to land on a cut is worse than ending mid-shot on a complete thought: the transcript is the content, the shot is the packaging. `snap_to_sentences` runs before `snap_candidates`, so the end arriving here is already sentence-aligned — move it only where that alignment is not broken.
    - _Requirements: 11.3_

  - [ ] 10.3 Wire into `snap_candidates` before edge-silence trimming
    - Preserve the existing stage order; record the shift in Effects_Applied.
    - _Requirements: 11.7, 11.9_

  - [ ] 10.4* Tests: priority and guards → `tests/test_scene_detect.py`
    - Sentence end wins over a nearby cut; shift cap respected; min-duration guard respected; no scanning beyond the bounded window; existing `snap_start` behaviour byte-identical.
    - _Requirements: 11.2, 11.3, 11.4, 11.6_

  - [ ] 10.5 Gate the default on the Boundary_Metric, not the Primary_Metric
    - End snapping cannot change *which* moments are found, only where they stop. `mean_best_iou` can see that; F1@0.5 will barely move. Gating on the wrong metric would produce a null result and an incorrect conclusion.
    - _Requirements: 11.8_

- [ ] 11. Assets (A14, A21)
  - [ ] 11.1 Choose tracks whose licence is unambiguous
    - One track per mood `worker/effects/audio.find_user_tracks` resolves — satisfy the existing taxonomy, do not invent one. Redistribution must be as unambiguous as the OFL fonts already vendored: read the actual licence, do not trust a "royalty-free" badge. A track of unclear provenance is worse than an empty directory, because the empty directory degrades honestly and a mislicensed track is a legal problem shipped in a container image.
    - _Requirements: 13.1, 13.7_

  - [ ] 11.2 Commit tracks and licences
    - Licences as siblings, per `assets/font-licenses/`. Check `.gitignore` and `.dockerignore`: `assets/emoji-*/` is excluded but `assets/emoji/` is not — follow the latter. A directory silently absent from the image would degrade only in production.
    - _Requirements: 13.2, 13.5_

  - [ ] 11.3 Add `scripts/fetch_music.py --check` and wire it into CI
    - Modelled on `scripts/fetch_emoji.py` and `scripts/fetch_models.py`: manifest of filename, digest, source, licence id; `--check` verifies the working tree with **no network**, exiting non-zero and naming the offending file. Add to the `backend` job beside the existing checks. No download-at-render-time path.
    - _Requirements: 13.3, 13.4, 13.12_

  - [ ] 11.4 Assert through the running application in the container smoke test
    - Extend `scripts/docker_smoke.sh` to assert the music assets resolve via the API, per the emoji `/api/info` precedent — not by listing the filesystem.
    - _Requirements: 13.6_

  - [ ] 11.5 Create `assets/broll/` without enabling b-roll
    - The directory does not currently exist, so enabling the option hits a missing path rather than an empty library. Empty-and-present degrades correctly through the existing code; absent does not. **Leave the b-roll option off.**
    - _Requirements: 13.10, 13.11_

  - [ ] 11.6* Tests: assets resolve and verify → `tests/test_music_assets.py`
    - `--check` passes offline; a truncated copy fails and names the file; a real mood resolves **without** `music_degraded:synthesised`; the synthesised bed remains reachable as the labelled last resort.
    - _Requirements: 13.4, 13.8, 13.9_

- [ ] 12. Configuration contract and options surface
  - [ ] 12.1 Document every new setting in `.env.example`
    - `tests/test_config_documentation.py` fails if a `Settings` field is undocumented or a documented key is not a real setting. For each new threshold, state whether its default is **measured or provisional** — most of these are provisional and saying so is what keeps the next person from treating them as calibrated.
    - _Requirements: 15.1, 15.2, 15.3_

  - [ ] 12.2 Surface any new option through API, form, and UI
    - `OptionsModel`, the `/api/upload` form fields, `/api/info` domains, `App.jsx` `DEFAULT_SETTINGS` **and** `toOptions()`, `SettingsPanel.jsx`. Unrecognised values apply the documented default without raising, per the existing convention.
    - _Requirements: 15.4, 15.6_

  - [ ] 12.3 Verify the drift pins
    - `tests/conftest.py`'s `EFFECTS_OFF` / `assert_effects_off_is_exhaustive()` — interior silence removal defaults **on**, so **verify** whether it must be listed rather than assuming either way.
    - _Requirements: 14.1, 14.4_

  - [ ] 12.4 Confirm no existing marker changed spelling
    - Diff the full set of Effects_Applied marker strings against v0.11.0. Every pre-existing marker keeps its exact spelling; this spec only adds. A renamed marker silently breaks any consumer parsing them, and the markers are the product's only mechanism for explaining an absent feature.
    - _Requirements: 14.3, 14.6_

  - [ ] 12.5* Property test: new option fields round-trip → `tests/test_options_roundtrip.py`
    - **Property 8** — every new field survives `from_dict(asdict(...))`, and any unrecognised value resolves to the documented default without raising.
    - _Requirements: 15.5, 15.6_ · _Properties: P8_

- [ ] 13. Make `docs/IMPROVEMENT_PLAN.md` true again (M8)
  - [ ] 13.1 Re-verify every quoted "current value" against v0.11.0
    - The plan's figures are quoted against v0.10.0 and nearly every P0 it describes is already fixed: the Arial fallback, the green karaoke secondary, absent `loudnorm`, missing `pix_fmt`, `base` Whisper, longest-segment fallback in production. Read cold it materially overstates the project's problems, which makes it worse than no document.
    - _Requirements: 16.1, 16.2_

  - [ ] 13.2 Mark blocked items with what would unblock them
    - For each deferred item record the blocker and its resolution: weights CI cannot have (`S5`, `S13`, `T2`, `T6`, `V3`, `V7`, `AU6`), credentials (`PB1`, `PB9`), data (`S16`), infrastructure (`I1`, `I2`).
    - **`S5` stays open specifically**, annotated to say that task 2.3 shipped a signal-processing proxy and that a trained audio-event classifier remains the correct implementation. A proxy marked as done is how a gap disappears from a backlog without being closed.
    - _Requirements: 16.3, 5.8_

  - [ ] 13.3 Add this spec's new IDs and record the verification release
    - `S19`, `S20`, `T11`, `C23`, `AU10`, `M8`. State the release the plan was last verified against, so the next reader knows how much to trust it.
    - _Requirements: 16.4, 16.5_

  - [ ] 13.4 Mark superseded diagnoses rather than deleting them
    - The history of what was believed and later disproved is useful. Mark, do not remove.
    - _Requirements: 16.6_

- [ ] 14. Verification and close-out
  - [ ] 14.1 Full gate run
    - `ruff check .` clean · `ruff format --check .` clean · `mypy .` clean · `pytest` at **2619 + new tests, 0 skipped, 0 warnings** · `python scripts/check_wired.py --check` reports no new dead code · `cd frontend && npm run lint && npm run test:run && npm run build` (node 20 or 22 — vitest crashes on 18) · `scripts/docker_smoke.sh` builds and serves.
    - _Requirements: 17.7, 17.8_

  - [ ] 14.2 Triage any new warning at its source
    - If a new dependency or a new import location surfaces a deprecation, add a **targeted** `filterwarnings` ignore in `pyproject.toml` with a comment saying why it cannot be fixed. Never broaden the existing ignores and never relax `filterwarnings = error`.
    - _Requirements: 17.8_

  - [ ] 14.3 Add the mutation specification
    - `tests/mutations/clip-quality-uplift.json`. Highest-value mutations: return `measured` instead of the blend; invert the `opinion_weight == 1.0` branch; blend *after* dedup; snap a span to a time with no onset; drop the order-preservation rejection in snapping; prefer minimum-duration over non-overlap in hygiene; include boundary-straddling silences as interior; drop the seam fade; snap the end past the min-duration guard. Each should be **CAUGHT**; an ESCAPE is a real gap in the tests, not a mutation to delete.
    - _Requirements: 17.9_

  - [ ] 14.4 Re-run the benchmark and commit the after-report
    - `scripts/eval_selection.py compare` against `eval/baseline-v0.11.0.json`. Record every metric move, including the ones that went the wrong way. Remember that the `ai` path is not reproducible and a single-run diff on it is not a regression signal.
    - _Requirements: 2.1, 2.2, 2.4, 2.5_

  - [ ] 14.5 Write the close-out, including what this did not fix
    - Follow `.kiro/specs/face-detection-upgrade/CLOSE_OUT.md`. State plainly what remains: on two-person footage we still follow the largest, most-diarisation-active face rather than the person speaking (`V3` — `LR-ASD` at 0.84 M params is the first credible vendoring candidate this project has had, and is a separate spec); transcript quality is still capped at Whisper `small` pending the GPU image (`I2`); the pass count is still three (`O6`).
    - Record the risk this spec cannot design away: a 15-source benchmark labelled by one person to one taste, with the Selector then tuned to that taste, would raise the Primary_Metric without improving the product. If a weighted mean of nine heuristics cannot beat `longest`, the honest conclusion is that the approach is wrong — not that the weights need another pass.
    - _Requirements: 2.7, 17.10_
