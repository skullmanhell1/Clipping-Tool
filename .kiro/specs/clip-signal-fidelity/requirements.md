# Requirements Document

## Introduction

**Clip Signal Fidelity** fixes the parts of the render chain that damage the picture between
the source and the delivered clip. Unlike its sibling specs, this one contains **outright
defects** rather than missing enhancements — cases where the current output is wrong, not
merely improvable.

The project's own `docs/IMPROVEMENT_PLAN.md` has a 154-item audit and a section on output
encoding. None of the items below appear in it. They were found by reading the render path
directly.

### The defects

**Colour is not handled at all.** Searching `worker/`, `api/`, and `config.py` for
`tonemap`, `zscale`, `colorspace`, `color_trc`, `color_primaries`, and `bt2020` returns
**zero hits**. Three consequences:

1. **HDR input is mangled.** An HDR10/HLG source — every recent iPhone, most current
   cameras, a growing share of YouTube — is decoded and re-encoded as if it were Rec.709.
   The result is the well-known washed-out, grey, low-contrast look. `probe()` already
   requests `-show_streams`, so `color_transfer` and `color_primaries` are **sitting in the
   JSON it already parses** and are never read: `MediaInfo` (`ffmpeg_utils.py:324`) carries
   `duration`, `width`, `height`, `fps`, `has_audio`, `video_codec`, `audio_codec`,
   `size_bytes` — and nothing about colour.
2. **Output carries no colour metadata.** Nothing sets `-colorspace`, `-color_primaries`,
   `-color_trc`. Untagged H.264 means every player and platform guesses, and they do not all
   guess alike.
3. **Range is never resolved.** Phone footage is frequently full-range (`pc`). Mishandled,
   blacks crush or the whole image lifts and goes milky.

**The encoder is configured for speed at the cost of visible quality.**
`x264_preset` defaults to `"veryfast"` (`config.py:354`). At CRF 20 the gap from `veryfast`
to `medium` is visible — softer detail, more blocking on motion, degraded edges on exactly
the heavy caption typography this project vendored 12 fonts to get right — and it is **paid
three times**, because there are three encode passes per clip. This is a one-word change
with more visible effect than most of the feature work on the backlog.

**Every scale uses swscale's default.** No `-sws_flags` anywhere. All the `scale=` filters
in `ffmpeg_utils.py` and `reframe.py` run bicubic. Downscaling 4K → 1080×1920 — the common
case — is measurably softer and more aliased than `lanczos` or `spline`.

**Frame rate is normalised unconditionally.** `output_fps` defaults to 30 and `-r 30` is
applied to every delivered clip. The stated reasoning is sound *for variable-frame-rate
sources*: `config.py`'s comment notes VFR is "every screen recording and most phone
footage," and burned captions drift against a wandering frame duration. But applied to a
**CFR 24 fps** source it resamples 24→30, which is a 3:2 judder pattern visible on every pan;
applied to 60 fps it discards half the temporal information. The fix is to normalise when
the source needs it, not always.

**Two smaller items.** No `-g` / keyframe interval is set anywhere, so x264's default of 250
frames (~8 s at 30 fps) applies — longer than platforms prefer, and it affects scrubbing and
platform-side thumbnail extraction. And AAC is fixed at `-b:a 128k`
(`ffmpeg_utils.py:262`), which is adequate for speech alone and thin once a music bed sits
under it.

**No source repair.** No `yadif`/`bwdif`, so interlaced broadcast footage keeps its combing
artifacts — which the subsequent crop and scale then smear. No `vidstab`/`deshake`, so
handheld footage stays shaky, and a shaky source under an EMA-smoothed crop compounds rather
than cancels.

### Relationship to the other specs

**This spec should not land before `render-quality-measurement`.** That spec builds the
SSIM/PSNR/VMAF harness; there is currently no `vmaf`, `psnr`, or `ssim` anywhere in the
repository. Without it, the encoder preset and scaler changes here are taste asserted
against taste, and the requirements below that demand measurement (R5.6, R6.5, R7.5) have no
tool. The colour requirements (R1–R3) are the exception: they fix an objectively wrong
result and do not need a preference judgement.

`clip-quality-uplift` R12 raises the CRF of intermediate renders. That is complementary and
smaller than R5 here — preset affects quality-per-bit at *every* pass, including the final
one. If only one lands, it should be this one.

### Out of scope

- **10-bit output** (`yuv420p10le`). Reduces banding and platforms accept it, but it
  interacts with the `-profile:v high` and `-pix_fmt yuv420p` compatibility contract that
  `O1`/`O2` established for good reasons, and with hardware encoder support. Separate change.
- **Per-platform output variants.** `output_profiles` treats aspect as advisory by design.
- **Reducing the encode pass count** (`O6`). Architectural; belongs to the pipeline.
- **AV1 or HEVC output.** Compatibility contract change, not a fidelity fix.
- **Film-grain synthesis, denoising for compressibility, or any generative restoration.**
- **Upscaling low-resolution sources.** A separate judgement call about whether to fabricate
  detail; this spec only avoids *destroying* detail.

## Glossary

- **Clipper**: The overall AI Video Clipper application.
- **Source_Colour**: The colour characteristics of the input — transfer function, primaries, matrix, and range — as reported by `ffprobe`.
- **HDR_Source**: A source whose transfer function indicates high dynamic range, principally PQ (`smpte2084`) or HLG (`arib-std-b67`).
- **Tone_Map**: The conversion of an HDR_Source to SDR Rec.709 for delivery.
- **Colour_Tags**: The `-colorspace` / `-color_primaries` / `-color_trc` / `-color_range` arguments written onto a delivered file.
- **Scaler_Flags**: The `-sws_flags` value governing swscale's resampling algorithm.
- **Frame_Rate_Policy**: The rule deciding whether a delivered clip's frame rate is resampled or passed through.
- **CFR_Source** / **VFR_Source**: A source whose frame durations are constant / variable.
- **Platform_Frame_Rate**: A frame rate short-form platforms accept without re-timing — 24, 25, 30, 50, or 60.
- **Intermediate_Render**: A re-encode whose output feeds a later stage.
- **Final_Render**: The delivered re-encode.
- **Capability_Status**: The frozen availability record `worker/engines/capabilities.py` produces for a probed capability id.
- **Effects_Applied**: The `ClipResult.effects_applied` string markers recording which enhancements ran and how they degraded.
- **Processing_Options**: The user options record (`worker/models.py::ProcessingOptions`) and its API/form/UI mirrors.
- **Info_Endpoint**: `/api/info`, advertising available option values to the UI.

## Requirements

---

## Group A — Colour correctness (O13, O14, O15)

### Requirement 1: Source colour is probed and carried

**User Story:** As a maintainer, I want the pipeline to know a source's colour characteristics, so that decisions about conversion can be made from data rather than assumption.

#### Acceptance Criteria

1. THE Clipper SHALL read the transfer function, colour primaries, colour matrix, and colour range from the existing `ffprobe` output.
2. THE Clipper SHALL carry Source_Colour on the probed media record.
3. THE Clipper SHALL NOT add an additional `ffprobe` invocation to obtain Source_Colour.
4. WHERE a source omits any colour field, THE Clipper SHALL record it as unknown and SHALL NOT substitute a guess as though it had been reported.
5. THE Clipper SHALL preserve the existing positional construction of the probed media record, so existing callers continue to work.
6. THE Clipper SHALL classify a source as an HDR_Source only from its reported transfer function, and SHALL NOT infer HDR from bit depth or resolution.
7. THE Clipper SHALL treat an unrecognised transfer function as unknown rather than as HDR or as SDR.

### Requirement 2: HDR sources are tone-mapped to SDR

**User Story:** As a creator shooting on a modern phone, I want my clip to look like my source, so that HDR footage does not come out grey and flat.

#### Acceptance Criteria

1. WHEN the source is an HDR_Source, THE Clipper SHALL Tone_Map it to SDR Rec.709 for delivery.
2. THE Clipper SHALL apply the Tone_Map before any colour-dependent operation, including any grade, and before scaling.
3. THE Clipper SHALL determine the availability of the filters a Tone_Map requires through the existing capability probe.
4. IF the filters required for a Tone_Map are unavailable, THEN THE Clipper SHALL deliver the clip without tone-mapping and SHALL record the omission in Effects_Applied, naming the missing capability.
5. THE Clipper SHALL NOT fail a job because tone-mapping is unavailable.
6. THE Clipper SHALL NOT Tone_Map a source that is not an HDR_Source.
7. THE Clipper SHALL NOT Tone_Map a source whose transfer function is unknown.
8. THE Clipper SHALL apply the Tone_Map at most once per clip.
9. THE Clipper SHALL record in Effects_Applied that a Tone_Map was applied, naming the detected transfer function.
10. THE Clipper SHALL expose the tone-mapping operator and its target peak luminance as configuration.
11. THE Clipper SHALL default tone-mapping to enabled, because the alternative is knowingly delivering incorrect colour.

### Requirement 3: Delivered files declare their colour, and range is resolved

**User Story:** As a creator, I want my clip to look the same in every player, so that a platform's guess about colour does not change my footage.

#### Acceptance Criteria

1. THE Clipper SHALL write Colour_Tags on every Final_Render.
2. THE Clipper SHALL write Colour_Tags consistent with the content actually delivered, not with the source's original characteristics.
3. THE Clipper SHALL resolve colour range explicitly for delivery rather than passing through whatever the source declared.
4. THE Clipper SHALL NOT alter the existing pixel format, profile, or level behaviour established by `O1` and `O2`.
5. THE Clipper SHALL write Colour_Tags through the single existing argument builder, so the drift pin against naming encoder flags elsewhere continues to hold.
6. THE Clipper SHALL NOT write Colour_Tags that contradict the pixel format.
7. WHERE the source range is unknown, THE Clipper SHALL apply the documented default and SHALL record which was applied.
8. THE Clipper SHALL verify the delivered file's declared colour metadata by probing the output, not by inspecting the arguments used to produce it.

---

## Group B — Encode quality (O16, O17, O19, O20)

### Requirement 4: Encoder speed and quality are a measured trade

**User Story:** As a creator, I want the encoder configured for the quality I am waiting for, so that a speed default I never chose is not costing me detail three times per clip.

#### Acceptance Criteria

1. THE Clipper SHALL measure the fidelity and cost of the current encoder preset against slower presets, using the render-fidelity instrument.
2. THE Clipper SHALL report encode wall-clock time and output file size alongside every fidelity reading.
3. THE Clipper SHALL change the default preset only IF the measurement shows a fidelity improvement.
4. WHERE the default preset changes, THE Clipper SHALL change it in a commit that changes nothing else and SHALL re-freeze the affected fixtures in that same commit.
5. THE Clipper SHALL keep the preset configurable.
6. THE Clipper SHALL NOT change the CRF default as part of this requirement.
7. THE Clipper SHALL record the measured time cost in the change, so an operator can choose speed knowingly.
8. THE Clipper SHALL apply the preset through the single existing argument builder.

### Requirement 5: Scaling preserves detail

**User Story:** As a creator uploading 4K footage, I want the downscale to keep my detail, so that a vertical clip is not softer than it needs to be.

#### Acceptance Criteria

1. THE Clipper SHALL set Scaler_Flags explicitly for every scaling operation it performs.
2. THE Clipper SHALL expose Scaler_Flags as configuration.
3. THE Clipper SHALL apply the same Scaler_Flags to every scale in a single job, so no two stages resample differently.
4. THE Clipper SHALL NOT change the geometry, aspect handling, or letterbox behaviour of any scaling operation.
5. THE Clipper SHALL default Scaler_Flags to the value the fidelity measurement supports, and SHALL record the measurement.
6. IF the measurement does not distinguish the candidate algorithms, THEN THE Clipper SHALL keep the current default and SHALL record the finding.
7. THE Clipper SHALL name the applied Scaler_Flags in the job record.

### Requirement 6: Keyframe interval is set deliberately

**User Story:** As a creator, I want my clip to scrub smoothly and thumbnail correctly on the platform, so that an unset default does not produce eight-second keyframe gaps.

#### Acceptance Criteria

1. THE Clipper SHALL set the keyframe interval explicitly on every Final_Render.
2. THE Clipper SHALL derive the keyframe interval from the delivered frame rate rather than from a fixed frame count.
3. THE Clipper SHALL expose the keyframe interval as configuration, expressed in seconds.
4. THE Clipper SHALL NOT set a keyframe interval on an Intermediate_Render, where it would constrain the encoder for no delivered benefit.
5. THE Clipper SHALL NOT disable scene-change keyframe insertion.
6. THE Clipper SHALL verify the delivered keyframe interval by probing the output.
7. THE Clipper SHALL apply the keyframe interval through the single existing argument builder.

### Requirement 7: Audio bitrate suits the content

**User Story:** As a creator using a music bed, I want the audio bitrate to carry it, so that music under speech is not the weakest part of the clip.

#### Acceptance Criteria

1. THE Clipper SHALL expose the delivered audio bitrate as configuration.
2. THE Clipper SHALL NOT change the sample rate or channel count behaviour established by `AU8`.
3. THE Clipper SHALL keep the audio bitrate within the limits of the active platform profile.
4. THE Clipper SHALL apply the audio bitrate through the single existing argument builder.
5. THE Clipper SHALL change the default only IF a measurement supports it, and SHALL record the measurement.
6. THE Clipper SHALL NOT raise the audio bitrate on an Intermediate_Render beyond what the Final_Render will use.

---

## Group C — Frame rate policy (O18)

### Requirement 8: Frame rate is normalised when it needs to be, not always

**User Story:** As a creator shooting at 24 or 60 fps, I want my motion preserved, so that a rule written for variable-frame-rate screen recordings does not add judder to my footage.

#### Acceptance Criteria

1. THE Clipper SHALL determine whether a source is a CFR_Source or a VFR_Source.
2. WHEN the source is a VFR_Source, THE Clipper SHALL normalise the delivered frame rate, preserving the existing behaviour that keeps burned captions in sync.
3. WHEN the source is a CFR_Source at a Platform_Frame_Rate, THE Clipper SHALL deliver at the source frame rate.
4. WHEN the source is a CFR_Source at a rate that is not a Platform_Frame_Rate, THE Clipper SHALL normalise it.
5. THE Clipper SHALL NOT deliver a frame rate above the active platform profile's maximum.
6. WHERE the source frame rate cannot be determined, THE Clipper SHALL normalise it.
7. THE Clipper SHALL record the delivered frame rate and whether it was normalised in Effects_Applied.
8. THE Clipper SHALL keep unconditional normalisation available through configuration.
9. THE Clipper SHALL default to the Frame_Rate_Policy only IF sync verification passes for a CFR_Source at each Platform_Frame_Rate.
10. THE Clipper SHALL NOT change the frame-rate behaviour of Intermediate_Renders.
11. THE Clipper SHALL verify the delivered frame rate by probing the output.

---

## Group D — Source repair (V20, V21)

### Requirement 9: Interlaced sources are deinterlaced

**User Story:** As a publisher clipping broadcast footage, I want interlacing removed, so that combing artifacts are not cropped and scaled into a smeared mess.

#### Acceptance Criteria

1. THE Clipper SHALL detect whether a source is interlaced.
2. WHEN a source is interlaced, THE Clipper SHALL deinterlace it before any crop or scale.
3. THE Clipper SHALL NOT deinterlace a progressive source.
4. THE Clipper SHALL determine deinterlacing filter availability through the existing capability probe, and SHALL degrade with a marker naming the missing capability.
5. THE Clipper SHALL preserve the source frame rate when deinterlacing rather than doubling it, unless configured otherwise.
6. THE Clipper SHALL record in Effects_Applied that deinterlacing was applied.
7. THE Clipper SHALL expose deinterlacing as configuration, defaulting to automatic detection.
8. WHERE interlacing cannot be determined, THE Clipper SHALL NOT deinterlace, and SHALL record that the determination was inconclusive.

### Requirement 10: Shaky sources can be stabilised

**User Story:** As a creator with handheld footage, I want the option to stabilise it, so that a shaky source and a moving crop do not compound.

#### Acceptance Criteria

1. THE Clipper SHALL provide optional stabilisation.
2. THE Clipper SHALL determine stabilisation filter availability through the existing capability probe, and SHALL degrade with a marker naming the missing capability.
3. THE Clipper SHALL default stabilisation to disabled.
4. THE Clipper SHALL apply stabilisation before reframing, so the crop tracks a stabilised subject.
5. THE Clipper SHALL account for stabilisation's cropping in the geometry it hands to reframing, so the two do not each consume the same margin.
6. THE Clipper SHALL record in Effects_Applied that stabilisation was applied, and the strength used.
7. THE Clipper SHALL expose stabilisation strength as configuration.
8. WHERE stabilisation requires an analysis pass, THE Clipper SHALL account for it in the job's progress reporting rather than appearing stalled.
9. THE Clipper SHALL NOT apply stabilisation to a source it has detected as a screen recording or other synthetic content.

---

## Group E — Cross-cutting

### Requirement 11: Every addition degrades honestly

**User Story:** As an operator, I want to know when a fidelity feature did not run, so that an absent conversion is visible rather than silent.

#### Acceptance Criteria

1. THE Clipper SHALL resolve every new ffmpeg filter dependency through the existing capability probe rather than a second mechanism.
2. THE Clipper SHALL record an Effects_Applied marker for every conversion applied and every conversion skipped for unavailability.
3. THE Clipper SHALL name in each marker the value actually applied, never the value requested.
4. THE Clipper SHALL NOT fail a job because any filter in this spec is unavailable.
5. THE Clipper SHALL keep every existing Effects_Applied marker spelled exactly as it is today.
6. THE Clipper SHALL keep the container, pixel format, and profile compatibility contract established by `O1`, `O2`, and `O10` unchanged.

### Requirement 12: Configuration is documented as a contract

**User Story:** As an operator, I want every new knob documented, so that the configuration test keeps the contract true.

#### Acceptance Criteria

1. FOR every configuration setting this spec adds, THE Clipper SHALL provide a matching documented entry in `.env.example`.
2. THE Clipper SHALL document, for each new default, whether it is measured or provisional.
3. THE Clipper SHALL NOT introduce a documented key that is not a real setting.
4. THE Clipper SHALL surface through the Info_Endpoint any new option value the UI must offer.
5. THE Clipper SHALL round-trip every new Processing_Options field through serialisation without loss.
6. WHERE a new Processing_Options value is unrecognised or malformed, THE Clipper SHALL apply the documented default and SHALL NOT raise.
7. THE Clipper SHALL default every new setting to previously shipped behaviour, except where a requirement explicitly directs otherwise.

### Requirement 13: Every claim is verified against the real program

**User Story:** As a maintainer, I want each fidelity claim demonstrated by probing real output, so that a filter string is not mistaken for a result.

#### Acceptance Criteria

1. THE Clipper SHALL verify colour metadata, frame rate, and keyframe interval by probing rendered output rather than by asserting on arguments.
2. THE Clipper SHALL include a test that runs a real HDR-signalled source through the real pipeline and asserts the delivered file's colour metadata.
3. THE Clipper SHALL include a test that a CFR source at each Platform_Frame_Rate is delivered at its own frame rate, and that a VFR source is normalised.
4. THE Clipper SHALL include a test that a real interlaced source is detected and a real progressive source is not.
5. THE Clipper SHALL include a test asserting the resolved value recorded in Effects_Applied is the one that ran, for every conversion in this spec.
6. THE Clipper SHALL cross-check any parsed program output through an independent mechanism sharing no parsing code with the implementation.
7. THE Clipper SHALL verify sync is preserved by every change in this spec that alters timing or frame rate.
8. THE Clipper SHALL NOT introduce any test that is skipped when its dependencies are present.
9. THE Clipper SHALL NOT introduce any new warning into the test run.
10. THE Clipper SHALL add a mutation specification covering the highest-value mutations of the colour classification, frame-rate policy, and keyframe arithmetic.
11. THE Clipper SHALL attach rendered output for every requirement whose effect is visible, because the suite cannot judge a picture.
