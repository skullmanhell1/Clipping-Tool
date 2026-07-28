# Session Handoff

Written at the end of a working session on branch `docs/stem-foundation-gate`
(head `36f6251`). Everything below is either verified in this repo or an
explicit user decision. Where something is *unverified*, it says so.

## 1. Status

**kinetic-typography — complete.** All 17 epics are implemented and their tasks
are closed. One known open defect (P12, see section 5) which the user has
decided to move to its own bugfix spec rather than patch inline.

**audio-stem-inpainting — barely started.** Epic 1 (the foundation
prerequisite gate) is done and committed. 84 of the 86 remaining tasks are
untouched. The gate's findings are the single most valuable artefact from epic 1
because they correct nine wrong assumptions the design had baked in about the
existing worker API (section 6).

## 2. Open PRs — merge in this order

Each is a fast-forward on top of the previous one. Merging out of order will
force a rebase.

| Order | PR | Contents |
| --- | --- | --- |
| 1 | #36 | Kinetic parity gate: flag-off parity test plus the caption drift pins. |
| 2 | #37 | libass burn sweep and the end-to-end single-pass render test. |
| 3 | #38 | Stem foundation prerequisite gate (epic 1 of audio-stem-inpainting). |

## 3. User decisions carried into the next session

**(a) Widen the host gate.** In `host.py`, the gate currently admits only
`APPLIED`. It must be widened to admit `DEGRADED` as well, on the condition
that `result.media is not None` and `engine.produces_media`. A degraded engine
that still produced media has produced usable output and its media must not be
discarded.

**(b) Additive `notes` parameter — accepted.** Add a `notes` parameter to the
public `run_stage` and thread it through into `_build_context`. It is strictly
additive: existing callers must keep working unchanged, so it needs a default.

**(c) Kinetic P12 gets its own bugfix spec.** Do not fold the fix into any
kinetic-typography task. Open a dedicated bugfix spec, seeded with the analysis
and the counter-examples in section 5.

## 4. Baselines

Test command:

```
PYENV_VERSION=3.11.15 python3 -m pytest tests/ -q
```

| Environment | Result |
| --- | --- |
| With `ffmpeg` on `PATH` | 410 passed, 1 failed |
| Bare (no `ffmpeg`) | 336 passed, 75 skipped, 0 failed |

The single failure with ffmpeg present is the P12 test in section 5.

**A skip is not a pass.** The 75-test delta between the two runs is entirely
media-gated tests that silently skip when `ffmpeg` is absent. Any session that
intends to claim a green suite needs a static `ffmpeg`/`ffprobe` on `PATH`;
otherwise those 75 tests assert nothing and the "0 failed" bare number is not
evidence of correctness.

## 5. Kinetic P12

### Symptom

`test_p12_malformed_timings_degrade_instead_of_raising` fails, minimising to
`Time_Base(fps=14.0)`.

### This is not a one-line fix

The `+ frame` widening term — the obvious suspect — **is already correct**. It
was checked by sweeping fps from 1.0 to 240.0 against a 200-position grid: the
smallest span produced was 0.08218 s, and in every single case the snapped span
was at least `ceil(MIN_WORD_S * fps)` frames. The widening term is not the bug
and changing it will not fix this.

### Actual root cause

The point-cue ceiling scan in planner step 5 required `later_start >
cue_start`. That predicate skips a later cue whose *snapped* start landed
exactly on the point cue's start. With that neighbour invisible to the scan,
the ceiling was left too high, the widened point cue overran the neighbour, and
the downstream disjointness cursor shaved the overlap off the widened cue's
**front**. What survives is a 1-frame cue that still contains its synthesised
words — shorter than `MIN_WORD_S` with words inside it.

### Partial fix — present in the working tree, UNVERIFIED, and reverted

The diff below was captured from `git diff worker/engines/kinetic.py` before
being reverted at the end of the session. It addresses the ceiling scan but it
was never validated against a full run, and it does **not** close the two
counter-examples that follow. Treat it as a starting hypothesis for the bugfix
spec, not as a fix.

```diff
diff --git a/worker/engines/kinetic.py b/worker/engines/kinetic.py
index 294e408..30eed27 100644
--- a/worker/engines/kinetic.py
+++ b/worker/engines/kinetic.py
@@ -1636,7 +1636,17 @@ def plan_kinetic(
             cue_start = max(cue_start, cursor_pre)
             ceiling = grid_limit
             for later_start, later_end in bounds[index + 1 :]:
-                if later_end > later_start and later_start > cue_start:
+                if later_end > later_start:
+                    # The *first* later cue with a real interval is the ceiling
+                    # whatever its start — including a start that snapped onto
+                    # this very point, or that this cue's ``cursor_pre`` shift
+                    # already reached. Requiring ``later_start > cue_start``
+                    # here let the widened point cue run *over* such a
+                    # neighbour; the disjointness cursor below then shaved the
+                    # overlap off the widened cue's front, leaving it shorter
+                    # than MIN_WORD_S with its synthesised words inside it
+                    # (Req 6.2). With no room left the cue collapses to a point
+                    # and normalisation drops it with its words (Req 5.5).
                     ceiling = min(ceiling, later_start)
                     break
             # ``+ frame`` absorbs the half-frame snap error, so the snapped span
```

### Two counter-examples remain, by a different path

These survive the diff above, which is the evidence that the ceiling scan is
not the whole story. They arrive at the same bad state — a sub-`MIN_WORD_S` cue
holding synthesised words — via a different route. Note `timing_synthesised=True`
and the collapse of `normalised` to a single interval in both traces.

**Counter-example 1 — 30 fps** (`.verify_tmp/h1.txt`):

```
words = [(0.155, 'NOATTR', "'hello'"), (0.554, 0.905, "'like'"), (1.0, 1.61, "'so'"), (1.904, 2.45, "'\\t'"), (2.98, 3.738, "'\\t'"), ('', 4.821, "'word'"), (None, 6.155, "'so'"), ([0.0], 6.245, "'hello'")]
duration = 7.218
base = Time_Base(fps=30.0, sample_rate=8000, rounding=<Rounding.NEAREST: 'nearest'>, fps_substituted=True)
options = {'style': 'slide_up', 'reveal': 'cumulative', 'preset_name': 'typewriter', 'font_override': '', 'preset_font': 'Noto Sans JP', 'font_size': 59, 'position': 'top', 'max_lines': 1, 'max_line_width': 66, 'safe_area_x_pct': 5.030300410544809, 'safe_area_y_pct': 30.74874886414598, 'motion_duration_ms': 253, 'highlight_keywords': True, 'keyword_ai': False, 'emoji_inline': True, 'confidence_floor': 1.0, 'captions_enabled': True, 'hook_enabled': True, 'hook_duration_s': 4.610499555557423, 'hook_font_size': 137, 'durable_subtitle': True, 'permissibility': True}
bad = [(6.166666666666667, 6.233333333333333, Kinetic_Word(text='hello', start=6.166666666666667, end=6.233333333333333, rel_ms=0, emphasis=False, timing_synthesised=True, emoji='', line=0))]
plan = [(0.0, 4.833333333333333, [('word', 0.0, 4.833333333333333, True)]), (4.833333333333333, 6.166666666666667, [('so', 4.833333333333333, 6.166666666666667, True)]), (6.166666666666667, 6.233333333333333, [('hello', 6.166666666666667, 6.233333333333333, True)])]
trace = {'drafts': [(0.155, 1.61, ['hello', 'like', 'so']), (0.0, 4.821, ['word']), (0.0, 6.155, ['so']), (0.0, 6.245, ['hello'])], 'bounds': [(0.16666666666666666, 1.6), (0.0, 4.833333333333333), (0.0, 6.166666666666667), (0.0, 6.233333333333333)], 'filled': [(0.16666666666666666, 1.6), (0.0, 4.833333333333333), (0.0, 6.166666666666667), (0.0, 6.233333333333333)], 'normalised': [(0.0, 6.233333333333333)], 'grid_limit': 7.2, 'frame': 0.03333333333333333}
hits: 1
```

**Counter-example 2 — 240 fps** (`.verify_tmp/h5.txt`):

```
words = [(1.192, 0.328, "'um'"), (1.645, 1.279, "'like'"), (1.879, 'NOATTR', "'like'"), (2.285, 2.85, "''"), ('nan', 3.465, "'word'"), (None, 4.378, "'like'"), ('nan', 4.438, "'word'")]
duration = 4.438
base = Time_Base(fps=240.0, sample_rate=96000, rounding=<Rounding.NEAREST: 'nearest'>, fps_substituted=True)
options = {'style': 'bounce', 'reveal': 'cumulative', 'preset_name': 'boxed', 'font_override': '', 'preset_font': 'Inter-Bold', 'font_size': 61, 'position': 'top', 'max_lines': 3, 'max_line_width': 24, 'safe_area_x_pct': 15.368890325562075, 'safe_area_y_pct': 24.704870077009314, 'motion_duration_ms': 271, 'highlight_keywords': True, 'keyword_ai': False, 'emoji_inline': True, 'confidence_floor': 0.5, 'captions_enabled': True, 'hook_enabled': False, 'hook_duration_s': 5.577269891137479, 'hook_font_size': 115, 'durable_subtitle': True, 'permissibility': True}
bad = [(4.379166666666666, 4.4375, Kinetic_Word(text='word', start=4.379166666666666, end=4.4375, rel_ms=0, emphasis=False, timing_synthesised=True, emoji='', line=0))]
plan = [(0.0, 4.379166666666666, [('like', 0.0, 4.379166666666666, True)]), (4.379166666666666, 4.4375, [('word', 4.379166666666666, 4.4375, True)])]
trace = {'drafts': [(1.192, 3.465, ['um', 'like', 'like', 'word']), (0.0, 4.378, ['like']), (0.0, 4.438, ['word'])], 'bounds': [(1.1916666666666667, 3.466666666666667), (0.0, 4.379166666666666), (0.0, 4.4375)], 'filled': [(1.1916666666666667, 3.466666666666667), (0.0, 4.379166666666666), (0.0, 4.4375)], 'normalised': [(0.0, 4.4375)], 'grid_limit': 4.4375, 'frame': 0.004166666666666667}
hits: 1
```

## 6. Nine binding API corrections from the stem foundation gate

The audio-stem-inpainting design was written against an imagined worker API.
The gate checked each assumption against the code. These nine corrections are
binding on every remaining stem task — write new code against these, not
against the design's prose.

1. `FakeWord` is a class in `tests/conftest.py` whose constructor argument order
   is `(start, end, text)` — e.g. `FakeWord(0.0, 0.5, "hi")` — **not**
   `(text, start, end)`. It exposes `.start`, `.end`, `.text` and sets
   `.probability = 1.0` (hard-coded, not a constructor parameter), which is why
   a generator needing a real confidence must set `.probability` on the
   instance.
2. `Engine_Workspace.path` is a **method**, not an attribute. The directory is
   `.root`.
3. There is no `Engine_Result.applied()` constructor. Do not call it.
4. The stage entry points are named `run_stage` and `finish_clip`.
5. The `raw = out.media or raw` reassignment lives in `pipeline.py`, not in the
   engine or the host.
6. `normalize_segments` **drops plain tuples**. Anything passed to it has to be
   the real segment type or it vanishes silently.
7. `Engine_Artifact` carries a `storage_key`.
8. `Engine_Context` has three fields beyond what the design assumed. Construct
   it fully.
9. `make_video` is a **fixture factory**, so it must be called to get a video —
   it is not the video itself.

## 7. Next steps

1. **Epic 2** — generators and test doubles, built against the corrected
   bindings in section 6.
2. **Epics 4–6** — data models, then the pure planner.

Before claiming any suite green, install a static `ffmpeg`/`ffprobe` so the 75
gated tests actually run (section 4).
