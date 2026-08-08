# Requirements Document

## Introduction

This spec defines **Clip Quality Uplift** — the set of output-quality improvements that
are buildable *now*, on the AI Video Clipper (self-hosted, CPU-first, currently
**v0.11.0**), without a single new model checkpoint, credential, or GPU.

It exists because of a specific finding. This project was audited against the current
generation of commercial clippers (Opus Clip, Submagic, Vizard, Klap), and the result was
not the expected one. Our **finishing** is at or above parity: two-pass `loudnorm` to
per-platform LUFS with a true-peak limiter, 14 ASS/libass caption presets over 12
vendored heavy display faces, measured-width wrapping, platform safe areas, 326 vendored
emoji, `yuv420p`/`high`/fps-normalised/VBV-capped output with a real hardware-encoder
probe. Competitors do not obviously beat any of that.

The gap is **upstream of the finishing**, and it is concentrated in four places:

1. **Selection is unvalidated, and its ranking discards its own measurements.**
   `eval/labels/` contains only `.gitkeep`. The harness in `evaluation/` — one-to-one
   greedy matching, IoU swept at 0.3/0.5/0.7, `mean_best_iou` to separate "right moment,
   wrong boundaries" from "wrong place" — is well built and **has never scored the real
   selector**. Meanwhile `worker/selection.py:466` sorts candidates on
   `c.score` alone, and on the LLM path `score` is the model's unanchored 0–100 opinion.
   Every measured feature the codebase computes — `hook_score`, `relative_speech_rate`,
   `relative_energy_db`, `structure_score`, `standalone_score`, `intensity_score` — is
   attached to `ClipCandidate.features` and **deliberately excluded from the ranking**.
   We measure the right things and then throw them away at the moment of decision.
2. **Word timestamps are unaligned, and captions are where that shows.** Whisper's native
   word timings drift; with `words_to_cues(max_words=3)` and `\kf` karaoke fill, that
   drift is painted directly onto the frame. Forced alignment (`T2`) is blocked on
   wav2vec2 weights, but the drift is reducible offline with signals already computed.
3. **We ship the worse face detector on purpose.** `face_detector` defaults to `"haar"`.
   The project's own CHANGELOG measures Haar at 0.886 detection coverage overall and
   **0.60 on a profile turn**, against BlazeFace at 0.971 and 0.90 — and the BlazeFace
   model is *already vendored* at `assets/models/blaze_face_short_range.tflite`. The
   default exists to keep golden renders frozen, which means a test-fixture convenience
   is currently costing every user framing accuracy on every clip.
4. **The edit is loose, and the asset shelves are bare.** Interior dead air survives
   (`trim_edge_silence` touches only the outer 1.25 s; `filler_removal` defaults off),
   clip *ends* are never scene-snapped (`scene_detect.snap_candidates` snaps the start
   only), every clip absorbs three full `libx264` CRF-20 re-encodes, and
   `assets/music/`, `assets/sfx/` are empty while `assets/broll` does not exist at all —
   so out of the box "music" is a synthesised bed labelled `music_degraded:synthesised`
   and b-roll produces nothing.

This spec addresses all four. It is scoped by a single rule: **nothing here may be
blocked on an artefact this repository cannot legally or practically contain.** That rule
is what separates it from the eight items already parked on model weights.

### The ordering constraint is not negotiable

`.kiro/steering/working-agreement.md` states: *"Do not start clip-selection quality work
(§3) before the evaluation harness (S1) exists; without it the results cannot be
judged."* The harness exists; **the dataset it consumes does not**. Group A is therefore
a hard prerequisite for Group B, and Group B's tasks MUST NOT begin until Group A's
baseline report is committed. Tuning a ranker against no ground truth is not engineering,
and this document will not pretend otherwise.

### Relationship to `docs/IMPROVEMENT_PLAN.md`

Every requirement below carries the backlog ID it discharges, so the plan stays
navigable per the working agreement. This spec discharges: **S1/M4** (benchmark),
**S18** (score calibration), **S3** (pitch), **S5** (laughter — by proxy, see R5),
**S9** (extended to ends, as S20), **V2** (detector default close-out), **T11** (new),
**C23** (new), **AU10** (new), **S19** (new), **O6** (partially — fidelity, not pass
count), **A14**/**A21** (assets), **M8** (new — plan accuracy).

**`docs/IMPROVEMENT_PLAN.md` is itself stale and this spec must fix it.** Its "current
values" are quoted against v0.10.0, and nearly every P0 defect it describes — Arial
fallback, green karaoke secondary, absent `loudnorm`, missing `pix_fmt`, `base` Whisper,
longest-segment fallback in production — has since been fixed. Read cold, it materially
overstates the project's problems, which makes it worse than no document (Requirement 16).

### Out of scope

Excluded deliberately, with the reason recorded so the boundary is not rediscovered:

- **Audio-visual active-speaker detection (`V3`) and subject/body detection (`V7`).**
  These are the largest remaining *visual* gap — on two-person footage we follow the
  largest, most-diarisation-active face rather than the person actually talking, and Opus
  ships true ASD. `LR-ASD` (IJCV 2025) is unusually small at 0.84 M parameters and 94.5 %
  mAP on AVA-ActiveSpeaker, which makes it the first credible vendoring candidate this
  project has had — but the checkpoint, its licence, and its `torch` dependency all need
  evaluating on their own terms. **That is a separate spec**, and R5's mouth-motion
  correlation is explicitly *not* a down payment on it.
- **Forced alignment via wav2vec2 (`T2`).** Blocked on weights CI cannot have. R7 reduces
  the same error with signals already in hand and does not close T2.
- **The GPU image (`I2`) and larger Whisper models.** `whisper_model` defaults to `small`,
  which caps transcript quality and therefore compounds into both selection and captions.
  This is real, and it is an infrastructure spec, not a quality one.
- **Reducing the ffmpeg pass count (`O6` proper).** Collapsing cut → geometry → composite
  into fewer passes is an architectural change to `worker/pipeline.py`'s per-clip loop.
  R12 addresses only the *fidelity* cost of the existing passes, which is the part
  obtainable without touching the stage boundaries.
- **The published-performance feedback loop (`S16`).** Blocked on data nobody has yet.
- **Per-platform output variants.** `output_profiles` treats aspect as advisory by
  design; rendering N variants per clip is a product decision, not a quality defect.
- **`assets/sfx/`.** `worker/effects/sfx.py` synthesises only `pop`/`click` and
  deliberately *refuses* `whoosh`. Shipping sample packs to feed a mode that defaults to
  `off` is not a quality win. `SFX_MODE` stays `off`.

## Glossary

- **Clipper**: The overall AI Video Clipper application (self-hosted, ffmpeg-based, CPU-first).
- **Pipeline**: The per-source flow in `worker/pipeline.py::run_pipeline` (probe → transcribe → select → per clip: trim → cut → filler → geometry → composite → thumbnail).
- **Selector**: Whatever produces Clip_Candidates for a source — the LLM path in `worker/selection.py` or the deterministic fallback in `worker/segmentation.py`.
- **Clip_Candidate**: The `ClipCandidate` record (`start`, `end`, `score`, `reason`, `title`, `features`).
- **Candidate_Score**: `ClipCandidate.score`, the single number the Selector's final ordering is performed on (`worker/selection.py:466`).
- **Measured_Feature**: Any entry in `ClipCandidate.features` derived from the audio or transcript by an offline annotator — `hook_score`, `relative_speech_rate`, `relative_energy_db`, `structure_score`, `standalone_score`, `intensity_score`, and those this spec adds.
- **LLM_Opinion**: The 0–100 `score` field returned by the language model on the `ai` selection path, before any blending.
- **Blended_Score**: A Candidate_Score computed from LLM_Opinion together with Measured_Features, as defined by Requirement 3.
- **Benchmark_Dataset**: The labelled selection corpus in `eval/labels/*.json`, in the format `evaluation/dataset.py` documents (`source`, `notes`, `moments[{start, end, note}]`).
- **Baseline_Report**: The committed JSON output of `scripts/eval_selection.py run`, recording scores for the Selector and for the `uniform`, `random`, and `longest` baselines on the Benchmark_Dataset.
- **Primary_Metric**: F1 at `PRIMARY_IOU = 0.5`, pooled across sources, as `evaluation/metrics.py` computes it.
- **Boundary_Metric**: `mean_best_iou`, the diagnostic distinguishing correct moments with poor boundaries from incorrect moments.
- **Pitch_Variation**: A measure of fundamental-frequency spread within a candidate window, relative to the source's own median spread.
- **Reaction_Event**: A detected non-speech audience response — laughter or applause — expressed as a time and a confidence.
- **Onset**: A time at which the audio level rose sharply, as `worker/audio_features.detect_onsets` reports. Onset detection, **not** beat tracking: every Onset corresponds to a real transient.
- **Word_Span**: One word's `(start, end)` interval as used by `worker/captions.words_to_cues` and the ASS builder.
- **Span_Hygiene**: The invariants a sequence of Word_Spans must satisfy before it is rendered — monotonic, non-overlapping, and no shorter than a legibility floor.
- **Interior_Silence**: A silent interval that lies wholly inside a clip, as distinct from the edge silences `segmentation.trim_edge_silence` already removes.
- **Keep_Interval**: One `Interval` in the keep list `worker/effects/filler.plan_keep_intervals` produces and `apply_keep_intervals` renders in a single re-encode.
- **Intermediate_Render**: Any re-encode whose output is consumed by a later stage rather than delivered to the user — today the cut, the optional trim concat, and the geometry pass.
- **Final_Render**: The last re-encode of a clip, whose output is delivered.
- **Effects_Applied**: The free-form `ClipResult.effects_applied` string markers recording which enhancements ran and how they degraded.
- **Processing_Options**: The user options record (`worker/models.py::ProcessingOptions`, mirrored by `OptionsModel`, upload Form fields, `App.jsx` defaults/`toOptions`, and `SettingsPanel.jsx`).
- **Info_Endpoint**: The `/api/info` endpoint advertising available option values to the UI.
- **Vendored_Asset**: A media or model file committed into the repository with its licence, verified offline in CI — the pattern `assets/emoji/`, `assets/fonts/`, and `assets/models/` already establish.

## Requirements

---

## Group A — Selection can be judged at all (gating; discharges S1/M4)

### Requirement 1: A labelled benchmark dataset exists

**User Story:** As a maintainer, I want a corpus of human-marked moments, so that a change to the Selector can be shown to help rather than argued about.

#### Acceptance Criteria

1. THE Clipper SHALL contain a Benchmark_Dataset of at least 15 labelled sources under `eval/labels/`.
2. THE Benchmark_Dataset SHALL conform to the format `evaluation/dataset.py` documents, and SHALL load through `Dataset` without raising `DatasetError`.
3. THE Benchmark_Dataset SHALL span at least three distinct footage shapes, including at minimum single-speaker footage, two-person conversational footage, and footage containing audience reaction.
4. FOR every labelled source, THE Benchmark_Dataset SHALL record at least two labelled moments.
5. THE Clipper SHALL NOT require the labelled source media to be committed to the repository.
6. WHERE a labelled source's media is absent from the host, THE Clipper SHALL report that source as unavailable and SHALL continue scoring the remaining sources.
7. THE Clipper SHALL document, alongside the Benchmark_Dataset, the provenance of each source and the labelling instruction used, so a second labeller can extend it consistently.
8. THE Clipper SHALL NOT include any labelled moment whose `end` is not greater than its `start`.

### Requirement 2: The baseline is measured, committed, and reproducible

**User Story:** As a maintainer, I want the Selector's score against the naive baselines written down, so that later work has a number to beat and cannot silently regress.

#### Acceptance Criteria

1. THE Clipper SHALL produce a Baseline_Report for the Benchmark_Dataset covering the Selector and the `uniform`, `random`, and `longest` baselines.
2. THE Baseline_Report SHALL record the Primary_Metric and the Boundary_Metric for every scored path.
3. THE Clipper SHALL commit the Baseline_Report to the repository.
4. THE Clipper SHALL produce a byte-identical Baseline_Report when re-run on unchanged inputs with an unchanged configuration and an unchanged transcript cache.
5. WHERE the Selector's path is non-deterministic, THE Clipper SHALL record in the Baseline_Report which paths are non-deterministic and SHALL NOT present their single-run figures as reproducible.
6. THE Clipper SHALL record in the Baseline_Report the model identity, configuration, and code revision the run was performed at.
7. THE Clipper SHALL state explicitly in the Baseline_Report whether the Selector beats the `longest` baseline on the Primary_Metric.
8. THE Clipper SHALL NOT gate CI on an absolute Primary_Metric threshold.

---

## Group B — The ranking uses what the Clipper already measures

**Prerequisite: Group A is complete and its Baseline_Report is committed.**

### Requirement 3: Measured features inform the final ordering (discharges S18)

**User Story:** As a creator, I want the tool's own measurements of pace, energy, and hook strength to affect which clips I am given, so that a confident-sounding model cannot override evidence.

#### Acceptance Criteria

1. THE Selector SHALL compute a Blended_Score from LLM_Opinion and the available Measured_Features, and SHALL order candidates by it.
2. THE Selector SHALL expose the relative influence of LLM_Opinion against the Measured_Features as a single configurable weight.
3. WHEN the configurable weight assigns all influence to LLM_Opinion, THE Selector SHALL order candidates exactly as v0.11.0 does.
4. THE Clipper SHALL default the configurable weight to the value that reproduces v0.11.0 ordering, until the Baseline_Report demonstrates a better one.
5. WHERE a Measured_Feature is unavailable or was measured unreliably, THE Selector SHALL treat it as neutral and SHALL NOT penalise the candidate for the absence.
6. THE Selector SHALL retain LLM_Opinion unmodified on the Clip_Candidate alongside the Blended_Score, so the two are separately inspectable.
7. THE Selector SHALL NOT present a Blended_Score to the user as if it were the model's virality estimate.
8. THE Selector SHALL apply blending before deduplication and before the candidate-count cap.
9. THE Clipper SHALL record the weight actually used in the Baseline_Report for any run that reports a blended configuration.
10. THE Selector SHALL compute the Blended_Score through the existing `worker/candidate_ranking` scoring vocabulary rather than a second, parallel definition of the same features.

### Requirement 4: Pitch variation is a measured feature (discharges S3)

**User Story:** As a creator, I want animated delivery to count in my favour, so that a monotone passage and an excited one are not scored alike.

#### Acceptance Criteria

1. THE Clipper SHALL compute Pitch_Variation for each Clip_Candidate window.
2. THE Clipper SHALL compute Pitch_Variation relative to the source's own median, so a low-pitched or high-pitched speaker is not advantaged.
3. THE Clipper SHALL compute Pitch_Variation without any network access and without any model checkpoint.
4. THE Clipper SHALL attach Pitch_Variation to `ClipCandidate.features`.
5. THE Clipper SHALL mark Pitch_Variation unreliable WHERE the window contains insufficient voiced audio to estimate it.
6. WHERE Pitch_Variation is unreliable, THE Selector SHALL treat it as neutral per Requirement 3.5.
7. THE Clipper SHALL measure pitch in at most one additional pass over the source audio per job.
8. THE Clipper SHALL produce identical Pitch_Variation values for identical input audio across runs and platforms.
9. THE Clipper SHALL include Pitch_Variation in the delivery annotations offered to the LLM prompt WHERE `selection_features_in_prompt` is enabled, expressed as a qualitative departure from the speaker's norm rather than a number.

### Requirement 5: Audience reaction is detected without model weights (discharges S5 by proxy)

**User Story:** As a podcast editor, I want the moments the room laughed at to be found, so that the funniest clip is not passed over for a fluent one.

#### Acceptance Criteria

1. THE Clipper SHALL detect Reaction_Events from the source audio using signal-processing features only, with no model checkpoint and no network access.
2. THE Clipper SHALL attach a per-candidate reaction measure derived from Reaction_Events to `ClipCandidate.features`.
3. THE Clipper SHALL record in Effects_Applied that reaction detection ran by proxy rather than by a trained classifier.
4. THE Clipper SHALL NOT name the proxy in a way that implies a trained audio-event classifier produced it.
5. THE Clipper SHALL express Reaction_Event confidence in the closed interval `[0.0, 1.0]`.
6. THE Clipper SHALL report its measured false-positive behaviour on the Benchmark_Dataset in the Baseline_Report, rather than asserting accuracy.
7. THE Clipper SHALL default reaction detection to enabled only IF the Baseline_Report shows it does not reduce the Primary_Metric.
8. THE Clipper SHALL leave the `S5` backlog item open, recording that a trained classifier remains the correct implementation.

### Requirement 6: A non-speech opening is not automatically disqualifying (discharges S19)

**User Story:** As a creator, I want a clip that opens on a laugh or a visual beat to remain eligible, so that the hook rule does not delete a good hook for not being a word.

#### Acceptance Criteria

1. THE Clipper SHALL continue to treat an opening containing neither speech nor any detected Reaction_Event nor any Onset as disqualifying for hook purposes.
2. WHERE speech begins after `SPEECH_DEADLINE_S` BUT a Reaction_Event or an Onset occurs within the hook window, THE Clipper SHALL NOT reduce the hook score to zero.
3. THE Clipper SHALL keep the existing hard-zero behaviour reachable through configuration.
4. THE Clipper SHALL default to the v0.11.0 behaviour until the Baseline_Report shows the relaxed rule does not reduce the Primary_Metric.
5. THE Clipper SHALL NOT change `SPEECH_DEADLINE_S` or `HOOK_WINDOW_S`.
6. THE Clipper SHALL record the hook components separately, as it does today, so the cause of a zero remains inspectable.

---

## Group C — Caption timing precision

### Requirement 7: Word timings are snapped to real transients (discharges T11)

**User Story:** As a creator, I want the karaoke highlight to land on the word being said, so that the most visible thing in the clip is not visibly wrong.

#### Acceptance Criteria

1. THE Clipper SHALL adjust Word_Span start times toward nearby Onsets.
2. THE Clipper SHALL NOT move a Word_Span start by more than a configurable maximum displacement.
3. THE Clipper SHALL NOT move a Word_Span start onto a time at which no Onset was detected.
4. WHERE no Onset lies within the maximum displacement of a Word_Span start, THE Clipper SHALL leave that Word_Span unchanged.
5. THE Clipper SHALL preserve the ordering of Word_Spans after snapping.
6. THE Clipper SHALL apply snapping before cue grouping, so `words_to_cues` groups the corrected spans.
7. THE Clipper SHALL apply snapping to the spans used for caption rendering and SHALL NOT alter the cached ASR transcript.
8. THE Clipper SHALL reuse the energy envelope already computed for the source rather than performing a second audio pass.
9. THE Clipper SHALL default snapping to disabled until its effect is measured per Requirement 17.
10. THE Clipper SHALL record in Effects_Applied when snapping ran and how many spans it moved.
11. THE Clipper SHALL NOT snap Word_Spans that have passed through filler removal rebasing in a way that breaks the rebased timeline.

### Requirement 8: Word spans are legible by construction (discharges C23)

**User Story:** As a viewer, I want every highlighted word to be on screen long enough to read, so that a compressed timestamp does not produce an invisible flash.

#### Acceptance Criteria

1. THE Clipper SHALL enforce that rendered Word_Spans are monotonically non-decreasing in start time.
2. THE Clipper SHALL enforce that rendered Word_Spans do not overlap.
3. THE Clipper SHALL enforce a configurable minimum duration for a rendered Word_Span.
4. WHEN enforcing the minimum duration would overlap the following Word_Span, THE Clipper SHALL preserve non-overlap in preference to the minimum duration.
5. THE Clipper SHALL NOT extend a Word_Span beyond the end of the cue that contains it.
6. THE Clipper SHALL apply Span_Hygiene to every caption path, including the kinetic typography engine.
7. THE Clipper SHALL leave a Word_Span sequence that already satisfies Span_Hygiene bit-identical.
8. THE Clipper SHALL record in Effects_Applied when Span_Hygiene altered any span.

---

## Group D — Framing default (discharges V2 close-out)

### Requirement 9: The better detector is the default

**User Story:** As a creator, I want the framing to use the detector this project measured as more accurate, so that a test-fixture convenience is not costing me every clip.

#### Acceptance Criteria

1. THE Clipper SHALL default the face detector backend to `mediapipe`.
2. THE Clipper SHALL keep `haar` selectable, and SHALL keep its behaviour byte-identical to v0.11.0 when selected.
3. IF the `mediapipe` backend is unavailable for any reason, THEN THE Clipper SHALL fall back to `haar` and SHALL record the substitution, exactly as it does today.
4. THE Clipper SHALL re-freeze every golden and parity fixture the default change invalidates, in a commit that changes no behaviour other than the default.
5. THE Clipper SHALL NOT re-freeze any golden fixture without recording, in the commit message, which fixtures moved and why.
6. THE Clipper SHALL report measured detection coverage for both backends on the Benchmark_Dataset footage, so the default change rests on a number rather than on the CHANGELOG's synthetic source.
7. IF measured coverage on real footage does not favour `mediapipe`, THEN THE Clipper SHALL leave the default at `haar` and SHALL record the finding.
8. THE Clipper SHALL keep the `face_detector` Processing_Options field, the Info_Endpoint advertisement, and the settings UI unchanged in shape.

---

## Group E — Editorial tightness

### Requirement 10: Interior dead air is removed by default (discharges AU10)

**User Story:** As a creator, I want the pauses inside my clip tightened, so that a 45-second clip does not contain six seconds of nothing.

#### Acceptance Criteria

1. THE Clipper SHALL detect Interior_Silences within each clip and SHALL express their removal as Keep_Intervals.
2. THE Clipper SHALL merge Interior_Silence removal into the same single re-encode that filler removal and the transcript cut list already share.
3. THE Clipper SHALL NOT introduce an additional re-encode pass for Interior_Silence removal.
4. THE Clipper SHALL apply the existing seam fade treatment at every interior seam it creates.
5. THE Clipper SHALL rebase Word_Spans, emoji placements, and speaker turns onto the tightened timeline, reusing the existing rebasing path.
6. THE Clipper SHALL expose the minimum Interior_Silence duration that is eligible for removal as a configuration setting.
7. THE Clipper SHALL retain a configurable amount of silence at each seam rather than removing it entirely, so speech does not butt against speech.
8. THE Clipper SHALL NOT remove an Interior_Silence WHERE doing so would reduce the clip below the minimum duration for its length preset.
9. THE Clipper SHALL default Interior_Silence removal to enabled, with a conservative minimum duration.
10. THE Clipper SHALL record in Effects_Applied how much duration Interior_Silence removal removed.
11. WHERE Interior_Silence removal would remove more than a configurable fraction of the clip, THE Clipper SHALL refuse and SHALL record the refusal, rather than producing a clip unrecognisable against its source.
12. THE Clipper SHALL leave `filler_removal` defaulting to disabled; these are separate features and this requirement does not enable filler removal.

### Requirement 11: Clip ends are snapped to shot boundaries (discharges S20)

**User Story:** As a viewer, I want a clip to stop at a cut rather than partway through a shot, so that the ending does not read as an accident.

#### Acceptance Criteria

1. THE Clipper SHALL snap a Clip_Candidate's end toward a nearby shot boundary.
2. THE Clipper SHALL cap end displacement by the same configurable maximum shift that governs start snapping.
3. THE Clipper SHALL prefer a sentence end over a shot boundary WHERE the two conflict, so a clip does not end mid-sentence to land on a cut.
4. THE Clipper SHALL NOT snap an end in a way that reduces the candidate below the minimum duration guard that governs start snapping.
5. THE Clipper SHALL NOT extend a candidate's end beyond the source duration.
6. THE Clipper SHALL scan for end-adjacent cuts within a bounded window per candidate, and SHALL NOT scan the whole source.
7. THE Clipper SHALL apply end snapping before edge-silence trimming, preserving the existing stage order.
8. THE Clipper SHALL default end snapping to enabled only IF the Baseline_Report shows it does not reduce the Boundary_Metric.
9. THE Clipper SHALL record in Effects_Applied when an end was moved and by how much.

---

## Group F — Signal-chain fidelity (discharges O6 in part)

### Requirement 12: Intermediate renders do not spend the final render's quality budget

**User Story:** As a creator, I want the visible quality of my clip set by the last encode, so that three generations of compression do not accumulate in the gradients and on the caption edges.

#### Acceptance Criteria

1. THE Clipper SHALL encode every Intermediate_Render at a quality setting at least as high as the Final_Render's.
2. THE Clipper SHALL expose the Intermediate_Render quality setting as configuration.
3. THE Clipper SHALL keep the Final_Render's quality setting unchanged from v0.11.0.
4. THE Clipper SHALL continue to route every encode through the single `ffmpeg_utils` argument builder, so the existing drift pin against naming `libx264` or `-crf` elsewhere still holds.
5. THE Clipper SHALL NOT change the number of encoding passes.
6. THE Clipper SHALL NOT change the container, pixel format, profile, frame rate, or bitrate-ceiling behaviour of any pass.
7. THE Clipper SHALL report the measured effect of the change on Final_Render file size and on wall-clock render time.
8. WHERE an Intermediate_Render's quality setting would produce an intermediate file larger than a configurable ceiling, THE Clipper SHALL fall back to the previous setting and SHALL record that it did.
9. THE Clipper SHALL verify the change improves a perceptual measure of the Final_Render rather than asserting that it must.

---

## Group G — Asset shelves are not empty (discharges A14, A21)

### Requirement 13: A music bed ships with the product

**User Story:** As a new user, I want the music option to produce music, so that my first clip is not scored by a synthesised tone labelled as degraded.

#### Acceptance Criteria

1. THE Clipper SHALL contain at least one Vendored_Asset music track for each mood `worker/effects/audio.find_user_tracks` resolves.
2. THE Clipper SHALL store each track's licence alongside it, in the manner `assets/font-licenses/` establishes.
3. THE Clipper SHALL provide a Model_Manifest-equivalent record for the music assets, recording filename, digest, source, and licence identifier.
4. THE Clipper SHALL verify the music assets offline in CI, exiting non-zero and naming the offending file on a missing, truncated, or mismatched track.
5. THE Clipper SHALL include the music assets in the built container image.
6. THE Clipper SHALL assert the music assets resolve through the running application in the container smoke test, rather than by listing the filesystem alone.
7. THE Clipper SHALL only vendor tracks whose licence permits redistribution as unambiguously as the OFL fonts already vendored.
8. WHEN a real track is available for the requested mood, THE Clipper SHALL NOT record `music_degraded:synthesised`.
9. THE Clipper SHALL retain the synthesised bed as the labelled last resort.
10. THE Clipper SHALL create the b-roll asset directory the b-roll path expects, so an enabled b-roll option resolves an empty library rather than a missing path.
11. THE Clipper SHALL NOT enable the b-roll option by default.
12. THE Clipper SHALL NOT add any download-at-render-time path.

---

## Group H — Cross-cutting compatibility, configuration, and verification

### Requirement 14: Default output stays parity-checkable

**User Story:** As a maintainer, I want every change here to be either off by default or deliberately re-frozen, so that the golden renders keep detecting accidental change.

#### Acceptance Criteria

1. THE Clipper SHALL default every new setting introduced by this spec to previously shipped behaviour, except where a requirement explicitly directs otherwise.
2. WHERE a requirement changes a default, THE Clipper SHALL change it in a commit that changes nothing else, and SHALL re-freeze the affected fixtures in that same commit.
3. THE Clipper SHALL keep every existing Effects_Applied marker spelled exactly as it is today.
4. THE Clipper SHALL keep every pre-existing Processing_Options field and default unchanged except `face_detector` per Requirement 9.
5. THE Clipper SHALL record every degradation and every refusal this spec introduces as an Effects_Applied marker, so no feature can be absent without explanation.
6. THE Clipper SHALL name in each marker the value that was actually applied, never the value that was requested.

### Requirement 15: Configuration is documented as a contract

**User Story:** As an operator, I want every new knob to appear in `.env.example`, so that the documentation test keeps the contract true.

#### Acceptance Criteria

1. FOR every configuration setting this spec adds, THE Clipper SHALL provide a matching documented entry in `.env.example`.
2. THE Clipper SHALL document, for each new threshold, whether its default is measured or provisional.
3. THE Clipper SHALL NOT introduce a documented key that is not a real setting.
4. THE Clipper SHALL surface through the Info_Endpoint any new option value the UI must offer.
5. THE Clipper SHALL round-trip every new Processing_Options field through serialisation without loss.
6. WHERE a new Processing_Options value is unrecognised or malformed, THE Clipper SHALL apply the documented default and SHALL NOT raise.

### Requirement 16: The backlog document tells the truth (discharges M8)

**User Story:** As a contributor, I want `docs/IMPROVEMENT_PLAN.md` to describe the code as it is, so that I do not spend a day fixing a defect that was fixed two releases ago.

#### Acceptance Criteria

1. THE Clipper SHALL update `docs/IMPROVEMENT_PLAN.md` so that every "current value" it quotes matches v0.11.0.
2. THE Clipper SHALL mark as complete every plan item that the code already discharges.
3. THE Clipper SHALL record, for each item this spec defers, the reason it is blocked and what would unblock it.
4. THE Clipper SHALL add the new backlog IDs this spec introduces to the plan.
5. THE Clipper SHALL state in the plan the release it was last verified against.
6. THE Clipper SHALL NOT delete the plan's history of superseded diagnoses; superseded entries SHALL be marked rather than removed.

### Requirement 17: Every claim in this spec is verified against the real program

**User Story:** As a maintainer, I want each quality claim demonstrated by measurement, so that "this improves the output" is a finding rather than an intention.

#### Acceptance Criteria

1. THE Clipper SHALL include a test that runs the real pitch estimator against real audio, without mocking the estimator.
2. THE Clipper SHALL include a test that runs the real Onset detector and the real snapping path against real audio.
3. THE Clipper SHALL include a test asserting Span_Hygiene invariants hold for arbitrary Word_Span sequences.
4. THE Clipper SHALL include a test asserting that Interior_Silence removal, filler removal, and the transcript cut list still resolve into exactly one re-encode when combined.
5. THE Clipper SHALL include a test asserting the resolved detector backend recorded in Effects_Applied is the one that ran, for the new default.
6. THE Clipper SHALL cross-check any parsed program output through an independent mechanism sharing no parsing code with the implementation.
7. THE Clipper SHALL NOT introduce any test that is skipped when its dependencies are present.
8. THE Clipper SHALL NOT introduce any new warning into the test run.
9. THE Clipper SHALL add a mutation specification covering the highest-value mutations of the blending, snapping, hygiene, and silence-removal arithmetic.
10. THE Clipper SHALL attach rendered output to the change for every requirement whose effect is visible in pixels or audible in the mix, because the suite cannot judge either.
