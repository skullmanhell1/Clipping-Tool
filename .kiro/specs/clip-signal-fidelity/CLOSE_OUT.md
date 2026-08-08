# Close-out — Clip Signal Fidelity, Group A (O13, O14, O15)

**Group A only.** Groups B and C are not started, and the reason is in the spec rather than in the
effort: they depend on `render-quality-measurement`, and without that instrument every one of them
is taste asserted against taste. See [What is not done](#what-is-not-done).

## What landed

| Item | What it does now |
| --- | --- |
| **O13** | PQ and HLG sources are tone-mapped to SDR Rec.709 before any scale or grade. On by default. |
| **O14** | Every encode carries `-colorspace`/`-color_primaries`/`-color_trc`/`-color_range` describing **what was delivered**. |
| **O15** | Full-range sources are converted to limited rather than passed through; an unstated range records the default applied. |

New module `worker/colour.py` owns the decision; `worker/ffmpeg_utils.py` reads four colour fields
that `probe()` was **already fetching and discarding**, and `h264_args` gained a `colour_tags`
slot. The pipeline resolves one plan per job and spends its filter chain exactly once.

## Measured

Synthetic PQ / BT.2020 10-bit source, delivered through the real pipeline:

| | Mean luma | Mean saturation |
| --- | --- | --- |
| Untone-mapped (what shipped before) | 125.3 | 112.1 |
| Tone-mapped | 79.7 | 142.7 |

Saturation up 27%, luma brought down to a correct level — PQ values interpreted as gamma render
too bright and washed out, which is the "grey and flat" complaint in the plan's own words. **This
is a controlled demonstration on a drawn source, not a claim about real footage.** The same
caveat the face-detection close-out records for its BlazeFace figures applies here.

Delivered file, probed rather than inferred from argv: `color_transfer=bt709`,
`color_primaries=bt709`, `color_space=bt709`, `color_range=tv`, `pix_fmt=yuv420p`,
`profile=High` — so O1 and O2 are intact (R3.4) and no tag contradicts the pixel format (R3.6).

## Gates

| Gate | Result |
| --- | --- |
| `pytest` | **2198 passed, 0 failed, 0 skipped, 0 warnings** (2160 before, 38 new) |
| `mypy .` | clean, 102 source files |
| `ruff check .` | clean |
| `scripts/mutate.py --spec tests/mutations/clip-signal-fidelity-colour.json` | **13 caught, 0 escaped** |

## What the mutation run actually found

The first run was **10 caught, 3 escaped, and one mutation I had wrongly declared equivalent**.
Recording that rather than only the final 13/13, because the handoff's rule is that a suspiciously
clean first result is a reason to look harder — and here the first result was not clean, which is
what made it useful.

1. **`tone_map_ignores_the_disabled_setting` escaped.** There was no test that switching
   tone-mapping off actually switched it off. The default is on, so nothing else in the suite ever
   took that branch.
2. **`range_conversion_inverted` escaped.** Swapping `in_range` and `out_range` still yields a
   valid filter and a plausible picture — washed rather than crushed, reading as a grading choice.
   The end-to-end test could not catch it either: the *tag* would still say `tv` while the samples
   were full-range. Only the chain's direction distinguishes them.
3. **`tone_map_probe_fails_open` escaped, and this one found a defect in a test rather than in the
   code.** `Capability_Report._probe` catches everything a prober throws and returns
   `available=False`, so the `except` in `tonemap_filters_missing` is unreachable from a raising
   prober. The test named for that fallback was passing through the ordinary path the whole time.
   The branch is now reached the only way it can be — an unimportable `capabilities` module — and
   the misleading test was renamed to say what it actually checks.
4. **`tags_prepended_to_the_encode_args` was declared equivalent and was caught.** The reasoning
   ("ffmpeg parses output options by name, not position") is true of ffmpeg and irrelevant to the
   test, which pins append-not-interleave. The claim was removed rather than the mutation.

## A defect found while writing this, unrelated to colour

`Capability_Report`'s `get_report(prober)` honours its argument **only on first construction** —
its own docstring says so — and returns the process-wide singleton otherwise. So in any process
where something has already probed a capability, an injected prober is accepted and silently
ignored. Two tests here passed against the real ffmpeg for the wrong reason before this was
found.

`worker/colour.py` therefore constructs a fresh `Capability_Report(prober)` when given one, and
uses the shared report only when not. **This is worth knowing for any future spec that injects a
prober** — the pattern is easy to copy from existing call sites and wrong in a test suite.

## Deliberate decisions worth not re-litigating

**Tone-mapping defaults ON**, which is this project's only output setting that does not default to
previously shipped behaviour. That rule protects the parity goldens from *accidental* change and it
is the right rule — but it protects goldens, not defects, and the alternative here is knowingly
delivering incorrect colour (R2.11). It is only defensible because the conversion cannot fire on a
source that is not positively HDR: `test_an_sdr_source_produces_no_filters_at_all` pins that, and
if it ever fails every golden in the project moves.

**No golden or parity fixture was re-frozen**, and none needed to be. The plan is empty for SDR and
for unknown sources, so an existing library renders byte-identically.

**Range conversion uses `scale`, not `zscale`.** `scale` is in every ffmpeg build. The tone-map may
degrade on a build without `libzimg`; the range fix must not, because it addresses the more common
defect — full-range phone footage crushing its blacks. Asserted, so "unifying" the two later
cannot pass quietly.

**The probe fails closed**, the opposite of `background_style_available`. Claiming `zscale` exists
when we do not know produces a filter-graph configuration error, which is a failed job, and R2.5
forbids failing a job over tone-mapping.

**Tags state what arrived when nothing converted it.** A Rec.601 source passed through untouched is
tagged `smpte170m`, not `bt709`. Tagging everything Rec.709 because almost everything is Rec.709
would make the file confidently wrong, which is worse than the absent tag a player would fill in
with the same assumption.

**`tone_mapping` is a `settings` field, not a `ProcessingOptions` field**, so
`assert_effects_off_is_exhaustive()` does not apply — verified by reading it, as task 9.4 asks,
rather than assumed. It sits with `x264_crf` and `x264_preset`: an output-contract setting, not a
per-job creative choice. That also means it is **not** surfaced through the API/form/UI (task 9.2),
consistent with every other setting in that group.

## What is not done

**Groups B and C of this spec, and they are blocked rather than skipped.**

- **O16 (encoder preset), O17 (scaler flags), O20 (audio bitrate)** — R4.1/R5.5/R7.5 each require
  the change to be *measured* against a fidelity instrument before the default moves. There is no
  `vmaf`, `psnr` or `ssim` anywhere in this repository. Making these changes now would be
  substituting one unmeasured default for another and calling it an improvement.
- **O18 (frame-rate policy)** — gated on `render-quality-measurement` task 5 (`Sync_Offset`) by
  R8.9. Frame-rate handling is the likeliest place to introduce A/V drift, and drift desynchronises
  every burned caption, which is the exact harm the current unconditional `-r 30` prevents.
- **O19 (keyframe interval)** — derives from the delivered frame rate, so it follows O18.
- **V20 (deinterlacing), V21 (stabilisation)** — buildable, but ordering matters: both belong
  *before* the tone-map in the filter order the design fixes (`deinterlace → tone-map → stabilise →
  crop/scale`), and V21 must hand its consumed margin to reframing through `build_sendcmd`'s
  existing `origin_x`/`src_w` mechanism or the crop drifts outside valid pixels and produces black
  edges. Neither is a colour change and neither should ride along with one.

**Still true of the delivered file after this change:**

- **The pass count is still three** (`O6`), so generation loss is reduced, not eliminated. The
  tone-map now happens once instead of never, but the clip is still re-encoded at cut, geometry and
  composite.
- **Output is still 8-bit.** Tone-mapping to 8-bit Rec.709 can band on smooth gradients — a sunset
  or a studio backdrop. 10-bit output is out of scope and would break the O1/O2 compatibility
  contract, which is the more valuable guarantee.
- **`npl` is fixed at the configured target**, not read from the source's mastering-display
  metadata. A source mastered at 4000 nits and one at 1000 are tone-mapped identically. Reading
  `master_display` is a real improvement and needs its own measurement to justify an operator
  default.
- **Nothing here improves framing, pacing, or selection.** Colour was wrong; it is now right. The
  clip is otherwise the same clip.
