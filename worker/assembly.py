"""Cold-open assembly: put a clip's strongest line first (S21).

Every clip this project delivers is one contiguous range, so a hook buried eighteen seconds in stays
buried. A cold open lifts the strongest sentence out of the clip and plays it at the front.

THE MECHANISM IS ALREADY BUILT, WHICH IS WHY THIS MODULE IS PURE PLANNING
------------------------------------------------------------------------
`worker/effects/filler.py` already renders a non-contiguous keep list in **one** re-encode:
`apply_keep_intervals` emits `trim`/`atrim` per keep and joins them with `concat`, and `_seam_fades`
puts a few-ms `afade` at each interior seam. Assembling a clip out of two ranges is that same
operation with a different list, so R2.1 and R2.2 are satisfied by *reusing* it and adding no second
way to cut video.

Two properties of that renderer are load-bearing here, and both were verified by reading it rather
than assumed:

* it iterates the keep list **in the order given** and never re-sorts, so a non-monotonic list
  renders in assembly order;
* video and audio trims are built from the **same loop variable** in the same iteration, so R2.7
  ("never reorder audio and video independently") holds by construction rather than by discipline.

`filler._merge` *does* sort by start, and must therefore never be applied to an assembly. It is used
inside `plan_keep_intervals`, where the keeps are monotonic and coalescing is correct.

THE NON-MONOTONIC REBASE IS THE DANGEROUS PART
----------------------------------------------
An assembly produces ``[hook, body]`` where the hook's source times come *after* the body's. Two
distinct hazards follow, and only the first is obvious:

1. **Order.** `filler.rebase_words` accumulates its offsets in list order, so it is already correct
   for a reordered list. Good, but incidental -- nothing documented it, so
   :func:`rebase_onto` states it and the tests pin it.
2. **Duplication.** When the cold open is *left* in the body (R1.7), the same source range appears in
   the keep list **twice**, and `rebase_words` stops at the first match (`break`). A word inside that
   range therefore gets one output position when it is heard two. Captions would appear for the cold
   open and then be missing when the line is heard again in the body -- a defect that looks like an
   ASR gap, which is exactly the misattribution the spec warns about.

:func:`rebase_onto` handles both, and emits one output item per occurrence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from worker import discourse, hook_score
from worker.effects.filler import Interval

#: Shortest cold open worth making.
#:
#: Below this a lifted line reads as a stutter before the clip rather than as an opening statement,
#: and the seam fade occupies a noticeable fraction of it.
MIN_COLD_OPEN_S = 1.0

#: Content words a sentence needs before it can be the cold open.
#:
#: `hook_score.text_signal` will happily score a three-word fragment, and "and then this" can outrank
#: a real line on keyword density alone. This is the same evidential argument
#: `candidate_ranking.MIN_TEXT_TOKENS` makes: acting on too little text deletes or promotes something
#: for no reason.
MIN_COLD_OPEN_WORDS = 4


def _bounds(item: Any) -> tuple[float, float] | None:
    """``(start, end)`` of a timed item, or ``None`` when it has no usable timing.

    ``None`` rather than a default of ``0.0``, because a word defaulted to zero would silently anchor
    itself to the clip start -- which is both a wrong caption and the hardest kind of wrong to notice.
    """
    start_raw = getattr(item, "start", None)
    if start_raw is None:
        return None
    try:
        start = float(start_raw)
    except (TypeError, ValueError):
        return None
    end_raw = getattr(item, "end", None)
    try:
        end = float(end_raw) if end_raw is not None else start
    except (TypeError, ValueError):
        end = start
    return start, max(start, end)


@dataclass(frozen=True)
class Sentence:
    """One sentence of the clip, in clip-relative time."""

    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class Assembly_Plan:
    """An ordered assembly, or an inert plan explaining why there is not one.

    ``segments`` is in **delivery order**, so ``segments[0]`` is what the viewer hears first. It is
    empty for an inert plan; a caller checks :attr:`assembled` rather than truthiness of the list,
    because "no assembly" and "an assembly of one segment" are different answers.
    """

    segments: tuple[Interval, ...] = ()
    cold_open: Interval | None = None
    retained_in_body: bool = True
    marker: str = ""
    refusal: str = ""
    detail: str = ""

    @property
    def assembled(self) -> bool:
        return self.cold_open is not None and len(self.segments) >= 2


def sentences_from_words(words: Sequence[Any]) -> list[Sentence]:
    """Group clip-relative ``words`` into sentences (R1.3).

    Sentence boundaries come from `discourse._sentences`, the splitter this project already uses to
    decide what a sentence is, rather than a second regex. The words carry the timing, so the text is
    split and then walked back onto word times -- which keeps the boundary *audible*: a sentence ends
    where its last word ends, not at a punctuation mark's notional position.

    Words without usable timing are skipped rather than defaulted to zero, because a word at 0.0 would
    silently anchor a sentence to the clip start.
    """
    usable: list[tuple[float, float, str]] = []
    for word in words:
        span = _bounds(word)
        if span is None:
            continue
        text = str(getattr(word, "text", "") or "")
        if not text.strip():
            continue
        usable.append((span[0], span[1], text))
    if not usable:
        return []

    joined = " ".join(text for _s, _e, text in usable)
    pieces = discourse._sentences(joined)
    if not pieces:
        return []

    out: list[Sentence] = []
    cursor = 0
    for piece in pieces:
        needed = len([t for t in piece.split() if t.strip()])
        if needed <= 0:
            continue
        window = usable[cursor : cursor + needed]
        if not window:
            break
        out.append(Sentence(start=window[0][0], end=window[-1][1], text=piece))
        cursor += needed
    return out


def choose_cold_open(
    sentences: Sequence[Sentence],
    *,
    max_seconds: float,
    clip_duration: float,
) -> tuple[Sentence | None, str]:
    """The sentence to lift, or ``(None, reason)`` (R1.2-R1.6, R1.13).

    Scored with `hook_score.text_signal`, the same reading the hook scorer already uses to judge an
    opener -- so "strongest line" means the same thing here as it does everywhere else in this
    project. A second definition of a strong opening would drift from the first.

    Every rejection returns a *named* reason, because "no cold open" has several causes and only some
    of them are worth acting on. The guards, and why each exists:

    * **already first** (R1.5) -- reordering an already-correct clip produces a duplicate for nothing.
      Compared by index rather than by time, so a clip whose first sentence starts slightly after 0.0
      (edge-silence trimming) still counts as already-first.
    * **dangling opener** (R1.6) -- `discourse.standalone_completeness` is the detector that already
      exists, reused rather than restated. A hook opening on *"and that's why he quit"* is worse than
      no hook: the cold open is the one position where missing context cannot be recovered from what
      came before, because nothing came before.
    * **too long / too short** -- bounded by configuration (R1.4) and by
      :data:`MIN_COLD_OPEN_S`.
    * **too few words** -- see :data:`MIN_COLD_OPEN_WORDS`.

    At most one is ever returned, which is R1.13 by construction.
    """
    if len(sentences) < 2:
        return None, "single_sentence"

    limit = max(0.0, float(max_seconds))
    if limit < MIN_COLD_OPEN_S:
        return None, "bound_below_minimum"

    ranked: list[tuple[float, int, Sentence]] = []
    for index, sentence in enumerate(sentences):
        if sentence.duration < MIN_COLD_OPEN_S or sentence.duration > limit:
            continue
        if len([t for t in sentence.text.split() if t.strip()]) < MIN_COLD_OPEN_WORDS:
            continue
        # Ties broken by earlier position, so the choice is deterministic on repeated runs.
        ranked.append((hook_score.text_signal(sentence.text), -index, sentence))
    if not ranked:
        return None, "no_eligible_sentence"

    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best_score, negative_index, best = ranked[0]
    # `no_signal` is tested BEFORE `already_first`, and the order matters. `text_signal` is sparse --
    # it returns 0.0 for any sentence without opener-ish wording, which is most of them -- so on a clip
    # where nothing stands out every score ties at zero and the earlier-index tiebreak hands back
    # sentence 0. Reporting that as "the strongest line is already first" would be a false claim about
    # the material: the truth is that no line stood out at all, and those are different findings.
    if best_score <= 0.0:
        return None, "no_signal"
    if -negative_index == 0:
        return None, "already_first"
    if discourse.standalone_completeness(best.text).dangling_opener:
        return None, "dangling_opener"
    # A cold open cannot be most of the clip: the body would then be a short remainder and the
    # delivered clip is a repeat rather than an edit.
    if clip_duration > 0.0 and best.duration >= clip_duration * 0.5:
        return None, "too_large_a_share"
    return best, ""


def plan(
    words: Sequence[Any],
    *,
    clip_duration: float,
    enabled: bool = False,
    max_seconds: float = 6.0,
    retain_in_body: bool = True,
    min_repeat_gap: float = 8.0,
    min_clip_seconds: float = 0.0,
) -> Assembly_Plan:
    """Plan a cold-open assembly, or return an inert plan (R1.*).

    Returns segments in **delivery order**, clip-relative. Inert when disabled (R1.10), so the default
    path builds no assembly and the rendered clip is unchanged.

    ``retain_in_body`` is the R1.7 taste decision and genuinely has no right answer: leaving the line
    in means the viewer hears it twice, which is a recognised short-form device; removing it means the
    body loses its best line. Configuration decides, and both are implemented.

    ``min_repeat_gap`` (R1.8) applies only when retaining. If the lifted line would be heard again too
    soon after the cold open, the two occurrences read as a stutter rather than as a callback -- so the
    line is dropped from the body instead, which is the *other* configured behaviour rather than a
    refusal. Reporting a refusal there would decline an assembly that is fine in its other form.

    ``min_clip_seconds`` (R1.9) is the length preset's floor. Removing the lifted line shortens the
    delivered clip; if that would breach the floor the line is retained instead, for the same reason.

    Total: any malformed input yields an inert plan rather than an exception. An editorial refinement
    must never be why a clip fails.
    """
    if not enabled:
        return Assembly_Plan(detail="cold-open assembly disabled")
    try:
        duration = float(clip_duration)
    except (TypeError, ValueError):
        return Assembly_Plan(refusal="assembly_refused:bad_duration")
    if duration <= 0.0:
        return Assembly_Plan(refusal="assembly_refused:bad_duration")

    sentences = sentences_from_words(words)
    cold, reason = choose_cold_open(sentences, max_seconds=max_seconds, clip_duration=duration)
    if cold is None:
        return Assembly_Plan(detail=f"no cold open: {reason}")

    hook = Interval(round(cold.start, 3), round(cold.end, 3))

    # Whether the line stays in the body. Both overrides below choose the *other configured
    # behaviour* rather than refusing, because an assembly that is fine in one form should not be
    # abandoned for failing the other.
    retain = bool(retain_in_body)
    notes: list[str] = []
    if retain and float(min_repeat_gap) > 0.0 and hook.start < float(min_repeat_gap):
        # The line is near the clip's own start, so the cold open and its repeat would be seconds
        # apart. R1.8.
        retain = False
        notes.append("repeat_gap")
    if not retain and duration - hook.duration < float(min_clip_seconds):
        # Removing it would take the clip under its preset's floor. R1.9 outranks R1.8: a clip that is
        # too short is a broken deliverable, while hearing a line twice is a style someone chose.
        retain = True
        notes.append("length_floor")

    body = _body_segments(hook, duration, retain=retain)
    if not body:
        return Assembly_Plan(refusal="assembly_refused:empty_body")

    segments = (hook, *body)
    detail = "retained in body" if retain else "lifted from body"
    if notes:
        detail = f"{detail} ({', '.join(notes)})"
    return Assembly_Plan(
        segments=segments,
        cold_open=hook,
        retained_in_body=retain,
        marker=f"cold_open:{hook.start:.3f}-{hook.end:.3f}",
        detail=detail,
    )


def _body_segments(hook: Interval, duration: float, *, retain: bool) -> list[Interval]:
    """The body, after the cold open has been lifted from it.

    Retaining gives one segment covering the whole clip. Removing gives the clip minus the hook, which
    is **two** segments when the hook is in the middle -- and both are kept, in source order, so the
    body still plays forward. Dropping the tail would silently truncate the clip.
    """
    if retain:
        return [Interval(0.0, round(duration, 3))]
    out: list[Interval] = []
    if hook.start > 0.01:
        out.append(Interval(0.0, hook.start))
    if duration - hook.end > 0.01:
        out.append(Interval(hook.end, round(duration, 3)))
    return out


def compose(
    segments: Sequence[Interval],
    base_keeps: Sequence[Interval] | None,
) -> list[Interval]:
    """One keep list from the assembly and whatever else already removed regions (R2.3).

    ``base_keeps`` is the keep list filler removal and the U4 transcript cut list already produced --
    monotonic, clip-relative. The assembly is the **outer ordering** and those keeps are an inner
    filter: each assembly segment is intersected with them, in assembly order, so a removed filler
    word stays removed wherever its range is played and the result is still a single list.

    That ordering is the whole reason this composes into one re-encode rather than two. Applying them
    in sequence would concatenate twice, and the second pass's keeps would be expressed against the
    first pass's output timeline rather than against the source offsets everything else refers to.

    With no ``base_keeps`` the assembly passes through unchanged.
    """
    if not base_keeps:
        return [Interval(s.start, s.end) for s in segments]

    out: list[Interval] = []
    for segment in segments:
        for keep in base_keeps:
            start = max(segment.start, keep.start)
            end = min(segment.end, keep.end)
            if end - start > 0.01:
                out.append(Interval(round(start, 3), round(end, 3)))
    return out


def rebase_onto(items: Sequence[Any], keeps: Sequence[Interval], *, build) -> list[Any]:
    """Remap ``items`` onto the assembled timeline, duplicates included (R2.5, R2.6).

    `filler.rebase_words` cannot be used for an assembly, and the reason is not the ordering -- it
    accumulates offsets in list order and is already correct there. It is the ``break``: it stops at
    the first keep containing an item, so when the cold open is retained and the same source range
    appears **twice**, a word gets one output position for two occurrences. Captions would show for the
    cold open and then be absent when the line is heard again, which reads as an ASR failure rather
    than as an assembly bug.

    So this emits **one output item per occurrence**, in output-time order.

    ``build(item, start, end)`` constructs the rebased object, which keeps this function agnostic about
    what it is remapping. R2.5 names three consumers -- words, emoji placements and speaker turns --
    and one of them working does not imply the others, so each gets its own builder and its own test.

    Membership is by **midpoint**, matching `rebase_words` exactly: an item straddling a boundary
    belongs to whichever side holds most of it, and changing that rule here would move captions
    relative to the existing filler path.
    """
    offsets: list[tuple[float, float, float]] = []
    offset = 0.0
    for keep in keeps:
        offsets.append((keep.start, keep.end, offset))
        offset += keep.duration

    out: list[tuple[float, Any]] = []
    for item in items:
        span = _bounds(item)
        if span is None:
            continue
        start, end = span
        mid = (start + end) / 2.0
        for keep_start, keep_end, new_offset in offsets:
            if keep_start <= mid < keep_end:
                new_start = new_offset + (max(start, keep_start) - keep_start)
                new_end = new_offset + (min(max(start, end), keep_end) - keep_start)
                out.append(
                    (new_start, build(item, round(new_start, 3), round(max(new_start, new_end), 3)))
                )
                # Deliberately no `break`: a retained cold open puts the same source range in the
                # keep list twice, and both occurrences are heard.
    out.sort(key=lambda pair: pair[0])
    return [built for _at, built in out]


# --------------------------------------------------------------------------- #
# The three consumers R2.5 names. One working does not imply the others.      #
# --------------------------------------------------------------------------- #


def rebase_words(words: Sequence[Any], keeps: Sequence[Interval]) -> list[Any]:
    """Words on the assembled timeline (R2.5).

    Signature-compatible with `filler.rebase_words` so the pipeline can choose between them at the
    call site, and returns `worker.transcribe.Word` objects for the same reason: every downstream
    caption path is typed against them.
    """
    from worker.transcribe import Word

    def build(word: Any, start: float, end: float) -> Word:
        return Word(
            start=start,
            end=end,
            text=getattr(word, "text", ""),
            probability=getattr(word, "probability", 1.0),
        )

    return rebase_onto(words, keeps, build=build)


def rebase_emoji(cues: Sequence[Any], keeps: Sequence[Interval]) -> list[Any]:
    """Emoji cues on the assembled timeline (R2.5).

    `dataclasses.replace` rather than a constructor call, because `EmojiCue` carries fields this module
    has no business knowing about -- naming them here would silently drop whichever one is added next.

    **Not on the pipeline's path, and that is correct rather than an oversight.** R2.5 names emoji
    cues as one of three consumers that must follow an assembly, and the other two — words
    (`rebase_words`) and speaker turns (`rebase_turns`) — are wired at their call sites because they
    are produced *before* the keep list is applied. Emoji cues are not: `effects.emoji.plan_emoji`
    is called from the compositor with the already-rebased clip-relative words, so its cues are
    born on the delivered timeline and there is nothing to remap.

    Kept, rather than deleted as dead code, because the requirement names it and because the
    invariant it depends on ("cues are planned after the rebase, never before") is a property of
    the *caller*, not of this module. If any path ever plans cues against source-relative words,
    this is what it must use -- and a reader who finds no rebaser here would reasonably conclude
    none is needed.
    """
    from dataclasses import replace as dataclass_replace

    def build(cue: Any, start: float, end: float) -> Any:
        try:
            return dataclass_replace(cue, start=start, end=end)
        except TypeError:
            return cue

    return rebase_onto(cues, keeps, build=build)


def rebase_turns(turns: Sequence[Any], keeps: Sequence[Interval]) -> list[Any]:
    """Speaker turns on the assembled timeline (R2.5).

    Turns are the consumer most likely to be forgotten, and the one whose failure is quietest: a
    mis-rebased turn does not change a single pixel, it just points the reframe crop and the AU12 gain
    at the wrong person for part of the clip.
    """
    from worker.diarization import Speaker_Turn

    def build(turn: Any, start: float, end: float) -> Speaker_Turn:
        return Speaker_Turn(getattr(turn, "speaker_label", ""), start, end)

    return rebase_onto(turns, keeps, build=build)
