# Retention-first clipping

The retention editor turns the selector's measured evidence into a small,
reviewable edit plan. It is designed for talking-head videos, podcasts,
webinars, streams, interviews and gameplay commentary where the strongest
moment is not always the loudest sentence.

## What is implemented

### Moment detection

The selector combines the existing transcript, audio and visual signals with
the new deterministic engagement layer:

1. **Hook quality** — whether speech starts promptly, whether the opening is
   faster/louder than the rest of the clip, and whether the wording contains a
   question, concrete number, second-person address or meaningful tension.
2. **Delivery change** — speech-rate departure from the speaker's own baseline,
   RMS energy departure from the source median, and optional pitch variation.
3. **Discourse structure** — question/answer shape, enumeration markers,
   emotional wording and standalone completeness.
4. **Audience reactions** — optional source-relative reaction timestamps. A
   reaction spike corroborates a candidate; it never creates a claim that is
   absent from the transcript.
5. **Temporal accents** — energy onsets, pitch jumps and reaction timestamps
   are retained as bounded events so editing can happen at a real moment rather
   than at a guessed percentage of the clip.

When retention editing is enabled, the model remains the primary selector and
the deterministic engagement score is blended into the ranking. This prevents
one noisy signal from replacing editorial judgment.

## Processing modes

The setting is available in **Advanced settings → Retention editing**.

| Mode | Selection | Rendered edit |
| --- | --- | --- |
| **Off** | Existing ranking and behavior | No retention-specific cues |
| **Balanced** | 25% engagement blend | Restrained zoom accents and deterministic caption emphasis |
| **Aggressive** | 40% engagement blend | Balanced edits plus long dead-air cuts and up to four low-level emphasis pops |

All output is bounded: at most twelve zoom cues, four sound-effect cues, and
twelve emphasis words per clip. Repeated events within the same short window
are coalesced.

## Audience reaction input

Reaction data is optional. Enter source-relative seconds in the UI as JSON:

```json
[
  {"time": 42.5, "count": 18},
  {"time": 43.1, "count": 7}
]
```

The parser also accepts `timestamp`, `at`, `weight`, and `value`, or an object
wrapper such as `{"reactions": [...]}`. Invalid, negative, zero-weight,
non-finite and malformed entries are ignored safely. Events are relative to the
original source, not the selected clip. The pipeline converts them to the
delivered clip timeline after silence trimming and transcript cuts.

Reaction data can come from a chat export, live-chat event log, comments
timeline, stream replay annotations or a custom integration. The current
application deliberately accepts a simple JSON contract so no platform
credentials or network dependency are required.

## Edit plan contract

Each rendered `ClipResult` exposes a JSON-safe `retention_plan`:

```json
{
  "mode": "balanced",
  "hook_score": 0.84,
  "hook_window_s": 3.0,
  "emphasis_indices": [0, 3, 7],
  "zoom_beats": [0.35, 1.22, 8.4],
  "sfx_times": [],
  "pacing_cuts": [],
  "reasons": ["front_loaded_hook", "energy_accents", "audience_reactions"]
}
```

The indices refer to the clip's original word list, not caption text. This is
important when the same word is spoken twice or ASR contains blank words. The
rendering layer maps those indices into the cue-flattened list before writing
ASS captions, so a missing ASR word cannot move emphasis to a different word.

The plan is recalculated after destructive timeline edits. That means exposed
cue times describe the file the viewer receives, not the pre-trim candidate.

## Prompt instructions used by the selector

When a retention mode is enabled, the selection prompt instructs the model to:

- treat the first three seconds as a retention gate;
- prefer direct openings, surprising statements, emotional turns,
  question-and-answer structures and high-energy delivery;
- treat audience reaction spikes as corroborating evidence, not proof;
- prefer moments whose strongest line is near the opening;
- tighten pacing only where meaning remains intact; and
- never invent facts, audience reactions, quotes or missing context.

Metadata generation uses the same safety rules. The generated `hook_text` is a
short transcript-faithful opening for the first three seconds, and the
deterministic fallback uses the first transcript words rather than generic
clickbait.

## Technical workflow

```text
probe + transcribe
    ↓
measure source energy / optional pitch / transcript discourse features
    ↓
parse optional reaction events
    ↓
LLM candidate selection (or scored deterministic fallback)
    ↓
engagement annotation + retention-aware rerank
    ↓
deduplicate and cap requested clip count
    ↓
cut and apply existing silence/transcript edits
    ↓
build retention plan on the delivered timeline
    ↓
one compositor pass:
  captions + emphasis → zoom accents → b-roll → music → SFX → loudness
    ↓
persist ClipResult, retention_plan and provenance markers
```

The planner is pure Python and does not invoke ffmpeg, open sockets, use a
clock, or make an additional LLM call. Existing media analysis remains in the
pipeline's established cached feature passes. If reaction input, pitch, visual
analysis, a b-roll asset or an SFX asset is unavailable, the clip still renders
and the existing degradation-marker conventions remain in force.

## Editorial guidance

The automated plan is an assistant, not a promise of audience retention. Review
the first three seconds and the final sentence before publishing. In particular:

- do not cut a pause that carries emotional meaning;
- do not use a zoom or pop on every word;
- keep captions away from faces and platform UI safe areas;
- use only licensed music, b-roll and sound effects; and
- do not treat a chat spike as evidence that a statement is true.

For high-stakes content, leave retention mode on **Balanced**, inspect the
review card's reasons and cue counts, then make any transcript cuts manually.
