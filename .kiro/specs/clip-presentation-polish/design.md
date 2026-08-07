# Design Document — Clip Presentation Polish

## Overview

Three subsystems, one property in common: each change is small, applies to **every clip**, and
has **no objective metric**. That last point governs the whole design.

SSIM cannot tell you a face is framed better — it will report that moving the crop made the
render *less* similar to the previous one, which is precisely the intended change. There is no
metric for "this line break reads better" or "this speech is clearer on a phone." So every
default change in this spec is gated on `render-quality-measurement`'s pairwise preference
harness (`M12`), and every one of them **defaults to current behaviour** until a trial says
otherwise (R1.7, R2.8, R4.12, R5.9, R6.6, R7.8).

That is unusually conservative for a spec whose whole content is improvements. It is
deliberate: these are exactly the changes where a developer's conviction is strongest and the
evidence is weakest.

### Placement

```
reframe.py
  smooth_centers()  →  apply_headroom_bias()  →  build_sendcmd()      (R1)  after smoothing
  shot boundaries   →  normalise_subject_scale()                       (R2)  per shot
NEW content_class.py
  sampled frames    →  Content_Class  →  Fit_Mode or crop             (R3)
captions.py
  words_to_cues()   →  enforce_cue_constraints()  →  break_lines()     (R4, R5)
effects/audio.py
  speech  →  presence_chain()  →  turn_gain()  →  loudnorm  →  limiter (R6, R7)
```

Three orderings below are load-bearing and are stated as requirements rather than left to
implementation: headroom **after** smoothing, presence and turn gain **before** loudnorm, and
cue constraints **without** touching word spans.

---

## Group A — Framing

### R1: headroom, and why it goes after the smoother

`reframe.py:487` positions the crop on the face centre:

```python
y = origin_y + int(round(_clamp(c.cy - origin_y - crop_h / 2.0, 0, max_y)))
```

Face centre → frame centre. In 9:16 that reads as machine-made, and it also parks the mouth
at the vertical midpoint, which is roughly where captions land. `caption_avoid_faces` exists
to resolve that collision by moving the captions; moving the subject is usually the better
answer and is currently impossible.

**R1.5 — apply the bias after `smooth_centers`, not before.** If the bias is folded into the
sample centres before EMA smoothing, it becomes part of the smoothed signal, and at
`alpha=0.35` with `reset_at` shot-change breaks it gets attenuated and re-converged at every
cut. The bias is a constant compositional offset, not a signal to be tracked. Applying it
after smoothing keeps it exact and keeps the smoother's behaviour byte-identical, which also
means `V4`'s shot-change reset logic needs no revisiting.

**R1.2 derives the Eye_Line from the face box** rather than a fixed pixel offset, because a
detected box scales with subject distance — a fixed offset that looks right on a close-up is
wrong on a wide shot, which is the same footage the R2 work is about.

**R1.4's clamp is not a formality.** The bias moves the crop toward the top of the frame; a
subject already near the top yields a negative `y`. The existing `_clamp(..., 0, max_y)` does
the work, and the requirement makes explicit that the bias must be inside it. `reframe.py`'s
own comments record a related lesson at :148 — *"Order is fixed: convert, then clamp, then
test for degeneracy"* — and a mutation once *"moved the degeneracy test to the wrong side of
the clamp."* Same hazard, same place.

R1.6 skips the bias with no detected face: with no subject there is no Eye_Line, and biasing a
fallback centre crop just moves the frame up for no reason.

R1.10 requires split-screen either to get the same treatment or to record that it did not.
Tiles are composed by `apply_speaker_reframe` at `split_screen_max_regions=2`; headroom within
a tile is the same idea at a different scale, and silently applying it to one path and not the
other would produce inconsistent framing in one output.

### R2: subject scale across shots

R2.4 is the constraint that keeps this safe: **adjust between shots, never within one.**
Changing crop size during continuous footage is a zoom, and the project already has zoom and
ken-burns effects with their own easing. Two independent scale changes on one shot compound
into something neither intended, which is what R2.10 forbids.

R2.5 reuses `scene_detect.scan_cuts` — the same cut list `V4` already maps to sample indices
for the EMA reset. A second shot-boundary mechanism would drift from the first.

R2.7 leaves faceless shots alone rather than guessing, and R2.3 bounds the adjustment so one
outlying detection cannot drive an extreme crop. Default off (R2.8): this is the least
certain item in the spec, since a director may have *chosen* to alternate between close and
wide.

### R3: content class, and the honest limits of a heuristic

No content-type notion exists today. `detect_letterbox` finds bars, which is a different
question — a pillarboxed camera shot and a 16:9 slide both have bars.

Screen content has strong signal-level tells: large flat regions, high-contrast text edges,
a near-static background with localised change, and colour histograms with a few dominant
spikes rather than a continuous distribution. Camera footage has sensor noise, continuous
gradients, and global motion. R3.2 requires signal features only — no checkpoint, no network —
which keeps this consistent with how `S5`-style proxies are handled elsewhere in the project.

It will misclassify. A whiteboard talk, a plain studio backdrop, a heavily graded flat shot,
and a slide with a video embedded are all genuinely ambiguous. So:

- **R3.5, R3.9, R3.10** — unknown means unchanged behaviour; measured misclassification is
  *reported*, not asserted away; and automatic classification defaults on **only if** it does
  not degrade camera handling. The asymmetry is intentional: wrongly fitting camera footage
  puts blurred bars on a clip that was fine, which is worse than continuing to crop a slide
  the way we do today.
- **R3.8** gives the user an override, because on genuinely ambiguous footage a person knows
  and the heuristic does not.

**R3.6 classifies per clip, not per source.** A podcast that cuts between cameras and a shared
screen is the normal case for this feature, and a per-source decision would be wrong for half
the clips.

R3.11 exposes the class to other components — `clip-signal-fidelity` R10.9 consumes it to
refuse stabilising a screen recording, where `vidstab` would find spurious motion in scrolling
text and introduce wobble that was not in the source.

R3.12 reuses letterbox detection rather than re-deriving bar geometry, for the same
one-definition reason as R2.5.

---

## Group B — Captions

### R4: cue duration, and the constraint hierarchy

`words_to_cues` breaks on `max_words=3`, `max_gap=0.6`, `max_duration=3.0`, and measured
`too_wide`. All four are *ceilings*. Nothing is a floor, so fast speech yields a ~0.3 s
three-word cue.

The interesting part is the conflict, because there are now four competing constraints across
two specs:

| Priority | Constraint | Source |
| --- | --- | --- |
| 1 | Cues must not overlap | R4.4, R4.5 |
| 2 | Word spans must not overlap | `clip-quality-uplift` R8.2 |
| 3 | Minimum cue duration / Reading_Rate | R4.1, R4.2 |
| 4 | Minimum word span duration | `clip-quality-uplift` R8.3 |

**R4.5 sets the top of that hierarchy: cue non-overlap wins over everything.** Two overlapping
cues in ASS render as two dialogue events on screen simultaneously — visibly broken, not
merely suboptimal. A cue slightly under the minimum duration is fast; a cue on top of another
cue is a bug. And R4.5 requires recording *which* constraint was relaxed, so a debugging
operator can see why a cue is short.

**R4.8 is the subtle one: extending a cue must not move word spans.** A cue's on-screen
duration and its words' karaoke timings are different things. Extending the cue's display
window while leaving `\kf` timings on speech means the last word stays highlighted a little
longer — correct. Stretching the word spans to fill the extended cue would drift the highlight
off the speech, which is the exact defect `clip-quality-uplift`'s onset snapping exists to
reduce. Doing both at once would have one feature undo the other.

R4.7's merge is the fallback when extension is impossible, bounded by the width and line
budget so a merge cannot produce an overflowing cue — `TextFit` already measures this.

R4.9 covers `engines/kinetic.py`. It has its own layout and timing logic (2599 lines); a
constraint applied only to the main path leaves the kinetic path with the defect and the two
diverge. R4.10's bit-identical guarantee for already-compliant input is what allows applying
this unconditionally without moving any golden.

### R5: line breaking

`TextFit` measures pixels, so a break lands wherever the width runs out — which can split
"New York" or separate an article from its noun.

R5.4 keeps width and the line budget **above** any linguistic preference. This is a
*preference among fitting breaks*, never a licence to overflow. R5.6 forbids dropping or
reordering words to achieve a nicer break — `captions.py`'s docstring already records that
without measured fitting "the wrap below has to drop words to stay inside the frame, which is
a caption missing its ending." Not repeating that.

R5.7 keeps it checkpoint-free: a small stop-word and function-word list plus capitalisation
runs for proper nouns. R5.8 restricts it to languages with rules and falls back to width
otherwise, which matters because `script_support.py` already reports
`caption_script_unsupported` for scripts nothing vendored can render — this must not add a
second, quieter language assumption.

R5.10's bit-identical clause means the common case where the width break already *is* the
preferred break changes nothing.

---

## Group C — Audio

### R6: presence, placed before loudnorm

`effects/audio.py` has `afftdn`, a de-esser, a `lowpass`, two-pass `loudnorm`, and
`alimiter`. The only spectral shaping is the lowpass. Nothing lifts presence, and nothing
does multiband dynamics.

This matters because of where clips are watched. A phone speaker reproduces almost nothing
below ~500 Hz, so the energy `loudnorm` measures and normalises is partly energy the viewer
cannot hear. A presence lift in the 2–5 kHz region raises *intelligibility* rather than level —
`loudnorm` cannot substitute, because it is a level operation and this is a spectral problem.

**R6.2 puts the chain before loudness normalisation.** Two-pass `loudnorm` measures then
corrects; if the shaping happened after measurement, the measured value would describe a
signal that no longer exists and the delivered clip would miss its LUFS target. R6.10
verifies that from the rendered file, and R6.9 verifies the true-peak ceiling still holds —
presence boosts add peaks, `alimiter` runs with `level=disabled` deliberately (so it does not
re-normalise), and the interaction must be measured rather than assumed.

R6.5 applies the chain to speech only. The music bed already has `bed_fit_filter` and
sidechain ducking; presence-shaping music would be an arbitrary EQ on someone's track.

R6.11 forbids a new pass — this is filter-graph work inside the compositor's existing single
pass.

### R7: turn gain, the feature diarisation was already built for

`worker/diarization.py` exists, with `slice_turns` and `rebase_turns` already used by
speaker-aware reframing. Nothing uses it for gain. `loudnorm` normalises the clip, so it
*cannot* fix balance within it — a clip that is correct at −14 LUFS integrated can still have
one speaker 8 dB below the other.

Four requirements carry the risk:

- **R7.4, ramping not stepping.** A step gain change at a turn boundary is an audible click.
  This is the same reasoning `filler._seam_fades` uses when it applies `afade` at interior
  seams — and deliberately *not* `acrossfade`, because that would shift the timeline. Ramp
  within the turn, do not cross-fade between turns.
- **R7.3 and R7.7, bounded and confidence-gated.** Diarisation here is a **transcript proxy**,
  offline and CPU-only, capped at `diarization_max_speakers=2` — not `pyannote` (`T6` is a
  seam, unimplemented). A misattributed turn with unbounded gain is a large audible jump, so
  the gain is bounded and low-confidence intervals are skipped.
- **R7.5, the timeline.** Turn gain must be applied on the timeline actually delivered. Filler
  removal, the `U4` cut list, and `clip-quality-uplift`'s interior-silence removal all shorten
  the clip, and `rebase_turns` exists precisely for this. Applying gains at original-timeline
  positions would put a guest's gain on the host's words — plausible-sounding and completely
  wrong, the hardest defect class here to see.
- **R7.12, no side effects.** `diarization` defaults to `False`. Turn gain must not silently
  enable it — that would add a per-source analysis stage to jobs that did not ask for one. When
  diarisation is off, turn gain is unavailable and records why.

R7.11 orders turn gain before loudnorm for the same reason as R6.2.

---

## Testing strategy

| Area | File | Nature |
| --- | --- | --- |
| Headroom geometry | `tests/test_reframe_geometry.py` | Eye_Line above midpoint; bias cannot produce an out-of-frame crop; clamp applied after bias. |
| Bias vs. smoother | `tests/test_reframe_geometry.py` | Bias applied **after** `smooth_centers`, exact and unattenuated; smoother output byte-identical to v0.11.0. |
| Subject scale | `tests/test_reframe_geometry.py` | Adjusts between shots, never within one; bounded; faceless shots unchanged; no compounding with zoom. |
| Content class | `tests/test_content_class.py` | Real ffmpeg-rendered screen-like and camera-like footage through the real classifier. Misclassification **reported**, not asserted. |
| Class consumers | `tests/test_content_class.py` | The class is exposed such that stabilisation can refuse synthetic content (R3.11). |
| Cue constraints | `tests/test_caption_cues.py` | **Property**: never overlapping; word spans never altered; already-compliant input bit-identical; merge respects width budget. |
| Constraint hierarchy | `tests/test_caption_cues.py` | Construct the conflict where minimum duration and non-overlap disagree; assert non-overlap wins and the relaxation is recorded. |
| Line breaking | `tests/test_caption_lines.py` | Preferred break chosen when it fits; width wins when it does not; no words dropped or reordered; unsupported language falls back. |
| Presence chain | `tests/test_audio_presence.py` | Measure integrated loudness **and** true peak from the rendered file with the chain enabled. Cross-check independently of the filter builder. |
| Turn gain ramp | `tests/test_audio_turn_gain.py` | Ramped, not stepped; bounded; low-confidence intervals skipped. |
| Turn gain timeline | `tests/test_audio_turn_gain.py` | Gains land on the **delivered** timeline after interval removal — construct a case where original and rebased positions differ. |
| No extra pass | `tests/test_pipeline_passes.py` | Every feature here is filter-graph work; assert the encode count is unchanged. |
| Markers | `tests/test_effects_markers.py` | Resolved value recorded, never requested; pre-existing markers spelled unchanged. |

Property tests use `hypothesis` at `max_examples=100`, one property per test, tagged
`# Feature: clip-presentation-polish, Property N: <text>`.

**Baseline:** `pytest` → **2030 passed, 0 skipped, 0 warnings**; `npm run test:run` → **141
passed**.

### What the suite cannot tell us — and what to do instead

Almost everything that matters here:

- Whether the headroom looks right. The test proves the eye line is above the midpoint; only
  eyes decide whether the amount is good.
- Whether presence shaping helps or just sounds brighter. The test proves LUFS and true peak
  still hold; only ears judge intelligibility, **and it has to be on a phone speaker**, since
  that is the entire premise.
- Whether the line breaks read better.
- Whether subject-scale normalisation is an improvement or the removal of a director's choice.

Hence R10.13: rendered output for everything visible or audible, and **preference trials for
every default this spec proposes to change** (R1.8, R2.8, R4.12, R5.9, R6.7, R7.8). That is
six trials, which is the honest cost of six changes with no metric. If the harness from
`render-quality-measurement` does not exist yet, this spec can land entirely — every feature
off — and the defaults decided later.

---

## Consequences

**What this fixes:** framing stops looking centred-by-machine; screen shares stop being
cropped into unreadable slices; cues stop flashing; lines stop splitting names; speech gets
shaped for the device it is actually watched on; a quiet guest stops being quiet.

**What it does not fix:** we still follow the largest, most-diarisation-active face rather
than the person actually speaking (`V3`). Better framing of the wrong subject is still the
wrong subject — and on two-person footage that remains the most visible weakness, which no
amount of headroom bias addresses.

**The likeliest way this goes wrong:** turn gain applied on the wrong timeline. Every other
failure here is visible or audible immediately; that one produces a clip where the gain
changes at almost-right moments, sounds vaguely wrong, and takes a long time to attribute.
R7.5 and its test exist for it, and the mutation specification should attack it first.
