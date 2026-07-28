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

import ast
import dataclasses
import importlib.util
import inspect
import sys
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
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
from worker import captions as cap_module
from worker.effects import caption_presets, compositor, overlays
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



# =========================================================================== #
# Epic 15 — the flag-off backward-compatibility parity gate (tasks 15.1, 15.2) #
# =========================================================================== #
# This is the spec's central guarantee (Req 19): with ``kinetic_typography_enabled``
# off and no compose contributions, nothing about a render may differ from v0.8.0.
# Neither sub-task is optional, and neither derives its expectation from the engine.
#
# How the baseline avoids being circular
# --------------------------------------
# "Compare ``render_clip`` against ``render_clip``" would prove nothing, so the
# expectation is triangulated from three *independent* sources, and every case in the
# matrix is checked against all three:
#
#   A. an **independent reference oracle** (:func:`_v080_expectation`) that recomputes
#      the expected ``effects_applied``, ``-filter_complex`` and caption ASS from the
#      v0.8.0 primitives this spec never touched — ``worker.captions`` (``words_to_cues``,
#      ``build_ass``, ``subtitles_filter``), ``worker.effects.caption_presets``
#      (``resolve_preset``, ``plan_keywords``) and ``worker.effects.overlays``
#      (``build_video_chain``, ``progress_bar_filter``) — plus the documented v0.8.0
#      assembly rule. It never calls the one file this spec changed
#      (``worker/effects/compositor.py``), so agreement is a genuine cross-check rather
#      than a tautology. Task 15.2 pins those primitives against drift with fixed
#      expected outputs, which is what keeps this oracle trustworthy over time.
#   B. a **reconstructed v0.8.0 compositor** (:func:`_v080_module`) in which the feature
#      is *physically absent*: the shipped module's AST is rewritten to delete
#      ``KINETIC_ENGINE_ID``, ``_kinetic_subtitle_path``, the ``kinetic_ass`` /
#      ``engine_owns_captions`` bindings and the whole ownership branch (turning its
#      ``elif`` back into the v0.8.0 ``if``), and the result is executed as its own
#      module. Every removal is asserted to have actually matched, the rewritten tree is
#      asserted to contain none of those identifiers, and
#      :func:`test_the_reconstructed_v080_baseline_really_lacks_the_engine` proves the
#      reconstruction genuinely lost the behaviour (handed a kinetic contribution it
#      still builds its own ASS). Because both legs render into the **same** working
#      directory, the comparison is over the complete ffmpeg **argv**, not just the
#      filter graph.
#   C. **frozen literal goldens** for four canonical cases — the exact
#      ``effects_applied`` list and the exact ``-filter_complex`` string (with only the
#      absolute ASS path normalised to ``<ASS>``), written out in full and generated
#      from source B, i.e. from code with the kinetic feature deleted. These are what
#      catch a drift that happened to move oracle and compositor together.
#
# Everything stays offline: ``probe`` is stubbed, ``_run`` is a recording spy, and no
# ffmpeg process is created.

_V080_IDENTIFIERS = (
    "KINETIC_ENGINE_ID",
    "_kinetic_subtitle_path",
    "kinetic_ass",
    "engine_owns_captions",
)

#: What :func:`_v080_source` must remove — asserted, so a silent no-op transform (which
#: would make the whole of source B a comparison of the shipped module with itself) fails
#: loudly instead of passing vacuously.
_EXPECTED_REMOVALS = (
    "KINETIC_ENGINE_ID",
    "_kinetic_subtitle_path",
    "kinetic_ass",
    "engine_owns_captions",
    "if engine_owns_captions:",
)


def _strip_kinetic_statements(statements: list, removed: list) -> list:
    """Drop the kinetic ownership statements from a ``render_clip`` body.

    The ownership branch is an ``if engine_owns_captions: ... elif need_caps or
    need_hook: ...`` chain, so replacing the outer ``If`` with its ``orelse`` restores
    the v0.8.0 ``if need_caps or need_hook:`` ladder exactly.
    """
    out: list = []
    for node in statements:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in ("kinetic_ass", "engine_owns_captions")
            for t in node.targets
        ):
            removed.append(node.targets[0].id)
            continue
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "engine_owns_captions"
        ):
            removed.append("if engine_owns_captions:")
            out.extend(node.orelse)
            continue
        out.append(node)
    return out


def _v080_source() -> str:
    """The shipped compositor with this spec's caption-ownership feature deleted.

    A source-level reconstruction of the v0.8.0 file: task 12.1 is the *only* edit this
    spec made to ``worker/effects/compositor.py``, so removing exactly what it added
    yields the pre-feature module. Raises if any expected removal does not match, so the
    reconstruction can never silently degrade into "the shipped module again".
    """
    tree = ast.parse(Path(compositor.__file__).read_text(encoding="utf-8"))
    removed: list = []
    kept: list = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "KINETIC_ENGINE_ID" for t in node.targets
        ):
            removed.append("KINETIC_ENGINE_ID")
            continue
        if isinstance(node, ast.FunctionDef) and node.name == "_kinetic_subtitle_path":
            removed.append("_kinetic_subtitle_path")
            continue
        if isinstance(node, ast.FunctionDef) and node.name == "render_clip":
            node.body = _strip_kinetic_statements(node.body, removed)
        kept.append(node)
    tree.body = kept

    assert sorted(removed) == sorted(_EXPECTED_REMOVALS), removed
    source = ast.unparse(ast.fix_missing_locations(tree))
    # Identifier-level absence (docstrings may still discuss the feature; code may not).
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            assert node.id not in _V080_IDENTIFIERS, node.id
        if isinstance(node, ast.FunctionDef):
            assert node.name not in _V080_IDENTIFIERS, node.name
    return source


_V080_MODULE: list = []


def _v080_module():
    """The executed v0.8.0-shaped compositor module (built once, then cached)."""
    if not _V080_MODULE:
        source = _v080_source()
        spec = importlib.util.spec_from_loader("tests._v080_compositor", loader=None)
        module = importlib.util.module_from_spec(spec)
        module.__file__ = compositor.__file__
        sys.modules["tests._v080_compositor"] = module
        exec(compile(source, "<v080-compositor>", "exec"), module.__dict__)
        assert hasattr(module, "render_clip")
        assert not hasattr(module, "KINETIC_ENGINE_ID")
        assert not hasattr(module, "_kinetic_subtitle_path")
        _V080_MODULE.append(module)
    return _V080_MODULE[0]


@dataclass
class Render:
    """One ``render_clip`` invocation, observed without running ffmpeg."""

    work: Path
    result: Any = None
    commands: list = field(default_factory=list)

    @property
    def passes(self) -> int:
        return len(self.commands)

    @property
    def argv(self) -> list:
        return list(self.commands[0]) if self.commands else []

    @property
    def graph(self) -> str:
        for cmd in self.commands:
            if "-filter_complex" in cmd:
                return cmd[cmd.index("-filter_complex") + 1]
        return ""

    @property
    def effects_applied(self) -> list:
        return list(self.result.effects_applied) if self.result is not None else []

    @property
    def ass_path(self) -> Path:
        return self.work / "base.ass"

    @property
    def ass_text(self) -> Optional[str]:
        path = self.ass_path
        return path.read_text(encoding="utf-8") if path.exists() else None


def _parity_render(module, work: Path, *, options, words, hook_text, contributions) -> Render:
    """Render ``work/base.mp4`` -> ``work/out.mp4`` through ``module``, offline."""
    work.mkdir(parents=True, exist_ok=True)
    base = work / "base.mp4"
    if not base.exists():
        base.write_bytes(b"stub-clip")

    record = Render(work=work)

    def spy_run(cmd):
        record.commands.append([str(part) for part in cmd])
        Path(str(cmd[-1])).write_bytes(b"stub-render")
        return None

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(module, "probe", lambda path: MEDIA_INFO))
        stack.enter_context(mock.patch.object(module, "_run", spy_run))
        record.result = module.render_clip(
            base,
            work / "out.mp4",
            options,
            list(words),
            work,
            hook_text=hook_text,
            engine_contributions=contributions,
        )
    return record


def _normalised_graph(graph: str, ass_path: Path) -> str:
    """``graph`` with the absolute caption-ASS path replaced by ``<ASS>``."""
    return graph.replace(compositor.cap.subtitles_filter(ass_path), "subtitles='<ASS>'")


# --------------------------------------------------------------------------- #
# Source A — the independent v0.8.0 reference oracle                            #
# --------------------------------------------------------------------------- #
def _v080_expectation(*, options, words, hook_text, ass_path: Path, oracle_ass: Path) -> dict:
    """Recompute the v0.8.0 outcome from the untouched primitives.

    Deliberately written against ``worker.captions`` / ``worker.effects.caption_presets``
    / ``worker.effects.overlays`` and the documented v0.8.0 assembly rule — **not** by
    calling ``worker.effects.compositor``, the one file this spec changed. ``ass_path`` is
    where the compositor is expected to have written its document (so the expected filter
    string carries the right path); ``oracle_ass`` is where this oracle writes its own
    copy for the content comparison.
    """
    info = MEDIA_INFO
    applied: list = []
    subtitles: Optional[str] = None
    ass_text: Optional[str] = None

    need_caps = bool(options.captions) and bool(words)
    need_hook = bool(options.hook_title) and bool(hook_text.strip())

    if need_caps or need_hook:
        cues = cap_module.words_to_cues(words) if need_caps else []
        use_preset = need_caps and (
            options.caption_preset != "karaoke"
            or bool(options.caption_animation)
            or options.caption_keyword_highlight
            or options.caption_emoji
        )
        if use_preset:
            preset, substituted = caption_presets.resolve_preset(options.caption_preset)
            if options.caption_animation:
                preset = replace(preset, animation=options.caption_animation)
            preset = replace(preset, emoji_inline=bool(options.caption_emoji))
            keyword_indices = None
            if options.caption_keyword_highlight:
                flat_words = [w for cue in cues for w in cue.words]
                keyword_indices = caption_presets.plan_keywords(
                    flat_words, use_ai=options.caption_keyword_ai, client=None
                )
            notes: list = []
            cap_module.build_ass(
                cues, oracle_ass,
                video_width=info.width, video_height=info.height,
                preset=preset,
                keyword_indices=keyword_indices,
                position=options.caption_position or None,
                hook_text=hook_text if need_hook else "",
                clip_duration=info.duration,
                permissibility=options.permissibility_mode,
                notes=notes,
            )
            applied.append(f"caption_preset:{preset.name}")
            if substituted:
                applied.append("caption_preset_substituted")
            if keyword_indices is not None:
                applied.append("keyword_highlight")
            if options.caption_emoji:
                applied.append("caption_emoji")
            for note in notes:
                if note not in applied:
                    applied.append(note)
        else:
            cap_module.build_ass(
                cues, oracle_ass,
                video_width=info.width, video_height=info.height,
                template=options.caption_template,
                position=options.caption_position,
                hook_text=hook_text if need_hook else "",
            )
        subtitles = cap_module.subtitles_filter(ass_path)
        if need_caps:
            applied.append("captions")
        if need_hook:
            applied.append("hook_title")
        ass_text = oracle_ass.read_text(encoding="utf-8")

    look_chain = overlays.build_video_chain(
        duration=info.duration, fps=info.fps or 30.0,
        width=info.width, height=info.height,
        color=options.color, zoom=options.zoom, transitions=options.transitions,
        fades=options.fades, progress_bar=False, subtitles=None,
    )
    caption_chain: list = []
    if subtitles:
        caption_chain.append(subtitles)
    if options.progress_bar:
        caption_chain.append(
            overlays.progress_bar_filter(info.duration, info.width, info.height)
        )

    if options.color:
        applied.append(f"color:{options.color}")
    if options.zoom:
        applied.append("zoom")
    if options.transitions:
        applied.append("transitions")
    if options.fades:
        applied.append("fades")
    if options.progress_bar:
        applied.append("progress_bar")

    graph_parts: list = []
    full_chain = look_chain + caption_chain
    if full_chain:
        graph_parts.append(f"[0:v]{','.join(full_chain)}[vbase]")

    audio_changed = False
    if options.fades and info.has_audio:
        out_start = max(0.0, info.duration - 0.4)
        graph_parts.append(
            f"[0:a]afade=t=in:st=0:d=0.400,afade=t=out:st={out_start:.3f}:d=0.400[aout]"
        )
        audio_changed = True

    video_changed = bool(full_chain)
    return {
        "effects_applied": applied,
        "graph": ";".join(graph_parts),
        "ass_text": ass_text,
        "passes": 1 if (video_changed or audio_changed) else 0,
        "renders": video_changed or audio_changed,
    }


# --------------------------------------------------------------------------- #
# The options matrix                                                            #
# --------------------------------------------------------------------------- #
#: Six words: five fit one cue (``max_words=5``), the sixth opens a second one, and
#: ``money`` / ``fire`` are in ``captions._CAPTION_EMOJI`` so the inline-emoji path really
#: emits glyphs. Every word carries ``probability == 1.0``, so ``plan_keywords`` selects a
#: non-trivial subset (``THIS`` is a stopword and is not selected).
MATRIX_WORDS = [
    FakeWord(0.10, 0.50, "THIS"),
    FakeWord(0.60, 1.00, "money"),
    FakeWord(1.10, 1.60, "changed"),
    FakeWord(1.70, 2.20, "everything"),
    FakeWord(2.30, 2.70, "fire"),
    FakeWord(2.80, 2.95, "wow"),
]

MATRIX_HOOK = "you won't believe this"


def _matrix_options(**overrides) -> ProcessingOptions:
    """A flag-off ``ProcessingOptions`` for the parity matrix.

    Built through the constructor rather than ``from_dict`` so an unknown
    ``caption_preset`` survives to the compositor (``from_dict`` coerces it to the known
    default, which would make the ``caption_preset_substituted`` case unreachable). The
    resolver-driven effects are pinned off so every render is offline; the look flags are
    exercised by their own cases below.
    """
    data = dict(
        captions=True,
        hook_title=False,
        caption_template="karaoke",
        caption_position="bottom",
        caption_preset="karaoke",
        caption_animation="",
        caption_keyword_highlight=False,
        caption_keyword_ai=False,
        caption_emoji=False,
        permissibility_mode=False,
        color="",
        zoom=False,
        transitions=False,
        fades=False,
        progress_bar=False,
        music="",
        emoji="off",
        broll=False,
        kinetic_typography_enabled=False,
    )
    data.update(overrides)
    return ProcessingOptions(**data)


def _parity_cases() -> list:
    """A representative flag-off matrix: ``(name, options, words, hook_text)``."""
    cases: list = []

    # captions on/off x hook on/off (legacy default caption path)
    for caps in (True, False):
        for hook in (True, False):
            cases.append((
                f"caps{int(caps)}-hook{int(hook)}",
                _matrix_options(captions=caps, hook_title=hook),
                MATRIX_WORDS,
                MATRIX_HOOK,
            ))

    # every Caption_Preset x keyword highlight on/off x caption emoji on/off
    for name in sorted(caption_presets.BUILTIN_PRESETS):
        for keyword in (False, True):
            for glyphs in (False, True):
                cases.append((
                    f"preset-{name}-kw{int(keyword)}-emoji{int(glyphs)}",
                    _matrix_options(
                        hook_title=True, caption_preset=name,
                        caption_keyword_highlight=keyword, caption_emoji=glyphs,
                    ),
                    MATRIX_WORDS,
                    MATRIX_HOOK,
                ))

    # every caption position, on both the legacy and the preset path
    for position in ("", "bottom", "center", "top"):
        label = position or "default"
        cases.append((
            f"position-{label}-legacy",
            _matrix_options(hook_title=True, caption_position=position),
            MATRIX_WORDS, MATRIX_HOOK,
        ))
        cases.append((
            f"position-{label}-preset",
            _matrix_options(
                hook_title=True, caption_preset="hormozi", caption_position=position,
            ),
            MATRIX_WORDS, MATRIX_HOOK,
        ))

    # the legacy ``caption_template`` path, every template, hook on/off
    for template in ("karaoke", "boxed", "minimal"):
        for hook in (False, True):
            cases.append((
                f"template-{template}-hook{int(hook)}",
                _matrix_options(caption_template=template, hook_title=hook),
                MATRIX_WORDS, MATRIX_HOOK,
            ))

    # explicit animation overrides (each engages the preset path)
    for animation in ("", "none", "pop", "typewriter", "karaoke_fill"):
        cases.append((
            f"animation-{animation or 'default'}",
            _matrix_options(hook_title=True, caption_animation=animation),
            MATRIX_WORDS, MATRIX_HOOK,
        ))

    # an unknown preset name -> ``caption_preset_substituted``
    cases.append((
        "preset-unknown",
        _matrix_options(
            hook_title=True, caption_preset="not-a-preset",
            caption_keyword_highlight=True,
        ),
        MATRIX_WORDS, MATRIX_HOOK,
    ))

    # look-effect combinations, so the caption filter is asserted inside a real chain
    cases.append((
        "look-everything",
        _matrix_options(
            hook_title=True, caption_preset="hormozi",
            caption_keyword_highlight=True, caption_emoji=True,
            color="vivid", zoom=True, transitions=True, fades=True, progress_bar=True,
        ),
        MATRIX_WORDS, MATRIX_HOOK,
    ))
    cases.append((
        "look-audio-fades-only",
        _matrix_options(captions=False, hook_title=False, fades=True),
        MATRIX_WORDS, MATRIX_HOOK,
    ))
    cases.append((
        "look-progress-only",
        _matrix_options(captions=False, hook_title=False, progress_bar=True),
        MATRIX_WORDS, MATRIX_HOOK,
    ))
    cases.append((
        "permissibility-mode",
        _matrix_options(
            hook_title=True, caption_preset="hormozi", caption_emoji=True,
            permissibility_mode=True,
        ),
        MATRIX_WORDS, MATRIX_HOOK,
    ))

    # degenerate inputs: an empty timeline, a blank hook, and nothing enabled at all
    cases.append((
        "empty-timeline",
        _matrix_options(hook_title=True, caption_preset="pop"),
        [], MATRIX_HOOK,
    ))
    cases.append((
        "blank-hook",
        _matrix_options(captions=False, hook_title=True),
        MATRIX_WORDS, "   ",
    ))
    cases.append((
        "all-off",
        _matrix_options(captions=False, hook_title=False),
        MATRIX_WORDS, MATRIX_HOOK,
    ))
    return cases


# --------------------------------------------------------------------------- #
# Task 15.1 — flag-off parity of ``effects_applied`` and the ffmpeg filter graph #
# --------------------------------------------------------------------------- #
def test_flag_off_parity_of_effects_applied_and_the_filter_graph(tmp_path):
    """Validates: Requirements 19.1, 19.4, 19.6

    With ``kinetic_typography_enabled`` off and no compose contributions, every case in
    the matrix must reproduce v0.8.0 exactly: the same ``effects_applied`` list (order
    included), the same ``-filter_complex`` string, the same caption ASS content, the same
    ffmpeg invocation count, and — against the reconstructed module, which renders from
    the same directory so all paths coincide — the same complete ffmpeg argv.

    The two baselines are independent of the shipped compositor (see the module header):
    source A recomputes the expectation from the untouched v0.8.0 primitives, source B is
    the shipped module with the feature deleted from its AST. The flag-off caller shape is
    checked in both of its spellings — ``engine_contributions=None`` (every v0.8.0 caller)
    and ``engine_contributions=[]`` (an enabled host whose engine skipped).
    """
    baseline_module = _v080_module()
    cases = _parity_cases()
    assert len(cases) >= 50                       # a representative matrix, not a token one
    oracle_dir = tmp_path / "oracle"
    oracle_dir.mkdir()
    checked_with_captions = 0

    for name, options, words, hook_text in cases:
        assert options.kinetic_typography_enabled is False
        work = tmp_path / "case" / name
        work.mkdir(parents=True, exist_ok=True)

        # --- the shipped compositor, flag off, no contributions ---------------
        shipped = _parity_render(
            compositor, work, options=options, words=words,
            hook_text=hook_text, contributions=None,
        )
        shipped_ass = shipped.ass_text
        shipped_argv = shipped.argv

        # An empty contribution list is the other flag-off caller shape (an enabled host
        # whose engine returned ``skipped``): it must be indistinguishable from ``None``.
        if shipped.ass_path.exists():
            shipped.ass_path.unlink()
        empty = _parity_render(
            compositor, work, options=options, words=words,
            hook_text=hook_text, contributions=[],
        )
        assert empty.argv == shipped_argv, name
        assert empty.effects_applied == shipped.effects_applied, name
        assert empty.ass_text == shipped_ass, name

        # --- source B: the same code with the feature physically removed ------
        if shipped.ass_path.exists():
            shipped.ass_path.unlink()
        baseline = _parity_render(
            baseline_module, work, options=options, words=words,
            hook_text=hook_text, contributions=None,
        )
        assert baseline.argv == shipped_argv, f"{name}: argv drifted from v0.8.0"
        assert baseline.graph == shipped.graph, name
        assert baseline.effects_applied == shipped.effects_applied, name
        assert baseline.ass_text == shipped_ass, name
        assert baseline.passes == shipped.passes, name
        assert (baseline.result is None) == (shipped.result is None), name

        # --- source A: the independent reference oracle -----------------------
        expected = _v080_expectation(
            options=options, words=words, hook_text=hook_text,
            ass_path=shipped.ass_path, oracle_ass=oracle_dir / f"{name}.ass",
        )
        assert shipped.effects_applied == expected["effects_applied"], name
        assert shipped.graph == expected["graph"], name
        assert shipped.passes == expected["passes"], name
        assert (shipped.result is not None) is expected["renders"], name
        assert shipped_ass == expected["ass_text"], f"{name}: caption ASS drifted"
        # Exactly one libass instance whenever caption text is wanted (Req 2.6), and the
        # invocation count is anchored absolutely so the parity is never a vacuous 0 == 0.
        assert shipped.graph.count("subtitles=") == (1 if expected["ass_text"] else 0), name
        assert shipped.passes == (1 if expected["renders"] else 0), name
        if expected["ass_text"]:
            checked_with_captions += 1

    # Non-vacuity: the great majority of the matrix really did produce a caption document.
    assert checked_with_captions >= 40


#: Source C — literal v0.8.0 goldens, generated from the **reconstructed** module (the one
#: with the caption-ownership feature deleted from its AST) and frozen here verbatim. Only
#: the absolute ASS path is normalised, to ``<ASS>``. ``font_available`` is pinned per case
#: because font substitution is a property of the host, not of the compositor: an absent
#: family appends ``font_substituted:<name>``, so both answers are frozen and both are
#: asserted.
_V080_GOLDENS: dict = {
    "legacy-karaoke-captions-hook": {
        "options": dict(hook_title=True),
        "effects_applied": ["captions", "hook_title"],
        "font_substituted_effects": ["captions", "hook_title"],
        "graph": "[0:v]subtitles='<ASS>'[vbase]",
    },
    "legacy-boxed-captions-only": {
        "options": dict(caption_template="boxed"),
        "effects_applied": ["captions"],
        "font_substituted_effects": ["captions"],
        "graph": "[0:v]subtitles='<ASS>'[vbase]",
    },
    "preset-hormozi-keywords-emoji": {
        "options": dict(
            hook_title=True, caption_preset="hormozi", caption_position="",
            caption_keyword_highlight=True, caption_emoji=True,
        ),
        "effects_applied": [
            "caption_preset:hormozi", "keyword_highlight", "caption_emoji",
            "captions", "hook_title",
        ],
        "font_substituted_effects": [
            "caption_preset:hormozi", "keyword_highlight", "caption_emoji",
            "font_substituted:Arial", "captions", "hook_title",
        ],
        "graph": "[0:v]subtitles='<ASS>'[vbase]",
    },
    "everything-on": {
        "options": dict(
            hook_title=True, caption_preset="pop", caption_keyword_highlight=True,
            color="vivid", zoom=True, transitions=True, fades=True, progress_bar=True,
        ),
        "effects_applied": [
            "caption_preset:pop", "keyword_highlight", "captions", "hook_title",
            "color:vivid", "zoom", "transitions", "fades", "progress_bar",
        ],
        "font_substituted_effects": [
            "caption_preset:pop", "keyword_highlight", "font_substituted:Arial",
            "captions", "hook_title", "color:vivid", "zoom", "transitions", "fades",
            "progress_bar",
        ],
        "graph": (
            "[0:v]eq=contrast=1.12:saturation=1.35:brightness=0.02,"
            "zoompan=z='if(lt(on,15),1.18-(1.18-(1+0.12*on/90))*on/15,"
            "(1+0.12*on/90))':d=1:fps=30:s=1080x1920:x='iw/2-(iw/zoom/2)':"
            "y='ih/2-(ih/zoom/2)',fade=t=in:st=0:d=0.400,"
            "fade=t=out:st=2.600:d=0.400,subtitles='<ASS>',"
            "drawbox=x=0:y=ih-12:w='iw*t/3.000':h=12:color=0x22D3EE@0.9:t=fill[vbase];"
            "[0:a]afade=t=in:st=0:d=0.400,afade=t=out:st=2.600:d=0.400[aout]"
        ),
    },
}


def test_flag_off_graph_matches_the_frozen_v080_goldens(tmp_path):
    """Validates: Requirements 19.1, 19.4, 19.6

    Source C of the parity gate: four canonical flag-off renders asserted against
    ``effects_applied`` lists and ``-filter_complex`` strings frozen in this file, so any
    future drift in the flag-off output is a one-line diff — including a drift that moved
    the reference oracle and the compositor together. Both host font answers are pinned,
    so the goldens hold with or without the preset font installed.
    """
    for name, golden in sorted(_V080_GOLDENS.items()):
        options = _matrix_options(**golden["options"])
        for available, key in ((True, "effects_applied"), (False, "font_substituted_effects")):
            work = tmp_path / f"{name}-font{int(available)}"
            with mock.patch.object(cap_module, "font_available", lambda _n: available):
                record = _parity_render(
                    compositor, work, options=options, words=MATRIX_WORDS,
                    hook_text=MATRIX_HOOK, contributions=None,
                )
            assert record.effects_applied == golden[key], name
            assert _normalised_graph(record.graph, record.ass_path) == golden["graph"], name
            assert record.passes == 1, name
            assert record.graph.count("subtitles=") == 1, name


def test_the_reconstructed_v080_baseline_really_lacks_the_engine(tmp_path):
    """Validates: Requirements 19.1, 19.6

    Non-vacuity for source B: the reconstruction must have genuinely *lost* the
    caption-ownership feature, not merely been rebuilt. Handed the very contribution that
    makes the shipped compositor stand down, the reconstructed module still builds its own
    caption ASS and still routes it into the graph — which is precisely v0.8.0 behaviour,
    and proves the removal was real. The shipped module, on the same input, stands down.
    """
    baseline_module = _v080_module()
    options = _matrix_options(hook_title=True, caption_preset="hormozi")
    contribution = [_kinetic_contribution(tmp_path)]

    baseline = _parity_render(
        baseline_module, tmp_path / "v080", options=options, words=MATRIX_WORDS,
        hook_text=MATRIX_HOOK, contributions=contribution,
    )
    shipped = _parity_render(
        compositor, tmp_path / "shipped", options=options, words=MATRIX_WORDS,
        hook_text=MATRIX_HOOK, contributions=contribution,
    )

    # The v0.8.0-shaped module knows nothing about caption ownership.
    assert baseline.ass_text is not None
    assert baseline.graph.count("subtitles=") == 2       # its own ASS + the contribution's
    assert "captions" in baseline.effects_applied
    # The shipped module stands down for the same contribution (task 12.1's branch).
    assert shipped.ass_text is None
    assert shipped.graph.count("subtitles=") == 1
    # ...and the two therefore differ, which is what makes the flag-off parity above a
    # statement about behaviour rather than about two copies of the same code.
    assert baseline.graph != shipped.graph



# --------------------------------------------------------------------------- #
# Task 15.2 — pin the existing caption symbols against drift                    #
# --------------------------------------------------------------------------- #
# Reqs 19.2/19.3: every ``worker.effects.caption_presets`` value and the behaviour of
# ``captions.build_ass`` / ``build_word_span`` / ``words_to_cues`` / ``subtitles_filter``
# must survive this engine unchanged **for callers that do not use it**. Every expectation
# below is therefore a **fixed literal** — no expectation is computed from
# ``worker.engines.kinetic``, from ``worker.effects.compositor``, or from the symbol it is
# pinning. The engine reuses several of these helpers, so this is also what keeps the
# reference oracle in task 15.1 trustworthy as the engine evolves.

#: Every built-in preset, field by field (``CaptionPreset.to_dict()`` output).
_EXPECTED_BUILTIN_PRESETS: dict = {
    "boxed": {
        "name": "boxed", "animation": "none", "font": "Arial", "font_size": 84,
        "colors": {"primary": "&H00FFFFFF", "highlight": "&H0000E5FF",
                   "outline": "&H00000000", "box": "&H80000000"},
        "position": "bottom", "highlight_keywords": False, "highlight_scale": 1.18,
        "emoji_inline": False, "border_style": 3,
    },
    "hormozi": {
        "name": "hormozi", "animation": "pop", "font": "Arial", "font_size": 96,
        "colors": {"primary": "&H00FFFFFF", "highlight": "&H0000E5FF",
                   "outline": "&H00000000", "box": "&H80000000"},
        "position": "center", "highlight_keywords": True, "highlight_scale": 1.18,
        "emoji_inline": True, "border_style": 1,
    },
    "karaoke": {
        "name": "karaoke", "animation": "karaoke_fill", "font": "Arial", "font_size": 84,
        "colors": {"primary": "&H00FFFFFF", "highlight": "&H0000E5FF",
                   "outline": "&H00000000", "box": "&H80000000"},
        "position": "bottom", "highlight_keywords": False, "highlight_scale": 1.18,
        "emoji_inline": False, "border_style": 1,
    },
    "minimal": {
        "name": "minimal", "animation": "none", "font": "Arial", "font_size": 76,
        "colors": {"primary": "&H00FFFFFF", "highlight": "&H0000E5FF",
                   "outline": "&H00000000", "box": "&H80000000"},
        "position": "bottom", "highlight_keywords": False, "highlight_scale": 1.18,
        "emoji_inline": False, "border_style": 1,
    },
    "pop": {
        "name": "pop", "animation": "pop", "font": "Arial", "font_size": 84,
        "colors": {"primary": "&H00FFFFFF", "highlight": "&H0000E5FF",
                   "outline": "&H00000000", "box": "&H80000000"},
        "position": "bottom", "highlight_keywords": True, "highlight_scale": 1.18,
        "emoji_inline": False, "border_style": 1,
    },
    "typewriter": {
        "name": "typewriter", "animation": "typewriter", "font": "Arial", "font_size": 84,
        "colors": {"primary": "&H00FFFFFF", "highlight": "&H0000E5FF",
                   "outline": "&H00000000", "box": "&H80000000"},
        "position": "bottom", "highlight_keywords": False, "highlight_scale": 1.18,
        "emoji_inline": False, "border_style": 1,
    },
}

#: The three fixed words every pin below uses (``money`` is in the inline-emoji map).
_PIN_WORDS = [
    FakeWord(0.20, 0.60, "THIS"),
    FakeWord(0.70, 1.10, "changed"),
    FakeWord(1.20, 1.70, "money"),
]

#: ``build_word_span(_PIN_WORDS[1], karaoke-with-animation, highlighted, cue_start=0.2)``
#: for the whole 4 x 2 matrix — the v0.8.0 tag shapes, spelled out.
_EXPECTED_WORD_SPANS: dict = {
    ("none", False): "changed",
    ("none", True): (
        "{\\c&H0000E5FF&\\fscx118\\fscy118}changed"
        "{\\c&H00FFFFFF&\\fscx100\\fscy100}"
    ),
    ("pop", False): "{\\fscx60\\fscy60\\t(500,620,\\fscx100\\fscy100)}changed",
    ("pop", True): (
        "{\\c&H0000E5FF&\\fscx118\\fscy118}"
        "{\\fscx60\\fscy60\\t(500,620,\\fscx100\\fscy100)}changed"
        "{\\c&H00FFFFFF&\\fscx100\\fscy100}"
    ),
    ("typewriter", False): "{\\alpha&HFF&\\t(500,530,\\alpha&H00&)}changed",
    ("typewriter", True): (
        "{\\c&H0000E5FF&\\fscx118\\fscy118}"
        "{\\alpha&HFF&\\t(500,530,\\alpha&H00&)}changed"
        "{\\c&H00FFFFFF&\\fscx100\\fscy100}"
    ),
    ("karaoke_fill", False): "{\\kf40}changed",
    ("karaoke_fill", True): (
        "{\\c&H0000E5FF&\\fscx118\\fscy118}{\\kf40}changed"
        "{\\c&H00FFFFFF&\\fscx100\\fscy100}"
    ),
}

_ASS_HEADER = (
    "[Script Info]\n"
    "ScriptType: v4.00+\n"
    "PlayResX: 1080\n"
    "PlayResY: 1920\n"
    "WrapStyle: 2\n"
    "ScaledBorderAndShadow: yes\n"
    "\n"
    "[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour,"
    " BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle,"
    " BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
)
_ASS_HOOK_STYLE = (
    "Style: Hook,Arial,110,&H0000E5FF,&H0000E5FF,&H00000000,&H64000000,"
    "-1,0,0,0,100,100,0,0,1,5,2,8,60,60,160,1\n"
)
_ASS_EVENTS_HEADER = (
    "\n"
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    "\n"
)

#: The full legacy documents ``build_ass`` writes for the three templates (bottom
#: position, 1080x1920, the three fixed words; the karaoke case also carries a hook).
_EXPECTED_LEGACY_DOCUMENTS: dict = {
    "karaoke": (
        _ASS_HEADER
        + "Style: Default,Arial,84,&H00FFFFFF,&H0000FF00,&H00000000,&H64000000,"
          "-1,0,0,0,100,100,0,0,1,4,2,2,80,80,220,1\n"
        + _ASS_HOOK_STYLE
        + _ASS_EVENTS_HEADER
        + "Dialogue: 1,0:00:00.00,0:00:02.50,Hook,,0,0,0,,{\\fad(250,350)}WATCH THIS\n"
        + "Dialogue: 0,0:00:00.20,0:00:01.70,Default,,0,0,0,,"
          "{\\kf40}THIS {\\kf40}changed {\\kf50}money\n"
    ),
    "boxed": (
        _ASS_HEADER
        + "Style: Default,Arial,84,&H00FFFFFF,&H00FFFFFF,&H80000000,&H80000000,"
          "-1,0,0,0,100,100,0,0,3,0,0,2,80,80,220,1\n"
        + _ASS_HOOK_STYLE
        + _ASS_EVENTS_HEADER
        + "Dialogue: 0,0:00:00.20,0:00:01.70,Default,,0,0,0,,THIS changed money\n"
    ),
    "minimal": (
        _ASS_HEADER
        + "Style: Default,Arial,84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
          "-1,0,0,0,100,100,0,0,1,2,1,2,80,80,220,1\n"
        + _ASS_HOOK_STYLE
        + _ASS_EVENTS_HEADER
        + "Dialogue: 0,0:00:00.20,0:00:01.70,Default,,0,0,0,,THIS changed money\n"
    ),
}

#: The full preset-driven document for ``hormozi`` (inline emoji on, one highlighted
#: keyword index, hook title, preset position ``center`` inherited).
_EXPECTED_HORMOZI_DOCUMENT = (
    _ASS_HEADER
    + "Style: Default,Arial,96,&H00FFFFFF,&H0000E5FF,&H00000000,&H64000000,"
      "-1,0,0,0,100,100,0,0,1,2,1,5,80,80,0,1\n"
    + _ASS_HOOK_STYLE
    + _ASS_EVENTS_HEADER
    + "Dialogue: 1,0:00:00.00,0:00:02.50,Hook,,0,0,0,,{\\fad(250,350)}WATCH THIS\n"
    + "Dialogue: 0,0:00:00.20,0:00:01.70,Default,,0,0,0,,"
      "{\\fscx60\\fscy60\\t(0,120,\\fscx100\\fscy100)}THIS "
      "{\\c&H0000E5FF&\\fscx118\\fscy118}"
      "{\\fscx60\\fscy60\\t(500,620,\\fscx100\\fscy100)}changed"
      "{\\c&H00FFFFFF&\\fscx100\\fscy100} "
      "{\\fscx60\\fscy60\\t(1000,1120,\\fscx100\\fscy100)}money \U0001f4b0\n"
)


def test_caption_preset_values_are_unchanged():
    """Validates: Requirements 19.2, 19.5

    ``BUILTIN_PRESETS`` (names **and** every field value), ``VALID_ANIMATIONS``,
    ``VALID_POSITIONS`` and ``FALLBACK_PRESET_NAME``, pinned as literals. The dataclass
    field sets are pinned too, so an added field — which would silently widen every
    preset — fails here rather than surfacing as a rendering change.
    """
    assert sorted(caption_presets.BUILTIN_PRESETS) == [
        "boxed", "hormozi", "karaoke", "minimal", "pop", "typewriter",
    ]
    for name, expected in sorted(_EXPECTED_BUILTIN_PRESETS.items()):
        preset = caption_presets.BUILTIN_PRESETS[name]
        assert preset.to_dict() == expected, name
        assert preset.name == name

    assert caption_presets.VALID_ANIMATIONS == frozenset(
        {"none", "pop", "typewriter", "karaoke_fill"}
    )
    assert caption_presets.VALID_POSITIONS == frozenset({"bottom", "center", "top"})
    assert caption_presets.FALLBACK_PRESET_NAME == "karaoke"

    assert sorted(f.name for f in dataclasses.fields(caption_presets.CaptionPreset)) == [
        "animation", "border_style", "colors", "emoji_inline", "font", "font_size",
        "highlight_keywords", "highlight_scale", "name", "position",
    ]
    assert sorted(f.name for f in dataclasses.fields(caption_presets.CaptionColors)) == [
        "box", "highlight", "outline", "primary",
    ]

    # Resolution is unchanged for callers that do not use this engine: a known name is
    # returned as-is, anything else falls back to ``karaoke`` and reports it.
    for name in sorted(_EXPECTED_BUILTIN_PRESETS):
        assert caption_presets.resolve_preset(name) == (
            caption_presets.BUILTIN_PRESETS[name], False
        )
    for bad in ("", "not-a-preset", "KARAOKE", None, 3, [], {}):
        preset, substituted = caption_presets.resolve_preset(bad)
        assert (preset.name, substituted) == ("karaoke", True), bad


def test_build_word_span_behaviour_is_unchanged():
    """Validates: Requirements 19.3

    The 4 animations x highlighted/not matrix, asserted against literal v0.8.0 spans, plus
    the two documented edge behaviours: an unknown animation renders the plain escaped
    word, and ``\\kf`` never emits a zero duration.
    """
    word = _PIN_WORDS[1]
    karaoke = caption_presets.BUILTIN_PRESETS["karaoke"]
    for (animation, highlighted), expected in sorted(_EXPECTED_WORD_SPANS.items()):
        preset = replace(karaoke, animation=animation)
        span = cap_module.build_word_span(word, preset, highlighted, cue_start=0.2)
        assert span == expected, (animation, highlighted)

    # An unrecognised animation is the plain escaped word (the ``none`` branch).
    assert cap_module.build_word_span(
        word, replace(karaoke, animation="wobble"), False, cue_start=0.2
    ) == "changed"
    # Braces/backslashes in word text are neutralised, and a zero-length word still gets
    # a one-centisecond fill.
    assert cap_module.build_word_span(
        FakeWord(1.0, 1.0, "a{b}c\\d"), karaoke, False, cue_start=1.0
    ) == "{\\kf1}a(b)c\\\\d"
    # ``cue_start`` past the word clamps the offset at zero rather than going negative.
    assert cap_module.build_word_span(
        word, replace(karaoke, animation="pop"), False, cue_start=5.0
    ) == "{\\fscx60\\fscy60\\t(0,120,\\fscx100\\fscy100)}changed"


def test_words_to_cues_grouping_is_unchanged():
    """Validates: Requirements 19.3

    The three v0.8.0 split rules (``max_words=5``, ``max_gap=0.6``, ``max_duration=3.0``)
    and the empty-text skip, pinned as a literal grouping.
    """
    timeline = [
        FakeWord(0.00, 0.30, "one"), FakeWord(0.35, 0.60, "two"),
        FakeWord(0.65, 0.90, ""),                 # empty text: skipped entirely
        FakeWord(0.95, 1.20, "three"), FakeWord(1.25, 1.50, "four"),
        FakeWord(1.55, 1.80, "five"),
        FakeWord(1.85, 2.10, "six"),              # 6th survivor: max_words split
        FakeWord(3.00, 3.40, "gap"),              # 0.90 s gap: max_gap split
        FakeWord(3.45, 6.90, "loooong"),          # span > 3.0 s: max_duration split
        FakeWord(6.95, 7.20, "tail"),
    ]
    expected = [
        (0.00, 1.80, ["one", "two", "three", "four", "five"]),
        (1.85, 2.10, ["six"]),
        (3.00, 3.40, ["gap"]),
        (3.45, 6.90, ["loooong"]),
        (6.95, 7.20, ["tail"]),
    ]
    cues = cap_module.words_to_cues(timeline)
    assert [(c.start, c.end, [w.text for w in c.words]) for c in cues] == expected
    # An empty timeline yields no cues and never raises.
    assert cap_module.words_to_cues([]) == []
    # The documented defaults are still the defaults.
    signature = inspect.signature(cap_module.words_to_cues)
    assert signature.parameters["max_words"].default == 5
    assert signature.parameters["max_gap"].default == 0.6
    assert signature.parameters["max_duration"].default == 3.0


def test_build_ass_documents_are_unchanged(tmp_path):
    """Validates: Requirements 19.3

    Full ASS documents, byte for byte, for the legacy ``caption_template`` path (all three
    templates) and for the preset path (``hormozi`` with inline emoji, one highlighted
    keyword and a hook title). The legacy path is additionally proven not to consult the
    host font list at all — ``font_available`` is patched to raise — and the preset path's
    font-substitution note is pinned on both answers.
    """
    cues = [cap_module.Cue(0.20, 1.70, list(_PIN_WORDS))]

    def _explode(_name):                    # pragma: no cover - must never be reached
        raise AssertionError("the legacy caption path must not probe host fonts")

    for template, expected in sorted(_EXPECTED_LEGACY_DOCUMENTS.items()):
        dest = tmp_path / f"legacy-{template}.ass"
        with mock.patch.object(cap_module, "font_available", _explode):
            cap_module.build_ass(
                cues, dest, video_width=1080, video_height=1920,
                template=template, position="bottom",
                hook_text="  watch this  " if template == "karaoke" else "",
            )
        assert dest.read_text(encoding="utf-8") == expected, template

    # Preset path, font present: the document is exactly the v0.8.0 one and no note.
    notes: list = []
    dest = tmp_path / "hormozi.ass"
    with mock.patch.object(cap_module, "font_available", lambda _n: True):
        cap_module.build_ass(
            cues, dest, video_width=1080, video_height=1920,
            preset=caption_presets.BUILTIN_PRESETS["hormozi"],
            keyword_indices={1}, position=None, hook_text="watch this",
            clip_duration=3.0, notes=notes,
        )
    assert dest.read_text(encoding="utf-8") == _EXPECTED_HORMOZI_DOCUMENT
    assert notes == []

    # Preset path, font absent: same document (the preset font *is* the fallback family)
    # plus exactly the v0.8.0 substitution note.
    notes = []
    substituted = tmp_path / "hormozi-substituted.ass"
    with mock.patch.object(cap_module, "font_available", lambda _n: False):
        cap_module.build_ass(
            cues, substituted, video_width=1080, video_height=1920,
            preset=caption_presets.BUILTIN_PRESETS["hormozi"],
            keyword_indices={1}, position=None, hook_text="watch this",
            clip_duration=3.0, notes=notes,
        )
    assert notes == ["font_substituted:Arial"]
    assert substituted.read_text(encoding="utf-8") == _EXPECTED_HORMOZI_DOCUMENT

    # An explicit position still overrides the preset default (Alignment 2, MarginV 220),
    # and an empty cue list with no hook yields a header-only document with no events.
    overridden = tmp_path / "hormozi-bottom.ass"
    with mock.patch.object(cap_module, "font_available", lambda _n: True):
        cap_module.build_ass(
            cues, overridden, video_width=1080, video_height=1920,
            preset=caption_presets.BUILTIN_PRESETS["hormozi"],
            position="bottom", clip_duration=3.0,
        )
    text = overridden.read_text(encoding="utf-8")
    assert (
        "Style: Default,Arial,96,&H00FFFFFF,&H0000E5FF,&H00000000,&H64000000,"
        "-1,0,0,0,100,100,0,0,1,2,1,2,80,80,220,1" in text
    )
    empty = tmp_path / "empty.ass"
    cap_module.build_ass([], empty, video_width=1080, video_height=1920)
    assert empty.read_text(encoding="utf-8") == (
        _ASS_HEADER
        + "Style: Default,Arial,84,&H00FFFFFF,&H0000FF00,&H00000000,&H64000000,"
          "-1,0,0,0,100,100,0,0,1,4,2,2,80,80,220,1\n"
        + _ASS_HOOK_STYLE
        # No events at all: the document is exactly the header plus ``build_ass``'s own
        # trailing newline (the blank line before the first ``Dialogue:`` is v0.8.0's).
        + _ASS_EVENTS_HEADER
    )


def test_subtitles_filter_escaping_is_unchanged():
    """Validates: Requirements 19.3

    The libass filter string and its ffmpeg argument escaping, pinned literally.
    """
    assert cap_module.subtitles_filter("/tmp/plain.ass") == "subtitles='/tmp/plain.ass'"
    # ``:`` and ``'`` are the two characters ffmpeg's filter syntax needs escaped.
    assert cap_module.subtitles_filter("/tmp/a:b/it's.ass") == (
        "subtitles='/tmp/a\\:b/it\\'s.ass'"
    )
    # A relative path is resolved against the cwd, and a ``Path`` behaves like a string.
    relative = cap_module.subtitles_filter("clip.ass")
    assert relative == f"subtitles='{Path('clip.ass').resolve()}'"
    assert cap_module.subtitles_filter(Path("/tmp/plain.ass")) == (
        cap_module.subtitles_filter("/tmp/plain.ass")
    )
