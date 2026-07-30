# Selection benchmark (S1)

The harness that answers: **given sources with the moments you would actually post marked by
hand, how often does the selector find them?**

Everything in §3 of `docs/IMPROVEMENT_PLAN.md` — audio energy, pitch, speech rate, hook
scoring, scene awareness — is a change to a ranking. A ranking cannot be improved by
inspection, which is why the plan puts this first and says every change below it is otherwise
unmeasurable.

## Labelling

```bash
# one label file per source
python scripts/eval_selection.py template --source /media/podcast-ep12.mp4

# then edit eval/labels/podcast-ep12.json and replace the placeholder moment
python scripts/eval_selection.py validate
```

A label file is deliberately small:

```json
{
  "source": "../media/podcast-ep12.mp4",
  "notes": "two hosts, lots of cross-talk",
  "moments": [
    {"start": 412.0, "end": 455.0, "note": "the story about the failed launch"},
    {"start": 1123.5, "end": 1160.0, "note": "punchline about pricing"}
  ]
}
```

### How to label

- **Mark what you would post**, not what looks technically clean. The benchmark is a model of
  your judgement; if you label what you think the tool can find, it will score well and teach
  you nothing.
- **3–8 moments per source.** Timestamps to the nearest second are fine — the metrics use
  overlap, not equality, precisely so that a second either way does not matter.
- **Do not try to be exhaustive.** A moment you *meant* to mark but forgot counts against the
  selector when it finds it. Under-labelling is the one bias worth actively avoiding.
- **Label before you look at any output.** Once you have seen what the tool picked, you cannot
  un-see it, and the labels stop being independent.
- **Aim for 20 sources.** Fewer works and the harness will score three, but a difference of a
  few points between runs is then noise rather than signal.
- **Moments must not overlap.** Merge them instead. An overlap lets one returned clip match two
  "different" wanted moments, so the selector is rewarded twice for one decision.

## Running

```bash
# first run transcribes (cached afterwards); needs ffmpeg, a Whisper model, an LLM key
python scripts/eval_selection.py run --k 5

# the deterministic strategies need no transcript and no model at all - useful for
# establishing what the fallback scores before paying for anything
python scripts/eval_selection.py run --k 5 --strategy silence

# before/after a change to a selection signal
python scripts/eval_selection.py run --k 5 --json before.json
#   ... make the change ...
python scripts/eval_selection.py run --k 5 --json after.json
python scripts/eval_selection.py compare --before before.json --after after.json
```

Transcripts are cached under `eval/cache/`, keyed on each source's path, size and mtime, so only
the first run pays for transcription. Re-exporting the footage invalidates the entry.

## Reading the result

```
                            IoU 0.3      IoU 0.5      IoU 0.7  mean best
                         prec   rec   prec   rec   prec   rec        IoU
------------------------------------------------------------------------------
selector:ai             0.67   1.00  0.67   1.00  0.00   0.00       0.57
baseline:uniform        0.40   1.00  0.40   1.00  0.00   0.00       0.52
baseline:longest        0.67   1.00  0.67   1.00  0.00   0.00       0.57
```

**The baselines are the point.** Whether precision@5 of 0.40 is good depends entirely on what
picking clips without thinking scores on the *same* footage, and that number is not guessable —
it moves with source length, how many moments were labelled and how long they are. On a
three-minute video with five labelled moments, evenly spaced guesses do well; on a three-hour
podcast with five, they score near zero.

- `uniform` — evenly spaced clips. The no-information floor.
- `random` — seeded random placement. Chance level.
- `longest` — the longest spans between silences, which is what the shipped deterministic
  fallback actually does when it caps the count. **If the LLM selector cannot beat this, the
  fallback is not a fallback — it is the product.**

Then:

- **precision** — of the clips returned, how many you wanted. What a user experiences as "how
  much of this output is worth keeping".
- **recall** — of the moments you wanted, how many were found. Exposes a selector that returns
  few but safe clips: perfect precision over one clip while missing nine moments is not success.
- **IoU 0.3 / 0.5 / 0.7** — how strict "found it" is. 0.3 is "the right part of the video",
  0.7 is "the boundaries too". A change that improves 0.3 while leaving 0.7 flat has improved
  targeting, not cutting.
- **mean best IoU** — the diagnostic precision cannot express. Two selectors both scoring zero
  at 0.5 are not equivalent: one landing at 0.45 is cutting the right moment badly (a boundary
  problem — look at S9 and sentence snapping), one at 0.05 is looking in the wrong place (a
  scoring problem). The report calls the first case out as a near miss.

## What this does not measure

- **Whether a clip performs.** It measures agreement with your judgement, which is the best
  available proxy and not the same thing. S16 (recording published-clip performance) is the
  only thing that closes that gap.
- **Anything about the render.** Captions, audio and encoding are covered elsewhere;
  `scripts/smoke_reel.py` is the visual equivalent of this harness.
