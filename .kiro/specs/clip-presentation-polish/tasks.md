# Implementation Plan — Clip Presentation Polish

Incremental, test-first coding steps. Execute **one task at a time**, in order.

**The defining constraint of this spec: nothing here has an objective metric.** SSIM cannot
tell you a face is framed better — it reports that moving the crop made the render *less*
similar to the old one, which is the intended change. So **every feature lands off**, and
every default change is gated on `render-quality-measurement`'s pairwise preference harness
(`M12`). That is six trials for six changes, which is the honest cost of improving things no
instrument can score.

This ordering is deliberate: the code can land complete and inert without the harness
existing. Only task 9 needs it.

Three orderings are load-bearing and are not implementation details: headroom **after**
smoothing, presence and turn gain **before** loudnorm, and cue constraints **without**
touching word spans.

Tasks marked `*` are optional test sub-tasks. Property tests use `hypothesis` with
`@settings(max_examples=100)`, one property per test, tagged
`# Feature: clip-presentation-polish, Property N: <text>`.

**Before starting, record the baseline:** `pytest` → **2030 passed, 0 skipped, 0 warnings**;
`cd frontend && npm run test:run` → **141 passed**.

## Tasks

- [ ] 1. Headroom framing (V22)
  - [ ] 1.1 Derive the Eye_Line from the detected face box
    - Not a fixed pixel offset: a detected box scales with subject distance, so an offset that
      looks right on a close-up is wrong on a wide shot — which is the same footage task 2 is
      about.
    - _Requirements: 1.2_

  - [ ] 1.2 Apply Headroom_Bias **after** `smooth_centers`, inside the clamp
    - `reframe.py:487` currently centres the crop on the face centre. Folding the bias into
      sample centres *before* EMA smoothing makes it part of the smoothed signal, where at
      `alpha=0.35` with `reset_at` shot-change breaks it gets attenuated and re-converged at
      every cut. The bias is a constant compositional offset, not a signal to track.
    - Applying it after smoothing also keeps the smoother byte-identical, so `V4`'s shot-change
      reset needs no revisiting.
    - The bias must sit **inside** the existing `_clamp(..., 0, max_y)`. `reframe.py:148`
      records the related lesson — *"Order is fixed: convert, then clamp, then test for
      degeneracy"* — and a mutation once moved that test to the wrong side of the clamp. Same
      hazard, same place.
    - _Requirements: 1.1, 1.4, 1.5_

  - [ ] 1.3 Express the bias as a fraction of crop height; skip it with no face
    - With no subject there is no Eye_Line, and biasing a fallback centre crop just moves the
      frame up for no reason.
    - _Requirements: 1.3, 1.6_

  - [ ] 1.4 Apply to split-screen too, or record that it was not applied
    - Tiles are composed by `apply_speaker_reframe` at `split_screen_max_regions=2`. Headroom
      within a tile is the same idea at a different scale; applying it to one path silently
      produces inconsistent framing inside one output.
    - _Requirements: 1.10_

  - [ ] 1.5 Default the bias to zero and record what was applied
    - Zero reproduces v0.11.0 framing exactly. The value is decided in task 9.
    - _Requirements: 1.7, 1.9, 8.1_

  - [ ] 1.6* Test: geometry and smoother invariance → `tests/test_reframe_geometry.py`
    - Eye_Line above the midpoint; bias cannot produce an out-of-frame crop; bias applied after
      smoothing, exact and unattenuated; `smooth_centers` output byte-identical to v0.11.0.
    - _Requirements: 10.1, 10.2_

- [ ] 2. Subject-scale normalisation (V23)
  - [ ] 2.1 Measure Subject_Scale per shot using the existing cut list
    - Reuse `scene_detect.scan_cuts` — the same cuts `V4` already maps to sample indices for the
      EMA reset. A second shot-boundary mechanism would drift from the first.
    - _Requirements: 2.1, 2.5_

  - [ ] 2.2 Adjust crop size between shots only, bounded
    - **Never within a shot** (R2.4): changing crop size during continuous footage *is* a zoom,
      and the project already has zoom and ken-burns with their own easing. Two scale changes on
      one shot compound into something neither intended.
    - Bound the adjustment so one outlying detection cannot drive an extreme crop; never scale
      beyond available pixels; leave faceless shots alone rather than guessing.
    - _Requirements: 2.2, 2.3, 2.4, 2.6, 2.7_

  - [ ] 2.3 Ensure no compounding with zoom or ken-burns
    - _Requirements: 2.10_

  - [ ] 2.4 Default off, record when a crop size was altered
    - The least certain item in this spec: a director may have *chosen* to alternate between
      close and wide, and normalising that removes an intentional edit.
    - _Requirements: 2.8, 2.9, 8.1_

  - [ ] 2.5* Test: between-shot only, bounded → `tests/test_reframe_geometry.py`
    - _Requirements: 2.4, 2.3, 2.7, 2.10_

- [ ] 3. Content classification (V24)
  - [ ] 3.1 Add `worker/content_class.py` using signal features only
    - Screen content has strong tells: large flat regions, high-contrast text edges, a
      near-static background with localised change, and histograms with a few dominant spikes
      rather than a continuous distribution. Camera footage has sensor noise, continuous
      gradients, and global motion.
    - No checkpoint, no network — consistent with how proxy signals are handled elsewhere.
    - _Requirements: 3.1, 3.2_

  - [ ] 3.2 Classify per clip, not per source
    - A podcast cutting between cameras and a shared screen is the normal case for this feature;
      a per-source decision would be wrong for half the clips.
    - _Requirements: 3.6_

  - [ ] 3.3 Route screen/graphics content to Fit_Mode and skip face tracking
    - `crop_blur` already exists as fit-with-background; this is the first time anything chooses
      it on content grounds. Reuse `detect_letterbox` rather than re-deriving bar geometry — a
      pillarboxed camera shot and a 16:9 slide both have bars, so these are different questions
      answered by one existing mechanism.
    - _Requirements: 3.3, 3.4, 3.12_

  - [ ] 3.4 Unknown means unchanged; expose an override
    - On genuinely ambiguous footage — a whiteboard talk, a plain studio backdrop, a flat graded
      shot, a slide with embedded video — a person knows and the heuristic does not.
    - _Requirements: 3.5, 3.8_

  - [ ] 3.5 Expose the class for other components to consume
    - `clip-signal-fidelity` R10.9 uses it to refuse stabilising a screen recording, where
      `vidstab` finds spurious motion in scrolling text and introduces wobble that was not in the
      source.
    - _Requirements: 3.7, 3.11_

  - [ ] 3.6 Measure misclassification, then decide the default
    - **Report** the rate; do not assert accuracy. Enable automatically **only if** camera
      handling does not degrade — the asymmetry is intentional: wrongly fitting camera footage
      puts blurred bars on a clip that was fine, which is worse than continuing to crop a slide
      the way we do today.
    - _Requirements: 3.9, 3.10_

  - [ ] 3.7* Test: real footage, both classes → `tests/test_content_class.py`
    - Real ffmpeg-rendered screen-like and camera-like footage through the real classifier.
      Assert the class is exposed such that a consumer can refuse synthetic content.
    - _Requirements: 10.3, 3.11_

- [ ] 4. Cue duration and reading rate (C24)
  - [ ] 4.1 Add minimum Cue duration and maximum Reading_Rate
    - `words_to_cues` has only *ceilings* — `max_words=3`, `max_gap=0.6`, `max_duration=3.0`,
      `too_wide`. Nothing is a floor, so fast speech yields a ~0.3 s three-word cue.
    - _Requirements: 4.1, 4.2_

  - [ ] 4.2 Extend cues into available space, never into the next cue
    - Never past the clip end either.
    - _Requirements: 4.3, 4.4, 4.6_

  - [ ] 4.3 Implement the constraint hierarchy, and record the relaxation
    - Four constraints now compete across two specs. **Cue non-overlap wins over everything**
      (R4.5): two overlapping cues render as two simultaneous ASS dialogue events — visibly
      broken, not merely suboptimal — whereas a short cue is only fast.
    - Order: cue non-overlap → word-span non-overlap (`clip-quality-uplift` R8.2) → minimum cue
      duration → minimum word span (`clip-quality-uplift` R8.3). Record **which** constraint was
      relaxed so a debugging operator can see why a cue is short.
    - _Requirements: 4.5_

  - [ ] 4.4 Do not move word spans when extending a cue
    - **The subtle one.** A cue's display window and its words' karaoke timings are different
      things. Extending the window while leaving `\kf` on speech keeps the last word highlighted
      slightly longer — correct. Stretching word spans to fill the extended cue drifts the
      highlight off the speech, which is the exact defect `clip-quality-uplift`'s onset snapping
      exists to reduce. Doing both would have one feature undo the other.
    - _Requirements: 4.8_

  - [ ] 4.5 Merge as the fallback, bounded by the width budget
    - When extension is impossible. `TextFit` already measures the budget, so a merge cannot
      produce an overflowing cue.
    - _Requirements: 4.7_

  - [ ] 4.6 Apply on every caption path including kinetic; guarantee bit-identity
    - `engines/kinetic.py` has its own timing logic; a constraint applied only to the main path
      leaves the kinetic path with the defect and the two diverge. An already-compliant sequence
      must return **bit-identical**, which is what allows unconditional application without
      moving a golden.
    - _Requirements: 4.9, 4.10_

  - [ ] 4.7 Default to values reproducing v0.11.0; record extends and merges
    - _Requirements: 4.11, 4.12, 8.1_

  - [ ] 4.8* Property tests: cue invariants → `tests/test_caption_cues.py`
    - **Property 1** — cues never overlap after constraint enforcement, for arbitrary input.
    - **Property 2** — word span times are never altered by cue extension.
    - **Property 3** — an already-compliant sequence returns bit-identical.
    - _Requirements: 10.4, 10.5_ · _Properties: P1, P2, P3_

  - [ ] 4.9* Test: the hierarchy under conflict → `tests/test_caption_cues.py`
    - Construct the case where minimum duration and non-overlap disagree. Assert non-overlap
      wins **and** the relaxation is recorded. A test where they agree proves nothing.
    - _Requirements: 4.5_

- [ ] 5. Linguistic line breaking (C25)
  - [ ] 5.1 Prefer linguistic Break_Candidates among breaks that fit
    - A stop-word and function-word list plus capitalisation runs for proper nouns. No
      checkpoint, no network.
    - _Requirements: 5.1, 5.2, 5.3, 5.7_

  - [ ] 5.2 Keep width and the line budget above any preference
    - This is a *preference among fitting breaks*, never a licence to overflow. And **never drop
      or reorder words** to achieve a nicer break — `captions.py` already records that without
      measured fitting "the wrap below has to drop words to stay inside the frame, which is a
      caption missing its ending." Not repeating that.
    - _Requirements: 5.4, 5.5, 5.6_

  - [ ] 5.3 Restrict to languages with rules; fall back to width
    - `script_support.py` already reports `caption_script_unsupported` for scripts nothing
      vendored can render. This must not add a second, quieter language assumption.
    - _Requirements: 5.8_

  - [ ] 5.4 Default off; guarantee bit-identity when the width break already matches
    - _Requirements: 5.9, 5.10, 8.1_

  - [ ] 5.5* Test: preference, fallback, and invariance → `tests/test_caption_lines.py`
    - Preferred break chosen when it fits; width wins when it does not; no words dropped or
      reordered; unsupported language falls back; matching case bit-identical.
    - _Requirements: 5.4, 5.5, 5.6, 5.8, 5.10_

- [ ] 6. Speech presence chain (AU11)
  - [ ] 6.1 Add the Presence_Chain — spectral shaping plus dynamic control
    - `effects/audio.py`'s only spectral shaping today is a `lowpass`. A phone speaker
      reproduces almost nothing below ~500 Hz, so part of what `loudnorm` measures is energy the
      viewer cannot hear. A 2–5 kHz presence lift raises *intelligibility* rather than level —
      `loudnorm` cannot substitute, because it is a level operation and this is a spectral
      problem.
    - _Requirements: 6.1, 6.4_

  - [ ] 6.2 Place it **before** loudness normalisation
    - Two-pass `loudnorm` measures then corrects. Shaping after measurement would make the
      measured value describe a signal that no longer exists, and the delivered clip would miss
      its LUFS target.
    - Change nothing about `AU1` two-pass loudnorm or `AU3` true-peak limiting.
    - _Requirements: 6.2, 6.3_

  - [ ] 6.3 Apply to speech only, and add no pass
    - Not the music bed — it already has `bed_fit_filter` and sidechain ducking, and
      presence-shaping music is an arbitrary EQ on someone's track. Filter-graph work inside the
      compositor's existing single pass.
    - _Requirements: 6.5, 6.11_

  - [ ] 6.4 Default off; record strength when it runs
    - _Requirements: 6.6, 6.8, 8.1_

  - [ ] 6.5* Test: loudness and true peak from the rendered file → `tests/test_audio_presence.py`
    - Measure **integrated loudness and true peak from the output**, not from the filter string.
      Presence boosts add peaks, and `alimiter` runs with `level=disabled` deliberately so it does
      not re-normalise — that interaction must be measured, not assumed. Cross-check
      independently of the filter builder.
    - _Requirements: 6.9, 6.10, 10.6, 10.9_

- [ ] 7. Per-speaker level matching (AU12)
  - [ ] 7.1 Measure per-Speaker_Turn loudness within the clip
    - `loudnorm` normalises the *clip*, so by construction it cannot fix balance *within* it: a
      clip correct at −14 LUFS integrated can still have one speaker 8 dB below the other.
    - _Requirements: 7.1_

  - [ ] 7.2 Apply bounded, confidence-gated Turn_Gain
    - Diarisation here is a **transcript proxy** — offline, CPU-only, capped at
      `diarization_max_speakers=2`, not `pyannote` (`T6` is an unimplemented seam). A
      misattributed turn with unbounded gain is a large audible jump. Bound it; skip
      low-confidence intervals.
    - _Requirements: 7.2, 7.3, 7.7_

  - [ ] 7.3 Ramp, never step
    - A step at a turn boundary is an audible click. Same reasoning as `filler._seam_fades`
      applying `afade` at interior seams — and deliberately not `acrossfade`, which would shift
      the timeline. Ramp within the turn; do not cross-fade between turns.
    - _Requirements: 7.4_

  - [ ] 7.4 Apply on the **delivered** timeline
    - **The highest-risk item in this spec.** Filler removal, the `U4` cut list, and
      `clip-quality-uplift`'s interior-silence removal all shorten the clip; `rebase_turns` exists
      for exactly this. Gains applied at original-timeline positions put a guest's gain on the
      host's words — plausible-sounding and completely wrong.
    - _Requirements: 7.5_

  - [ ] 7.5 Before loudnorm; no extra pass; no diarisation side effect
    - `diarization` defaults to `False` and this must not silently enable it — that would add a
      per-source analysis stage to jobs that did not ask for one. When diarisation is off or
      reports one speaker, Turn_Gain is unavailable and **records why**.
    - _Requirements: 7.6, 7.10, 7.11, 7.12_

  - [ ] 7.6 Default off; record the gain range applied
    - _Requirements: 7.8, 7.9, 8.1_

  - [ ] 7.7* Test: ramp and timeline → `tests/test_audio_turn_gain.py`
    - Assert ramped not stepped, bounded, low-confidence skipped. Separately: construct a case
      where original and rebased turn positions **differ**, and assert gains land on the delivered
      timeline. A fixture where they coincide proves nothing.
    - _Requirements: 10.7, 7.5_

- [ ] 8. Cross-cutting: passes, markers, configuration
  - [ ] 8.1* Test: no feature here adds an encoding pass → `tests/test_pipeline_passes.py`
    - Everything in this spec is filter-graph or geometry work. Assert the encode count per clip
      is unchanged.
    - _Requirements: 10.8, 6.11, 7.10_

  - [ ] 8.2 Verify marker discipline
    - Every feature applied or skipped gets a marker naming the **resolved** value, never the
      requested one. Every pre-existing marker keeps its exact spelling — a renamed marker
      silently breaks consumers, and markers are the only mechanism explaining an absent feature.
    - _Requirements: 8.3, 8.4, 8.5_

  - [ ] 8.3 Document every new setting in `.env.example`
    - `tests/test_config_documentation.py` fails on an undocumented field or a documented
      non-setting. State for each default whether it is **measured or provisional** — at this
      point in the plan they are all provisional, and saying so stops the next person treating
      them as calibrated.
    - _Requirements: 9.1, 9.2, 9.3_

  - [ ] 8.4 Surface new options through API, form, and UI
    - `OptionsModel`, `/api/upload` form fields, `/api/info` domains, `App.jsx`
      `DEFAULT_SETTINGS` **and** `toOptions()`, `SettingsPanel.jsx`. Unrecognised values apply the
      documented default without raising. Keep every pre-existing option and default unchanged.
    - _Requirements: 8.6, 9.4, 9.5, 9.6_

  - [ ] 8.5* Property test: new option fields round-trip → `tests/test_options_roundtrip.py`
    - **Property 4** — every new field survives `from_dict(asdict(...))`, and any unrecognised
      value resolves to the documented default without raising.
    - _Requirements: 9.5, 9.6_ · _Properties: P4_

- [ ] 9. Decide the defaults by preference trial
  **Depends on `render-quality-measurement` task 6 (M12).**
  - [ ] 9.1 Run one single-dimension trial per proposed default
    - Six trials: Headroom_Bias, subject-scale normalisation, cue constraints, linguistic
      breaking, Presence_Chain, Turn_Gain. **One dimension per set** — judging an accumulation
      tells you nothing about any of them.
    - Blind, order-randomised, declines recorded. Report trials, judges, split, and whether the
      judge authored the change. **Do not claim significance**; a 4–2 split is noise.
    - _Requirements: 1.8, 2.8, 4.12, 5.9, 6.7, 7.8, 10.13_

  - [ ] 9.2 Judge the Presence_Chain on a phone speaker
    - The chain's entire premise is small-speaker reproduction. A trial conducted on studio
      monitors or good headphones measures something else and will likely reject a change that
      helps, because on good speakers a presence lift mostly just sounds brighter.
    - _Requirements: 6.7_

  - [ ] 9.3 Flip supported defaults, each in its own commit
    - One commit per default, fixtures re-frozen in that same commit, message naming what moved.
      Where a trial does not support a change, **leave the default off and record the finding** —
      "no visible difference" is the most useful outcome for a feature that costs render time.
    - _Requirements: 8.1, 8.2_

  - [ ] 9.4 Full gate run
    - `ruff check .` clean · `pytest` at **2030 + new, 0 skipped, 0 warnings** ·
      `cd frontend && npm run lint && npm run test:run && npm run build` ·
      `scripts/docker_smoke.sh`.
    - Triage any new warning at source with a **targeted** `filterwarnings` ignore and a comment
      saying why it cannot be fixed. Never broaden the existing ignores; never relax
      `filterwarnings = error`.
    - _Requirements: 10.10, 10.11_

  - [ ] 9.5 Add the mutation specification
    - `tests/mutations/clip-presentation-polish.json`. Attack the highest-risk item first, per
      the design: **turn gain on the original rather than the rebased timeline**. Then: apply
      headroom before smoothing; move the bias outside the clamp; let cue extension move word
      spans; prefer minimum duration over cue non-overlap; step turn gain instead of ramping;
      adjust crop scale within a shot; classify unknown content as screen. Each **CAUGHT**; an
      ESCAPE is a real test gap.
    - _Requirements: 10.12_

  - [ ] 9.6 Write the close-out
    - Follow `.kiro/specs/face-detection-upgrade/CLOSE_OUT.md`. Record every trial result
      including the null ones, and state plainly what remains: we still follow the largest,
      most-diarisation-active face rather than the person actually speaking (`V3`). **Better
      framing of the wrong subject is still the wrong subject**, and on two-person footage that
      remains the most visible weakness — no amount of headroom bias addresses it.
    - _Requirements: 10.13_
