"""Kinetic typography — the compositor caption-ownership handoff (spec tasks 12.2, 12.3).

Task 12.2 lands **Property 3** ("caption text is rendered by exactly one producer") and
task 12.3 the engine-owned-path unit tests (spy assertions on the suppressed v0.8.0
producers, plus the ``effects_applied`` marker spellings and the compositor/host division
of labour).

Everything here is **offline and ffmpeg-free**: ``compositor.probe`` is stubbed with a
fixed :class:`~worker.ffmpeg_utils.MediaInfo` and ``compositor._run`` is replaced by a
recording spy that only writes a placeholder byte-string at the destination, so the
assertions are on the argv / ``-filter_complex`` string the compositor builds and on the
number of times it would have invoked ffmpeg. No ffmpeg process is ever created.

Shared test utilities are **reused, not forked**: the option/word generators come from
``tests/strategies.py`` (:func:`st_options_mapping`, :func:`st_word_timeline`,
:func:`st_engine_outcomes`), the word double from ``tests/conftest.py`` (``FakeWord``) and
the engine/capability doubles from ``tests/fakes.py`` (``FakeEngine``, ``StaticProber``,
``FakeClock``). Nothing was added to either module for this task.

How "the compositor built its own caption ASS" is observed
---------------------------------------------------------
Two independent observations, asserted to agree on every example:

* the **call spy** — ``captions.build_ass`` (the only writer of the compositor's own ASS),
  ``captions.words_to_cues`` and ``caption_presets.plan_keywords`` are wrapped in
  recording spies that call through, so the v0.8.0 ladder still behaves exactly as it
  does in production on the paths where it is supposed to run;
* the **file on disk** — the compositor writes its ASS to
  ``temp_dir/<base-clip-stem>.ass``, so its absence is direct evidence that no caption
  document was produced by the compositor.

Guarding against vacuous passes
-------------------------------
Property 3 runs **two renders per example** — the drawn outcome and the flag-disabled
baseline for the same input — and asserts the *counterfactual* as well as the claim: on
every example where a kinetic contribution is supplied, the baseline leg is asserted to
have really called ``build_ass`` and really written its own ``.ass`` file, so "the engine
path called nothing" can never pass because the spy was broken or because captions were
switched off. The six outcome kinds (``applied`` / ``skipped`` / ``degraded`` / ``failed``
/ ``timeout`` / flag-disabled) are each pinned by an ``@example`` so all six are exercised
on every run regardless of what the random draws happen to cover, and the ``applied``
examples additionally assert a positive fact (the engine's ASS path is in the graph, one
``subtitles=`` filter, one ffmpeg pass).
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Optional, Sequence
from unittest import mock

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from tests.conftest import FakeWord
from tests.fakes import FakeClock, FakeEngine, StaticProber
from tests.strategies import (
    st_engine_outcomes,
    st_options_mapping,
    st_word_timeline,
)
from worker.effects import caption_presets, compositor
from worker.engines.base import (
    Compose_Contribution,
    Engine_Result,
    Engine_Stage,
    Engine_Status,
    marker,
)
from worker.engines.capabilities import Capability_Report
from worker.engines.host import Engine_Host
from worker.engines.kinetic import ASS_NAME, ENGINE_ID, KINETIC_Z_ORDER
from worker.engines.registry import Engine_Registry
from worker.ffmpeg_utils import MediaInfo
from worker.models import ProcessingOptions

#: The clip every render in this module composites: 1080x1920, 3 s, with audio. Fixed so
#: the geometry cannot vary between the two legs of a comparison.
MEDIA_INFO = MediaInfo(duration=3.0, width=1080, height=1920, fps=30.0, has_audio=True)

#: A fully-timed reference Word_Timeline for the deterministic unit tests.
REFERENCE_WORDS = [
    FakeWord(0.2, 0.6, "THIS"),
    FakeWord(0.7, 1.1, "CHANGED"),
    FakeWord(1.2, 1.7, "EVERYTHING"),
]

#: The five Engine_Status outcomes Property 3 quantifies over, plus the flag-disabled
#: caller. ``timeout`` is not an ``Engine_Status`` member: the Engine_Host converts a
#: budget overrun into a ``failed`` result carrying exactly one
#: ``engine:kinetic_typography:timeout`` marker (``Engine_Host._failure``), which is the
#: shape modelled below.
OUTCOME_KINDS = ("applied", "skipped", "degraded", "failed", "timeout", "disabled")


# --------------------------------------------------------------------------- #
# Offline render harness                                                        #
# --------------------------------------------------------------------------- #
@dataclass
class Leg:
    """What one ``render_clip`` invocation did, observed without running ffmpeg."""

    result: Any
    commands: list = field(default_factory=list)
    build_ass_calls: list = field(default_factory=list)
    words_to_cues_calls: list = field(default_factory=list)
    plan_keywords_calls: list = field(default_factory=list)
    own_ass: Optional[Path] = None

    @property
    def passes(self) -> int:
        """The number of ffmpeg invocations the compositor would have performed."""
        return len(self.commands)

    @property
    def graph(self) -> str:
        """The ``-filter_complex`` string of the single pass (``""`` when none)."""
        for cmd in self.commands:
            if "-filter_complex" in cmd:
                return cmd[cmd.index("-filter_complex") + 1]
        return ""

    @property
    def subtitles_filters(self) -> int:
        """How many libass ``subtitles=`` filter instances the graph carries (Req 2.6)."""
        return self.graph.count("subtitles=")

    @property
    def built_own_ass(self) -> bool:
        """Whether the compositor generated a caption ASS of its own (Reqs 3.2, 3.9)."""
        return bool(self.build_ass_calls)

    @property
    def own_ass_written(self) -> bool:
        """Whether ``temp_dir/<stem>.ass`` — the compositor's own document — exists."""
        return self.own_ass is not None and self.own_ass.exists()

    @property
    def effects_applied(self) -> list:
        return list(self.result.effects_applied) if self.result is not None else []


def _render_leg(
    *,
    root: Path,
    name: str,
    options: ProcessingOptions,
    words: Sequence[Any],
    hook_text: str,
    contributions: Optional[Sequence[Compose_Contribution]],
    raise_in_producers: bool = False,
) -> Leg:
    """Render one clip offline into ``root/name`` and record what was called.

    ``probe``/``_run`` are stubbed so no ffmpeg process is created. The three v0.8.0
    caption producers are wrapped in recording spies; with
    ``raise_in_producers`` they record **and raise**, so a single call fails the test
    instead of being counted afterwards (task 12.3's "monkeypatched to raise").
    """
    work = root / name
    work.mkdir(parents=True, exist_ok=True)
    base = root / "base.mp4"
    if not base.exists():
        base.write_bytes(b"stub-clip")

    leg = Leg(result=None, own_ass=work / f"{base.stem}.ass")
    real_build_ass = compositor.cap.build_ass
    real_words_to_cues = compositor.cap.words_to_cues
    real_plan_keywords = caption_presets.plan_keywords

    def spy_run(cmd):
        leg.commands.append([str(part) for part in cmd])
        Path(str(cmd[-1])).write_bytes(b"stub-render")
        return None

    def spy_build_ass(*args, **kwargs):
        leg.build_ass_calls.append((args, kwargs))
        if raise_in_producers:
            raise AssertionError("captions.build_ass must not run when the engine owns captions")
        return real_build_ass(*args, **kwargs)

    def spy_words_to_cues(*args, **kwargs):
        leg.words_to_cues_calls.append((args, kwargs))
        if raise_in_producers:
            raise AssertionError("captions.words_to_cues must not run when the engine owns captions")
        return real_words_to_cues(*args, **kwargs)

    def spy_plan_keywords(*args, **kwargs):
        leg.plan_keywords_calls.append((args, kwargs))
        if raise_in_producers:
            raise AssertionError("caption_presets.plan_keywords must not run when the engine owns captions")
        return real_plan_keywords(*args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(compositor, "probe", lambda path: MEDIA_INFO))
        stack.enter_context(mock.patch.object(compositor, "_run", spy_run))
        stack.enter_context(mock.patch.object(compositor.cap, "build_ass", spy_build_ass))
        stack.enter_context(
            mock.patch.object(compositor.cap, "words_to_cues", spy_words_to_cues)
        )
        stack.enter_context(
            mock.patch.object(caption_presets, "plan_keywords", spy_plan_keywords)
        )
        leg.result = compositor.render_clip(
            base,
            work / "out.mp4",
            options,
            list(words),
            work,
            hook_text=hook_text,
            engine_contributions=contributions,
        )
    return leg


def _kinetic_ass(root: Path) -> Path:
    """The ASS document an ``applied`` Kinetic_Engine would have written."""
    path = root / ASS_NAME
    if not path.exists():
        path.write_text("[Script Info]\nPlayResX: 1080\n", encoding="utf-8")
    return path


def _kinetic_contribution(root: Path) -> Compose_Contribution:
    """The shipped shape of the engine's Compose_Contribution (Reqs 2.1, 2.3, 2.4)."""
    return Compose_Contribution(
        engine_id=ENGINE_ID,
        subtitle_path=_kinetic_ass(root),
        z_order=KINETIC_Z_ORDER,
    )


def _outcome_result(kind: str, root: Path, *, markers=(), detail: str = "") -> Engine_Result:
    """The real ``Engine_Result`` the host would hand on for ``kind``.

    Built through the foundation's own constructors precisely so the "only ``applied``
    carries a contribution" half of the biconditional is *observed* rather than assumed:
    ``skipped``/``degraded``/``failed`` leave ``contribution`` as ``None`` by
    construction, and the host's budget-overrun path (``timeout``) is a ``failed`` result
    carrying one ``engine:<id>:timeout`` marker and nothing else.
    """
    if kind == "applied":
        return Engine_Result(
            engine_id=ENGINE_ID,
            status=Engine_Status.APPLIED,
            markers=markers,
            contribution=_kinetic_contribution(root),
        )
    if kind == "skipped":
        return Engine_Result.skipped(ENGINE_ID)
    if kind == "degraded":
        return Engine_Result.degraded(ENGINE_ID, detail or "font:Impact", markers=markers)
    if kind == "failed":
        return Engine_Result.failed(ENGINE_ID, detail or "emitter raised")
    if kind == "timeout":
        return Engine_Result(
            engine_id=ENGINE_ID,
            status=Engine_Status.FAILED,
            markers=(marker(ENGINE_ID, "timeout"),),
            detail="timeout",
        )
    raise AssertionError(f"unreachable outcome kind {kind!r}")


# --------------------------------------------------------------------------- #
# Generators (local composition of the shared ones — nothing forked)            #
# --------------------------------------------------------------------------- #
@st.composite
def st_caption_option_fields(draw):
    """The Processing_Options fields the caption/hook decision actually reads.

    Drawn on top of the hostile :func:`st_options_mapping` noise (applied first, so these
    always win), with the effect fields that would pull in a resolver — ``music``,
    ``emoji``, ``broll`` — pinned off so every render stays offline and deterministic.
    The look flags are drawn, so the "exactly one ``subtitles=``" claim is made against a
    filter graph that really does carry other filters.
    """
    return {
        "captions": draw(st.booleans()),
        "hook_title": draw(st.booleans()),
        "caption_preset": draw(
            st.sampled_from(sorted(caption_presets.BUILTIN_PRESETS))
        ),
        "caption_animation": draw(st.sampled_from(["", "none", "pop", "typewriter"])),
        "caption_keyword_highlight": draw(st.booleans()),
        "caption_keyword_ai": False,          # never call an LLM (Req 15.1)
        "caption_emoji": draw(st.booleans()),
        "caption_template": draw(st.sampled_from(["karaoke", "boxed", "minimal"])),
        "caption_position": draw(st.sampled_from(["", "bottom", "center", "top"])),
        "permissibility_mode": draw(st.booleans()),
        "color": draw(st.sampled_from(["", "vivid"])),
        "zoom": draw(st.booleans()),
        "fades": draw(st.booleans()),
        "progress_bar": draw(st.booleans()),
        "music": "",
        "emoji": "off",
        "broll": False,
    }


@st.composite
def st_kinetic_outcome(draw):
    """One Engine_Status outcome of the Kinetic_Engine, plus the flag-disabled caller.

    ``kind`` is drawn uniformly over :data:`OUTCOME_KINDS` so each of the five outcomes
    (and the flag-off caller) is reached in roughly a sixth of the examples; the marker /
    detail payloads come from the foundation's :func:`st_engine_outcomes`, so the results
    handed to the compositor carry realistic, sometimes-duplicated marker text.
    ``drop_words`` models the empty rebased Word_Timeline of Req 3.5.
    """
    payload = draw(
        st_engine_outcomes(engine_id=ENGINE_ID, allow_exception=False, max_artifacts=0)
    )
    return {
        "kind": draw(st.sampled_from(OUTCOME_KINDS)),
        "markers": payload["markers"],
        "detail": payload["detail"],
        "drop_words": draw(st.booleans()),
    }


def _options(noise: dict, fields: dict, *, captions: bool) -> ProcessingOptions:
    """A real ``ProcessingOptions`` built from hostile noise plus the drawn fields.

    ``from_dict`` ignores unknown keys and coerces malformed known ones, so the noise
    proves the caption-ownership decision is insensitive to every option the compositor
    does not read.
    """
    data = dict(noise)
    data.update(fields)
    data["captions"] = bool(captions)
    return ProcessingOptions.from_dict(data)


_PINNED_TIMELINE = ([FakeWord(0.2, 0.6, "THIS"), FakeWord(0.7, 1.2, "CHANGED")], 3.0)
_PINNED_FIELDS = {
    "captions": True,
    "hook_title": True,
    "caption_preset": "hormozi",
    "caption_animation": "",
    "caption_keyword_highlight": True,
    "caption_keyword_ai": False,
    "caption_emoji": True,
    "caption_template": "karaoke",
    "caption_position": "",
    "permissibility_mode": False,
    "color": "vivid",
    "zoom": True,
    "fades": True,
    "progress_bar": True,
    "music": "",
    "emoji": "off",
    "broll": False,
}


def _pinned_outcome(kind: str) -> dict:
    return {"kind": kind, "markers": (), "detail": "", "drop_words": False}


# --------------------------------------------------------------------------- #
# Property 3 (task 12.2)                                                        #
# --------------------------------------------------------------------------- #
# Feature: kinetic-typography, Property 3: Caption text is rendered by exactly one
# producer — for any Processing_Options value, Word_Timeline, and Engine_Status outcome
# (including the flag disabled), ``render_clip`` builds its own caption ASS **iff** no
# ``kinetic_typography`` contribution with a ``subtitle_path`` was supplied, the resulting
# filter graph contains exactly one ``subtitles=`` filter when captions or a hook are
# wanted, and the ffmpeg pass count equals the flag-disabled pass count for the same
# input.
@settings(max_examples=100, deadline=None)
@given(
    timeline=st_word_timeline(),
    noise=st_options_mapping(),
    outcome=st_kinetic_outcome(),
    fields=st_caption_option_fields(),
    hook_text=st.sampled_from(["", "   ", "WAIT FOR IT", "watch this 🎬"]),
)
@example(timeline=_PINNED_TIMELINE, noise={}, outcome=_pinned_outcome("applied"),
         fields=dict(_PINNED_FIELDS), hook_text="WAIT FOR IT")
@example(timeline=_PINNED_TIMELINE, noise={}, outcome=_pinned_outcome("skipped"),
         fields=dict(_PINNED_FIELDS), hook_text="WAIT FOR IT")
@example(timeline=_PINNED_TIMELINE, noise={}, outcome=_pinned_outcome("degraded"),
         fields=dict(_PINNED_FIELDS), hook_text="WAIT FOR IT")
@example(timeline=_PINNED_TIMELINE, noise={}, outcome=_pinned_outcome("failed"),
         fields=dict(_PINNED_FIELDS), hook_text="WAIT FOR IT")
@example(timeline=_PINNED_TIMELINE, noise={}, outcome=_pinned_outcome("timeout"),
         fields=dict(_PINNED_FIELDS), hook_text="WAIT FOR IT")
@example(timeline=_PINNED_TIMELINE, noise={}, outcome=_pinned_outcome("disabled"),
         fields=dict(_PINNED_FIELDS), hook_text="WAIT FOR IT")
def test_p3_caption_text_is_rendered_by_exactly_one_producer(
    timeline, noise, outcome, fields, hook_text
):
    """**Validates: Requirements 2.5, 2.6, 3.1, 3.2, 3.6, 3.9, 13.2, 14.2, 19.6**

    Two renders per example — the drawn outcome and the flag-disabled baseline for the
    same input — with the three v0.8.0 caption producers spied on and the compositor's own
    ``<stem>.ass`` checked on disk.

    Two recorded judgement calls, neither of which narrows a clause:

    * ``captions`` is forced **on** exactly when the drawn outcome is ``applied``, and the
      Word_Timeline is never emptied for that outcome, because Reqs 3.4 / 3.5 make an
      ``applied`` result unreachable with captions disabled or no words: the engine
      returns ``skipped``. Every other outcome sees the freely drawn ``captions`` /
      ``hook_title`` / empty-timeline combinations, and the flag-disabled baseline is
      rendered for every drawn option value without exception.
    * the biconditional is asserted as
      ``built_own_ass <=> (no kinetic contribution) and (captions or a hook are wanted)``.
      Taken without the second conjunct the clause is false in the degenerate region where
      nothing is wanted — with ``captions=False``, ``hook_title=False`` and no contribution
      the compositor builds no ASS, which is v0.8.0 behaviour this spec does not change
      (the counter-example is recorded in the task notes). The "when captions or a hook are
      wanted" qualifier of the same design sentence is therefore read as governing the
      whole conjunction, and the degenerate region is asserted **more** strongly instead:
      *neither* producer renders caption text there.
    """
    words, _duration = timeline
    kind = outcome["kind"]
    if kind != "applied" and outcome["drop_words"]:
        words = []                      # Req 3.5 — an empty rebased Word_Timeline
    options = _options(
        noise, fields, captions=True if kind == "applied" else fields["captions"]
    )

    with TemporaryDirectory(prefix="kinetic-p3-") as tmp:
        root = Path(tmp)
        contributions: Optional[list]
        if kind == "disabled":
            # Exactly what every v0.8.0 caller and every flag-off run passes.
            contributions = None
        else:
            result = _outcome_result(
                kind, root, markers=outcome["markers"], detail=outcome["detail"]
            )
            if kind != "applied":
                # Observed, not assumed: the non-applied constructors carry no
                # contribution at all, which is why one check covers Reqs 3.5/3.6/13.2/14.2.
                assert result.contribution is None
            contributions = [
                r.contribution for r in (result,) if r.contribution is not None
            ]

        supplied = any(
            c.engine_id == ENGINE_ID and c.subtitle_path is not None
            for c in (contributions or ())
        )
        assert supplied == (kind == "applied")

        engine_leg = _render_leg(
            root=root, name="engine", options=options, words=words,
            hook_text=hook_text, contributions=contributions,
        )
        baseline_leg = _render_leg(
            root=root, name="baseline", options=options, words=words,
            hook_text=hook_text, contributions=None,
        )

        wanted_caps = bool(options.captions) and bool(words)
        wanted_hook = bool(options.hook_title) and bool(hook_text.strip())
        wanted = wanted_caps or wanted_hook

        # --- clause 1: exactly one producer of caption text ------------------
        assert engine_leg.built_own_ass is ((not supplied) and wanted)
        assert engine_leg.own_ass_written is engine_leg.built_own_ass
        # The flag-disabled leg never receives a contribution, so it owns the captions
        # whenever any are wanted (Req 3.1) — the unchanged v0.8.0 behaviour.
        assert baseline_leg.built_own_ass is wanted
        assert baseline_leg.own_ass_written is wanted
        if wanted:
            assert engine_leg.built_own_ass is not supplied
        else:
            assert not engine_leg.built_own_ass and not baseline_leg.built_own_ass

        # --- clause 2: exactly one libass instance when text is wanted -------
        expected_filters = 1 if wanted else 0
        assert engine_leg.subtitles_filters == expected_filters
        assert baseline_leg.subtitles_filters == expected_filters

        # --- clause 3: pass-count parity with the flag-disabled render -------
        assert engine_leg.passes == baseline_leg.passes
        if wanted:
            assert engine_leg.passes == 1

        # --- non-vacuity: the owned path is positively exercised -------------
        if supplied:
            assert wanted                       # the applied outcome always wants text
            # The suppression really is total: no cue building, no keyword planning
            # (hence no duplicate LLM call), no ASS write (Reqs 3.2, 3.6).
            assert engine_leg.words_to_cues_calls == []
            assert engine_leg.plan_keywords_calls == []
            assert not engine_leg.own_ass_written
            # The engine's document is the one that reached the single Subtitle_Slot.
            assert compositor.cap.subtitles_filter(_kinetic_ass(root)) in engine_leg.graph
            # Counterfactual: the very same input, rendered with the flag disabled,
            # provably *did* build its own ASS — so the assertions above cannot pass
            # because the spies or the caption options were inert.
            assert baseline_leg.build_ass_calls != []
            assert baseline_leg.own_ass_written
            assert "captions" in engine_leg.effects_applied or not wanted_caps


# --------------------------------------------------------------------------- #
# Task 12.3 — engine-owned path spies and marker spellings                      #
# --------------------------------------------------------------------------- #
def _owned_options(**overrides) -> ProcessingOptions:
    """Captions + hook on, with the preset/feature fields overridable per test."""
    data = {
        "captions": True,
        "hook_title": True,
        "caption_preset": "hormozi",
        "caption_keyword_highlight": True,
        "caption_emoji": True,
        "music": "",
        "emoji": "off",
    }
    data.update(overrides)
    return ProcessingOptions.from_dict(data)


def test_engine_owned_captions_never_reach_the_v080_producers(tmp_path):
    """Validates: Requirements 3.2, 3.6, 2.5, 2.6

    With a kinetic contribution present, ``caption_presets.plan_keywords`` and
    ``captions.build_ass`` are monkeypatched to **raise** — so a single call fails the
    render outright — and are then proven never to have been called at all. The engine's
    own ASS is the one document in the graph, in one ffmpeg pass.
    """
    leg = _render_leg(
        root=tmp_path, name="owned", options=_owned_options(),
        words=REFERENCE_WORDS, hook_text="WAIT FOR IT",
        contributions=[_kinetic_contribution(tmp_path)],
        raise_in_producers=True,
    )

    assert leg.result is not None                       # the render still succeeded
    assert leg.build_ass_calls == []                    # proven never called
    assert leg.plan_keywords_calls == []
    assert leg.words_to_cues_calls == []
    assert not leg.own_ass_written                      # no <stem>.ass was produced
    assert leg.passes == 1                              # Req 2.5
    assert leg.subtitles_filters == 1                   # Req 2.6
    assert compositor.cap.subtitles_filter(_kinetic_ass(tmp_path)) in leg.graph

    # Counterfactual on the same input: with no contribution the raising doubles ARE
    # reached — the render blows up on the first of them — which is what proves the
    # assertions above are not vacuous. The same doubles, the same options, the same
    # words: the only difference is the contribution.
    with pytest.raises(AssertionError, match="must not run when the engine owns captions"):
        _render_leg(
            root=tmp_path, name="legacy", options=_owned_options(),
            words=REFERENCE_WORDS, hook_text="WAIT FOR IT", contributions=None,
            raise_in_producers=True,
        )


def test_engine_owned_effects_applied_marker_spellings(tmp_path):
    """Validates: Requirements 3.7, 3.8

    The v0.8.0 spellings are all still recorded, in the v0.8.0 emission order, and the two
    feature markers appear exactly when the engine's planner really performs them (the
    option is on **and** the Base_Preset enables the feature).
    """
    # ``hormozi`` enables both keyword highlighting and inline emoji.
    both = _render_leg(
        root=tmp_path, name="hormozi", options=_owned_options(caption_preset="hormozi"),
        words=REFERENCE_WORDS, hook_text="WAIT FOR IT",
        contributions=[_kinetic_contribution(tmp_path)],
    )
    assert both.effects_applied == [
        "caption_preset:hormozi",
        "keyword_highlight",
        "caption_emoji",
        "captions",
        "hook_title",
    ]

    # ``karaoke`` enables neither, so the two feature markers must NOT be recorded even
    # though both options are on — the engine performs no emphasis/emoji there, and
    # emitting them would over-report versus an identical v0.8.0 run.
    neither = _render_leg(
        root=tmp_path, name="karaoke", options=_owned_options(caption_preset="karaoke"),
        words=REFERENCE_WORDS, hook_text="WAIT FOR IT",
        contributions=[_kinetic_contribution(tmp_path)],
    )
    assert neither.effects_applied == [
        "caption_preset:karaoke",
        "captions",
        "hook_title",
    ]

    # ``pop`` enables keyword highlighting only, and the emoji option is off: exactly one
    # of the two "as applicable" markers.
    highlight_only = _render_leg(
        root=tmp_path, name="pop",
        options=_owned_options(caption_preset="pop", caption_emoji=False),
        words=REFERENCE_WORDS, hook_text="WAIT FOR IT",
        contributions=[_kinetic_contribution(tmp_path)],
    )
    assert highlight_only.effects_applied == [
        "caption_preset:pop",
        "keyword_highlight",
        "captions",
        "hook_title",
    ]

    # The hook marker is gated on the hook, not on ownership: no hook text, no marker.
    no_hook = _render_leg(
        root=tmp_path, name="nohook", options=_owned_options(caption_preset="hormozi"),
        words=REFERENCE_WORDS, hook_text="   ",
        contributions=[_kinetic_contribution(tmp_path)],
    )
    assert "hook_title" not in no_hook.effects_applied
    assert "captions" in no_hook.effects_applied


def test_engine_markers_are_appended_by_the_host_not_the_compositor(tmp_path):
    """Validates: Requirements 3.7

    ``engine:kinetic_typography:style:<style>`` and
    ``engine:kinetic_typography:supersedes_captions`` are the Engine_Host's to record: the
    compositor's ``effects_applied`` carries no ``engine:`` marker at all, while the host
    merges exactly those two for the same applied outcome. The two marker sets are
    disjoint, so neither surface duplicates the other.
    """
    contribution = _kinetic_contribution(tmp_path)
    engine_markers = (
        marker(ENGINE_ID, "style:karaoke_fill"),
        marker(ENGINE_ID, "supersedes_captions"),
    )

    leg = _render_leg(
        root=tmp_path, name="owned", options=_owned_options(),
        words=REFERENCE_WORDS, hook_text="WAIT FOR IT",
        contributions=[contribution],
    )
    assert leg.result is not None
    assert [m for m in leg.effects_applied if m.startswith("engine:")] == []

    # The same outcome routed through the real host: the markers land there instead.
    engine = FakeEngine(
        ENGINE_ID,
        Engine_Stage.COMPOSE,
        status=Engine_Status.APPLIED,
        markers=engine_markers,
        contribution=contribution,
    )
    registry = Engine_Registry()
    registry.register(engine)
    host = Engine_Host(
        SimpleNamespace(kinetic_typography_enabled=True, permissibility_mode=False),
        job_id="job-kinetic-compositor",
        temp_dir=tmp_path / "host",
        registry=registry,
        capabilities=Capability_Report(StaticProber({})),
        clock=FakeClock(),
    )
    outcome = host.run_stage(
        Engine_Stage.COMPOSE,
        clip_id="01_abc123",
        source=tmp_path / "src.mp4",
        clip_path=tmp_path / "base.mp4",
        clip_start=0.0,
        clip_end=3.0,
        duration=3.0,
        words=REFERENCE_WORDS,
        clip_metadata={"hook_text": "WAIT FOR IT", "clip_size": (1080, 1920)},
    )

    assert list(outcome.markers) == list(engine_markers)
    assert [c.engine_id for c in outcome.contributions] == [ENGINE_ID]
    assert set(outcome.markers).isdisjoint(leg.effects_applied)
