# Requirements Document

## Introduction

**Clip Presentation Polish** addresses three defects that share a property: each is small in
code, affects **every clip**, and is the kind of thing a viewer registers as "this was made by
a machine" without being able to name.

### The findings

**Faces are framed dead centre.** `worker/effects/reframe.py:487`:

```python
y = origin_y + int(round(_clamp(c.cy - origin_y - crop_h / 2.0, 0, max_y)))
```

The crop centres on the detected face centre. In a 9:16 frame that is the single most
recognisable auto-crop tell — human framing puts the eye line around the upper third, leaving
headroom above and body below. Centring also parks the mouth in the vertical middle, which is
where `caption_offset_px` and the safe-area margins tend to put captions, so the two features
fight each other. `caption_avoid_faces` exists to resolve that collision by moving the
*captions*; moving the *subject* is usually the better answer and is not currently possible.

**Nothing stops a caption cue from flashing.** `words_to_cues`
(`worker/captions.py:116`) breaks a cue on `max_words=3`, `max_gap=0.6`,
`max_duration=3.0`, or a measured `too_wide`. There is **no floor** — no minimum cue duration
and no reading-rate limit. During fast speech a three-word cue can be on screen for ~0.3 s.
Subtitle practice is roughly 17–21 characters per second with a minimum around 1 s; burned
short-form captions can run faster than broadcast subtitles, but not arbitrarily fast, and
right now nothing bounds it at all.

Note this is **not** the same as `clip-quality-uplift` R8, which sets a minimum *word span*
inside a cue. That governs the karaoke highlight; this governs the cue itself. Both are needed
and they interact — R4.5 below owns the interaction.

**Cropping destroys screen shares.** There is no notion of frame content type. Searching for
`screen_share`, `slide`, `is_screen`, `content_type`, and `text_heavy` returns nothing.
`detect_letterbox` (`V16`) finds bars, which is a different question. So a 16:9 slide, a code
demo, or a shared screen gets cropped to a 9:16 slice of itself — usually the middle third,
usually unreadable. The pipeline already has `crop_blur` as a fit-with-background fallback; it
simply never chooses it on the basis of content.

**Audio is levelled but not shaped, and not balanced between speakers.**
`worker/effects/audio.py` has `afftdn` denoise, a de-esser, a `lowpass`, two-pass `loudnorm`,
and an `alimiter`. There is **no presence EQ and no multiband compression** — the only
spectral shaping in the file is a lowpass. Most short-form viewing happens on phone speakers
with essentially no low-frequency response, where a presence lift in the 2–5 kHz region does
more for intelligibility than any amount of loudness normalisation, because `loudnorm`
operates on level and this is a spectral problem.

Separately, diarisation exists (`worker/diarization.py`, with `slice_turns` and
`rebase_turns` already used by speaker-aware reframing) and is **never used for gain**. One
quiet guest and one loud host is the most common podcast audio defect, and `loudnorm`
normalises the *clip*, so by construction it cannot fix balance *within* the clip.

### Relationship to the other specs

- `render-quality-measurement` (`M12`) provides the pairwise preference harness. **Framing and
  audio-shaping changes have no objective metric** — SSIM against the old render would report
  that moving the crop made it less similar, which is the intended change. R1.8, R6.7, and
  R7.8 below require preference trials, and without that spec they have no instrument.
- `clip-signal-fidelity` R10.9 refuses to stabilise synthetic content and names this spec's
  screen-recording detection as the source. R3 here provides it.
- `clip-quality-uplift` R8 sets minimum *word span*; R4 here sets minimum *cue* duration.
  R4.5 defines which wins.

### Out of scope

- **Active-speaker detection (`V3`) and subject/body detection (`V7`).** Still the largest
  visual gap; still blocked pending a separate evaluation of `LR-ASD`. R1 improves framing of
  whichever face is chosen; it does not improve the choosing.
- **Multi-tile layouts beyond two regions (`V6`) and per-time tracking within tiles (`V5`).**
- **Reverb reduction, breath removal, and plosive repair.** Real, but each needs its own
  evaluation and none is as broadly applicable as presence shaping.
- **Caption text rewriting** — disfluency cleanup, number and currency normalisation, and
  speaker labels in captions. These change what the captions *say*, not how they are laid out
  or timed, and conflating "the captions are hard to read" with "the captions should say
  something different" would make both harder to judge.
- **Any new font, preset, or animation style.** Typography is at parity; this is about timing
  and layout of what exists.

## Glossary

- **Clipper**: The overall AI Video Clipper application.
- **Crop_Centre**: The `(cx, cy)` point the reframe crop is positioned on, per `Center` in `worker/effects/reframe.py`.
- **Eye_Line**: The approximate vertical position of a subject's eyes within the detected face box.
- **Headroom_Bias**: A vertical offset applied to the Crop_Centre so the Eye_Line sits above the frame's vertical midpoint.
- **Subject_Scale**: The detected face box height as a fraction of the crop height.
- **Content_Class**: A classification of a source's visual content — camera footage, screen recording or graphics, or unknown.
- **Fit_Mode**: Delivery that scales the whole frame to fit and fills the remainder with a background, as `crop_blur` does, rather than cropping into the frame.
- **Cue**: One caption group produced by `worker/captions.words_to_cues`.
- **Reading_Rate**: A Cue's character count divided by its on-screen duration.
- **Word_Span**: One word's `(start, end)` interval within a Cue.
- **Break_Candidate**: A position between two words at which a Cue's text may be split across lines.
- **Presence_Chain**: The spectral and dynamic processing applied to speech for intelligibility on small speakers.
- **Speaker_Turn**: One diarised interval attributed to a speaker, per `worker/diarization.py`.
- **Turn_Gain**: A per-Speaker_Turn gain adjustment applied to balance speakers within a clip.
- **Effects_Applied**: The `ClipResult.effects_applied` string markers.
- **Processing_Options**: The user options record and its API/form/UI mirrors.
- **Info_Endpoint**: `/api/info`.

## Requirements

---

## Group A — Framing composition (V22, V23, V24)

### Requirement 1: Subjects are framed with headroom

**User Story:** As a creator, I want my face positioned the way a human editor would place it, so that my clip does not look automatically cropped.

#### Acceptance Criteria

1. THE Clipper SHALL position the Crop_Centre so that the subject's Eye_Line sits above the vertical midpoint of the delivered frame.
2. THE Clipper SHALL derive the Eye_Line from the detected face box rather than from a fixed pixel offset.
3. THE Clipper SHALL express Headroom_Bias as a configurable fraction of the crop height.
4. THE Clipper SHALL clamp the biased Crop_Centre to valid pixels, so the bias cannot produce a crop extending outside the frame.
5. THE Clipper SHALL apply Headroom_Bias after smoothing, so the bias does not enter the smoothed signal and cannot be attenuated by it.
6. THE Clipper SHALL NOT apply Headroom_Bias where no face was detected.
7. THE Clipper SHALL default Headroom_Bias to zero, reproducing v0.11.0 framing, until a preference trial supports a non-zero value.
8. THE Clipper SHALL determine the default Headroom_Bias by preference trial rather than by assertion.
9. THE Clipper SHALL record the applied Headroom_Bias in Effects_Applied.
10. THE Clipper SHALL apply Headroom_Bias identically in single-crop and split-screen paths, or SHALL record that it was not applied to a path.

### Requirement 2: Subject scale is consistent across shots

**User Story:** As a viewer, I want the speaker to stay a similar size across cuts, so that the clip does not jump between a close-up and a wide shot.

#### Acceptance Criteria

1. THE Clipper SHALL measure Subject_Scale per shot.
2. THE Clipper SHALL adjust the crop size so Subject_Scale is comparable across shots within a clip.
3. THE Clipper SHALL bound the adjustment, so an outlying shot cannot drive an extreme crop.
4. THE Clipper SHALL NOT adjust crop size within a shot, so normalisation cannot introduce a visible zoom during continuous footage.
5. THE Clipper SHALL use the existing shot-change detection rather than a second mechanism.
6. THE Clipper SHALL NOT scale beyond the source's available pixels.
7. WHERE a shot contains no detected face, THE Clipper SHALL leave its crop size unchanged.
8. THE Clipper SHALL default subject-scale normalisation to disabled.
9. THE Clipper SHALL record in Effects_Applied when normalisation altered a crop size.
10. THE Clipper SHALL NOT interact with the zoom or ken-burns effects in a way that compounds two scale changes on the same shot.

### Requirement 3: Screen recordings and graphics are fitted, not cropped

**User Story:** As a creator sharing a slide or a demo, I want the content to remain readable, so that a vertical crop does not deliver an unreadable middle third.

#### Acceptance Criteria

1. THE Clipper SHALL classify a source's Content_Class.
2. THE Clipper SHALL classify from sampled frames using signal features only, with no model checkpoint and no network access.
3. WHEN the Content_Class is screen recording or graphics, THE Clipper SHALL deliver using Fit_Mode rather than cropping into the frame.
4. THE Clipper SHALL NOT apply face-tracking reframing to a source classified as screen recording or graphics.
5. WHERE the Content_Class is unknown, THE Clipper SHALL apply the existing behaviour unchanged.
6. THE Clipper SHALL classify per clip rather than once per source, so a source that alternates between camera and screen is handled per clip.
7. THE Clipper SHALL expose the classification decision in Effects_Applied, naming the class detected.
8. THE Clipper SHALL expose an override allowing a user to force camera or screen handling.
9. THE Clipper SHALL report its measured misclassification behaviour rather than asserting accuracy.
10. THE Clipper SHALL default automatic classification to enabled only IF measured misclassification does not degrade camera-footage handling.
11. THE Clipper SHALL make the Content_Class available to other components, so a consumer such as stabilisation can refuse synthetic content.
12. THE Clipper SHALL reuse the existing letterbox detection rather than re-deriving bar geometry.

---

## Group B — Caption readability (C24, C25)

### Requirement 4: Cues are on screen long enough to read

**User Story:** As a viewer, I want each caption to persist long enough to read, so that fast speech does not produce a flicker of unreadable text.

#### Acceptance Criteria

1. THE Clipper SHALL enforce a configurable minimum Cue duration.
2. THE Clipper SHALL enforce a configurable maximum Reading_Rate.
3. WHEN a Cue would exceed the maximum Reading_Rate, THE Clipper SHALL extend its duration where the following Cue's start allows.
4. THE Clipper SHALL NOT extend a Cue so that it overlaps the following Cue.
5. WHERE extending a Cue conflicts with the minimum Word_Span requirement of the caption timing path, THE Clipper SHALL preserve Cue non-overlap above both, and SHALL record which constraint was relaxed.
6. THE Clipper SHALL NOT extend a Cue beyond the end of the clip.
7. WHERE a Cue cannot reach the minimum duration without overlapping, THE Clipper SHALL merge it with an adjacent Cue rather than deliver an unreadable Cue, provided the merge does not exceed the width or line budget.
8. THE Clipper SHALL NOT alter Word_Span times when extending a Cue, so karaoke timing continues to track speech.
9. THE Clipper SHALL apply these constraints on every caption path, including the kinetic typography engine.
10. THE Clipper SHALL leave a Cue sequence that already satisfies both constraints bit-identical.
11. THE Clipper SHALL record in Effects_Applied how many Cues were extended or merged.
12. THE Clipper SHALL default both constraints to values reproducing v0.11.0 output, until measured.

### Requirement 5: Lines break at sensible places

**User Story:** As a viewer, I want caption lines split where a reader would split them, so that a name or a phrase is not broken across lines.

#### Acceptance Criteria

1. THE Clipper SHALL prefer Break_Candidates at linguistic boundaries over Break_Candidates chosen purely by measured width.
2. THE Clipper SHALL NOT break between an article and its noun, or between a preposition and its object, where an alternative break fits.
3. THE Clipper SHALL NOT break inside a multi-word proper noun where an alternative break fits.
4. THE Clipper SHALL continue to respect the measured width and the preset's line budget above any linguistic preference.
5. WHERE no linguistically preferable break fits, THE Clipper SHALL break by measured width as it does today.
6. THE Clipper SHALL NOT drop or reorder words in order to achieve a preferable break.
7. THE Clipper SHALL operate without a model checkpoint and without network access.
8. THE Clipper SHALL apply linguistic preference only for languages it has rules for, and SHALL fall back to width-based breaking otherwise.
9. THE Clipper SHALL default linguistic breaking to disabled until measured.
10. THE Clipper SHALL leave output bit-identical for any Cue whose width-based break already coincides with the preferred break.

---

## Group C — Audio clarity (AU11, AU12)

### Requirement 6: Speech is shaped for small speakers

**User Story:** As a viewer watching on a phone, I want speech to be clear, so that a clip normalised to the right loudness is also intelligible.

#### Acceptance Criteria

1. THE Clipper SHALL provide a Presence_Chain applying spectral shaping and dynamic control to speech.
2. THE Clipper SHALL apply the Presence_Chain before loudness normalisation, so the measured loudness reflects the delivered signal.
3. THE Clipper SHALL NOT alter the two-pass loudness normalisation or the true-peak limiting established by `AU1` and `AU3`.
4. THE Clipper SHALL express the Presence_Chain's strength as a single configurable control.
5. THE Clipper SHALL NOT apply the Presence_Chain to a music bed or to b-roll audio, only to speech.
6. THE Clipper SHALL default the Presence_Chain to disabled.
7. THE Clipper SHALL determine the default by preference trial rather than by assertion.
8. THE Clipper SHALL record in Effects_Applied that the Presence_Chain ran, and at what strength.
9. THE Clipper SHALL verify that enabling the Presence_Chain does not cause the delivered clip to exceed its true-peak ceiling.
10. THE Clipper SHALL verify that the delivered integrated loudness still meets the platform target with the Presence_Chain enabled.
11. THE Clipper SHALL NOT add an encoding pass.

### Requirement 7: Speakers are balanced against each other

**User Story:** As a podcast editor, I want a quiet guest and a loud host levelled, so that the viewer is not reaching for the volume control mid-clip.

#### Acceptance Criteria

1. THE Clipper SHALL measure per-Speaker_Turn loudness within a clip.
2. THE Clipper SHALL apply Turn_Gain so that speakers are comparable in loudness within the clip.
3. THE Clipper SHALL bound Turn_Gain, so a misattributed turn cannot produce a large level jump.
4. THE Clipper SHALL ramp Turn_Gain across turn boundaries rather than stepping it, so a gain change is not audible as a click.
5. THE Clipper SHALL apply Turn_Gain on the timeline actually delivered, accounting for any interval removal that preceded it.
6. THE Clipper SHALL NOT apply Turn_Gain where diarisation is unavailable or reports a single speaker.
7. THE Clipper SHALL NOT apply Turn_Gain to intervals diarisation attributed with low confidence.
8. THE Clipper SHALL default Turn_Gain to disabled, and SHALL determine the default by preference trial.
9. THE Clipper SHALL record in Effects_Applied that Turn_Gain ran, and the range of gains applied.
10. THE Clipper SHALL NOT add an encoding pass.
11. THE Clipper SHALL apply Turn_Gain before loudness normalisation.
12. THE Clipper SHALL NOT enable diarisation as a side effect; where diarisation is disabled, Turn_Gain SHALL be unavailable and SHALL record why.

---

## Group D — Cross-cutting

### Requirement 8: Defaults stay parity-checkable and degradations are labelled

#### Acceptance Criteria

1. THE Clipper SHALL default every feature in this spec to previously shipped behaviour.
2. WHERE a preference trial supports changing a default, THE Clipper SHALL change it in a commit that changes nothing else and SHALL re-freeze the affected fixtures in that same commit.
3. THE Clipper SHALL record an Effects_Applied marker for every feature applied and every feature skipped for unavailability.
4. THE Clipper SHALL name in each marker the value actually applied, never the value requested.
5. THE Clipper SHALL keep every existing Effects_Applied marker spelled exactly as it is today.
6. THE Clipper SHALL keep every pre-existing Processing_Options field and default unchanged.

### Requirement 9: Configuration is documented as a contract

#### Acceptance Criteria

1. FOR every configuration setting this spec adds, THE Clipper SHALL provide a matching documented entry in `.env.example`.
2. THE Clipper SHALL document, for each new default, whether it is measured or provisional.
3. THE Clipper SHALL NOT introduce a documented key that is not a real setting.
4. THE Clipper SHALL surface through the Info_Endpoint any new option value the UI must offer.
5. THE Clipper SHALL round-trip every new Processing_Options field through serialisation without loss.
6. WHERE a new Processing_Options value is unrecognised or malformed, THE Clipper SHALL apply the documented default and SHALL NOT raise.

### Requirement 10: Every claim is verified against the real program

#### Acceptance Criteria

1. THE Clipper SHALL include a test asserting the biased Crop_Centre places the Eye_Line above the midpoint, and that the bias cannot produce an out-of-frame crop.
2. THE Clipper SHALL include a test asserting Headroom_Bias is applied after smoothing and is not attenuated by it.
3. THE Clipper SHALL include a test running the real Content_Class classifier against real rendered screen-recording-like and camera-like footage.
4. THE Clipper SHALL include a property test asserting Cue constraints never produce overlapping Cues and never alter Word_Span times.
5. THE Clipper SHALL include a test asserting an already-compliant Cue sequence is returned bit-identical.
6. THE Clipper SHALL include a test measuring delivered integrated loudness and true peak with the Presence_Chain enabled, from the rendered file.
7. THE Clipper SHALL include a test asserting Turn_Gain is ramped rather than stepped.
8. THE Clipper SHALL include a test asserting no feature in this spec adds an encoding pass.
9. THE Clipper SHALL cross-check any parsed program output through an independent mechanism sharing no parsing code with the implementation.
10. THE Clipper SHALL NOT introduce any test that is skipped when its dependencies are present.
11. THE Clipper SHALL NOT introduce any new warning into the test run.
12. THE Clipper SHALL add a mutation specification covering the highest-value mutations of the headroom, cue-constraint, and turn-gain arithmetic.
13. THE Clipper SHALL attach rendered output for every requirement whose effect is visible or audible, and SHALL run preference trials for every default it proposes to change.
