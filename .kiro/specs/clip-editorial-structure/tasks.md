# Implementation Plan — Clip Editorial Structure

Incremental, test-first coding steps. Execute **one task at a time**, in order.

**This is the highest-risk spec of the four.** The others improve measurement, signal, and
presentation — where a bad change is visible immediately. Editorial changes fail differently: a
wrongly reordered clip, a boundary moved to the wrong topic, or a set pruned for the wrong
diversity produces something that plays fine and is subtly worse, and nobody can point at the
frame where it went wrong.

So: **every feature lands off** (R7.1), and two different instruments decide the defaults.
Tasks 3, 5, and 6 need `clip-quality-uplift` Group A's labelled selection benchmark — they
change which clips exist and where they start, which is exactly what `mean_best_iou` measures,
and `eval/labels/` currently holds one `.gitkeep`. Task 2 needs
`render-quality-measurement`'s preference harness instead, because "does the clip open
stronger" is a taste question.

Ordering note: **task 1 builds the offline semantic primitives first** because tasks 3 and 5
both consume them, and because getting the no-checkpoint constraint right early prevents a
later retrofit.

Tasks marked `*` are optional test sub-tasks. Property tests use `hypothesis` with
`@settings(max_examples=100)`, one property per test, tagged
`# Feature: clip-editorial-structure, Property N: <text>`.

**Before starting, record the baseline:** `pytest` → **2030 passed, 0 skipped, 0 warnings**;
`cd frontend && npm run test:run` → **141 passed**.

## Tasks

- [ ] 1. Offline semantic primitives (S22/S23 groundwork)
  - [ ] 1.1 Add lexical cohesion and lexical similarity, no checkpoint, no network
    - A TextTiling-style cohesion measure (compare adjacent windows' vocabulary; cohesion dips
      where the subject changes) and TF-IDF cosine for candidate similarity.
    - **No model checkpoint.** Eight backlog items are already blocked on weights CI cannot have,
      `requirements-ml.txt` has never been built into an image, and `permissibility_mode`
      deliberately forces `local_only`. A semantic feature that silently needs a download would
      not run for the people this project is built for.
    - Deterministic across runs and platforms.
    - _Requirements: 6.1, 6.7_

  - [ ] 1.2 Name it honestly
    - The offline path is **weaker than embeddings at exactly the thing task 6 wants** —
      paraphrase. Lexical cosine is blind to "same point, different words," which is how a
      talkative speaker's transcript looks.
    - Do not name any symbol so it implies a learned semantic model. The precedent is
      `music_degraded:synthesised` and the refusal to ship a hiss labelled `whoosh`: a proxy is
      fine, a proxy labelled as the real thing is the defect.
    - _Requirements: 6.6_

  - [ ] 1.3 Add the optional embedding backend as an enhancement, with a marked fallback
    - Resolve availability through the existing capability probe. Configured-but-absent falls back
      to offline and records the substitution naming the missing capability. **Never download or
      call out unless explicitly configured.**
    - **Honour `permissibility_mode`** (R6.5): it already forces `local_only` and clears `music`.
      A semantic feature quietly making a network call under that mode would break a promise the
      product makes.
    - _Requirements: 6.2, 6.3, 6.4, 6.5, 6.8_

  - [ ] 1.4* Test: determinism and offline default → `tests/test_semantic_offline.py`
    - **Property 1** — identical input produces identical cohesion and similarity, across runs.
    - Assert no network and no checkpoint by default; `permissibility_mode` forces offline; a
      configured-but-absent backend falls back with a marker.
    - _Requirements: 6.1, 6.3, 6.5, 6.7, 9.8_ · _Properties: P1_

- [ ] 2. Cold-open assembly (S21)
  - [ ] 2.1 Express an Assembly as Keep_Intervals through the existing path
    - `filler.py` already does the hard part: `plan_keep_intervals` builds a non-contiguous keep
      list, `apply_keep_intervals` renders it as `trim`/`atrim` + `concat` in **one** re-encode,
      `_seam_fades` applies a few-ms `afade` at each seam — deliberately not `acrossfade`, which
      would shift the timeline the rebased words depend on.
    - **Add no cutting mechanism and no encode pass.** A second way to cut video would be the
      worst outcome of this spec.
    - _Requirements: 1.1, 2.1, 2.2, 2.4_

  - [ ] 2.2 Resolve assembly into the one shared keep list
    - Together with filler removal, the `U4` transcript cut list, and
      `clip-quality-uplift`'s interior-silence removal — one keep list, one re-encode.
    - _Requirements: 2.3_

  - [ ] 2.3 Handle **non-monotonic** rebasing
    - **This is the single highest-risk item in all four specs.** Filler removal only ever
      produces keeps in increasing source order, so `rebase_words` can reasonably assume
      monotonicity. An assembly produces `[hook_range, body_range]` where
      `hook_range.start > body_range.start`.
    - A rebasing routine that assumes monotonic keeps will place the hook's captions at the
      *body's* timeline positions. The failure is nasty because it is plausible: captions still
      appear, still look like captions, and are attached to the wrong words — and it will be
      blamed on the ASR before anyone suspects the assembly.
    - Handle words, emoji placements, **and** speaker turns.
    - _Requirements: 2.5, 2.6_

  - [ ] 2.4 Never reorder audio and video independently
    - `trim` and `atrim` are separate filters given separate arguments. A copy-paste error that
      reorders one and not the other produces a clip whose audio and video are each internally
      coherent and mutually wrong.
    - _Requirements: 2.7_

  - [ ] 2.5 Apply the editorial guards
    - Cold open drawn **from within the candidate's own range** (R1.2) — pulling from elsewhere
      breaks the invariant that a clip is a contiguous source region, which `U4`, `U7`, and the
      selection benchmark all lean on. Sentence-aligned, duration-bounded, at most one per clip.
    - No cold open where the strongest line is already first (R1.5) — otherwise it reorders an
      already-correct clip and produces a duplicate for nothing.
    - **Never lift a Dangling_Opener as a cold open** (R1.6): a hook opening on *"and that's why
      he quit"* is worse than no hook, and `discourse.py` can already tell us. Same detection task
      7 repairs with, used here as a filter.
    - Respect the length preset's minimum.
    - _Requirements: 1.2, 1.3, 1.4, 1.5, 1.6, 1.9, 1.13_

  - [ ] 2.6 Make the duplication behaviour configurable and bounded
    - Genuinely a matter of taste: leaving the line in the Body means hearing it twice, a
      recognised short-form device; removing it means the Body loses its best line. Configuration
      decides. Bound the repeat interval so the two occurrences do not sound like a stutter.
    - _Requirements: 1.7, 1.8_

  - [ ] 2.7 Refuse rather than partially assemble
    - Follow the established `transcript_trim_refused:*` pattern: decline, record, carry on.
    - _Requirements: 2.9_

  - [ ] 2.8 Default off; record the assembly and its source range
    - _Requirements: 1.10, 1.12, 7.1_

  - [ ] 2.9* Test: one pass, and non-monotonic correspondence → `tests/test_assembly.py`
    - Assert assembly ∪ filler ∪ cut list ∪ interior silence resolve to exactly **one**
      `apply_keep_intervals` call.
    - Construct an assembly where the second Segment's source times **precede** the first's, and
      assert words, emoji, **and** speaker turns each land correctly — one test per consumer,
      because one rebased consumer does not imply three. A monotonic fixture proves nothing here.
    - _Requirements: 9.1, 9.2, 2.6_

  - [ ] 2.10* Test: seams, guards, refusal → `tests/test_assembly.py`
    - `afade` at the Cold_Open/Body boundary and **not** `acrossfade`; no cold open when the best
      line is already first; Dangling_Opener never lifted; repeat interval honoured; length
      minimum respected; at most one cold open; unrenderable assembly refused and recorded.
    - _Requirements: 1.5, 1.6, 1.8, 1.9, 1.13, 2.4, 2.9_

  - [ ] 2.11* Test: sync on an assembled clip → `tests/test_sync.py`
    - Measure Sync_Offset on the **rendered** assembled clip, using
      `render-quality-measurement`'s instrument. This is what catches audio and video being
      reordered independently.
    - Establish the expected offset independently of the assembly code — from the fixture's
      construction, not from the keep list the implementation produced.
    - _Requirements: 2.8, 9.3, 9.9_

  - [ ] 2.12 Decide the default by preference trial
    - **Depends on `render-quality-measurement` task 6.** "Does the clip open stronger" is a
      taste question, not a benchmark question. Blind, order-randomised, single-dimension,
      declines recorded, no significance claimed. Attach rendered output either way.
    - _Requirements: 1.11, 7.3, 7.4, 9.13_

- [ ] 3. Topic-shift boundaries (S22)
  **Depends on `clip-quality-uplift` Group A (labelled benchmark).**
  - [ ] 3.1 Derive Topic_Boundaries from the cohesion measure
    - _Requirements: 3.1_

  - [ ] 3.2 Prefer topic-aligned boundaries, **below** sentence alignment
    - Priority order, and it must not be rearranged: sentence alignment → topic boundary → scene
      cut → edge silence. **Sentence alignment stays on top** (R3.4): beginning or ending
      mid-sentence to reach a topic boundary trades a subtle problem for an obvious one, because
      the transcript is the content and a truncated sentence is audible where a slightly-off topic
      edge is not.
    - _Requirements: 3.2, 3.4_

  - [ ] 3.3 Reuse the existing guards; preserve stage order
    - The **existing** configurable maximum shift and minimum-duration guard, not parallel ones.
      Apply before scene-cut snapping and edge-silence trimming.
    - _Requirements: 3.3, 3.5, 3.6_

  - [ ] 3.4 Attach the cohesion score at each boundary to `features`; record moves
    - _Requirements: 3.7, 3.10_

  - [ ] 3.5* Test: priority and guards → `tests/test_topic_boundaries.py`
    - Sentence alignment beats topic alignment; shift limit and minimum-duration guard never
      violated; stage order preserved.
    - _Requirements: 9.7_

  - [ ] 3.6 Gate the default on the **Boundary_Metric**
    - Not the Primary_Metric. Topic boundaries cannot change *which* moments are found, only
      where they start and stop — F1@0.5 will barely move and `mean_best_iou` is the metric that
      can see it. Gating on the wrong one yields a null result and an incorrect conclusion, the
      same trap `clip-quality-uplift` R11.8 flags for end snapping.
    - _Requirements: 3.8, 3.9, 7.2, 7.4_

- [ ] 4. Semantic diversity (S23)
  **Depends on `clip-quality-uplift` Group A.**
  - [ ] 4.1 Add Diversity_Selection as maximal marginal relevance
    - Greedy on `score − λ · max_similarity_to_already_selected`.
    - _Requirements: 4.1, 4.2, 4.4_

  - [ ] 4.2 Leave the existing deduplication exactly as it is
    - `candidate_ranking.deduplicate` is well reasoned and stays: `overlap_fraction` uses the
      shorter candidate as denominator so containment is caught rather than IoU, and
      `text_similarity` is asymmetric on purpose — returning 0.0 below `MIN_TEXT_TOKENS=6` —
      because "a false positive deletes a wanted moment." Diversity is **additional**, not a
      replacement.
    - _Requirements: 4.3_

  - [ ] 4.3 Make the neutral weight an explicit branch
    - The neutral value must reproduce the v0.11.0 set **exactly**. Branch on it; do not rely on
      arithmetic that happens to cancel. Same discipline as `clip-quality-uplift`'s
      `selection_opinion_weight`: land inert, turn on by measurement.
    - _Requirements: 4.5, 4.6, 7.1_

  - [ ] 4.4 Apply after dedup, before the count cap
    - Dedup removes genuine near-duplicates; diversity then shapes what fills the remaining
      slots. Reversing them would let the cap discard candidates diversity would have wanted.
    - _Requirements: 4.7_

  - [ ] 4.5 Never under-deliver
    - **The requirement that prevents the obvious bad outcome**: a user asks for ten clips, the
      source is genuinely about one thing, and an aggressive diversity term returns four.
      Diversity reorders and reprioritises; it does not refuse to fill the order.
    - _Requirements: 4.8_

  - [ ] 4.6 Treat short text as maximally dissimilar, not as duplicate
    - Below `MIN_TEXT_TOKENS` there is no reliable similarity signal, and defaulting to "similar"
      would silently drop short clips as a class.
    - _Requirements: 4.11_

  - [ ] 4.7* Property tests: neutrality and fill → `tests/test_diversity.py`
    - **Property 2** — the neutral weight reproduces the score-ordered set exactly, for arbitrary
      candidate sets.
    - **Property 3** — never returns fewer clips than requested while candidates remain.
    - _Requirements: 9.4, 9.5_ · _Properties: P2, P3_

  - [ ] 4.8* Test: layering and short text → `tests/test_diversity.py`
    - Existing containment and lexical dedup behaviour byte-identical; diversity applied after
      dedup and before the cap; sub-threshold candidates treated as maximally dissimilar.
    - _Requirements: 4.3, 4.7, 4.11_

  - [ ] 4.9 Choose the weight by measurement, and record the risk
    - Sweep against the benchmark on the Primary_Metric. Record in the finding the risk the
      design names: diversity tuned on 15 sources to prefer variety, applied to a focused
      single-topic source, can quietly return worse clips because the diversity term outweighed a
      genuinely better second clip on the same theme.
    - _Requirements: 4.9, 4.10, 7.2, 7.4_

- [ ] 5. Antecedent repair (S24)
  **Depends on `clip-quality-uplift` Group A.**
  - [ ] 5.1 Act on the detection that already exists
    - `discourse.py` already does the detection: `_DANGLING_OPENERS` covers pronoun and
      demonstrative openers "with no antecedent inside the clip," `standalone_completeness`
      returns `dangling_opener`, `to_dict` surfaces `standalone_dangling_opener`, and
      `prompt_note` already warns the LLM. The gap is purely that **nothing acts** — it feeds
      `standalone_score` and the clip ships weak.
    - **Reuse that detector; do not write a second definition.**
    - _Requirements: 5.1, 5.7_

  - [ ] 5.2 Extend to a sentence boundary, bounded
    - Within the length preset's maximum.
    - _Requirements: 5.2, 5.3, 5.4_

  - [ ] 5.3 Re-evaluate and **revert if unresolved**
    - The requirement that keeps this honest. Extending by one sentence often does not help: the
      antecedent may be three sentences back, in another speaker's turn, or never stated.
      Extending anyway makes the clip longer and no more coherent — a straight loss. Extend,
      re-run the same detector, put it back if still dangling.
    - _Requirements: 5.5, 5.6_

  - [ ] 5.4 Do not introduce opening silence
    - **A cross-feature interaction worth being careful about.**
      `hook_score.speech_promptness` zeroes the hook when speech does not begin within
      `SPEECH_DEADLINE_S = 1.0` — the in-source comment is explicit that "a clip that starts on
      dead air has no hook." Extending backwards can easily land in a pause before the previous
      sentence, which would resolve the dangling opener and **destroy the hook score**: two
      features each doing their job and producing a worse clip.
    - _Requirements: 5.13_

  - [ ] 5.5 Do not create an overlap dedup would then punish
    - An extension that overlaps a higher-scored candidate just gets the extended candidate
      removed, turning a repair into a deletion.
    - _Requirements: 5.9_

  - [ ] 5.6 Apply before scene snapping and edge trimming; record the extension
    - _Requirements: 5.8, 5.12_

  - [ ] 5.7* Test: revert and the hook interaction → `tests/test_antecedent.py`
    - An extension that does not resolve the opener is reverted; the existing detector is reused
      rather than duplicated. Separately: construct a case where the previous sentence is
      **preceded by a pause**, and assert the extension does not introduce opening silence.
    - _Requirements: 9.6, 5.6, 5.13_

  - [ ] 5.8 Gate the default on the Boundary_Metric
    - _Requirements: 5.10, 5.11, 7.2, 7.4_

- [ ] 6. Configuration, defaults, and close-out
  - [ ] 6.1 Document every new setting in `.env.example`
    - `tests/test_config_documentation.py` fails on an undocumented field or a documented
      non-setting. State for each default whether it is **measured or provisional**.
    - _Requirements: 8.1, 8.2, 8.3_

  - [ ] 6.2 Surface new options through API, form, and UI
    - `OptionsModel`, `/api/upload` form fields, `/api/info` domains, `App.jsx`
      `DEFAULT_SETTINGS` **and** `toOptions()`, `SettingsPanel.jsx`. Unrecognised values apply the
      documented default without raising.
    - _Requirements: 8.4, 8.5, 8.6_

  - [ ] 6.3* Property test: new option fields round-trip → `tests/test_options_roundtrip.py`
    - **Property 4** — every new field survives `from_dict(asdict(...))`; unrecognised values
      resolve to the documented default without raising.
    - _Requirements: 8.5, 8.6_ · _Properties: P4_

  - [ ] 6.4 Verify marker discipline
    - Every pre-existing marker keeps its exact spelling; every new marker names the **resolved**
      value, never the requested one.
    - _Requirements: 8.7, 8.8_

  - [ ] 6.5 Flip only what measurement supports, each in its own commit
    - One commit per default, fixtures re-frozen there, message naming what moved. Where a
      measurement does not support a change, **leave it off and record the finding** — a null
      result stops the next person re-running the experiment.
    - _Requirements: 7.5, 7.6_

  - [ ] 6.6 Full gate run
    - `ruff check .` clean · `pytest` at **2030 + new, 0 skipped, 0 warnings** ·
      `cd frontend && npm run lint && npm run test:run && npm run build` ·
      `scripts/docker_smoke.sh`.
    - Triage any new warning at source with a **targeted** `filterwarnings` ignore and a comment
      saying why it cannot be fixed. Never broaden the existing ignores; never relax
      `filterwarnings = error`.
    - _Requirements: 9.10, 9.11_

  - [ ] 6.7 Add the mutation specification
    - `tests/mutations/clip-editorial-structure.json`. Attack the highest-risk item first, per
      the design: **assume monotonic keeps in assembly rebasing**. Then: reorder video without
      audio; drop the Cold_Open seam fade; lift a Dangling_Opener as a cold open; put topic
      alignment above sentence alignment; invert the diversity neutral branch; let diversity
      under-deliver; treat short text as duplicate; skip the antecedent revert; allow an extension
      to introduce opening silence. Each **CAUGHT**; an ESCAPE is a real test gap.
    - _Requirements: 9.12_

  - [ ] 6.8 Write the close-out
    - Follow `.kiro/specs/face-detection-upgrade/CLOSE_OUT.md`. Record every measurement
      including the null ones.
    - State plainly what this does **not** fix: none of it improves *which moments* are found —
      that is `clip-quality-uplift`'s blended ranking and the missing signals. **Editorial
      structure applied to a mediocre moment produces a well-edited mediocre clip.**
    - Record both named risks: non-monotonic rebasing attaching captions to the wrong words in a
      way that looks entirely normal frame by frame, and diversity tuned for variety on a
      15-source benchmark degrading focused single-topic sources.
    - _Requirements: 7.4, 7.5, 9.13_
