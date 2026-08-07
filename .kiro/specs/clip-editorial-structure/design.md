# Design Document — Clip Editorial Structure

## Overview

This is the most product-shaped of the four specs and the one with the highest risk of making
things worse. The other three improve *measurement*, *signal*, and *presentation* — all
directions where a bad change is visible immediately. Editorial changes fail differently: a
wrongly reordered clip, a boundary moved to the wrong topic, or a set pruned for the wrong
diversity produces something that plays fine and is subtly worse, and nobody can point at
the frame where it went wrong.

So the design is built around three constraints:

1. **Everything defaults off** (R7.1) and nothing is enabled before it can be judged (R7.2,
   R7.3). Two different instruments are needed: R3/R4/R5 change *which clips and where they
   start* and are judged by the labelled selection benchmark from `clip-quality-uplift` Group A;
   R1 changes *how a clip opens* and is judged by preference trial from
   `render-quality-measurement` (`M12`).
2. **No new cutting mechanism.** Assembly is expressed as Keep_Intervals and rendered through
   the path `filler.py` already owns. A second way to cut video would be the worst outcome of
   this spec.
3. **No model checkpoint.** Eight backlog items are already blocked on weights CI cannot have,
   `requirements-ml.txt` has never been built into an image, and `permissibility_mode`
   deliberately forces `local_only`. Semantic work that silently needs a download would not run
   for the people this project is for.

### Placement

```
select_moments
  ├── candidates
  ├── discourse.standalone_completeness  →  dangling_opener  (EXISTS, only scored)
  │      └── antecedent_extension()                            (R5)  NEW — acts on it
  ├── cohesion_score() → topic boundaries                      (R3)  NEW
  ├── snap_to_sentences        (exists, stays above topic)
  ├── deduplicate              (exists, lexical, unchanged)
  ├── diversity_selection()                                    (R4)  NEW — in addition
  ├── scene_detect.snap_start/_end   (exists, after topic)
  └── trim_edge_silence              (exists, last)
per clip
  └── plan_keep_intervals ← assembly ∪ filler ∪ cut list ∪ interior silence   (R1, R2)
        └── ONE apply_keep_intervals → ONE re-encode
```

---

## Group A — Cold-open assembly

### R1: why this is closer than it looks

`worker/effects/filler.py` already does the hard part. `plan_keep_intervals` builds a
non-contiguous keep list, `apply_keep_intervals` renders it as `trim`/`atrim` + `concat` in
**one** re-encode, `_seam_fades` puts a few-ms `afade` at each interior seam — deliberately not
`acrossfade`, because a crossfade would shift the timeline the rebased words depend on — and
`rebase_words` maps captions onto the result.

A cold open is that machinery with one difference: **the keep list is not monotonic.** Filler
removal produces intervals in increasing source order. An assembly produces `[hook_range,
body_range]` where `hook_range.start > body_range.start`.

That single difference is where every hazard in this group lives.

### R2.6: the non-monotonic rebasing problem

`rebase_words(words, keeps)` maps original times onto the tightened timeline. Its existing
implementation can reasonably assume increasing keeps, because that is all filler removal ever
produces. Under an assembly the mapping is no longer monotonic, and a rebasing routine that
assumes it will place the hook's captions at the *body's* timeline positions.

The failure is nasty precisely because it is plausible: captions still appear, still look like
captions, and are attached to the wrong words. R9.2 therefore requires a test with a
**non-monotonic** assembly specifically, and R2.6 names all three consumers — words, emoji
placements, and speaker turns — because one rebased consumer does not imply three.

R2.7's clause about not reordering audio and video independently sounds obvious and is worth
stating: `trim` and `atrim` are separate filters given separate arguments, and a copy-paste
error that reorders one and not the other produces a clip whose audio and video are each
internally coherent and mutually wrong. R2.8's sync verification is the check for it, using
`render-quality-measurement`'s Sync_Offset instrument.

R2.9's refusal follows the established `transcript_trim_refused:*` pattern: if the assembly
cannot be expressed as a single keep list, decline and record it rather than deliver something
partial.

### R1.5–R1.8: the editorial guards

- **R1.5** — no cold open where the strongest line is already at the front. Otherwise the
  feature reorders a clip that was already correctly ordered, producing a duplicate for nothing.
- **R1.6** — never lift a Dangling_Opener as a cold open. A hook that opens on *"and that's why
  he quit"* is worse than no hook, and `discourse.py` can already tell us. This is the same
  detection R5 repairs with, used here as a filter.
- **R1.7 / R1.8** — the duplication question, which is genuinely a matter of taste. Leaving the
  line in the body means the viewer hears it twice, which is a recognised and often effective
  short-form device; removing it means the body loses its best line. Configuration decides, and
  R1.8 bounds the repeat interval so the two occurrences are not adjacent enough to sound like a
  stutter.
- **R1.2** — the cold open comes from *within the candidate's own range*, never from elsewhere
  in the source. Pulling from elsewhere is a much larger product change: it breaks the invariant
  that a clip corresponds to a contiguous region of the source, which the transcript editor
  (`U4`), the regenerate endpoint (`U7`), and the selection benchmark all lean on.
- **R1.13** — one cold open per clip. The out-of-scope note explains why: full multi-segment
  reordering fails as incoherence, not awkwardness.

---

## Group B — Semantic boundaries and diversity

### R6: offline first, and honest about it

Both R3 and R4 want sentence embeddings. This project cannot depend on them.

The offline computation is lexical: a TextTiling-style cohesion measure for R3 (compare the
vocabulary of adjacent windows; cohesion dips where the subject changes) and TF-IDF cosine for
R4. Neither needs a checkpoint, both are deterministic (R6.7), and both are genuinely weaker
than embeddings at exactly the thing R4 is for — paraphrase.

That is an honest trade and R6.6 requires it to be named honestly: the offline path must not be
called a semantic model. The precedent is `music_degraded:synthesised` and the refusal to ship
a hiss labelled `whoosh` — a proxy is fine, a proxy labelled as the real thing is the defect.

R6.2/R6.3 allow an embedding backend as an enhancement with a marked fallback, R6.8 resolves it
through the existing capability probe, and **R6.5 honours `permissibility_mode`**, which
already forces `local_only` and clears `music`. A semantic feature that quietly made a network
call under that mode would break a promise the product makes.

### R3: topic boundaries, and the priority order

There are now four boundary mechanisms and they must not fight:

| Priority | Mechanism | Status |
| --- | --- | --- |
| 1 | Sentence alignment (`snap_to_sentences`) | exists, stays on top |
| 2 | Topic boundary | **new (R3.2)** |
| 3 | Scene cut (`snap_start`, `snap_end`) | exists |
| 4 | Edge silence (`trim_edge_silence`) | exists, last |

**R3.4 keeps sentence alignment above topic alignment.** Beginning or ending mid-sentence to
reach a topic boundary trades a subtle problem for an obvious one — the transcript is the
content, and a truncated sentence is audible where a slightly-off topic edge is not.

R3.6 places topic preference before scene snapping, preserving the existing stage order rather
than inserting a new one. R3.3 and R3.5 reuse the *existing* shift limit and minimum-duration
guard rather than introducing parallel ones.

**R3.9 gates the default on the Boundary_Metric, not the Primary_Metric.** Topic boundaries
cannot change *which* moments are found, only where they start and stop. F1@0.5 will barely
move; `mean_best_iou` is the metric that can see it. Gating on the wrong one produces a null
result and an incorrect conclusion — the same trap `clip-quality-uplift` R11.8 flags for end
snapping.

### R4: diversity, layered on rather than replacing

`candidate_ranking.deduplicate` is good and stays exactly as it is (R4.3). Its two tests are
well reasoned: `overlap_fraction` uses the shorter candidate as denominator so containment is
caught rather than IoU, and `text_similarity` is asymmetric on purpose — returning 0.0 below
`MIN_TEXT_TOKENS=6` — because "a false positive deletes a wanted moment." Diversity_Selection
is **additional**, not a replacement.

The mechanism is maximal-marginal-relevance: select greedily on `score − λ · max_similarity_to_already_selected`.
Three requirements make it safe:

- **R4.5/R4.6** — the neutral weight reproduces the v0.11.0 set *exactly*. Same discipline as
  `clip-quality-uplift`'s `selection_opinion_weight`: land it inert, turn it on by measurement.
  And as there, the neutral case must be an explicit branch rather than arithmetic that happens
  to cancel.
- **R4.7** — after scoring and deduplication, before the count cap. Dedup removes genuine
  near-duplicates; diversity then shapes what fills the remaining slots. Reversing them would
  let the cap discard candidates diversity would have wanted.
- **R4.8** — **never deliver fewer clips than requested while a candidate remains.** This is the
  requirement that prevents the obvious bad outcome: a user asks for ten clips, the source is
  genuinely about one thing, and an aggressive diversity term returns four. Diversity reorders
  and reprioritises; it does not refuse to fill the order.
- **R4.11** — below the minimum token threshold, treat candidates as *maximally dissimilar*,
  not as duplicates. Short text carries no reliable similarity signal, and defaulting to
  "similar" would silently drop short clips as a class.

---

## Group C — Antecedent repair

### R5: acting on something already detected

`worker/discourse.py` is better than expected here. `_DANGLING_OPENERS` covers pronoun and
demonstrative openers "with no antecedent inside the clip," `standalone_completeness` returns
`dangling_opener`, `to_dict` surfaces it as `standalone_dangling_opener`, and `prompt_note`
already warns the LLM. The gap is purely that nothing *acts* — it feeds `standalone_score` and
the clip ships weak.

R5.7 requires reusing that detection rather than writing a second definition. R5.1 acts on it
by extending the start to a sentence boundary (R5.2), bounded (R5.3), and within the length
preset's maximum (R5.4).

**R5.5/R5.6 — re-evaluate, and revert if unresolved.** This is the requirement that keeps the
feature honest. Extending by one sentence often does not help: the antecedent may be three
sentences back, or in another speaker's turn, or never stated. Extending anyway makes the clip
longer and no more coherent, which is a straight loss. So: extend, re-run the same detector,
and if the opener is still dangling, put it back.

**R5.13 is a cross-feature interaction worth naming.** `hook_score.speech_promptness` zeroes
the hook when speech does not begin within `SPEECH_DEADLINE_S = 1.0` — the in-source comment is
explicit that "a clip that starts on dead air has no hook." Extending the start backwards can
easily land in a pause before the previous sentence, which would resolve the dangling opener
and *destroy the hook score*. Two features each doing their job and producing a worse clip. So
the extension must not introduce opening silence.

R5.9 avoids the other interaction: an extension that creates an overlap with a higher-scored
candidate would just get the extended candidate removed by dedup, turning a repair into a
deletion.

---

## Testing strategy

| Area | File | Nature |
| --- | --- | --- |
| Single re-encode | `tests/test_assembly.py` | Assembly ∪ filler ∪ cut list ∪ interior silence → exactly **one** `apply_keep_intervals` call. |
| Non-monotonic rebasing | `tests/test_assembly.py` | An assembly where the second Segment's source times **precede** the first's. Assert words, emoji, **and** speaker turns each land correctly — one test per consumer, because one rebased consumer does not imply three. |
| Seam treatment | `tests/test_assembly.py` | `afade` at the Cold_Open/Body boundary; **not** `acrossfade`, which would shift the timeline. |
| Assembled sync | `tests/test_sync.py` | Measure Sync_Offset on a rendered assembled clip. Catches audio and video being reordered independently. |
| Assembly guards | `tests/test_assembly.py` | No cold open when the best line is already first; never lift a Dangling_Opener; repeat-interval bound honoured; length-preset minimum respected; at most one cold open. |
| Refusal | `tests/test_assembly.py` | An unrenderable assembly is refused and recorded, not partially delivered. |
| Cohesion determinism | `tests/test_cohesion.py` | **Property**: identical input → identical output, across runs. |
| Boundary priority | `tests/test_topic_boundaries.py` | Sentence alignment beats topic alignment; existing shift limit and minimum-duration guard never violated; stage order preserved. |
| Diversity neutrality | `tests/test_diversity.py` | **Property**: the neutral weight reproduces the score-ordered set exactly — an explicit branch, not float luck. |
| Diversity fill | `tests/test_diversity.py` | **Property**: never returns fewer clips than requested while candidates remain. |
| Diversity layering | `tests/test_diversity.py` | Existing containment and lexical dedup behaviour byte-identical; diversity applied after dedup, before the cap. |
| Short-text handling | `tests/test_diversity.py` | Candidates below `MIN_TEXT_TOKENS` treated as maximally dissimilar, not as duplicates. |
| Extension revert | `tests/test_antecedent.py` | An extension that does not resolve the opener is reverted; the existing detector is reused, not duplicated. |
| Hook interaction | `tests/test_antecedent.py` | An extension never introduces opening silence — construct a case where the previous sentence is preceded by a pause. |
| Offline default | `tests/test_semantic_offline.py` | No network and no checkpoint by default; `permissibility_mode` forces offline; a configured-but-absent backend falls back with a marker. |

Property tests use `hypothesis` at `max_examples=100`, one property per test, tagged
`# Feature: clip-editorial-structure, Property N: <text>`.

**Baseline:** `pytest` → **2030 passed, 0 skipped, 0 warnings**; `npm run test:run` → **141
passed**.

### What the suite cannot tell us

Whether any of this is a better *edit*. The tests can prove an assembly renders in one pass
with correct captions and sync; they cannot prove that opening on the hook is better than
opening at the beginning. They can prove diversity selection is well-behaved; they cannot prove
a diverse set of clips outperforms a focused one for a given creator.

Two instruments cover part of it — the labelled benchmark for R3/R4/R5, preference trials for
R1 — and both have real limits. The benchmark is one labeller's taste on 15 sources, which
`clip-quality-uplift` already flags as its central risk. Preference trials at realistic *n*
cannot distinguish a real preference from noise.

Hence R9.13, and hence every default in this spec starting off.

---

## Consequences

**What this enables:** a clip can open on its strongest line; boundaries can respect what a
clip is about rather than only where the speaker paused; a delivered set can cover different
points; a clip that opens on a dangling reference can be repaired instead of merely marked
down.

**What it does not fix:** none of this improves *which moments* are found — that is
`clip-quality-uplift`'s blended ranking and the missing signals. Editorial structure applied to
a mediocre moment produces a well-edited mediocre clip.

**The likeliest way this goes wrong:** non-monotonic rebasing. Every other failure here is
detectable by watching the clip; that one produces captions attached to the wrong words in a
way that looks entirely normal frame by frame, and it will be attributed to the ASR before
anyone suspects the assembly. R2.6 and its dedicated non-monotonic test exist for it, and the
mutation specification should attack it first.

**The second-likeliest:** diversity tuned on a 15-source benchmark to prefer variety, applied
to a focused single-topic source, quietly returning worse clips because the diversity term
outweighed a genuinely better second clip on the same theme. R4.8 bounds the damage; the
benchmark's composition is the real defence, which is another reason R7.2 refuses to enable
this before that benchmark exists.
