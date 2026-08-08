# Design Document — Clip Signal Fidelity

## Overview

This spec is unusual among the four in that most of it is **corrective**. The others add
capability; this one stops the render chain from damaging the picture.

Two facts shape every decision below.

**First: the colour work is not optional.** An HDR source today is decoded and re-encoded as
if it were Rec.709, which produces the grey, flat, low-contrast result everyone recognises.
That is not a missing enhancement, it is wrong output, and the input class is growing —
every recent iPhone, most current cameras, a large share of YouTube. So R2.11 defaults
tone-mapping **on**, which breaks this project's usual default-to-shipped-behaviour rule.
The rule exists to protect golden renders from accidental change; it should not be used to
protect a defect.

**Second: nearly everything else here needs a measurement this project does not have.**
There is no `vmaf`, `psnr`, or `ssim` anywhere in the repository. The encoder preset and
scaler changes are exactly the kind that feel obviously right and can be invisible in
practice. `render-quality-measurement` builds the instrument; this spec consumes it. R4.3,
R5.5/R5.6, and R7.5 are all written as *"change the default only if the measurement supports
it"*, and R5.6 explicitly permits the answer "no difference — leave it."

### The shape of the change

Almost all of it lands in two files. `worker/ffmpeg_utils.py` owns the argument contract;
`worker/effects/compositor.py` owns the filter graph. That concentration is deliberate and
is protected by an existing drift pin: `tests/test_output_compat.py` fails if `libx264` or
`-crf` is named anywhere outside `ffmpeg_utils.py` and `video_encoders.py`, because these
flags were once duplicated across seven call sites with three of them missing flags. Every
requirement here that says "through the single existing argument builder" (R3.5, R4.8, R5.1,
R6.7, R7.4) is protecting that.

```
probe()                     → MediaInfo + Source_Colour        (R1)  ffmpeg_utils.py
  ├── HDR?          → tonemap chain, first in the graph         (R2)  compositor.py
  ├── interlaced?   → yadif/bwdif, before crop and scale        (R9)  compositor.py
  └── shaky + opt-in→ vidstab, before reframe                  (R10) new effects module
h264_args()
  ├── -colorspace / -color_primaries / -color_trc / -color_range (R3)
  ├── -preset  (measured)                                       (R4)
  ├── -sws_flags (measured, applied to every scale)              (R5)
  ├── -g  (derived from delivered fps, final render only)        (R6)
  └── -r  (conditional on Frame_Rate_Policy)                     (R8)
aac_args()
  └── -b:a  (configurable)                                       (R7)
```

Order in the filter graph is load-bearing and is stated as requirements rather than left to
implementation: **deinterlace → tone-map → stabilise → crop/scale → captions**. Each
boundary has a reason given in its section.

---

## Group A — Colour

### R1: reading what we already fetch

`probe()` already runs `ffprobe -show_format -show_streams`. `color_transfer`,
`color_primaries`, `color_space`, and `color_range` are **in the JSON it already parses** and
are simply never read. `MediaInfo` (`ffmpeg_utils.py:324`) carries duration, dimensions, fps,
`has_audio`, the two codec names, and size — no colour at all.

So R1.3 forbids a second `ffprobe` call: this is a field-reading change, not a new probe.

R1.5 preserves positional construction. `MediaInfo`'s existing comment records why
`video_codec`/`audio_codec` were "defaulted and last" — several tests build `MediaInfo`
directly. The new colour fields follow the same discipline: defaulted, appended.

**R1.4, R1.6, and R1.7 are the substance.** Sources lie or omit constantly, and the failure
modes are asymmetric:

- Tone-mapping an SDR source that was *mislabelled* HDR destroys it far more visibly than
  failing to tone-map an HDR source.
- Bit depth and resolution do **not** imply HDR (R1.6). 10-bit Rec.709 is common; 4K SDR is
  the norm. Inferring HDR from either would misfire on a large, ordinary class of footage.
- An unrecognised transfer function is *unknown*, not SDR (R1.7), and unknown means **do
  nothing** (R2.7).

This is the same conservatism `worker/language.py` already applies when it declines to
report a language for Han script rather than guessing between Chinese and Japanese, and
`script_support.py` when it reports `caption_script_unsupported` rather than substituting a
font that cannot render the text. Declining is an established pattern here.

### R2: tone-mapping, and where it must sit

`zscale` requires `libzimg` at build time and is **not guaranteed present** — the
`Dockerfile` deliberately does not pin ffmpeg, because pinning "fails to resolve the moment a
security update lands." So availability is resolved through
`worker/engines/capabilities.py` with `ffmpeg_filter:<name>` ids (R2.3), and absence degrades
with a marker naming the missing capability (R2.4) and never fails the job (R2.5).

The project has already been burned by assuming a filter exists — `golden_render.py`'s
docstring records that *"a capability probe hid 124 ffmpeg filters."* That is why
`capabilities.py` exists, and why R11.1 forbids adding a second probe.

**R2.2 places the tone-map before everything colour-dependent, and before scaling.** Two
independent reasons:

1. Any grade or LUT applied to PQ-encoded values does something arbitrary. Tone-mapping must
   establish a known colour space before anything interprets the pixels.
2. Scaling in the wrong transfer function is subtly wrong: interpolating PQ-coded values
   averages perceptual quantities, not light, so edges pick up haloes that survive to the
   output.

**R2.8's "at most once" matters** because the compositor is one pass but the pipeline is
three. A tone-map applied at the cut *and* at the composite would compress the range twice
and produce a visibly muddy, flat result — worse than not tone-mapping at all, and
frustratingly plausible-looking.

R2.10 exposes the operator and target peak because the right choice is content-dependent and
contested; hard-coding one would bake in a taste. R2.11 defaults on, per the Overview.

### R3: declaring what we deliver

R3.2 is the clause that prevents the most likely bug: tags must describe **what was
delivered**, not what arrived. After tone-mapping an HDR source, the output is Rec.709 and
must say so. Copying the source's `smpte2084` transfer onto a tone-mapped file tells players
to apply an HDR EOTF to SDR content — which produces a *worse* result than no tags at all,
because now the player is confidently wrong.

R3.6 guards the matching failure: tags that contradict `-pix_fmt yuv420p`.

R3.3 resolves range explicitly rather than passing through. Phone footage is often
full-range; passing `pc` through into a limited-range delivery pipeline crushes blacks or
lifts the image. R3.7 requires the applied value to be *recorded* when the source is silent,
because "we assumed limited" is a fact a debugging operator needs.

R3.8 requires verification by **probing the output**. Asserting that `-colorspace bt709` is
in the argv proves the argument was passed, not that the muxed file carries it — and those
are different things across ffmpeg versions and containers. This is the same distinction that
`golden_render.py` exists to enforce for pixels: check the artefact, not the intent.

---

## Group B — Encode quality

### R4: preset, and the honesty requirement

`x264_preset = "veryfast"` (`config.py:354`), paid three times per clip. `veryfast` disables
or reduces most of what x264 does to preserve detail at a given CRF; at CRF 20 the difference
from `medium` shows as softer texture, more blocking on motion, and degraded edges on the
heavy caption typography this project vendored 12 fonts to get right.

This is a one-word change and it would be easy to just make it. The design refuses to,
because *"slower preset is better"* is a general truth that can be **invisible on specific
footage** — and `medium` is roughly 2× the encode time, three times over. R4.1 requires
measuring against the fidelity instrument; R4.2 requires reporting time and size beside
quality; R4.7 requires the time cost to be recorded in the change so an operator can choose
speed knowingly.

R4.6 forbids touching CRF here. CRF and preset interact, and moving both at once means
neither measurement is attributable. `clip-quality-uplift` R12 owns intermediate CRF; this
owns preset.

R4.4 puts a default change in its own commit with the fixtures re-frozen there — the
established discipline, and the reason is the `font_substituted:Arial` failure mode, where a
golden had a defect frozen into it as correct. A default change bundled with behavioural work
is how that happens.

### R5: one scaler, everywhere

No `-sws_flags` anywhere, so every `scale=` in `ffmpeg_utils.py` (the cover/fit/blur chains
at :569–640, the thumbnail at :763) and `reframe.py` (:1465, :1978, :2038) runs bicubic.

**R5.3 is the important clause: the same flags on every scale in a job.** With three passes
and several scaling sites, two stages resampling differently is a real possibility, and the
result is a compounding softness nobody can attribute to a stage. One value, applied
uniformly.

R5.4 forbids touching geometry. This is an *algorithm* change only — no dimension, no aspect
handling, no letterbox behaviour moves. Bundling a geometry change with a resampling change
would make the fidelity measurement meaningless, since the reference and the candidate would
no longer be the same picture.

R5.6 explicitly permits "no measurable difference — keep the default." On heavily compressed
1080p sources the algorithms can be hard to separate; the 4K→1080×1920 downscale is where the
difference should appear, and if it does not, that is the finding.

### R6: keyframes derived from the delivered rate

Nothing sets `-g`, so x264's default of 250 applies — about 8 seconds at 30 fps.

R6.2 derives the interval from the **delivered** frame rate, not a fixed frame count, which
matters precisely because R8 makes the delivered rate variable. A hard-coded `-g 60` would
mean 2 s at 30 fps and 1 s at 60 fps, silently changing meaning per source.

R6.4 keeps intermediates alone: constraining an encoder that is about to be re-encoded costs
quality for no delivered benefit. This is the same reasoning `h264_args` already applies to
`normalise_fps` and `vbv_cap`, both documented as off for intermediates.

R6.5 keeps scene-change keyframes. Forcing a fixed GOP with `-sc_threshold 0` would put an
I-frame in the wrong place on every cut, which is worse for both quality and seeking.

### R7: audio bitrate

`-b:a 128k` (`ffmpeg_utils.py:262`) — adequate for speech, thin under a music bed. R7.2
leaves `AU8`'s sample-rate and channel-count normalisation alone; the existing docstring
explains why those exist (mono clips playing from one side, surround silently downmixed) and
none of that changes.

R7.3 keeps within the platform profile, and R7.6 forbids raising it on intermediates beyond
the final value — spending bits on an intermediate audio stream that is about to be re-encoded
is waste, not fidelity.

---

## Group C — Frame rate

### R8: the policy, and why the current rule was right for the wrong scope

`config.py`'s comment for `output_fps` is worth taking seriously:

> every screen recording and most phone footage — has no single frame duration, so burned
> captions drift against speech as the effective rate wanders.

That is **correct for VFR sources** and it is the reason `O3` exists. The defect is scope: it
is applied to *every* source. A CFR 24 fps film-look source resampled to 30 gets a 3:2 judder
pattern on every pan; a 60 fps source loses half its temporal information.

The policy:

| Source | Delivered | Why |
| --- | --- | --- |
| VFR | normalised | R8.2 — the original reasoning, preserved intact |
| CFR at 24/25/30/50/60 | source rate | R8.3 — platforms accept these; resampling only damages |
| CFR at another rate | normalised | R8.4 — an odd rate is a re-timing risk downstream |
| Undeterminable | normalised | R8.6 — normalising is the safe default |

R8.5 caps at the platform profile's maximum, so the pass-through cannot deliver 60 fps where
a profile allows 30.

**R8.9 is the gate, and it is specific:** default to the policy only if *sync verification
passes for a CFR source at each Platform_Frame_Rate*. Frame-rate handling is the single most
likely place to introduce A/V drift, and drift desynchronises every burned caption — the exact
harm the original unconditional rule was written to prevent. This is why
`render-quality-measurement` R4 (Sync_Offset, including the VFR case) needs to exist before
this requirement's default can be flipped. R8.8 keeps unconditional normalisation available
for anyone who wants the old guarantee.

R8.10 leaves intermediates alone, matching `h264_args`' existing documented behaviour.

---

## Group D — Source repair

### R9: deinterlacing, ordered first

R9.2 places deinterlacing **before crop and scale**, and the ordering is the whole point:
combing artifacts that are cropped and scaled become a smear no later filter can undo.
Deinterlacing must be the first thing that touches the frame — even before the tone-map,
because tone-mapping interleaved fields blends two different moments in time.

R9.5 preserves frame rate rather than doubling it. `yadif`'s field-doubling mode produces
50/60 fps from 25/30i, which sounds like a bonus and interacts badly with R8's policy and with
`-g` derivation. Default to frame-preserving.

R9.3 and R9.8 mirror Group A's conservatism: never deinterlace progressive content, and where
interlacing cannot be determined, do nothing and record that the determination was
inconclusive. Deinterlacing progressive footage costs real vertical detail.

### R10: stabilisation, opt-in, and the margin problem

Default off (R10.3): stabilisation is slow, needs a two-pass analysis, and is wrong for
plenty of footage.

**R10.5 is the subtle requirement.** `vidstab` corrects shake by moving the frame, which needs
margin — it crops in. Reframing *also* crops in. If both consume the same margin
independently, the result is over-cropped, and worse, the reframe's crop window can drift
outside valid pixels and produce black edges. So the stabiliser's consumed margin must be
handed to reframing as part of the geometry it is allowed to move within. `reframe.py`'s
`build_sendcmd` already accepts `origin_x`/`origin_y`/`src_w`/`src_h` for exactly this shape of
problem — it is how `V16` letterbox handling confines the crop to the content rectangle. The
same mechanism carries the stabilisation margin, which is why no new concept is needed.

R10.4 orders stabilisation before reframing so the crop tracks a stabilised subject; tracking a
shaking subject and then smoothing the crop with EMA fights the same motion twice.

R10.8 covers progress: a two-pass analysis on a long source looks like a stalled job, and this
project already reports per-stage timings at `GET /api/jobs/{id}/timings`.

R10.9 refuses to stabilise detected screen recordings — synthetic content has no camera shake,
and `vidstab` on a static screen capture finds spurious motion in scrolling text and
introduces wobble that was not there. (Screen-recording detection is
`clip-presentation-polish`'s; this requirement consumes it if present and otherwise does
nothing.)

---

## Testing strategy

The governing rule, per the working agreement: **verify by probing rendered output, never by
asserting on arguments** (R13.1). An `-colorspace bt709` in the argv proves the flag was
passed; only the muxed file proves it landed.

| Area | File | Nature |
| --- | --- | --- |
| Colour probing | `tests/test_probe_colour.py` | Real ffmpeg-generated sources with explicit colour signalling; unknown/absent fields report unknown, never a guess. |
| HDR classification | `tests/test_probe_colour.py` | PQ and HLG classify HDR; 10-bit Rec.709 and 4K SDR do **not** (R1.6); an unrecognised transfer is unknown. |
| Tone-map end to end | `tests/test_colour_pipeline.py` | Real HDR-signalled source through the real pipeline; **probe the delivered file** for Rec.709 tags. |
| Tone-map absence | `tests/test_colour_pipeline.py` | With `ffmpeg_filter:zscale` unavailable via an injected prober: clip still delivered, marker names the missing capability, job does not fail. |
| Double tone-map | `tests/test_colour_pipeline.py` | Assert exactly one tone-map in the job across all three passes (R2.8). |
| Colour tag consistency | `tests/test_output_compat.py` | Delivered tags describe delivered content, not the source (R3.2); no contradiction with `pix_fmt` (R3.6). |
| Frame-rate policy | `tests/test_frame_rate_policy.py` | Real CFR sources at 24/25/30/50/60 delivered at their own rate; real VFR normalised; undeterminable normalised. **Probe the output.** |
| Sync across the policy | `tests/test_sync.py` (extends `render-quality-measurement`) | Sync preserved for each Platform_Frame_Rate — R8.9's gate. |
| Keyframe interval | `tests/test_output_compat.py` | Probe delivered keyframe positions; interval derived from delivered fps; intermediates unconstrained. |
| Scaler uniformity | `tests/test_output_compat.py` | Every scale in a job carries the same flags (R5.3); geometry byte-identical (R5.4). |
| Deinterlace detection | `tests/test_deinterlace.py` | Real interlaced source detected, real progressive not; inconclusive → no action + marker. |
| Stabilisation margin | `tests/test_stabilisation.py` | The margin `vidstab` consumes is reflected in the geometry handed to reframing; no black edges (R10.5). |
| Drift pin | `tests/test_output_compat.py` | The existing `libx264`/`-crf` pin still holds after every new flag is added. |
| Markers | `tests/test_effects_markers.py` | Resolved value recorded, never requested (R11.3); every pre-existing marker spelled unchanged (R11.5). |

Property tests use `hypothesis` at `max_examples=100`, one property per test, tagged
`# Feature: clip-signal-fidelity, Property N: <text>`.

**Baseline:** `pytest` → **2030 passed, 0 skipped, 0 warnings**; `npm run test:run` → **141
passed**, recorded before starting.

### What the suite cannot tell us

It can prove the delivered file *says* Rec.709 and that exactly one tone-map ran. It cannot
tell you the tone-mapped image looks right — operator and peak-luminance choices are
judgements about how highlights should roll off, and only eyes settle those. Same for the
preset and scaler: the instrument gives SSIM and time, and whether 0.4 dB of PSNR is worth
2× encode time is a decision, not a measurement.

Hence R13.11: rendered output attached for every visible change. `scripts/smoke_reel.py`
exists for this, and the HDR case needs footage the repo cannot contain — so the artefacts
have to come from whoever runs it.

---

## Consequences

**What this fixes:** HDR sources stop coming out grey. Delivered files stop being untagged.
Range stops being accidental. Scaling stops being softer than necessary. 24 and 60 fps
footage stops being resampled for no reason. Interlaced footage stops being smeared.

**What it does not fix:** the pass count is still three (`O6`), so generation loss is reduced
but not eliminated. Nothing here improves framing, pacing, or selection — those are the other
three specs. And 10-bit output stays out of scope, so banding on gradients remains.

**The likeliest way this goes wrong:** tone-mapping applied twice, or applied to a mislabelled
SDR source. Both produce plausible-looking bad output rather than an error, which is the
hardest defect class in this codebase to see. R2.7, R2.8, and R1.6 exist specifically to make
those two cases impossible, and the mutation specification should attack them first.
