# Implementation Plan — Render Quality Measurement

Incremental, test-first coding steps. Execute **one task at a time**, in order.

This spec adds **instruments only — no behavioural change to `worker/`**. That boundary is
deliberate: an instrument that reaches into the render path can be made to agree with it.
If a measurement here reveals a defect, the defect is fixed in the sibling spec that owns
it (`clip-signal-fidelity`, `clip-presentation-polish`, `clip-editorial-structure`).

**Task 1 comes first for a reason.** SSIM and PSNR are built-in ffmpeg filters; VMAF needs
`libvmaf`, which Debian's ffmpeg routinely lacks — and the `Dockerfile` deliberately does
not pin ffmpeg, because pinning "fails to resolve the moment a security update lands." So
capability resolution is not a detail to add later; it is the shape of the whole module.
This project has already paid for assuming otherwise: `golden_render.py`'s docstring records
that *"a capability probe hid 124 ffmpeg filters."*

Tasks marked `*` are optional test sub-tasks. Property tests use `hypothesis` with
`@settings(max_examples=100)`, one property per test, tagged
`# Feature: render-quality-measurement, Property N: <text>`.

**Before starting, record the baseline:** `pytest` → **2030 passed, 0 skipped, 0 warnings**;
`cd frontend && npm run test:run` → **141 passed**. A skip is not a pass.

## Tasks

- [ ] 1. Capability resolution for metric filters
  - [ ] 1.1 Resolve SSIM, PSNR, and VMAF through the existing probe
    - Add `evaluation/fidelity.py` with `available_metrics(report=None)` resolving
      `ffmpeg_filter:libvmaf` through `worker.engines.capabilities.get_report()`.
    - **Use the existing probe, do not add a second one.** The 124-hidden-filters defect
      happened because an answer was cached where nobody was looking. `capabilities.py`
      already guarantees totality (any string, never raises) and per-process caching.
    - Accept an injected prober so tests can simulate an absent `libvmaf` without needing a
      differently-built ffmpeg.
    - _Requirements: 1.3, 6.6_

  - [ ] 1.2 Treat VMAF absence as a reported state, never a failure or a pass
    - SSIM and PSNR are the floor and always available. An absent `libvmaf` is recorded with
      a **named reason** and the run continues. Follow `video_encoders.resolve_encoder`'s
      philosophy: probe for real, fall back, record, never fail the job.
    - _Requirements: 1.2, 1.4, 1.5_

  - [ ] 1.3* Test: metrics degrade without libvmaf → `tests/test_fidelity.py`
    - With an injected prober reporting `ffmpeg_filter:libvmaf` unavailable, assert SSIM and
      PSNR still report and VMAF is an **explicit unavailable state with a reason** — not a
      silently omitted key, and **not a pass**.
    - _Requirements: 1.4, 1.5, 7.9_

- [ ] 2. Fidelity measurement (M9)
  - [ ] 2.1 Implement SSIM/PSNR/VMAF measurement with per-frame parsing
    - Parse per-frame output, not just the summary line. Report **mean and minimum** for every
      metric: a mean of 0.98 is compatible with one frame at 0.4, which is exactly what a
      keyframe or scene-change encode decision produces and exactly what a viewer notices.
    - _Requirements: 1.1, 1.8_

  - [ ] 2.2 Refuse misaligned comparisons
    - Verify frame count and resolution match before comparing. A one-frame mismatch makes
      ffmpeg's `ssim` compare frame *N* against *N+1* for the whole remainder, producing a
      plausible, catastrophic, and completely misleading number. **Refuse and say why.**
    - _Requirements: 1.6_

  - [ ] 2.3 Build the Reference_Render path
    - The reference is the **same filter graph at higher fidelity** — same crop, same
      captions, same everything, much lower CRF and a slower preset — so the measurement
      isolates the encode. Comparing the Final_Render against the raw source would measure
      the reframe instead.
    - _Requirements: 1.1, 1.6_

  - [ ] 2.4 Record cost beside quality
    - Wall-clock encode time and output file size in every reading. A quality gain with a 2×
      time cost is a trade to discuss, not a win to announce.
    - _Requirements: 2.3_

  - [ ] 2.5* Test: self-comparison identity → `tests/test_fidelity.py`
    - A real render compared against **itself**: SSIM exactly 1.0, PSNR infinite. Any parsing
      error that scales, offsets, or misreads a field breaks this. **Independent cross-check** —
      the expectation shares no code with the parser.
    - _Requirements: 7.1, 7.5, 1.7_

  - [ ] 2.6* Test: degradation orders correctly → `tests/test_fidelity.py`
    - A real CRF-45 render scores worse than a faithful one on **every** available metric.
      Catches sign and ordering errors, which a self-comparison cannot.
    - Derive the expectation independently of the parser — the degraded render's ordering must be
      established from how it was produced, not from the code being tested.
    - _Requirements: 7.2, 7.5, 7.6_

  - [ ] 2.7* Test: minima react where means do not → `tests/test_fidelity.py`
    - One deliberately damaged frame moves the reported minimum substantially and the mean
      barely at all.
    - _Requirements: 1.8_

  - [ ] 2.8* Test: misalignment is refused → `tests/test_fidelity.py`
    - Mismatched frame counts and mismatched resolutions each refuse rather than report.
    - _Requirements: 1.6_

- [ ] 3. Fidelity reporting and comparison
  - [ ] 3.1 Emit a Fidelity_Report with full provenance
    - ffmpeg version, resolved encoder, CRF, preset, resolution, code revision (R2.2), plus
      time and size from task 2.4. Machine-readable, committable, diffable.
    - _Requirements: 2.1, 2.2, 6.5_

  - [ ] 3.2 State the cross-version caveat in the report itself
    - Readings are **not comparable across ffmpeg versions**, a direct consequence of the
      deliberate no-pin decision. Two reports from different builds are two different
      experiments.
    - _Requirements: 2.6_

  - [ ] 3.3 Add `compare`, naming every metric that moved
    - Both directions. `compare` must **refuse to subtract** readings taken on different
      ffmpeg builds, per task 3.2 — silently differencing them would be the exact error the
      caveat exists to prevent.
    - _Requirements: 2.7_

  - [ ] 3.4 Commit the v0.11.0 reference reading
    - Baseline at the current configuration: `x264_preset=veryfast`, `x264_crf=20`,
      `output_fps=30`, default scaler. This is the number every sibling spec's encoder change
      will be measured against.
    - _Requirements: 2.4_

  - [ ] 3.5 Do not add a CI quality gate
    - No absolute SSIM/VMAF threshold. Same reasoning as `clip-quality-uplift` R2.8: it would
      either never fire or block unrelated work. These are recorded baselines and relative
      comparisons.
    - _Requirements: 2.8_

  - [ ] 3.6* Test: reproducibility on a fixed build → `tests/test_fidelity.py`
    - Identical inputs on the same ffmpeg build produce identical readings.
    - _Requirements: 2.5_

- [ ] 4. Caption alignment error (M10)
  - [ ] 4.1 Create the labelled caption-timing set
    - Hand-marked word times on a short passage, or synthesised speech at known timings.
      **Must not be derived from the ASR being evaluated** — that measures self-consistency.
      Document which method was used: synthetic overstates accuracy on real speech,
      hand-marked carries human variance.
    - _Requirements: 3.5_

  - [ ] 4.2 Measure the events actually rendered
    - Parse caption events back out of the generated ASS, or read the SRT sidecar
      `subtitle_export.py` produces — **not** the intermediate word list. Every transform
      between word list and screen is in scope: `words_to_cues` grouping,
      `_ass_timestamp` centisecond rounding, `\kf` fill durations, and any snapping a sibling
      spec adds. Measuring the word list would exclude the layers most likely to introduce
      error.
    - Record the inherent ±5 ms floor from `_ass_timestamp`'s centisecond rounding, so nobody
      chases 3 ms of "drift" that is the format.
    - _Requirements: 3.4_

  - [ ] 4.3 Report a signed distribution
    - Mean, median, p90, max — **signed** (R3.3). A systematic +150 ms lag and symmetric
      ±150 ms jitter give the same mean absolute error and are different defects with
      different fixes: a lag is one constant compensation, jitter needs alignment. Absolute
      values destroy exactly the information that distinguishes them.
    - _Requirements: 3.1, 3.2, 3.3_

  - [ ] 4.4 Do not reuse WER normalisation for time matching
    - `evaluation/wer.py`'s normalisation merges and drops tokens, and a merged token has no
      single true time. Correct for WER, wrong here.
    - _Requirements: 3.8_

  - [ ] 4.5 Report unmatched events rather than excluding them
    - Silently dropping the events that could not be matched is how a metric improves while
      the output gets worse.
    - _Requirements: 3.7_

  - [ ] 4.6 Emit and commit an Alignment_Report for v0.11.0
    - The baseline `clip-quality-uplift`'s onset-snapping work (its task 6) will be measured
      against. Without this number that work cannot be judged either.
    - _Requirements: 3.6_

  - [ ] 4.7* Test: zero at the labels, exact under shift → `tests/test_caption_timing.py`
    - Events constructed at the labelled times → 0.0. Events shifted by 120 ms → **+120 ms
      signed**, not 120 ms absolute.
    - _Requirements: 7.3, 3.3_

  - [ ] 4.8* Test: unmatched events are counted → `tests/test_caption_timing.py`
    - _Requirements: 3.7_

- [ ] 5. A/V sync verification (M11)
  - [ ] 5.1 Build fixtures with a synchronised transient in both streams
    - An ffmpeg-generated clap paired with a frame-level flash, so a real offset is
      measurable from the decoded file.
    - _Requirements: 4.1, 4.2_

  - [ ] 5.2 Measure from the rendered file's streams
    - Decode and compare the audio event's position against the visual event's. **A test
      asserting the argument list contains `-ss` proves nothing about the output.**
    - _Requirements: 4.2_

  - [ ] 5.3 Report the measured offset, not just a verdict
    - A run reporting 8 ms differs meaningfully from one reporting 0 ms, and only one is a
      trend. Fail only above a documented tolerance.
    - _Requirements: 4.6, 4.7_

  - [ ] 5.4* Test: a known offset is detected → `tests/test_sync.py`
    - A deliberate 200 ms offset measures as 200 ms; a synchronised clip reports ≈ 0 within
      tolerance.
    - _Requirements: 7.4, 7.5_

  - [ ] 5.5* Test: the three drift-prone paths → `tests/test_sync.py`
    - One test each, because each exercises a different mechanism:
      **(a)** a clip cut from a non-zero start — the seek path, plus audio-priming behaviour
      that varies between ffmpeg versions;
      **(b)** a VFR source — `output_fps=30` resamples VFR to CFR, and `config.py`'s own
      comment says VFR is "every screen recording and most phone footage"; resampling video
      without touching audio is a classic drift source;
      **(c)** a keep-interval concat — `filler.apply_keep_intervals` uses `afade` rather than
      `acrossfade` *specifically because* a crossfade would shift the timeline. That reasoning
      is sound and should be verified rather than trusted, especially since
      `clip-quality-uplift`'s interior-silence work will create far more seams.
    - _Requirements: 4.3, 4.4, 4.5_

  - [ ] 5.6 Record what was measured without alleging a defect
    - `cut_segment` uses `-ss` before `-i`, which is accurate under re-encoding in modern
      ffmpeg. This spec makes **no claim it is broken** — it observes that nothing measured
      it. Write the finding, whatever it is.
    - _Requirements: 4.8_

- [ ] 6. Pairwise preference harness (M12)
  - [ ] 6.1 Generate blind, order-randomised pairs
    - Two renders of identical source content under two named configurations. Blind
      presentation (R5.2) and randomised order within each trial (R5.3): knowing which is
      "the new one" produces the expected answer, and fixed order produces position bias.
    - _Requirements: 5.1, 5.2, 5.3_

  - [ ] 6.2 Restrict a set to one named dimension
    - Judging an accumulation of five changes tells you nothing about any of them.
    - _Requirements: 5.7_

  - [ ] 6.3 Record declines as data
    - "No visible difference" is the **most useful** finding for a change that costs render
      time. Discarding declines manufactures a preference.
    - _Requirements: 5.4_

  - [ ] 6.4 Report counts and refuse to claim significance
    - Trials, distinct judges, the split, and whether the judge authored the change (R5.9 —
      not forbidden, usually unavoidable here, but a reader must be able to discount it).
      State in the report that a small trial count **cannot** distinguish a real preference
      from noise. A 4–2 split is noise and the report must not imply otherwise.
    - _Requirements: 5.5, 5.6, 5.9_

  - [ ] 6.5 Keep it offline
    - A local static page plus a JSON results file. No hosted service — that would be a
      dependency and a privacy question for what is fundamentally a directory of clip pairs.
    - _Requirements: 5.8, 6.6_

  - [ ] 6.6* Test: blinding, randomisation, declines → `tests/test_preference.py`
    - **Property 1** — presentation order is randomised across trials and is not a fixed
      function of the configuration.
    - Assert blinding holds in the generated artefacts, declines are counted, and the output
      contains no significance claim.
    - _Requirements: 5.2, 5.3, 5.4, 5.5_ · _Properties: P1_

- [ ] 7. Interface, documentation, and close-out
  - [ ] 7.1 Add the scripts with the established subcommand shape
    - `scripts/eval_fidelity.py` and `scripts/eval_captions.py` with
      `template`/`validate`/`run`/`compare`, mirroring `scripts/eval_selection.py`;
      `scripts/preference.py` with `build`/`report`. Machine-readable output.
    - _Requirements: 6.1, 6.2, 6.5_

  - [ ] 7.2 Document what each instrument can and cannot conclude
    - Extend `eval/README.md`. State plainly: a Fidelity_Metric measures **reproduction, not
      quality** (R1.9) — a beautifully framed, badly encoded clip scores low, and a perfect
      reproduction of a badly framed reference scores 1.0. Name which measurements need which
      ffmpeg capabilities.
    - _Requirements: 1.9, 6.3, 6.4_

  - [ ] 7.3 Full gate run
    - `ruff check .` clean · `pytest` at **2030 + new, 0 skipped, 0 warnings** ·
      `cd frontend && npm run lint && npm run test:run && npm run build` ·
      `scripts/docker_smoke.sh`.
    - Triage any new warning at its source with a **targeted** `filterwarnings` ignore and a
      comment saying why it cannot be fixed. Never broaden the existing ignores; never relax
      `filterwarnings = error`.
    - _Requirements: 7.7, 7.8_

  - [ ] 7.4 Add the mutation specification
    - `tests/mutations/render-quality-measurement.json`. Highest-value mutations: return the
      mean where the minimum is required; invert the degradation comparison; report absolute
      instead of signed alignment error; skip the frame-count alignment check; treat an absent
      `libvmaf` as a pass; drop declines from the preference tally. Each should be **CAUGHT**;
      an ESCAPE is a real gap in the tests, not a mutation to delete.
    - _Requirements: 7.1, 7.2, 7.3, 7.9_

  - [ ] 7.5 Write the close-out, including the boundary
    - Follow `.kiro/specs/face-detection-upgrade/CLOSE_OUT.md`. State plainly what this does
      **not** deliver: it measures fidelity, timing, and sync, and **cannot** measure whether
      a clip is worth watching. `M12` gestures at that and is explicitly too small to be
      conclusive.
    - The honest summary to record: this spec makes render changes **arguable with evidence,
      not decidable by metric**. That is the difference between the current situation and a
      good one — not the difference between guessing and knowing.
    - _Requirements: 1.9, 5.6, 6.3_
