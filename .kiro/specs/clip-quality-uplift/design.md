# Design Document — Clip Quality Uplift

## Overview

This feature is unusual for this repository in that **most of it is not new code**. The
signals exist. The scoring vocabulary exists. The keep-interval renderer exists. The
seam fades exist. The rebasing exists. The evaluation harness exists. What is missing is
smaller and more embarrassing than a subsystem: a dataset, a weight, a directory, a
default, and a sort key.

That shapes the design. The governing decision is **where each change is allowed to
live**, because the failure mode here is not "the feature does not work" — it is "the
feature works, changes the output, and nobody can tell whether the output got better."

Three structural rules follow, and every section below obeys them:

1. **Group A is a gate, not a phase.** No ranking change lands before the Benchmark_Dataset
   and Baseline_Report exist. The working agreement already says this; this design makes
   it mechanical by putting the blending weight's default at "reproduce v0.11.0", so
   the code can land inert and be turned on by a measured decision rather than a hopeful
   one.
2. **New arithmetic goes in pure functions with no I/O.** Pitch estimation, reaction
   proxying, span snapping, span hygiene, blending, and silence planning are all pure.
   The only new I/O is one audio pass for pitch, and it reuses the existing pattern.
3. **Nothing gets a second definition.** The blend uses `worker/candidate_ranking`'s
   normalisation vocabulary, not a parallel one. Interior silence uses
   `filler.plan_keep_intervals`' output type, not a parallel one. End snapping uses
   `scene_detect`'s cut scanner, not a parallel one. Two definitions of "energy" or
   "keep interval" would be a worse defect than any this spec fixes.

### Where each change sits

```
run_pipeline (worker/pipeline.py)
  ├── transcribe ─────────────────────────────── unchanged
  ├── energy_envelope (once per source) ──────── reused by R7 snapping   ← no new pass
  ├── pitch_track   (once per source) ────────── NEW (R4)               ← one new pass
  ├── reaction_events (from envelope + spectra) ─ NEW (R5)              ← no new pass
  ├── select_moments (worker/selection.py)
  │     ├── annotate: speech rate, energy, hook, discourse ─ unchanged
  │     ├── annotate: pitch variation, reaction ──────────── NEW (R4, R5)
  │     ├── LLM path → LLM_Opinion
  │     ├── blend_scores(opinion, features, weight) ──────── NEW (R3)
  │     ├── deduplicate ──────────────────────────────────── unchanged, now after blend
  │     ├── snap_to_sentences ────────────────────────────── unchanged
  │     └── scene_detect.snap_candidates → snap_start + snap_end ── EXTENDED (R11)
  └── per clip
        ├── trim_edge_silence ────────────────────────────── unchanged
        ├── cut_segment  [Intermediate_Render] ───────────── quality raised (R12)
        ├── keep list: filler ∪ transcript cuts ∪ interior silence ── EXTENDED (R10)
        ├── slice_words → snap_word_spans → span_hygiene ─── NEW (R7, R8)
        ├── geometry [Intermediate_Render] ───────────────── quality raised (R12)
        │     └── resolve_detector default haar → mediapipe  ── DEFAULT CHANGE (R9)
        └── compositor [Final_Render] ───────────────────── quality unchanged (R12.3)
```

```mermaid
flowchart TD
    subgraph GateA["Group A — the gate"]
        L[eval/labels/*.json<br/>15+ sources] --> H[evaluation/harness.run_selector]
        H --> B[Baseline_Report<br/>F1@0.5 + mean_best_iou]
        B --> D{Selector beats<br/>longest?}
    end
    D -->|figures recorded| W[choose blend weight]
    W --> R3[blend_scores]
    subgraph Sel["Selection"]
        F1[hook / pace / energy / discourse] --> R3
        F2[pitch variation NEW] --> R3
        F3[reaction proxy NEW] --> R3
        R3 --> DD[deduplicate] --> SN[snap start + END]
    end
    subgraph Clip["Per clip"]
        SN --> KL[one keep list:<br/>filler ∪ cuts ∪ interior silence]
        KL --> SP[snap word spans → hygiene]
        SP --> CO[compositor / Final_Render]
    end
```

---

## Group A — the gate

### Why this is the whole spec's foundation, restated in design terms

`evaluation/` is genuinely good code. `match_predictions` is one-to-one and greedy, so
five near-duplicate predictions cannot score as five hits. `IOU_THRESHOLDS` sweeps
0.3/0.5/0.7 rather than picking one and hoping. `mean_best_iou` exists specifically to
separate *"found the right moment, cut it badly"* (≈ 0.45) from *"looked in the wrong
place"* (≈ 0.05) — which is the single most useful diagnostic anyone building a clipper
can have. `run_selector` injects `duration_of` and `transcript_of` so the harness needs
neither ffmpeg nor Whisper.

And it has never been pointed at the product. `eval/labels/` holds one `.gitkeep`.

So Group A adds **no code**. It adds data, and a committed number. The design work is
entirely in the labelling protocol, because a benchmark labelled inconsistently is worse
than none — it will confidently reward the wrong thing.

### Labelling protocol

`evaluation/dataset.py`'s docstring already argues the format down to `start`, `end`,
`note`, and explicitly refuses per-moment rank ("asking a human to order their own picks
invites second-guessing") and anything derivable. That reasoning is sound and this design
does not revisit it. What it adds is the **instruction**, which the format cannot carry:

> Mark every span you would actually post, watching at normal speed, once. Mark the span
> you would post — not the span containing the good bit. If you would trim two seconds off
> the front before posting, the label starts two seconds later. Do not mark a moment
> because it is *representative*; mark it because you would publish it. If a source yields
> one moment, record one.

Two clauses in that instruction are load-bearing:

- **"the span you would post"** makes the Boundary_Metric meaningful. If labels are sloppy
  at the edges, `mean_best_iou` measures the labeller's indifference rather than the
  Selector's precision, and boundary work (R11, `S9`, `AU7`) becomes unmeasurable.
- **"watching at normal speed, once"** prevents the labeller from doing a search the
  Selector is not doing. A human who scrubs a two-hour podcast four times finds moments no
  single-pass system can be asked to find.

Minimum 15 sources (R1.1) is not arbitrary: pooled — not per-source-averaged, which
`AggregateScore` already gets right — 15 sources at 2–5 moments each puts the
Primary_Metric on roughly 40–70 labelled moments. That is enough to see a 10-point F1
move and not enough to see a 2-point one, which is an honest resolution to claim.

The three required footage shapes (R1.3) map to the failure modes this spec touches:
single-speaker (isolates selection from framing), two-person conversational (where the
absent ASD hurts, so the Baseline_Report records the cost of the deferred `V3`), and
footage with audience reaction (without which R5 cannot be evaluated at all).

### `Baseline_Report` shape

`scripts/eval_selection.py` already has `template` / `validate` / `run` / `compare`.
`run` emits JSON and `compare` diffs two runs. So the report is that JSON, committed at
`eval/baseline-v0.11.0.json`, with the run metadata R2.6 requires.

The reproducibility clause (R2.4) has a wrinkle worth naming: **the `ai` path is not
reproducible.** The LLM is a network call with a sampling temperature. `random_baseline`
is seeded and fine; `uniform` and `longest` are deterministic; the Selector is not. R2.5
therefore requires the report to *say so* rather than the design pretending otherwise.
The practical consequence is that the report records the LLM path over a fixed transcript
cache and states its run count — and that **`compare` output for the `ai` path must never
be read as a regression signal from a single run.** The deterministic fallback path *is*
reproducible and is the one CI can meaningfully diff.

R2.8 forbids gating CI on an absolute F1 threshold. This is deliberate: an absolute
threshold on 15 sources would either be so low it never fires or so high it blocks
unrelated work, and it would tempt someone to add sources until it passes. The report is
a record, not a gate.

---

## Group B — using what we measure

### R3: the blend, and the trap inside it

Today, `worker/selection.py:466`:

```python
candidates.sort(key=lambda c: c.score, reverse=True)
```

On the LLM path, `c.score` is `LLM_Opinion` — the model's 0–100 number, clamped and
rounded. Every Measured_Feature sits in `c.features`, untouched. The in-source comment is
explicit that this is intentional, and the reasoning was good at the time: the features
had never been validated, so weighting them would have been guessing. Group A removes
that excuse.

The design is a single interpolation:

```python
def blend_scores(
    opinion: float,          # LLM_Opinion, 0..100
    measured: float,         # candidate_ranking-derived, 0..1
    *,
    opinion_weight: float,   # 1.0 reproduces v0.11.0 exactly
) -> float:
    """Combine the model's opinion with the Clipper's own measurements.

    ``opinion_weight == 1.0`` MUST return ``opinion`` unchanged, bit-for-bit, because that
    is the default and the golden fixtures depend on it. Do not write this as
    ``w*o + (1-w)*m*100`` and rely on float luck at ``w == 1.0``: branch on the boundary.

    ``measured`` arrives on 0..1 from ``candidate_ranking``; ``opinion`` on 0..100. They are
    reconciled here, once, rather than at the call site - a units mismatch here produces a
    plausible ordering that is wrong, which is the hardest defect class in this codebase to
    see.
    """
```

**The trap:** `score_candidate` already computes exactly the weighted mean of normalised
components this needs, using `settings.selection_weight_*`, and it is *only used on the
fallback path*. The temptation is to write a fresh weighted sum inside `selection.py`
over `c.features`. That would create a second definition of "how good does this look",
and the two would drift the first time someone tuned one of them. R3.10 forbids it. The
blend calls `score_candidate` for the measured half — which also means the seven existing
weight settings immediately become live on the LLM path, and pitch and reaction join them
as two more components rather than as a special case.

**Ordering (R3.8):** blending must happen *before* `deduplicate`, because dedup is greedy
over the score-sorted list — it keeps the highest-scored member of an overlapping set. If
blending happened after, dedup would have already chosen its survivors using the
unblended order, and the blend would only reorder what dedup left. Getting this backwards
is a silent, plausible-looking defect: the output is still a sensible list of clips.

**R3.7** — do not show the Blended_Score as the virality estimate. `LLM_Opinion` stays on
the record (R3.6). The UI's number is the model's; the ordering is ours. Conflating them
would make the displayed score unexplainable.

### R4: pitch, without a checkpoint

Pitch variation is the cheapest real signal this project is missing, and Opus's own API
documentation lists pitch range among its audio-dynamics inputs. It needs no model.

Constraint: **one additional audio pass per job, maximum** (R4.7), matching how
`audio_features.energy_envelope` does a single
`aresample → asetnsamples → astats → ametadata` pass for the whole source. Pitch follows
the same shape — one pass over the source producing a time-series, then pure windowing
functions over it, exactly as `energy_in_window` / `source_median_energy` already work.

The estimator is autocorrelation-based (YIN-family) over short frames. `librosa` would
be the obvious import and is the wrong choice here: it drags `numba`/`scipy` weight and,
more importantly, `filterwarnings = error` means every deprecation it emits becomes a
suite failure that is not ours to fix. A bounded autocorrelation estimator over frames is
~60 lines, pure, deterministic, and testable — and R4.8's cross-platform determinism
requirement is far easier to satisfy when the arithmetic is ours.

Two design points that follow from the signal's nature:

- **Relative, not absolute (R4.2).** A bass voice and a high voice must not be scored
  differently for being themselves. The measure is the window's F0 spread against the
  *source's own* median spread — the same "departure from this speaker's norm" framing
  `selection.py::_segment_annotation` already uses for pace and energy, and the same
  reason it uses it.
- **Unvoiced frames are not zero (R4.5).** An unvoiced frame has no F0; recording it as
  0 Hz would make silence look like enormous pitch variation. Unvoiced frames are
  *excluded*, and a window with too few voiced frames is marked unreliable and treated as
  neutral (R4.6) rather than as flat.

R4.9 puts pitch into the LLM prompt as a word (`animated`, `flat`), not a number,
because `_segment_annotation`'s existing design decision — only tag *departures* from the
speaker's norm, in words — is right, and a stray float in a prompt invites the model to
do arithmetic it cannot do.

### R5: reaction detection, honestly labelled

`S5` wants YAMNet and is blocked. The proxy: laughter and applause are broadband,
high-spectral-flatness, sustained-but-modulated energy bursts that do not carry an F0 —
which means the two new signals from R4 and the existing envelope together already
describe them better than either does alone. Applause in particular is close to shaped
noise: high flatness, no pitch, fast attack, sustained.

This will have false positives. Bursts of overlapping speech, room noise, and music
stings will trigger it. The design's response is not to claim otherwise:

- **R5.3/R5.4** — the Effects_Applied marker names it as a proxy. The precedent is
  `music_degraded:synthesised`: a synthesised bed is *offered* but *labelled*, because
  "shipping a hiss labelled `whoosh`" is exactly what `A15` exists to prevent. A
  heuristic labelled as a classifier would be the same defect in a new place.
- **R5.6/R5.7** — the false-positive rate is *measured on the Benchmark_Dataset* and the
  feature defaults on only if it does not hurt the Primary_Metric. This is the first
  feature in this project whose default is decided by the benchmark, which is the whole
  point of Group A.
- **R5.8** — `S5` stays open. This is a proxy standing in for a classifier, not a
  replacement for one.

### R6: the hook zero, and why it is being narrowed rather than removed

`worker/hook_score.py:193`:

```python
# Silence at the opening is disqualifying rather than merely costly: whatever the rest of
# the window measures, a clip that starts on dead air has no hook.
if promptness <= 0.0:
    total = 0.0
```

The comment is right, and the rule is right, and it is *too broad by exactly one case*: a
clip that opens on a laugh, on applause, or on a hard visual accent is not opening on dead
air, but `speech_promptness` cannot see any of that — it only looks at word starts. A clip
opening on the room erupting scores zero.

R6 narrows the predicate rather than deleting it: disqualify when there is *no* speech
**and** no Reaction_Event **and** no Onset in the hook window. That keeps the original
intent — dead air has no hook — while admitting hooks that are not words. It depends on
R5, which is why it sits in the same group.

R6.3/R6.4 keep the old behaviour reachable and default, pending the benchmark. R6.5
forbids touching `SPEECH_DEADLINE_S` or `HOOK_WINDOW_S`, because those are calibrated
against the rest of the hook arithmetic and moving them would confound the measurement of
this change.

---

## Group C — caption timing

### R7: snapping to onsets is not forced alignment, and must not be sold as it

Forced alignment (`T2`) uses an acoustic model to find each phoneme. This does something
much weaker: it nudges a word's *start* toward a nearby real transient. It will help where
Whisper's boundary drifted past a plosive or a syllable attack, and it will do nothing at
all inside a legato phrase with no transients. That is an honest, bounded improvement, and
it needs no weights.

The design leans entirely on `audio_features.detect_onsets`, whose docstring already makes
the crucial distinction:

> This only reports moments where the energy actually rose by `rise_db` between adjacent
> readings, which is a weaker claim and a true one: every returned time has a real
> transient at it.

That property is what makes snapping safe. **R7.3 forbids moving a span onto a time where
no Onset was detected** — so the worst case is that a word is not moved, never that it is
moved somewhere invented. Contrast with beat tracking, which infers tempo and phase and
would happily place a "beat" in silence; snapping to an inferred beat would move words
onto times where nothing happened.

```python
def snap_word_spans(
    words: Sequence[Word], onsets: Sequence[float], *, max_shift_s: float
) -> tuple[list[Word], int]:
    """Nudge word starts onto nearby real transients. Returns (words, moved_count).

    Starts only. A word's END is not snapped: the following word's start already defines
    where it stops for rendering purposes, and moving both independently is how you get
    crossing spans - which Span_Hygiene would then have to repair, hiding the cause.

    Order is preserved by construction (R7.5): a candidate onset is rejected if it would
    place this start at or before the previous word's snapped start. Sorting afterwards
    would silently reorder the transcript.
    """
```

**Two hazards.**

The first is the transcript cache. `transcribe()` caches raw ASR keyed on content hash,
and `T3` filtering is applied *after* the cache so the cache stays raw. Snapping must
follow the same discipline: it is a *rendering* transform (R7.7), applied to the spans
handed to captions, never written back. Snapping into the cache would poison every
downstream consumer of the transcript — the selection prompt, the WER harness, the
transcript editor endpoint, and the SRT sidecars — with times derived from an effect.

The second is filler rebasing (R7.11). `filler.rebase_words` maps words onto the
tightened timeline after `apply_keep_intervals` removes intervals. Onsets were detected on
the **original** timeline. Snapping rebased words against original-timeline onsets would
match words to transients belonging to removed audio. Therefore: **snap before rebasing,
or map the onsets through the same keep list.** The design snaps before, because
`plan_keep_intervals` and `rebase_words` are already a matched pair and inserting a third
transform between them is where an off-by-one lives.

R7.9 defaults it off, and R7.10 records the moved count — which is also the measurement
that tells you whether it did anything at all. If it moves 2 % of spans, it is not worth
enabling.

### R8: span hygiene is a repair with a priority order

Three invariants, and one case where they conflict:

1. monotonic non-decreasing starts,
2. no overlap,
3. minimum duration.

When extending a span to reach the minimum would overlap the next one, **non-overlap
wins** (R8.4). Overlapping `\kf` spans produce visibly wrong karaoke — two words
highlighted at once, or a highlight that jumps backwards — whereas a span slightly under
the legibility floor produces a fast highlight, which is merely suboptimal. Repairing in
the other priority order trades a cosmetic problem for a visible one.

R8.7 — a compliant sequence must come out **bit-identical**. This is what lets hygiene be
applied unconditionally on every path (R8.6) including `engines/kinetic.py`, without
changing any golden render for footage whose timings were already sane. It is also the
property that makes the property test worth writing: idempotence plus invariance on
already-valid input.

R8.6 including the kinetic engine matters because `worker/engines/kinetic.py` has its own
layout and timing logic (2599 lines). A repair applied only to the main caption path would
leave the kinetic path with the defect, and the two would diverge.

---

## Group D — the detector default

This is the smallest change in the spec and the one with the widest blast radius, because
`ProcessingOptions.reframe` defaults to `True` — so the detector is on the critical path
for essentially every clip.

The measurement already exists in the CHANGELOG: Haar 0.886 coverage overall and 0.60 on
a profile turn, against BlazeFace 0.971 and 0.90. The model is already vendored, licensed,
manifest-verified, CI-checked, and asserted through `/api/info` in the container smoke
test. `resolve_detector` already returns the resolved label and already degrades
`mediapipe → haar` with a substitution marker. **Everything needed to flip this default
was built by the `face-detection-upgrade` spec, which then deliberately did not flip it**,
to keep its own golden renders valid.

So the work is: flip one default, re-freeze the fixtures, and — the part that is not
bookkeeping — **verify the CHANGELOG's numbers on real footage** (R9.6). Those figures
were measured on *a synthetic source*. Synthetic footage is exactly where Haar's frontal
bias is least punished, so the real-footage gap is likely wider, but "likely" is not a
measurement, and R9.7 requires leaving the default alone if the real numbers do not
support the change. Group A's Benchmark_Dataset footage is the natural corpus, which is a
second reason Group A comes first.

R9.4/R9.5 and R14.2 put the flip in its own commit with the re-frozen fixtures and a
message naming what moved. A default change bundled with behavioural work is how a
fixture gets re-frozen around a real regression — the `font_substituted:Arial` failure
mode, where a golden file had the defect frozen into it as correct.

---

## Group E — editorial tightness

### R10: interior silence, and the single-re-encode invariant

`worker/pipeline.py` already resolves filler removal and the `U4` transcript cut list into
**one** keep list and **one** re-encode. That is the invariant to protect (R10.2/R10.3):
interior silence becomes a third contributor to the same list, not a fourth pass.

```
silences (cached per source, already computed for AU7)
   ∩ clip window
   → interior only (exclude those straddling a boundary — AU7 owns those)
   → filter by min duration, keep padding at each seam
   → intervals to DROP
        ⊕ filler drops
        ⊕ transcript cut-list drops
   → plan_keep_intervals → ONE apply_keep_intervals → ONE re-encode
   → rebase_words / emoji / speaker turns
```

`detect_silences` output is already memoised per source via
`intermediate_cache.memoise` for `AU7`, so this needs no new detection pass either.

Design points:

- **R10.7, padding.** Removing a silence *entirely* butts speech against speech, which
  sounds worse than the pause did. A configurable retained pad at each seam is the
  difference between "tightened" and "chopped". This is the setting most likely to need
  tuning by ear rather than by metric.
- **R10.4, seam fades.** `filler._seam_fades` already applies a few-ms `afade` at each
  interior seam, and deliberately *not* `acrossfade`, because a crossfade shifts the
  timeline the rebased words depend on. Interior silence removal creates many more seams
  than filler removal typically does, so this treatment stops being a nicety and becomes
  the thing preventing audible clicks.
- **R10.11, the refusal.** With an aggressive threshold on sparse footage this could
  remove a third of a clip and produce something the user cannot recognise as their own
  video. The refusal marker follows the established `transcript_trim_refused:*` pattern:
  decline, label, carry on.
- **R10.9 defaults this ON** — the one place this spec deliberately breaks its
  default-to-shipped-behaviour rule, because competitors trim dead air by default and a
  conservative threshold is a genuine improvement rather than a taste. R14.2 still applies:
  own commit, fixtures re-frozen there.
- **R10.12** keeps `filler_removal` off. Removing "um" is an editorial opinion about
  someone's speech; removing two seconds of nothing is not. They are different features
  and this spec does not conflate them.

### R11: end snapping, with sentence priority

`scene_detect.snap_candidates` calls `snap_start(start, end, cuts)` and never touches the
end. The asymmetry was correct when it landed — a clip that *begins* mid-shot reads as
broken, while one that *ends* mid-shot merely reads as unfinished — but the end is still
worth getting right.

The conflict rule (R11.3) is the design's substance: **a sentence end beats a shot
boundary.** Ending mid-sentence to land on a cut is worse than ending mid-shot on a
complete thought, because the transcript is the content and the shot is the packaging.
Since `snap_to_sentences` runs before `snap_candidates`, the end arriving at the snapper
is already sentence-aligned — so end snapping may only move it where doing so does not
break that alignment.

R11.6 bounds the scan per candidate. `detect_cuts(path, start)` already scans a narrow
window; the end scan uses the same mechanism at the other boundary rather than calling
`scan_cuts` over the whole source, which would be a full decode.

R11.8 gates the default on the **Boundary_Metric** specifically, not the Primary_Metric.
End snapping cannot change *which* moments are found — only where they stop. `mean_best_iou`
is the metric that can see that; F1@0.5 will barely move. Gating on the wrong metric would
produce a null result and an incorrect conclusion.

---

## Group F — fidelity

Each clip is encoded three times: `cut_segment` (re-encode for frame accuracy), the
geometry pass, and the compositor. All three run through `h264_args` at
`settings.x264_crf = 20`. Generation loss at CRF 20 × 3 is visible where it is least
wanted: in gradients, and on the high-contrast edges of exactly the heavy caption
typography this project vendored 12 fonts to get right.

Collapsing the passes is `O6` and is out of scope. What is in scope is that **an
intermediate file has no reason to be at the delivery quality setting.** Intermediates are
transient, local, and deleted; spending bits there is free in every dimension except disk
and a little encode time.

```python
def h264_args(*, intermediate: bool = False, normalise_fps=..., vbv_cap=...):
    """Single source of truth for H.264 arguments.

    ``intermediate=True`` selects ``settings.x264_crf_intermediate`` (a lower CRF, i.e.
    higher quality) for a render whose output is consumed by a later stage. The delivered
    render's CRF is unchanged, so the final file's quality target is exactly what it was.

    The drift pin in tests/test_output_compat.py fails if ``libx264`` or ``-crf`` is named
    outside this module and video_encoders.py. That pin is why this is a parameter here and
    not a conditional at the three call sites.
    """
```

R12.4's insistence on the single builder is the important clause. These flags were once
duplicated across seven call sites with three of them missing flags, which is why the
drift pin exists. Adding a per-call CRF override at the call sites would reintroduce
precisely that.

R12.8's size ceiling is the guard against the obvious failure: a near-lossless
intermediate for a 3-minute 4K clip can be very large, and a full disk fails the job.
Fall back and record it.

R12.9 is the honesty clause. "Fewer generations of compression is better" is true in
general and could still be *invisible* here — if CRF 20 three times is already
perceptually transparent on this footage, the change costs render time for nothing.
`evaluation/golden_render.py`'s perceptual hashing is the wrong tool (it is built to detect
*change*, not to rank quality), so this needs a direct measurement — PSNR/SSIM of the
final render against a single-pass reference — reported per R12.7.

---

## Group G — assets

Nothing here is clever; it is the emoji pattern applied twice more.

| Precedent (already shipped) | This spec |
| --- | --- |
| `assets/emoji/` — 326 PNGs committed | `assets/music/<mood>*.mp3` — one track per mood |
| `assets/font-licenses/`, `assets/models/LICENSE-blazeface.txt` | per-track licence files |
| `scripts/fetch_emoji.py --check`, `scripts/fetch_models.py --check` | `scripts/fetch_music.py --check` |
| CI runs the check; `docker_smoke.sh` asserts via `/api/info` | same shape (R13.4, R13.6) |

The moods come from whatever `find_user_tracks` resolves against `assets/music/<mood>*.mp3`
— the design does not invent a mood taxonomy, it satisfies the existing one.

R13.7 is the constraint that decides which tracks: redistribution must be as unambiguous
as the OFL fonts already vendored. In practice that means CC0, and it means reading the
actual licence rather than trusting a "royalty-free" badge on a download page. A track
whose provenance is unclear is worse than an empty directory, because the empty directory
degrades honestly and a mislicensed track is a legal problem shipped in a container image.

R13.10 creates the b-roll directory without enabling b-roll (R13.11). `assets/broll` does
not currently exist, so an operator who enables the option hits a missing path rather than
an empty library. Empty-and-present degrades correctly through the existing b-roll code;
absent does not. Note `.dockerignore` excludes `assets/emoji-*/` but not `assets/emoji/` —
the new directories must follow the latter pattern, and this is worth checking rather than
assuming, since a directory silently absent from the image would degrade only in
production.

---

## Testing strategy

Per the working agreement: **anything that parses or consumes another program's output
gets a test that runs the real program**, cross-checked through an independent mechanism
sharing no parsing code.

| Area | File | Nature |
| --- | --- | --- |
| Pitch estimation | `tests/test_pitch_features.py` | Real audio at known F0 via ffmpeg `sine`; assert estimate within tolerance. **Cross-check independently**: compute expected F0 from the generator's own parameters, not from the estimator. |
| Pitch windowing / determinism | `tests/test_pitch_features.py` | Property: relative measure invariant to a constant F0 offset; identical input → identical output. |
| Reaction proxy | `tests/test_reaction_features.py` | Real synthesised noise-burst vs. real speech-like signal; assert separation and **report** the false-positive behaviour rather than asserting a rate. |
| Blending | `tests/test_candidate_ranking.py` | Property: `opinion_weight == 1.0` returns opinion bit-identical; blend is monotonic in each input; ordering changes only where scores changed. |
| Blend/dedup order | `tests/test_selection.py` | Assert dedup observes blended order — construct a case where the two orders select *different* survivors. A test where both orders agree proves nothing. |
| Span snapping | `tests/test_caption_timing.py` | Property: no span moves further than `max_shift_s`; every moved start coincides with a supplied onset; order preserved. |
| Span snapping, real audio | `tests/test_caption_timing_real.py` | Real ffmpeg-generated audio with known transients → real `detect_onsets` → real snapping. No mocked onsets. |
| Span hygiene | `tests/test_caption_timing.py` | Property: output always monotonic, non-overlapping; already-valid input returned bit-identical; idempotent. |
| Hook narrowing | `tests/test_hook_score.py` | Assert zero retained for genuinely empty windows; assert not-zero when a reaction or onset is present; assert default config reproduces v0.11.0. |
| Interior silence | `tests/test_filler.py`, `tests/test_pipeline_trim.py` | Assert filler ∪ cuts ∪ interior resolve to **one** `apply_keep_intervals` call; assert seam fades at every interior seam; assert refusal marker over the fraction limit; assert min-duration guard. |
| Rebasing | `tests/test_pipeline_trim.py` | Assert words, emoji, and speaker turns all land on the tightened timeline — one test per consumer, because one rebased consumer does not imply three. |
| End snapping | `tests/test_scene_detect.py` | Assert sentence end wins over cut; assert shift cap; assert min-duration guard; assert no scan beyond the bounded window. |
| Detector default | `tests/test_face_detection.py`, `tests/test_reframe_geometry.py` | Assert the resolved marker names `mediapipe` by default and that `haar` remains byte-identical when selected. |
| Intermediate CRF | `tests/test_output_compat.py` | Assert the final render's args are unchanged; assert intermediates use the intermediate setting; assert the existing `libx264`/`-crf` drift pin still holds. |
| Assets | `tests/test_music_assets.py` | `--check` passes offline; a truncated copy fails and names the file; a real mood resolves without `music_degraded:synthesised`. |
| Container | `scripts/docker_smoke.sh` | Music assets resolve **through the API**, per the emoji precedent. |

Property tests use `hypothesis` with `@settings(max_examples=100)`, one property per test,
tagged `# Feature: clip-quality-uplift, Property N: <text>`.

**Baseline first:** record the current `pytest` count before starting. A drop at any point
means something stopped running, and a skip is not a pass.

### What the suite cannot tell us

This is the section that matters most, because most of this spec's value is not assertable:

- Whether the framing actually improved (R9) — only the pixels can say.
- Whether the tightened edit sounds natural or clipped (R10) — only the ears can.
- Whether karaoke lands better (R7) — a moved-span count is not a correctness measure.
- Whether the clips are *better clips* (Group B) — that is what the Benchmark_Dataset is
  for, and even then it measures agreement with one labeller's taste.

Hence R17.10: rendered output attached to the change for every requirement whose effect is
visible or audible. `scripts/smoke_reel.py` exists for this.

---

## Consequences, stated plainly

**What this spec will not fix.** After all of it lands, the largest visual gap against Opus
Clip remains open: on two-person footage we still follow the largest, most-diarisation-active
face rather than the person actually speaking. `V3` is where that is fixed, `LR-ASD` is the
first credible candidate for it, and this spec deliberately does not start it. Transcript
quality is still capped at Whisper `small`, which compounds into both selection and
captions and is fixed by building the GPU image, not by anything here. And the pass count
is still three.

**What could go wrong.** The most likely bad outcome is that Group A produces a
Benchmark_Dataset of 15 sources labelled by one person to one taste, and Group B tunes the
Selector to that taste. The Primary_Metric would rise and the product would not improve.
The mitigations are in the protocol — post-worthy spans, single pass, three footage shapes —
and in R2.7's requirement to state plainly whether we beat `longest`. If a weighted mean of
seven heuristics cannot beat "the longest silence-delimited segment", the honest conclusion
is that the selection approach is wrong, not that the weights need another pass.
