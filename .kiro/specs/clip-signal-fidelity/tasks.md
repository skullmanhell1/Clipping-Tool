# Implementation Plan — Clip Signal Fidelity

Incremental, test-first coding steps. Execute **one task at a time**, in order.

**Ordering rationale.** Group A (colour) comes first because it fixes output that is
currently *wrong*, not merely improvable, and because it needs no preference judgement to
justify. Groups B and C come after and **depend on `render-quality-measurement`**: there is no
`vmaf`, `psnr`, or `ssim` anywhere in this repository today, so without that instrument the
preset and scaler changes are taste asserted against taste. Task 5 (frame-rate policy)
additionally depends on that spec's Sync_Offset verification, because frame-rate handling is
the most likely place to introduce A/V drift and drift desynchronises every burned caption.

**Verification discipline for this whole spec: probe the rendered file, never assert on the
argument list.** `-colorspace bt709` appearing in argv proves a flag was passed, not that the
muxed file carries it, and those differ across ffmpeg versions and containers.

Tasks marked `*` are optional test sub-tasks. Property tests use `hypothesis` with
`@settings(max_examples=100)`, one property per test, tagged
`# Feature: clip-signal-fidelity, Property N: <text>`.

**Before starting, record the baseline:** `pytest` → **2030 passed, 0 skipped, 0 warnings**;
`cd frontend && npm run test:run` → **141 passed**.

## Tasks

- [ ] 1. Read the colour fields we already fetch (O13 groundwork)
  - [ ] 1.1 Extend the probed record with Source_Colour
    - Add transfer function, primaries, matrix, and range to `MediaInfo`. `probe()` already
      runs `ffprobe -show_format -show_streams`, so **these fields are already in the JSON it
      parses** — this is a field-reading change, not a new probe. Do not add an `ffprobe`
      invocation.
    - Append the fields **defaulted and last**, matching the discipline `MediaInfo`'s own
      comment records for `video_codec`/`audio_codec`: several tests construct it positionally.
    - _Requirements: 1.1, 1.2, 1.3, 1.5_

  - [ ] 1.2 Classify HDR conservatively
    - HDR only from the reported transfer function — PQ (`smpte2084`) or HLG
      (`arib-std-b67`). **Never infer HDR from bit depth or resolution**: 10-bit Rec.709 is
      common and 4K SDR is the norm, so either inference would misfire on a large class of
      ordinary footage.
    - An unrecognised transfer function is **unknown**, not SDR. An absent field is unknown,
      not a guess. The failure modes are asymmetric — tone-mapping a mislabelled SDR source
      destroys it far more visibly than failing to tone-map an HDR one.
    - This is the conservatism `worker/language.py` already applies when it declines to report
      a language for Han script, and `script_support.py` when it reports
      `caption_script_unsupported` rather than substituting a font that cannot render the text.
    - _Requirements: 1.4, 1.6, 1.7_

  - [ ] 1.3* Test: real sources, honest classification → `tests/test_probe_colour.py`
    - Real ffmpeg-generated sources with explicit colour signalling. PQ and HLG classify HDR;
      **10-bit Rec.709 and 4K SDR do not**; an unrecognised transfer and an absent field both
      report unknown.
    - _Requirements: 1.1, 1.4, 1.6, 1.7, 13.6_

- [ ] 2. Tone-mapping (O13)
  - [ ] 2.1 Resolve filter availability through the existing probe
    - `zscale` needs `libzimg` at build time and is **not guaranteed present** — the
      `Dockerfile` deliberately does not pin ffmpeg. Resolve via
      `worker.engines.capabilities` with `ffmpeg_filter:<name>` ids.
    - **Do not add a second probe.** `golden_render.py`'s docstring records that *"a capability
      probe hid 124 ffmpeg filters"* — that incident is why `capabilities.py` exists.
    - _Requirements: 2.3, 11.1_

  - [ ] 2.2 Insert the tone-map first among colour-dependent operations, and before scaling
    - Two independent reasons: any grade or LUT applied to PQ-coded values does something
      arbitrary, and scaling in the wrong transfer function interpolates perceptual quantities
      rather than light, producing haloes that survive to the output.
    - _Requirements: 2.1, 2.2_

  - [ ] 2.3 Guarantee at most one tone-map per clip
    - The compositor is one pass but the pipeline is three. Tone-mapping at the cut **and** at
      the composite compresses the range twice and yields a muddy, flat result — worse than not
      tone-mapping, and frustratingly plausible-looking.
    - _Requirements: 2.8_

  - [ ] 2.4 Degrade with a named marker; never fail the job
    - Missing filters → deliver untone-mapped, record the marker naming the missing capability,
      continue. Record the applied case too, naming the detected transfer function.
    - _Requirements: 2.4, 2.5, 2.9, 11.2, 11.3_

  - [ ] 2.5 Never tone-map SDR or unknown
    - _Requirements: 2.6, 2.7_

  - [ ] 2.6 Expose operator and target peak; default tone-mapping ON
    - The right operator and peak are content-dependent and contested; hard-coding one bakes in
      a taste. **Default on** (R2.11) — this deliberately breaks the project's
      default-to-shipped-behaviour rule, because that rule exists to protect goldens from
      accidental change, not to protect a defect. Own commit, fixtures re-frozen there.
    - _Requirements: 2.10, 2.11, 12.1, 12.2_

  - [ ] 2.7* Test: HDR end to end, probing the output → `tests/test_colour_pipeline.py`
    - Real HDR-signalled source through the real pipeline; **probe the delivered file** for
      Rec.709 metadata. Not an argv assertion.
    - _Requirements: 13.1, 13.2_

  - [ ] 2.8* Test: absence and doubling → `tests/test_colour_pipeline.py`
    - With `ffmpeg_filter:zscale` unavailable via an injected prober: clip still delivered,
      marker names the missing capability, job does not fail. Separately: assert **exactly one**
      tone-map across all three passes.
    - _Requirements: 2.4, 2.5, 2.8_

- [ ] 3. Colour tagging and range (O14, O15)
  - [ ] 3.1 Write Colour_Tags describing what was delivered
    - **Not what arrived.** After tone-mapping, output is Rec.709 and must say so. Copying the
      source's `smpte2084` onto a tone-mapped file tells players to apply an HDR EOTF to SDR
      content — *worse* than no tags, because now the player is confidently wrong.
    - Through the single existing argument builder, so the `libx264`/`-crf` drift pin keeps
      holding.
    - _Requirements: 3.1, 3.2, 3.5_

  - [ ] 3.2 Resolve range explicitly and record what was applied
    - Phone footage is frequently full-range; passing `pc` through crushes blacks or lifts the
      image. Where the source is silent, apply the documented default and **record which** — "we
      assumed limited" is a fact a debugging operator needs.
    - _Requirements: 3.3, 3.7_

  - [ ] 3.3 Do not contradict the existing compatibility contract
    - No change to pixel format, profile, or level behaviour from `O1`/`O2`; no tags that
      contradict `-pix_fmt yuv420p`.
    - _Requirements: 3.4, 3.6, 11.6_

  - [ ] 3.4* Test: tags match delivered content → `tests/test_output_compat.py`
    - Probe the muxed file. Assert tone-mapped output declares Rec.709, that no tag contradicts
      the pixel format, and that the drift pin still holds.
    - _Requirements: 3.2, 3.6, 3.8, 13.1_

- [ ] 4. Encoder preset and scaler — measured, not assumed (O16, O17)
  **Depends on `render-quality-measurement` tasks 2–3.**
  - [ ] 4.1 Measure presets against the fidelity instrument
    - `x264_preset` is `"veryfast"` (`config.py:354`), paid **three times per clip**. Measure
      `veryfast` / `faster` / `fast` / `medium` with SSIM/PSNR/VMAF, **plus wall-clock time and
      file size** for each.
    - Resist just making the change. "Slower is better" is a general truth that can be
      **invisible on specific footage**, and `medium` is roughly 2× the encode time, three times
      over.
    - _Requirements: 4.1, 4.2_

  - [ ] 4.2 Change the preset default only if measured, in its own commit
    - Record the time cost in the change so an operator can choose speed knowingly. **Do not
      touch CRF** — CRF and preset interact, and moving both makes neither attributable
      (`clip-quality-uplift` R12 owns intermediate CRF).
    - Own commit, fixtures re-frozen there, message naming what moved. A default change bundled
      with behavioural work is how a golden gets frozen around a real regression — the
      `font_substituted:Arial` failure mode.
    - Keep the preset configurable either way, so an operator who wants speed can have it.
    - _Requirements: 4.3, 4.4, 4.5, 4.6, 4.7_

  - [ ] 4.3 Set Scaler_Flags explicitly, identically, everywhere
    - Every `scale=` currently runs swscale's default bicubic: `ffmpeg_utils.py` :569, :574,
      :577, :630, :640, :763 and `reframe.py` :1465, :1978, :2038.
    - **The same flags on every scale in a job** (R5.3) — with three passes and several scaling
      sites, two stages resampling differently produces compounding softness nobody can
      attribute to a stage.
    - _Requirements: 5.1, 5.2, 5.3, 5.7_

  - [ ] 4.4 Change nothing about geometry
    - Algorithm only. No dimension, aspect handling, or letterbox behaviour moves — bundling a
      geometry change with a resampling change makes the fidelity measurement meaningless,
      because reference and candidate stop being the same picture.
    - _Requirements: 5.4_

  - [ ] 4.5 Measure the scaler on a 4K → 1080×1920 downscale, and accept a null result
    - That downscale is where the difference should appear. **If the measurement does not
      distinguish the algorithms, keep the current default and record the finding** (R5.6). On
      heavily compressed 1080p sources they can be genuinely hard to separate.
    - _Requirements: 5.5, 5.6_

  - [ ] 4.6* Test: uniformity and geometry invariance → `tests/test_output_compat.py`
    - Every scale in a job carries identical flags; all geometry byte-identical to v0.11.0; the
      preset flows through the single builder.
    - _Requirements: 4.8, 5.3, 5.4_

  - [ ] 4.7 Attach rendered output and the cost table
    - The instrument gives SSIM and time; whether 0.4 dB is worth 2× encode time is a decision,
      not a measurement. Attach both the numbers and the pixels.
    - _Requirements: 13.11_

- [ ] 5. Frame-rate policy (O18)
  **Depends on `render-quality-measurement` task 5 (Sync_Offset).**
  - [ ] 5.1 Detect CFR versus VFR
    - _Requirements: 8.1_

  - [ ] 5.2 Implement the policy
    - VFR → normalise, **preserving the existing behaviour exactly**. `config.py`'s reasoning is
      correct and stays: VFR is "every screen recording and most phone footage," and burned
      captions drift against a wandering frame duration. The defect is scope, not the rule.
    - CFR at 24/25/30/50/60 → deliver at the source rate. CFR at any other rate → normalise.
      Undeterminable → normalise. Never exceed the platform profile's maximum.
    - _Requirements: 8.2, 8.3, 8.4, 8.5, 8.6_

  - [ ] 5.3 Leave intermediates alone; record the outcome
    - Matches `h264_args`' existing documented behaviour for `normalise_fps`. Record the
      delivered rate and whether it was normalised.
    - _Requirements: 8.7, 8.10_

  - [ ] 5.4 Keep unconditional normalisation available
    - For anyone who wants the old blanket guarantee.
    - _Requirements: 8.8, 12.1_

  - [ ] 5.5* Test: the policy, probing the output → `tests/test_frame_rate_policy.py`
    - Real CFR sources at each of 24/25/30/50/60 delivered at their own rate; real VFR
      normalised; undeterminable normalised. **Probe the delivered file.**
    - _Requirements: 8.11, 13.1, 13.3_

  - [ ] 5.6 Gate the default on sync verification at every Platform_Frame_Rate
    - This is the specific gate (R8.9). Frame-rate handling is the most likely place to
      introduce drift, and drift desynchronises every burned caption — the exact harm the
      original unconditional rule prevented. Do not flip the default until sync passes at each
      rate.
    - _Requirements: 8.9, 13.7_

- [ ] 6. Keyframe interval and audio bitrate (O19, O20)
  - [ ] 6.1 Set the keyframe interval, derived from the delivered frame rate
    - Nothing sets `-g` today, so x264's default of 250 applies (~8 s at 30 fps). **Derive from
      the delivered rate, not a fixed frame count** — this matters precisely because task 5 makes
      the delivered rate variable, and a hard-coded `-g 60` would silently mean 2 s at 30 fps and
      1 s at 60 fps. Express the setting in seconds.
    - _Requirements: 6.1, 6.2, 6.3, 6.7_

  - [ ] 6.2 Final renders only; keep scene-change keyframes
    - Constraining an encoder that is about to be re-encoded costs quality for no delivered
      benefit — the same reasoning `h264_args` already applies to `normalise_fps` and `vbv_cap`.
      **Do not** set `-sc_threshold 0`: forcing a fixed GOP puts an I-frame in the wrong place on
      every cut, worse for quality and seeking both.
    - _Requirements: 6.4, 6.5_

  - [ ] 6.3 Make the audio bitrate configurable
    - `-b:a 128k` (`ffmpeg_utils.py:262`) is adequate for speech, thin under a music bed. Leave
      `AU8`'s sample-rate and channel-count normalisation untouched — its docstring explains why
      those exist (mono playing from one side, surround silently downmixed). Stay within the
      platform profile; never exceed the final value on an intermediate.
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.6_

  - [ ] 6.4 Change the audio default only if measured
    - _Requirements: 7.5_

  - [ ] 6.5* Test: keyframes and bitrate, probed → `tests/test_output_compat.py`
    - Probe delivered keyframe positions; assert the interval tracks the delivered fps and that
      intermediates are unconstrained.
    - _Requirements: 6.6, 13.1_

- [ ] 7. Deinterlacing (V20)
  - [ ] 7.1 Detect interlacing
    - _Requirements: 9.1_

  - [ ] 7.2 Deinterlace first — before crop, scale, and the tone-map
    - Combing that is cropped and scaled becomes a smear no later filter can undo. Even before
      tone-mapping, because tone-mapping interleaved fields blends two different moments in time.
    - _Requirements: 9.2_

  - [ ] 7.3 Preserve frame rate rather than doubling
    - `yadif`'s field-doubling mode yields 50/60 from 25/30i, which sounds like a bonus and
      interacts badly with task 5's policy and task 6's `-g` derivation.
    - _Requirements: 9.5_

  - [ ] 7.4 Never touch progressive or inconclusive sources
    - Deinterlacing progressive footage costs real vertical detail. Inconclusive → do nothing,
      record that the determination was inconclusive.
    - _Requirements: 9.3, 9.8_

  - [ ] 7.5 Probe availability, degrade with a marker, expose the option
    - Automatic detection by default.
    - _Requirements: 9.4, 9.6, 9.7, 11.1, 11.2_

  - [ ] 7.6* Test: real interlaced and real progressive → `tests/test_deinterlace.py`
    - _Requirements: 13.4_

- [ ] 8. Stabilisation (V21)
  - [ ] 8.1 Add optional stabilisation, default off
    - Slow, needs a two-pass analysis, and wrong for plenty of footage.
    - _Requirements: 10.1, 10.3, 10.7_

  - [ ] 8.2 Order it before reframing
    - So the crop tracks a stabilised subject. Tracking a shaking subject and then EMA-smoothing
      the crop fights the same motion twice.
    - _Requirements: 10.4_

  - [ ] 8.3 Hand the consumed margin to reframing
    - **The subtle one.** `vidstab` corrects shake by moving the frame, which needs margin — it
      crops in. Reframing also crops in. Consumed independently, the result is over-cropped and
      the crop window can drift outside valid pixels, producing black edges.
    - Use the existing mechanism: `build_sendcmd` already takes
      `origin_x`/`origin_y`/`src_w`/`src_h` to confine the crop to a content rectangle — that is
      how `V16` letterbox handling works. Carry the stabilisation margin the same way; **no new
      concept is needed**.
    - _Requirements: 10.5_

  - [ ] 8.4 Probe availability, degrade, record strength, report progress
    - A two-pass analysis on a long source looks like a stalled job; account for it in progress
      reporting, which already exists per-stage at `GET /api/jobs/{id}/timings`.
    - _Requirements: 10.2, 10.6, 10.8, 11.1, 11.2_

  - [ ] 8.5 Refuse to stabilise synthetic content
    - Screen recordings have no camera shake, and `vidstab` finds spurious motion in scrolling
      text and introduces wobble that was not there. Consume
      `clip-presentation-polish`'s screen-recording detection if present; otherwise do nothing.
    - _Requirements: 10.9_

  - [ ] 8.6* Test: the margin is respected → `tests/test_stabilisation.py`
    - The margin `vidstab` consumes is reflected in the geometry handed to reframing; assert no
      black edges at the frame boundary.
    - _Requirements: 10.5_

- [ ] 9. Configuration, markers, and close-out
  - [ ] 9.1 Document every new setting in `.env.example`
    - `tests/test_config_documentation.py` fails on an undocumented field or a documented
      non-setting. State for each default whether it is **measured or provisional**.
    - _Requirements: 12.1, 12.2, 12.3_

  - [ ] 9.2 Surface new options through API, form, and UI
    - `OptionsModel`, `/api/upload` form fields, `/api/info` domains, `App.jsx`
      `DEFAULT_SETTINGS` **and** `toOptions()`, `SettingsPanel.jsx`. Unrecognised values apply the
      documented default without raising.
    - _Requirements: 12.4, 12.5, 12.6_

  - [ ] 9.3 Verify marker discipline
    - Every conversion applied or skipped gets a marker naming the **resolved** value, never the
      requested one. Every pre-existing marker keeps its exact spelling — a renamed marker
      silently breaks consumers, and markers are the product's only mechanism for explaining an
      absent feature.
    - Confirm no filter in this spec can fail a job: every one of tone-mapping, deinterlacing,
      and stabilisation degrades to a marker. A fidelity feature that turns a deliverable clip
      into a failed job is a worse outcome than the defect it fixes.
    - _Requirements: 11.2, 11.3, 11.4, 11.5, 13.5_

  - [ ] 9.4 Confirm defaults, and the two deliberate exceptions
    - Everything defaults to previously shipped behaviour **except** tone-mapping (task 2.6).
      Check `tests/conftest.py`'s `EFFECTS_OFF` / `assert_effects_off_is_exhaustive()` —
      **verify** whether the on-by-default addition must be listed rather than assuming.
    - _Requirements: 12.7_

  - [ ] 9.5 Full gate run
    - `ruff check .` clean · `pytest` at **2030 + new, 0 skipped, 0 warnings** ·
      `cd frontend && npm run lint && npm run test:run && npm run build` ·
      `scripts/docker_smoke.sh`.
    - Triage any new warning at source with a **targeted** `filterwarnings` ignore and a comment
      saying why it cannot be fixed. Never broaden the existing ignores; never relax
      `filterwarnings = error`.
    - _Requirements: 13.8, 13.9_

  - [ ] 9.6 Add the mutation specification
    - `tests/mutations/clip-signal-fidelity.json`. Attack the two plausible-looking failures
      first, per the design: **tone-map applied twice** and **tone-map applied to a mislabelled
      SDR source**. Then: classify unknown transfer as HDR; infer HDR from bit depth; copy source
      colour tags onto tone-mapped output; invert the CFR/VFR branch; derive `-g` from a fixed
      frame count; deinterlace a progressive source; drop the stabilisation margin from reframe
      geometry. Each **CAUGHT**; an ESCAPE is a real test gap.
    - _Requirements: 13.10_

  - [ ] 9.7 Write the close-out
    - Follow `.kiro/specs/face-detection-upgrade/CLOSE_OUT.md`. Record what remains: the pass
      count is still three (`O6`), so generation loss is reduced not eliminated; 10-bit output is
      out of scope so gradient banding persists; nothing here improves framing, pacing, or
      selection.
    - Record every measured number — preset, scaler, audio bitrate — including the ones that came
      back null. "No measurable difference, kept the default" is a result worth writing down, and
      it stops the next person re-running the same experiment.
    - _Requirements: 4.7, 5.6, 7.5, 13.11_
