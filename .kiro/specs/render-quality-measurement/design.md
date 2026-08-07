# Design Document — Render Quality Measurement

## Overview

Four instruments, no behavioural change. Each one answers a question the project currently
cannot answer, and each one is careful about the boundary of what it can conclude.

| Instrument | Question it answers | Question it cannot answer |
| --- | --- | --- |
| `evaluation/fidelity.py` (`M9`) | How faithfully does this render reproduce a higher-quality reference? | Whether the clip is good |
| `evaluation/caption_timing.py` (`M10`) | How far do captions land from their words? | Whether the captions read well |
| `evaluation/sync.py` (`M11`) | Is audio aligned with video in the delivered file? | Whether the cut was well chosen |
| `scripts/preference.py` (`M12`) | Do humans prefer A or B? | Anything, at low trial counts, with confidence |

The fourth exists because the first three cannot judge quality — only fidelity, timing, and
sync. That distinction is the design's spine and every section returns to it.

### Placement

```
evaluation/
  golden_render.py   (M1, exists)  "did it change?"      ← 8x8 average hash, deliberately coarse
  wer.py             (M3, exists)  "are the words right?"
  metrics.py         (S1, exists)  "are the moments right?"
  fidelity.py        (M9, NEW)     "is the picture damaged?"
  caption_timing.py  (M10, NEW)    "are the captions on time?"
  sync.py            (M11, NEW)    "is audio with video?"
scripts/
  eval_fidelity.py   (NEW)  template/run/compare, mirroring eval_selection.py
  eval_captions.py   (NEW)
  preference.py      (M12, NEW)  offline A/B, no service
```

Nothing in `worker/` changes. That is a deliberate boundary: an instrument that reaches into
the render path can be made to agree with it.

---

## M9 — render fidelity

### The capability problem, and why it is designed for rather than around

VMAF is the metric everyone wants and the one this project cannot rely on. `libvmaf` needs
an explicit build flag; Debian's ffmpeg routinely lacks it. And the `Dockerfile` is explicit
that **ffmpeg is deliberately unpinned** — pinning "fails to resolve the moment a security
update lands," and a working image beats a byte-identical one.

The project has already paid for assuming a filter exists. `golden_render.py`'s docstring
records the incident: *"a capability probe hid 124 ffmpeg filters."* The response was
`worker/engines/capabilities.py`, which probes `ffmpeg_filter:<name>` ids, guarantees
totality (any string, never raises), and caches per process.

So the design is:

- **SSIM and PSNR are the floor.** Built-in filters in every practical build, no flags.
- **VMAF is an enhancement**, resolved through `capabilities.get_report()` with
  `ffmpeg_filter:libvmaf`, reported as unavailable with a named reason when absent (R1.4),
  and **never fatal** (R1.5).

This mirrors `video_encoders.resolve_encoder`, which the project already trusts: probe for
real, fall back, record a marker, never fail the job. The same philosophy, one layer up.

```python
def available_metrics(report=None) -> tuple[Metric, ...]:
    """SSIM and PSNR always; VMAF when the build has libvmaf.

    Resolved through worker.engines.capabilities so there is one probe in the codebase and
    not two. A second probe is how the 124-hidden-filters defect happened: the answer was
    cached in a place nobody was looking at.
    """
```

### Reference_Render: what we compare against

A full-reference metric needs a reference. There is no pristine master — the source itself
is not it, because the Final_Render has been cropped, scaled, captioned, and graded, and
comparing a 1080×1920 captioned vertical clip against a 4K horizontal source measures the
reframe, not the encode.

The reference is therefore **the same filter graph at a deliberately higher fidelity**:
same crop, same captions, same everything, encoded at a much lower CRF and a slower preset.
That isolates the encode. Any pipeline difference other than the encode settings makes the
number meaningless, which is what R1.6's alignment refusal guards.

R1.6 also refuses to report on inputs that could not be aligned on frame count and
resolution. A frame-count mismatch — one clip 899 frames, the other 900 — makes ffmpeg's
`ssim` filter compare frame *N* against frame *N+1* for the entire remainder, producing a
plausible, catastrophic, and completely misleading number. Refusing beats reporting.

### Minima, not just means (R1.8)

`ssim`/`psnr` print a mean over all frames. A mean of 0.98 is compatible with one frame at
0.4 — a single badly damaged frame, exactly what a keyframe placement bug or a
scene-change encode decision produces, and exactly what a viewer notices. So the design
parses per-frame output and reports the minimum alongside the mean.

### The cross-check (R1.7)

Per the working agreement: anything parsing another program's output gets a test running the
real program, cross-checked independently. For fidelity that is straightforward and worth
stating, because it is the one place a self-consistent bug is invisible:

- Compare a render **against itself**: SSIM must be exactly 1.0 and PSNR infinite. Any
  parsing error that scales, offsets, or misreads a field breaks this.
- Compare against a **deliberately degraded** render (CRF 45, heavy scaling): every metric
  must be worse, in the correct direction. This catches sign and ordering errors.

Neither cross-check shares code with the parser, which is the requirement.

### What R1.9 forbids

A Fidelity_Metric measures reproduction, not quality. A clip that is beautifully framed and
badly encoded scores low; a clip that is a perfect reproduction of a badly framed reference
scores 1.0. Reporting SSIM as "clip quality" would be precisely the category error `M12`
exists to prevent, and the report says so in its own text.

---

## M10 — caption alignment error

### Why `wer.py` cannot do this

`evaluation/wer.py` measures transcript accuracy with careful normalisation — casing,
punctuation, contractions. That normalisation is correct for WER and **wrong for timing**:
it merges and drops tokens, and a merged token has no single true time. R3.8 forbids reusing
it where it would do that.

The defect this measures is also different in kind. A transcript can be word-perfect and the
captions still visibly wrong, because with `words_to_cues(max_words=3)` and `\kf` karaoke
fill, a 200 ms error is a highlight landing on the wrong word. WER would report 0 %.

### Signed distribution, not absolute error (R3.3)

This is the design's most important choice. A systematic +150 ms lag and symmetric ±150 ms
jitter produce the same mean absolute error and are **different defects with different
fixes**: a lag is a constant offset (a pipeline or ASR-boundary issue, fixable with a single
compensation), jitter is per-word imprecision (fixable only with alignment). Reporting only
the absolute value destroys the information that tells you which one you have.

Hence mean, median, p90, and max (R3.2) over a **signed** distribution.

### Measuring what was rendered (R3.4)

The measurement reads the caption events as rendered — parsed back out of the generated ASS,
or from the SRT sidecar `subtitle_export.py` produces — not the intermediate word list. Every
transform between the word list and the screen is in scope: `words_to_cues` grouping,
`_ass_timestamp`'s centisecond rounding, `\kf` centisecond fill durations, and any snapping
or hygiene a sibling spec adds. Measuring the word list would exclude exactly the layers
most likely to introduce error.

`_ass_timestamp` rounds to centiseconds, so there is an inherent ±5 ms floor. The report
names it, so nobody chases 3 ms of "drift" that is the format.

### Ground truth (R3.5)

The labels must not come from the ASR being evaluated — that would measure self-consistency.
So: hand-marked word times on a short passage, or synthesised speech at known timings. Both
are legitimate; the report says which was used, because a synthetic passage overstates
accuracy on real speech and a hand-marked one has human variance.

R3.7 requires reporting unmatched caption events rather than dropping them. Silently
excluding the events that could not be matched is how a metric improves while the output
gets worse.

---

## M11 — A/V sync verification

### Not an accusation

`cut_segment` builds `-ss <start> -i <source> -t <duration>` — input seeking before the
input. With `reencode=True` that is accurate in modern ffmpeg, and this design makes **no
claim that it is broken** (R4.8). It observes that nothing measures it, and that if it ever
drifts, every burned caption drifts with it and no existing test would notice.

### Measuring from the file, not the arguments (R4.2)

The whole value is in reading the delivered file's actual streams. A test asserting the
argument list contains `-ss` proves nothing about the output. The measurement decodes and
compares a known audio event's position against a known visual event's position — which
means the fixtures must contain a synchronised transient in both streams: an ffmpeg-generated
clap paired with a frame-level flash.

### The three cases that matter (R4.3–R4.5)

Chosen because each exercises a distinct mechanism that could desynchronise:

1. **Non-zero start offset** — the seek path itself, and audio-priming/timestamp-offset
   behaviour that varies between ffmpeg versions.
2. **Variable frame rate source** — `output_fps=30` resamples VFR to CFR, and `config.py`'s
   own comment says VFR is "every screen recording and most phone footage." Resampling video
   without touching audio is a classic drift source.
3. **Keep-interval concat** — `filler.apply_keep_intervals` builds `trim`/`atrim` + `concat`
   with `_seam_fades`. It deliberately uses `afade` rather than `acrossfade` *because* a
   crossfade would shift the timeline. That reasoning is sound and should be verified rather
   than trusted, especially since `clip-quality-uplift`'s interior-silence work will create
   far more seams than filler removal does today.

R4.6 reports the measured offset rather than a bare pass. A run reporting 8 ms is
meaningfully different from one reporting 0 ms, and only one of them is a trend.

---

## M12 — pairwise preference

### Why it exists

The three sibling specs contain requirements like "attach rendered output" and "prove this
helped." For headroom framing, dead-air tightening, a colour grade, or an encoder preset,
**there is no objective metric.** SSIM against a reference cannot tell you a face is framed
better; it will report that moving the crop made the render *less* similar to the old one,
which is the intended change.

So `M12` formalises what would otherwise be one person's impression: blind, order-randomised,
one-dimension-at-a-time, declines recorded.

### Design decisions, each with its failure mode

| Decision | Requirement | Failure mode it prevents |
| --- | --- | --- |
| Blind presentation | R5.2 | Knowing which is "the new one" produces the expected answer |
| Randomised order | R5.3 | Position bias — first or second gets systematically favoured |
| Declines recorded, not discarded | R5.4 | "No visible difference" is the most useful finding for a change that costs render time, and discarding it manufactures a preference |
| One named dimension per set | R5.7 | Judging an accumulation of five changes tells you nothing about any of them |
| Trial count and judge count reported | R5.5 | Six trials by one person reads as evidence unless the numbers are visible |
| Never reported as significant | R5.5, R5.6 | This is the honest boundary. At realistic trial counts a 4–2 split is noise |
| Judge-is-author recorded | R5.9 | Not forbidden — usually unavoidable on a small project — but a reader must be able to discount it |

R5.8's offline constraint keeps this a local static page plus a JSON results file. A hosted
A/B service would be a dependency and a privacy question for what is fundamentally a
directory of clip pairs.

**What this cannot do**, stated in the report itself: distinguish a real preference from
noise at small *n*; represent an audience rather than the team; or detect a change that is
better on average and occasionally catastrophic. The last is why `M9`'s per-frame minima
exist — objective and subjective instruments cover each other's blind spots.

---

## Interface

Mirroring `scripts/eval_selection.py`'s `template` / `validate` / `run` / `compare` (R6.2),
because that shape is already in the repo and known:

```
python scripts/eval_fidelity.py run     --source clip.mp4 --out eval/fidelity-v0.11.0.json
python scripts/eval_fidelity.py compare eval/fidelity-v0.11.0.json eval/fidelity-medium.json
python scripts/eval_captions.py  run     --labels eval/caption-labels/ --out ...
python scripts/preference.py      build   --dimension x264_preset --pairs 12
python scripts/preference.py      report  eval/preference/x264_preset/
```

`compare` (R2.7) is what makes this useful in review: a PR changing `x264_preset` attaches
a diff naming every metric that moved, in both directions, with encode time and file size
beside it (R2.3). A quality gain with a 2× time cost is a trade to discuss, not a win to
announce.

R2.6's clause that readings are not comparable across ffmpeg versions is a direct
consequence of the unpinned-ffmpeg decision. Two reports taken on different builds are two
different experiments, and `compare` must say so rather than subtract them.

---

## Testing strategy

| Area | File | Nature |
| --- | --- | --- |
| Self-comparison identity | `tests/test_fidelity.py` | Real render vs. itself: SSIM exactly 1.0, PSNR infinite. **Independent cross-check** — no shared parsing. |
| Degradation ordering | `tests/test_fidelity.py` | Real CRF-45 render scores worse on every available metric. Catches sign/ordering errors. |
| Frame-count mismatch refusal | `tests/test_fidelity.py` | Mismatched inputs refuse rather than report. |
| Per-frame minima | `tests/test_fidelity.py` | A single deliberately damaged frame moves the minimum but barely moves the mean. |
| VMAF absence | `tests/test_fidelity.py` | With `ffmpeg_filter:libvmaf` unavailable via an injected prober, SSIM/PSNR still report and VMAF is an explicit unavailable state — **not** a pass (R7.9). |
| Alignment zero and shift | `tests/test_caption_timing.py` | Events at labelled times → 0.0; events shifted by 120 ms → +120 ms, **signed**. |
| Unmatched events | `tests/test_caption_timing.py` | Reported, not dropped. |
| Sync detection | `tests/test_sync.py` | Deliberate 200 ms offset detected at 200 ms; a synchronised clip reports ≈ 0 within tolerance. |
| Sync, three cases | `tests/test_sync.py` | Non-zero start, VFR source, keep-interval concat — one test each. |
| Preference harness | `tests/test_preference.py` | Order randomised across trials; blinding holds; declines counted; no significance claimed in output. |

All metric tests run **real ffmpeg** (R7.5) with no availability skip: ffmpeg is a hard
dependency and a skip would mean it vanished, which is what the no-skips rule exists to
surface. Property tests use `hypothesis` at `max_examples=100`, one property per test.

**Baseline:** `pytest` reports **2030 passed, 0 skipped, 0 warnings** and
`npm run test:run` **141 passed** before starting. A drop means something stopped running.

### What this spec cannot tell us

It measures fidelity, timing, and sync. It does not measure whether a clip is worth
watching. `M12` gestures at that and is explicitly too small to be conclusive. The honest
summary: **this spec makes render changes arguable with evidence, not decidable by
metric** — which is the difference between the current situation and a good one, but not
the difference between guessing and knowing.
