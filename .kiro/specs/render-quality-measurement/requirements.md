# Requirements Document

## Introduction

**Render Quality Measurement** gives this project the ability to tell whether a change to
the render made the picture, the caption timing, or the sync *better*. Today it cannot.

That is not a rhetorical claim. Searching `worker/`, `evaluation/`, and `scripts/` for
`vmaf`, `psnr`, and `ssim` returns **nothing**. The project has an unusually strong
measurement culture in every other dimension — `evaluation/wer.py` for transcript accuracy,
`evaluation/metrics.py` with IoU sweeps for selection, `evaluation/golden_render.py` for
visual regression, `scripts/mutate.py` for test strength, per-stage timings at
`GET /api/jobs/{id}/timings` — and a hole exactly where *render fidelity* should be.

The consequence is concrete and immediate:

- Three sibling specs (`clip-signal-fidelity`, `clip-presentation-polish`,
  `clip-editorial-structure`) each contain requirements of the form "prove this improved
  the output rather than asserting it." **None of them has a tool.**
- `clip-quality-uplift`'s R12.9 requires proving the intermediate-render fidelity change
  perceptually or reverting it. That requirement is currently unsatisfiable.
- The most valuable pending change in the project is arguably a one-word edit —
  `x264_preset` from `veryfast` to `medium` (`config.py:354`) — and there is no way to
  demonstrate its benefit or measure its cost.

`evaluation/golden_render.py` is the closest existing thing and is **deliberately the wrong
tool** for this. Its own docstring is explicit: it is "a regression net for *visible*
change, not a pixel-exact contract," using an 8×8 average hash chosen because larger grids
react to encoder noise as strongly as to real changes. It answers *"did this change?"*
It cannot answer *"is this better?"*, and the two questions need different instruments.

This spec builds four instruments:

1. **Render fidelity** (`M9`) — SSIM and PSNR always, VMAF where the ffmpeg build has it.
2. **Caption alignment error** (`M10`) — timing error, which `wer.py` does not measure.
   WER scores the *words*; the defect a viewer actually sees is the *timing*.
3. **A/V sync verification** (`M11`) — `cut_segment` seeks with `-ss` before `-i`, which is
   accurate under re-encoding in modern ffmpeg. This spec does **not** allege a bug. It
   observes that nothing measures sync, so if it ever drifts, every burned caption drifts
   with it and no test would notice.
4. **Pairwise preference** (`M12`) — the honest admission that SSIM cannot tell you whether
   a clip is well framed, well cut, or worth watching. A structured human A/B harness is
   the only instrument for that, and the three sibling specs all end in "attach rendered
   output," which is this, done informally.

### Why this spec comes first

Every sibling spec changes pixels or audio. Without this one, each of them lands as taste
asserted against taste, and their "prove it helped" clauses become paperwork. With it, the
one-line encoder changes in `clip-signal-fidelity` become measured decisions.

This is the same argument `clip-quality-uplift` makes for its labelled selection benchmark,
applied to the render instead of the selection. That spec's Group A is the gate for
*selection* work; this spec is the gate for *render* work. They are independent and can
proceed in parallel.

### A constraint that shapes the whole design

**ffmpeg is deliberately not pinned.** The `Dockerfile` says so at length: pinning
`ffmpeg=7:5.1.6-0+deb12u1` "fails to resolve the moment a security update lands," and the
project would rather have a working image than a byte-identical ffmpeg. It is tested
against ffmpeg 7.0.2 (static) and Debian bookworm's 5.1.x.

That means **VMAF may not exist on the host.** `libvmaf` requires an explicit build flag
and Debian's ffmpeg frequently lacks it; `zscale` likewise requires `libzimg`. So this spec
cannot depend on VMAF. SSIM and PSNR are built-in filters present in every practical build
and form the floor; VMAF is an enhancement, probed for and degraded from with a marker.

The project has already been burned by exactly this: `golden_render.py`'s docstring records
that "a capability probe hid 124 ffmpeg filters." `worker/engines/capabilities.py` exists
because of it, with `ffmpeg_filter:<name>` capability IDs, a totality guarantee, and
per-process caching. This spec reuses that machinery rather than adding a second probe.

### Out of scope

- **Changing any render behaviour.** This spec adds instruments only. Every reading it
  produces is a baseline for a sibling spec to move. If a measurement reveals a defect,
  that defect is fixed in the spec that owns it.
- **Gating CI on an absolute quality threshold.** Same reasoning as
  `clip-quality-uplift` R2.8: an absolute SSIM floor would either never fire or block
  unrelated work. These are recorded baselines and relative comparisons.
- **Replacing `golden_render.py`.** It answers a different question and stays.
- **Automated aesthetic scoring.** No model that claims to rate whether a clip is
  "engaging." That is what `M12`'s humans are for, and a learned aesthetic score would be
  an unvalidated opinion wearing a number, which is the defect `clip-quality-uplift` R3
  exists to fix.
- **Per-platform post-upload quality.** Measuring what TikTok's re-encode does to our
  output needs live accounts (`PB1`).

## Glossary

- **Clipper**: The overall AI Video Clipper application.
- **Final_Render**: The delivered clip — the compositor's output.
- **Reference_Render**: A deliberately higher-fidelity render of the same content, produced for comparison rather than delivery.
- **Fidelity_Metric**: A full-reference objective measure of one render against another — SSIM, PSNR, or VMAF.
- **Fidelity_Report**: The committed record of Fidelity_Metric readings, with the build, configuration, and revision they were taken at.
- **Alignment_Error**: The signed difference between a rendered caption event's time and the true time of the word it presents.
- **Alignment_Report**: The committed record of Alignment_Error statistics for a labelled set.
- **Sync_Offset**: The measured audio-relative-to-video offset of a rendered clip.
- **Preference_Trial**: One presentation of two clips of the same content, rendered under different configurations, to a human who picks one or declines.
- **Preference_Report**: The committed aggregate of Preference_Trials, including declines.
- **Capability_Status**: The frozen availability record `worker/engines/capabilities.py` produces for a probed capability id.
- **Effects_Applied**: The `ClipResult.effects_applied` string markers recording which enhancements ran and how they degraded.

## Requirements

### Requirement 1: Render fidelity is measurable (M9)

**User Story:** As a maintainer, I want to measure a render against a higher-fidelity reference, so that an encoder change can be shown to help rather than argued about.

#### Acceptance Criteria

1. THE Clipper SHALL compute SSIM and PSNR for a Final_Render against a Reference_Render.
2. THE Clipper SHALL compute VMAF in addition WHERE the ffmpeg build provides it.
3. THE Clipper SHALL determine VMAF availability through the existing capability probe rather than a second mechanism.
4. WHERE VMAF is unavailable, THE Clipper SHALL report SSIM and PSNR and SHALL record that VMAF was unavailable, naming the reason.
5. THE Clipper SHALL NOT fail a measurement run because VMAF is unavailable.
6. THE Clipper SHALL align the two renders on frame count and resolution before comparing, and SHALL refuse to report a Fidelity_Metric for inputs it could not align.
7. THE Clipper SHALL cross-check parsed ffmpeg metric output through an independent mechanism sharing no parsing code with the implementation.
8. THE Clipper SHALL report per-frame minima alongside the mean for every Fidelity_Metric, because a mean hides a single badly damaged frame.
9. THE Clipper SHALL NOT present a Fidelity_Metric as a measure of whether a clip is good, only of how faithfully it reproduces its reference.

### Requirement 2: Fidelity measurement is reproducible and recorded

**User Story:** As a maintainer, I want fidelity readings committed with their provenance, so that a later comparison is meaningful.

#### Acceptance Criteria

1. THE Clipper SHALL produce a Fidelity_Report recording every Fidelity_Metric it computed.
2. THE Fidelity_Report SHALL record the ffmpeg version, the encoder resolved, the CRF and preset, the resolution, and the code revision.
3. THE Clipper SHALL record wall-clock encode time and output file size alongside the quality readings, so a quality gain is visible against its cost.
4. THE Clipper SHALL commit a Fidelity_Report for the v0.11.0 configuration as the reference point.
5. THE Clipper SHALL produce identical readings for identical inputs on an identical ffmpeg build.
6. THE Clipper SHALL state in the Fidelity_Report that readings are not comparable across ffmpeg versions, because the project deliberately does not pin ffmpeg.
7. THE Clipper SHALL provide a comparison mode that diffs two Fidelity_Reports and names every metric that moved.
8. THE Clipper SHALL NOT gate CI on an absolute Fidelity_Metric threshold.

### Requirement 3: Caption timing error is measurable (M10)

**User Story:** As a maintainer, I want to measure how far captions land from the words they present, so that alignment work is judged on the defect users see rather than on word accuracy.

#### Acceptance Criteria

1. THE Clipper SHALL compute Alignment_Error for caption events against a labelled set of true word times.
2. THE Clipper SHALL report the mean, the median, the 90th percentile, and the maximum absolute Alignment_Error.
3. THE Clipper SHALL report Alignment_Error as a **signed** distribution, because a systematic lead or lag is a different defect from symmetric jitter and is fixed differently.
4. THE Clipper SHALL measure against the caption events actually rendered, not against the intermediate word list.
5. THE Clipper SHALL document how the labelled true word times were obtained and SHALL NOT derive them from the ASR output being evaluated.
6. THE Clipper SHALL produce an Alignment_Report and SHALL commit a reading for the v0.11.0 configuration.
7. THE Clipper SHALL report the count of caption events it could not match to a labelled word, rather than silently excluding them.
8. THE Clipper SHALL NOT reuse `evaluation/wer.py`'s normalisation for time matching where that normalisation would merge or drop words whose timing is being measured.

### Requirement 4: Audio/video sync is verified (M11)

**User Story:** As a maintainer, I want rendered clips checked for sync drift, so that a regression in cutting cannot silently desynchronise every burned caption.

#### Acceptance Criteria

1. THE Clipper SHALL measure the Sync_Offset of a rendered clip.
2. THE Clipper SHALL measure Sync_Offset from the rendered file's actual streams rather than from the arguments used to produce it.
3. THE Clipper SHALL verify sync for a clip cut from a non-zero start offset, because that is the case a seek defect would affect.
4. THE Clipper SHALL verify sync for a clip whose source has a variable frame rate.
5. THE Clipper SHALL verify sync for a clip that passed through a keep-interval concat.
6. THE Clipper SHALL report the measured Sync_Offset rather than only a pass or fail.
7. THE Clipper SHALL fail its verification WHERE the absolute Sync_Offset exceeds a documented tolerance.
8. THE Clipper SHALL NOT assert that the current seek behaviour is defective; the verification records what is measured.

### Requirement 5: Human preference is collected in a structured way (M12)

**User Story:** As a maintainer, I want a repeatable way to ask whether one render is better than another, so that decisions about framing, pacing, and grade rest on more than one person's impression.

#### Acceptance Criteria

1. THE Clipper SHALL produce pairs of clips rendered from identical source content under two named configurations.
2. THE Clipper SHALL present the two clips of a Preference_Trial without revealing which configuration produced which.
3. THE Clipper SHALL randomise presentation order within each Preference_Trial.
4. THE Clipper SHALL allow a decline, and SHALL record declines in the Preference_Report rather than discarding them.
5. THE Clipper SHALL record the number of trials, the number of distinct judges, and the split, and SHALL NOT report a preference as significant.
6. THE Clipper SHALL state in the Preference_Report that a small trial count cannot distinguish a real preference from noise.
7. THE Clipper SHALL support restricting a Preference_Trial set to a single named dimension, so a judgement is about one change rather than an accumulation.
8. THE Clipper SHALL work offline and SHALL NOT require any hosted service.
9. THE Clipper SHALL NOT require the judge to be the person who made the change, and SHALL record when they were.

### Requirement 6: The instruments are usable from one place

**User Story:** As a contributor, I want these measurements runnable the way the existing harnesses are, so that they get used.

#### Acceptance Criteria

1. THE Clipper SHALL expose each instrument through a script consistent with the existing `scripts/eval_*.py` interface.
2. THE Clipper SHALL support a subcommand structure comparable to `scripts/eval_selection.py`'s `template` / `validate` / `run` / `compare`.
3. THE Clipper SHALL document each instrument, what it can conclude, and what it cannot.
4. THE Clipper SHALL name in its documentation which measurements require which ffmpeg capabilities.
5. THE Clipper SHALL emit machine-readable output suitable for committing and diffing.
6. THE Clipper SHALL NOT require a GPU, a network connection, or a model checkpoint for any instrument.

### Requirement 7: The instruments are themselves verified

**User Story:** As a maintainer, I want the measurement code tested against known inputs, so that a broken instrument does not silently authorise a bad change.

#### Acceptance Criteria

1. THE Clipper SHALL include a test that a render compared against itself yields the maximum SSIM and an infinite or maximal PSNR.
2. THE Clipper SHALL include a test that a deliberately degraded render scores worse than a faithful one on every available Fidelity_Metric.
3. THE Clipper SHALL include a test that Alignment_Error is zero for caption events constructed at the labelled times, and non-zero by a known amount when shifted by that amount.
4. THE Clipper SHALL include a test that Sync_Offset detection identifies a deliberately introduced offset of a known size.
5. THE Clipper SHALL run the real ffmpeg for every metric test rather than mocking its output.
6. THE Clipper SHALL cross-check every parsed value through an independent mechanism, per Requirement 1.7.
7. THE Clipper SHALL NOT introduce a test that is skipped when its dependencies are present.
8. THE Clipper SHALL NOT introduce a new warning into the test run.
9. THE Clipper SHALL record a capability-unavailable outcome as an explicit reported state, never as a pass.
