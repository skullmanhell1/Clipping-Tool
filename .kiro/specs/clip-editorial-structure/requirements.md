# Requirements Document

## Introduction

**Clip Editorial Structure** addresses what the Clipper does *not* do to the material it
selects. Every other spec improves how a clip is measured, framed, or rendered. This one is
about the edit itself.

### The findings

**Every clip is one contiguous range.** Searching `worker/` for `multi_segment`, `rearrange`,
`cold_open`, `stitch`, and `non_contiguous` returns **nothing**. `ClipCandidate` is a
`(start, end)` pair and the pipeline cuts exactly that. Commercial tools do not stop there:
Opus Clip's own marketing describes picking highlight moments, rearranging them, and finishing
with a call to action. The most valuable form of this is the **cold open** — take the single
strongest line from the middle of a clip and put it at the front, so the hook is the first
thing a viewer hears rather than the thing they would have reached at 0:18.

The infrastructure is closer than it looks. `worker/effects/filler.py` already renders a
**non-contiguous keep list in one re-encode**: `plan_keep_intervals` builds it,
`apply_keep_intervals` emits `trim`/`atrim` + `concat`, `_seam_fades` puts a few-ms `afade` at
each interior seam, and `rebase_words` maps captions onto the resulting timeline. Assembling a
clip out of two ranges is the same operation with a different input.

**Boundaries only ever use surface signals.** `snap_to_sentences` uses transcript segment
edges, `scene_detect.snap_start` uses luma cuts, `segmentation.trim_edge_silence` uses
silence. All three are correct and none of them knows what the clip is *about*, so a clip can
span two unrelated topics and still snap cleanly at both ends. Nothing detects a topic shift.

**Deduplication is lexical only.** `candidate_ranking.deduplicate` runs two tests:
`overlap_fraction` (containment, using the shorter candidate as denominator) and
`text_similarity` (weighted Jaccard over content words, returning 0.0 below
`MIN_TEXT_TOKENS=6`). Both are good at what they do. Neither notices that ten clips make the
same point in different words — Jaccard over content words is blind to paraphrase, which is
exactly how a talkative speaker's transcript looks.

**The dangling-opener problem is detected but never repaired.** Credit where it is due:
`worker/discourse.py` already does more than expected. `_DANGLING_OPENERS` and
`standalone_completeness` detect a clip opening on a pronoun or demonstrative with no
antecedent, `dangling_opener` is surfaced in `to_dict`, and `prompt_note` warns the model
about it. But it is only ever **scored** — it feeds `standalone_score` and nothing acts on it.
A clip that opens *"and that's exactly why he quit"* is identified as weak and then shipped
weak, when the fix is usually to extend the start by one sentence to include the question.

### Dependencies, stated plainly

**This spec cannot be evaluated without `clip-quality-uplift` Group A.** Topic boundaries
(R3) and semantic diversity (R4) change *which clips are produced and where they start and
stop* — precisely what the labelled selection benchmark and its `mean_best_iou` Boundary_Metric
measure. `.kiro/steering/working-agreement.md` already forbids clip-selection quality work
before the harness can judge it, and `eval/labels/` currently holds one `.gitkeep`. R3 and R4
must not have their defaults flipped before that benchmark exists.

Cold-open assembly (R1) is different: its value is not "did we find a better moment" but "does
the clip open stronger," which is a preference judgement needing
`render-quality-measurement`'s `M12` harness instead.

### A constraint that shapes the semantic work

Topic detection and paraphrase-aware diversity both want sentence embeddings, and this project
**cannot depend on a model checkpoint**: eight backlog items are already blocked on weights CI
cannot have, `requirements-ml.txt` exists but that image has never been built, and
`permissibility_mode` deliberately forces `local_only`. So R6 requires a lexical, offline
default — TextTiling-style cohesion and TF-IDF cosine both work with no checkpoint — with
embeddings available only as an optional enhancement that degrades with a marker. A feature
that silently needs a download would not run for the people this project is built for.

### Out of scope

- **Rearranging a clip into more than a cold open plus body.** Full multi-segment editorial
  reordering is a much larger product question, and the failure mode of a wrongly reordered
  clip is incoherence rather than mild awkwardness. R1 is deliberately limited to one prepended
  segment.
- **Call-to-action generation.** End cards already exist (`V14`, `end_card_dialogue`).
- **B-roll placement decisions.** Owned by `A19`.
- **Metadata, titles, and hashtags.** `worker/metadata.py` already generates these via LLM with
  a deterministic fallback.
- **Cross-clip narrative sequencing** — ordering the delivered set as a series.
- **Any change to the LLM selection prompt's transcript formatting.** `S10`'s delivery
  annotations are established; R5 may add a repair, not a reformat.

## Glossary

- **Clipper**: The overall AI Video Clipper application.
- **Clip_Candidate**: The `ClipCandidate` record — `start`, `end`, `score`, `reason`, `title`, `features`.
- **Segment**: One contiguous time range contributing to a delivered clip.
- **Cold_Open**: A Segment lifted from later in a clip and placed at its front.
- **Body**: The remainder of a clip after a Cold_Open has been lifted from it.
- **Assembly**: The ordered list of Segments composing one delivered clip.
- **Keep_Interval**: One `Interval` in the keep list `worker/effects/filler.plan_keep_intervals` produces and `apply_keep_intervals` renders in a single re-encode.
- **Topic_Boundary**: A time at which the subject matter of the transcript changes.
- **Cohesion_Score**: A measure of lexical or semantic similarity between two adjacent transcript windows, low at a Topic_Boundary.
- **Semantic_Similarity**: A paraphrase-aware similarity between two candidates' transcript text.
- **Diversity_Selection**: Choosing a final clip set to maximise combined score and mutual dissimilarity, rather than score alone.
- **Dangling_Opener**: A clip opening whose first sentence refers to something outside the clip, as `worker/discourse.py` already detects.
- **Antecedent_Extension**: Extending a Clip_Candidate's start to include the sentence a Dangling_Opener refers back to.
- **Primary_Metric**: F1 at IoU 0.5, pooled, as `evaluation/metrics.py` computes it.
- **Boundary_Metric**: `mean_best_iou`, distinguishing correct moments with poor boundaries from incorrect moments.
- **Effects_Applied**: The `ClipResult.effects_applied` string markers.
- **Processing_Options**: The user options record and its API/form/UI mirrors.

## Requirements

---

## Group A — Cold-open assembly (S21)

### Requirement 1: A clip may open on its strongest line

**User Story:** As a creator, I want the best line in my clip to be the first thing a viewer hears, so that the hook is not buried eighteen seconds in.

#### Acceptance Criteria

1. THE Clipper SHALL support an Assembly consisting of a Cold_Open followed by a Body.
2. THE Clipper SHALL choose the Cold_Open from within the Clip_Candidate's own range, and SHALL NOT take it from elsewhere in the source.
3. THE Clipper SHALL align the Cold_Open to sentence boundaries.
4. THE Clipper SHALL bound the Cold_Open's duration by configuration.
5. THE Clipper SHALL NOT create a Cold_Open where the strongest line is already at the clip's start.
6. THE Clipper SHALL NOT create a Cold_Open from a Segment whose text is a Dangling_Opener.
7. THE Clipper SHALL leave the Cold_Open's audio in the Body, or remove it from the Body, according to configuration.
8. WHERE the Cold_Open remains in the Body, THE Clipper SHALL NOT place it so that the same words are heard twice within a configurable minimum interval.
9. THE Clipper SHALL NOT reduce the delivered clip below the minimum duration for its length preset.
10. THE Clipper SHALL default Cold_Open assembly to disabled.
11. THE Clipper SHALL determine the default by preference trial rather than by assertion.
12. THE Clipper SHALL record in Effects_Applied that a Cold_Open was assembled, and its source range.
13. THE Clipper SHALL NOT create more than one Cold_Open per clip.

### Requirement 2: Assembly reuses the existing single-pass rendering

**User Story:** As a maintainer, I want a multi-segment clip rendered the way a trimmed clip already is, so that assembly does not introduce a second cutting mechanism or an extra encode.

#### Acceptance Criteria

1. THE Clipper SHALL express an Assembly as Keep_Intervals and SHALL render it through the existing keep-interval path.
2. THE Clipper SHALL NOT add an encoding pass for Assembly.
3. THE Clipper SHALL resolve Assembly, filler removal, the transcript cut list, and interior-silence removal into a single keep list and a single re-encode.
4. THE Clipper SHALL apply the existing seam fade treatment at the boundary between Cold_Open and Body.
5. THE Clipper SHALL rebase word times, emoji placements, and speaker turns onto the assembled timeline.
6. THE Clipper SHALL preserve caption, emoji, and speaker-turn correspondence across a non-monotonic Assembly, where the second Segment's source times precede the first's.
7. THE Clipper SHALL NOT reorder the audio and video of a Segment independently.
8. THE Clipper SHALL verify audio/video sync for an assembled clip.
9. THE Clipper SHALL refuse an Assembly it cannot render as a single keep list, and SHALL record the refusal rather than delivering a partially assembled clip.

---

## Group B — Semantic boundaries and diversity (S22, S23)

### Requirement 3: Boundaries respect topic changes

**User Story:** As a creator, I want a clip to cover one subject, so that a clean cut does not disguise two unrelated topics stitched together.

#### Acceptance Criteria

1. THE Clipper SHALL compute a Cohesion_Score across the transcript and SHALL derive Topic_Boundaries from it.
2. THE Clipper SHALL prefer a Clip_Candidate boundary that coincides with a Topic_Boundary over one that does not, within the existing shift limits.
3. THE Clipper SHALL NOT move a boundary further than the existing configurable maximum shift.
4. THE Clipper SHALL preserve sentence alignment above topic alignment, so a clip does not begin or end mid-sentence to reach a Topic_Boundary.
5. THE Clipper SHALL NOT reduce a candidate below the existing minimum-duration guard.
6. THE Clipper SHALL apply topic preference before scene-cut snapping and before edge-silence trimming, preserving the existing stage order.
7. THE Clipper SHALL attach the Cohesion_Score at each candidate boundary to `ClipCandidate.features`.
8. THE Clipper SHALL default topic-boundary preference to disabled.
9. THE Clipper SHALL determine the default from the Boundary_Metric on the labelled selection benchmark, not from the Primary_Metric.
10. THE Clipper SHALL record in Effects_Applied when a boundary was moved to a Topic_Boundary, and by how much.

### Requirement 4: The delivered set is topically diverse

**User Story:** As a creator, I want my clips to cover different points, so that I am not given ten versions of the same idea in different words.

#### Acceptance Criteria

1. THE Clipper SHALL compute Semantic_Similarity between Clip_Candidates.
2. THE Clipper SHALL apply Diversity_Selection when choosing the final clip set.
3. THE Clipper SHALL retain the existing containment-overlap and lexical text-similarity deduplication unchanged, and SHALL apply Diversity_Selection in addition to them.
4. THE Clipper SHALL express the trade-off between score and dissimilarity as a single configurable weight.
5. WHEN the configurable weight assigns all influence to score, THE Clipper SHALL produce exactly the v0.11.0 clip set.
6. THE Clipper SHALL default the weight to the value reproducing v0.11.0 output.
7. THE Clipper SHALL apply Diversity_Selection after scoring and deduplication and before the candidate-count cap.
8. THE Clipper SHALL NOT drop a candidate for dissimilarity reasons where doing so would deliver fewer clips than requested and a candidate remains available.
9. THE Clipper SHALL determine the default weight from the Primary_Metric on the labelled selection benchmark.
10. THE Clipper SHALL record in Effects_Applied when Diversity_Selection changed the delivered set.
11. THE Clipper SHALL NOT compute Semantic_Similarity for candidates below the existing minimum token threshold, and SHALL treat those as maximally dissimilar rather than as duplicates.

---

## Group C — Antecedent repair (S24)

### Requirement 5: A dangling opener is repaired, not merely scored

**User Story:** As a creator, I want a clip that opens on "and that's why he quit" to include what came before, so that the opening makes sense on its own.

#### Acceptance Criteria

1. WHEN a Clip_Candidate's opening is a Dangling_Opener, THE Clipper SHALL attempt an Antecedent_Extension.
2. THE Clipper SHALL extend the start to a sentence boundary.
3. THE Clipper SHALL bound the extension by configuration.
4. THE Clipper SHALL NOT extend a candidate beyond the maximum duration for its length preset.
5. THE Clipper SHALL re-evaluate the opening after extension, and SHALL record whether the Dangling_Opener was resolved.
6. WHERE the extension does not resolve the Dangling_Opener, THE Clipper SHALL revert the extension.
7. THE Clipper SHALL reuse the existing dangling-opener detection rather than adding a second definition.
8. THE Clipper SHALL apply Antecedent_Extension before scene-cut snapping and edge-silence trimming.
9. THE Clipper SHALL NOT apply Antecedent_Extension where it would create an overlap with a higher-scored candidate that deduplication would then remove.
10. THE Clipper SHALL default Antecedent_Extension to disabled.
11. THE Clipper SHALL determine the default from the Boundary_Metric on the labelled selection benchmark.
12. THE Clipper SHALL record in Effects_Applied that an extension was applied, and its size.
13. THE Clipper SHALL NOT extend the start in a way that introduces silence at the clip opening, given that a silent opening is disqualifying for hook purposes.

---

## Group D — Cross-cutting

### Requirement 6: Semantic signals work offline, and degrade honestly

**User Story:** As a self-hosting operator with no API key, I want these features to work, so that the tool's editorial intelligence is not gated behind a download or a credential.

#### Acceptance Criteria

1. THE Clipper SHALL compute Cohesion_Score and Semantic_Similarity with no model checkpoint and no network access by default.
2. THE Clipper SHALL support an optional embedding backend as an enhancement.
3. WHERE an embedding backend is configured but unavailable, THE Clipper SHALL fall back to the offline computation and SHALL record the substitution, naming the missing capability.
4. THE Clipper SHALL NOT download a model or call a network service unless explicitly configured to.
5. THE Clipper SHALL honour `permissibility_mode` by using only the offline computation when it is active.
6. THE Clipper SHALL NOT name the offline computation in a way that implies a learned semantic model produced it.
7. THE Clipper SHALL produce identical offline results for identical input across runs and platforms.
8. THE Clipper SHALL resolve any optional backend's availability through the existing capability probe.

### Requirement 7: Nothing here is enabled before it can be judged

**User Story:** As a maintainer, I want these features measured against ground truth before they change anyone's output, so that plausible editorial reasoning is not mistaken for improvement.

#### Acceptance Criteria

1. THE Clipper SHALL default every feature in this spec to disabled.
2. THE Clipper SHALL NOT change a default for Requirements 3, 4, or 5 before the labelled selection benchmark exists.
3. THE Clipper SHALL NOT change the default for Requirement 1 before a preference trial has been run.
4. THE Clipper SHALL report, for each default it proposes to change, the measurement that supports it.
5. WHERE a measurement does not support a change, THE Clipper SHALL leave the default disabled and SHALL record the finding.
6. WHERE a default changes, THE Clipper SHALL change it in a commit that changes nothing else and SHALL re-freeze the affected fixtures in that same commit.

### Requirement 8: Configuration is documented as a contract

#### Acceptance Criteria

1. FOR every configuration setting this spec adds, THE Clipper SHALL provide a matching documented entry in `.env.example`.
2. THE Clipper SHALL document, for each new default, whether it is measured or provisional.
3. THE Clipper SHALL NOT introduce a documented key that is not a real setting.
4. THE Clipper SHALL surface through the Info_Endpoint any new option value the UI must offer.
5. THE Clipper SHALL round-trip every new Processing_Options field through serialisation without loss.
6. WHERE a new Processing_Options value is unrecognised or malformed, THE Clipper SHALL apply the documented default and SHALL NOT raise.
7. THE Clipper SHALL keep every existing Effects_Applied marker spelled exactly as it is today.
8. THE Clipper SHALL name in each new marker the value actually applied, never the value requested.

### Requirement 9: Every claim is verified against the real program

#### Acceptance Criteria

1. THE Clipper SHALL include a test asserting an Assembly, filler removal, the transcript cut list, and interior-silence removal resolve into exactly one re-encode when combined.
2. THE Clipper SHALL include a test asserting word, emoji, and speaker-turn correspondence survives a non-monotonic Assembly.
3. THE Clipper SHALL include a test measuring audio/video sync on a rendered assembled clip.
4. THE Clipper SHALL include a property test asserting Diversity_Selection never returns fewer clips than requested while candidates remain.
5. THE Clipper SHALL include a property test asserting the diversity weight's neutral value reproduces the score-ordered set exactly.
6. THE Clipper SHALL include a test asserting an unresolved Antecedent_Extension is reverted.
7. THE Clipper SHALL include a test asserting topic-boundary preference never violates the existing shift, sentence-alignment, or minimum-duration guards.
8. THE Clipper SHALL include a test asserting the offline semantic computation is deterministic across runs.
9. THE Clipper SHALL cross-check any parsed program output through an independent mechanism sharing no parsing code with the implementation.
10. THE Clipper SHALL NOT introduce any test that is skipped when its dependencies are present.
11. THE Clipper SHALL NOT introduce any new warning into the test run.
12. THE Clipper SHALL add a mutation specification covering the highest-value mutations of the assembly keep-list, diversity, and extension arithmetic.
13. THE Clipper SHALL attach rendered output for every requirement whose effect is audible or visible in the edit.
