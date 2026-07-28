"""Kinetic typography — engine-surface properties (spec tasks 3.6, 9.4-9.8).

Covers **Property 10** from the kinetic-typography design (an unrecognised
Kinetic_Style falls back once, and names it) plus the vocabulary pin that keeps
``tests/strategies.py`` and ``worker/engines/kinetic.py`` from drifting apart,
and — from epic 9 — the four engine-class properties and the import/registry
unit tests:

* **Property 1** (task 9.4) — the declared contract is exactly the pinned one,
  measured against a *freshly imported* module and an *empty* registry.
* **Property 2** (task 9.5) — applying contributes a subtitle-only compose
  fragment.
* **Property 4** (task 9.6) — the two ``skipped`` gates leave no contribution,
  no marker, and no file.
* **Property 17** (task 9.7) — exactly one font name, always from the ladder,
  marked at most once, with the injected probe the only font oracle consulted.
* **Unit tests** (task 9.8) — import isolation with a failing ``shutil.which`` /
  ``captions.font_available``, registration exactly once across two imports, and
  an ``OSError`` from the injected ``ass_writer`` reported as ``failed``.

Everything in epic 9 drives the **real** engine (``resolve_options`` -> ``plan``
-> ``run`` -> the emitted document on disk) with no mocks of the code under test;
the only injected collaborators are the foundation doubles from
``tests/fakes.py`` (``StaticProber`` / ``CountingProber``) and, where a failure
path is required, a raising ``ass_writer``. ``RecordingStorage`` is deliberately
*not* used here: durable-artifact persistence is task 11.2's clause, and this
file must not pre-empt it.

Temp directories are created with :func:`tempfile.TemporaryDirectory` inside each
property body rather than through the function-scoped ``tmp_path`` fixture — the
same convention ``tests/test_engine_artifacts.py`` established — because
hypothesis runs many examples inside one test function and each example needs its
own clean Pipeline ``temp_dir``. ``deadline=None`` is used for the same reason:
these properties touch the filesystem.

Note on ``resolve_options``
--------------------------
The design states Property 10 in terms of ``resolve_options(...)``. That method
does not exist yet: the ``Kinetic_Typography_Engine`` class lands in spec **task
9**, and its ``resolve_options`` is specified to *delegate* to
``Kinetic_Options.from_processing_options``. This property therefore exercises
``Kinetic_Options.from_processing_options`` directly — the function the future
``resolve_options`` will call — so the guarantee is already pinned when the
engine hook lands.

Provenance is carried on ``Kinetic_Options.notes`` at this stage of the spec; the
``engine:kinetic_typography:<note>`` marker namespacing is applied by the engine
in task 9, which copies these notes into ``Kinetic_Plan.markers`` /
``Engine_Result.markers``.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import math
import sys
import tempfile
from pathlib import Path
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from tests import strategies
from tests.conftest import FakeWord
from tests.strategies import (
    st_availability_map,
    st_font_availability,
    st_kinetic_options,
    st_options_mapping,
    st_word_timeline,
)
from tests.fakes import CountingProber, StaticProber
from worker.engines import kinetic
from worker.engines import registry as engine_registry
from worker.engines.artifacts import allocate_workspace
from worker.engines.base import Engine_Context, Engine_Stage, Engine_Status
from worker.engines.capabilities import Capability_Report
from worker.engines.kinetic import (
    ASS_NAME,
    DEFAULT_STYLE,
    ENGINE_ID,
    FALLBACK_FONT,
    KINETIC_STYLES,
    KINETIC_Z_ORDER,
    SUBTITLES_CAPABILITY,
    Kinetic_Options,
    Kinetic_Typography_Engine,
)
from worker.engines.timebase import Time_Base
from worker.models import ProcessingOptions

#: The note ``from_processing_options`` records when ``coerce_choice`` fell back
#: on the requested Kinetic_Style (Req 4.8).
STYLE_NOTE = "style_substituted"

#: Values that are *present but not a member* of ``KINETIC_STYLES``: unknown
#: names, near-misses, wrong case, whitespace-padded members, the empty string and
#: non-strings. ``None`` is deliberately excluded — in this codebase ``None`` is
#: the "attribute absent" sentinel (``kinetic.py::_read`` skips it), so a ``None``
#: style is an *unrequested* style, which Req 4.8 ("IF the requested Kinetic_Style
#: is unknown, empty, or not a string") does not treat as a substitution.
_NON_MEMBER_STYLES = (
    "",
    " ",
    "\t",
    "karaoke",              # the preset name, not the style name
    "KARAOKE_FILL",         # wrong case
    "karaoke_fill ",        # trailing space
    " pop",
    "Pop",
    "fade",
    "unknown",
    "kinetic",
    "none ",
    "🎬",
    "../../etc/passwd",
    "x" * 300,
)


def _st_non_member_style():
    """A present-but-invalid Kinetic_Style: unknown/empty/mis-cased names and
    non-strings (``None`` excluded — see :data:`_NON_MEMBER_STYLES`)."""
    return st.one_of(
        st.sampled_from(list(_NON_MEMBER_STYLES)),
        st.text(max_size=12).filter(lambda s: s not in KINETIC_STYLES),
        st.booleans(),
        st.integers(min_value=-10 ** 6, max_value=10 ** 6),
        st.floats(allow_nan=True, allow_infinity=True),
        st.lists(st.sampled_from(list(KINETIC_STYLES)), max_size=2),
        st.dictionaries(st.just("style"), st.just("pop"), max_size=1),
        st.just(object()),
    )


def _attachable_noise_key(key):
    """True when ``key`` names an *option* that can be attached as noise.

    A drawn key is skipped only when ``setattr`` would not attach an option key at
    all: dunder and private names address the instance's own machinery rather than
    the option namespace ``from_processing_options`` reads, and some of them are
    type-checked slots — ``setattr(options, "__dict__", None)`` raises
    ``TypeError`` before resolution is ever reached, and ``__class__`` likewise.
    (``st_options_mapping``'s ``st.text`` leg does draw such names; Hypothesis
    mines string literals out of the codebase, so ``"__dict__"`` is reachable.)
    Everything else the generator can produce — unknown names, the empty string,
    whitespace, ``"🎬"``, an 80-character key — stays attached, which is what the
    noise is for.
    """
    return (
        isinstance(key, str)
        and key != "kinetic_style"
        and not key.startswith("_")
    )


def _carrier(hostile, style):
    """A Processing_Options carrying ``style`` plus the drawn hostile noise.

    Every hostile key that names an attachable option (see
    :func:`_attachable_noise_key`) is attached as an attribute, so resolution is
    proved to ignore option keys it does not know while still reading the one key
    it does (``kinetic_style``). ``kinetic_style`` is set last so the noise can
    never shadow it.
    """
    options = ProcessingOptions()
    for key, value in hostile.items():
        if _attachable_noise_key(key):
            setattr(options, key, value)
    options.kinetic_style = style
    return options


# --------------------------------------------------------------------------- #
# Property 10 (task 3.6)                                                        #
# --------------------------------------------------------------------------- #
# Feature: kinetic-typography, Property 10: An unrecognised style falls back once, and
# names it — *For any* value that is not a member of `KINETIC_STYLES` (including
# non-strings, empty strings, and unknown names), `resolve_options` yields
# `style == DEFAULT_STYLE` and the result carries exactly one
# `engine:kinetic_typography:style_substituted` marker; for any member value it carries
# none.
@settings(max_examples=100, deadline=None)
@given(hostile=st_options_mapping(), data=st.data())
def test_p10_unrecognised_style_falls_back_once_and_names_it(hostile, data):
    """Validates: Requirements 4.8

    Resolution is exercised through ``Kinetic_Options.from_processing_options``
    (what task 9's ``resolve_options`` delegates to). An invalid requested style
    substitutes ``DEFAULT_STYLE`` and records exactly one ``style_substituted``
    note; a valid one is returned verbatim with no such note. The surrounding
    hostile option keys are attached to the Processing_Options as noise and must
    not influence either outcome.
    """
    # --- invalid style: substituted once, and named (Req 4.8) -------------
    invalid = data.draw(_st_non_member_style(), label="invalid_style")
    resolved = Kinetic_Options.from_processing_options(_carrier(hostile, invalid))

    assert resolved.style == DEFAULT_STYLE
    assert DEFAULT_STYLE in KINETIC_STYLES
    assert list(resolved.notes).count(STYLE_NOTE) == 1

    # --- valid style: passed through, no note ----------------------------
    member = data.draw(st.sampled_from(list(KINETIC_STYLES)), label="member_style")
    kept = Kinetic_Options.from_processing_options(_carrier(hostile, member))

    assert kept.style == member
    assert list(kept.notes).count(STYLE_NOTE) == 0

    # Substitution is the *only* difference the style makes: every other resolved
    # field is identical, so the fallback never smuggles in another change.
    fallback_twin = Kinetic_Options.from_processing_options(
        _carrier(hostile, DEFAULT_STYLE)
    )
    assert resolved.to_dict() == {
        **fallback_twin.to_dict(),
        "notes": sorted(set(list(fallback_twin.notes) + [STYLE_NOTE])),
    }

    # Re-resolving does not accumulate a second note (Req 10.8 idempotence).
    assert list(
        Kinetic_Options.from_processing_options(resolved).notes
    ).count(STYLE_NOTE) == 1


# --------------------------------------------------------------------------- #
# Vocabulary pin (discharges the duplication note in tests/strategies.py)        #
# --------------------------------------------------------------------------- #
def test_kinetic_vocabularies_match_the_shared_generators():
    """The duplicated vocabularies in ``tests/strategies.py`` cannot drift.

    ``tests/strategies.py`` repeats ``KINETIC_STYLES`` and ``REVEAL_MODES`` as
    literal constants (tranche 3 was written before ``worker/engines/kinetic.py``
    existed, so it could not import them without making foundation test
    collection depend on an unwritten module). Its module comment requires this
    assertion as the pin; landing it here discharges that note, so the two
    spellings are now checked equal on every run.
    """
    assert tuple(kinetic.KINETIC_STYLES) == strategies.KINETIC_STYLES
    assert tuple(kinetic.REVEAL_MODES) == strategies.REVEAL_MODES
    # Both spellings are sorted, de-duplicated vocabularies (Reqs 4.1, 4.9).
    assert list(kinetic.KINETIC_STYLES) == sorted(set(kinetic.KINETIC_STYLES))
    assert list(kinetic.REVEAL_MODES) == sorted(set(kinetic.REVEAL_MODES))


# =========================================================================== #
# Epic 9 — the Kinetic_Typography_Engine class (tasks 9.4-9.8)                  #
# =========================================================================== #
#: A well-formed Options_Digest (16 lowercase hex characters, the shape
#: ``options_digest`` produces) used wherever the digest is not the input.
DIGEST = "0123456789abcdef"

#: The frame grid used throughout: the project's default.
TIME_BASE = Time_Base(fps=30.0)

JOB_ID = "job-kinetic"

#: Whitespace-only word texts. Every one of these satisfies ``text.strip() == ""``
#: (``\u00a0`` and ``\u3000`` included — ``str.strip`` treats both as whitespace),
#: which is exactly the "no non-whitespace word" gate of Req 3.5.
_BLANK_TEXTS = ("", " ", "  ", "\t", "\n", "\r\n", "\u00a0", "\u3000")

#: A fixed, fully-timed reference timeline: three Latin words at 30 fps inside a
#: 3 s clip. Used wherever a drawn timeline could legitimately degenerate, so a
#: property is never satisfied by an example that planned nothing.
REFERENCE_WORDS = (
    FakeWord(1.0, 1.3, "THIS"),
    FakeWord(1.3, 1.8, "CHANGED"),
    FakeWord(1.8, 2.2, "EVERYTHING"),
)
REFERENCE_DURATION = 3.0


class Recording_Font_Probe:
    """A ``captions.font_available``-shaped double recording every consultation.

    Injected as ``Kinetic_Typography_Engine(font_probe=...)``. The engine treats
    the context's ``Capability_Report`` as the primary font oracle (the report is
    what resolves ``font:<family>`` through ``captions.font_available`` and caches
    it), so with a report present this double must never be called — which is how
    Property 17's "the injected probe is the only font oracle consulted" clause is
    asserted: there is exactly one oracle, and it is the injected
    :class:`~tests.fakes.CountingProber` behind the report.
    """

    def __init__(self, answer: bool = True) -> None:
        self.answer = bool(answer)
        self.calls: list[str] = []

    def __call__(self, family: Any) -> bool:
        self.calls.append(family)
        return self.answer


class Refusing_Writer:
    """An ``ass_writer`` that always raises ``OSError`` (the Req 12.5 injection)."""

    def __init__(self, exc: OSError | None = None) -> None:
        self.exc = exc or OSError("disk on fire")
        self.calls: list[tuple[Any, str]] = []

    def __call__(self, path: Any, text: str) -> None:
        self.calls.append((path, text))
        raise self.exc


def _report(mapping: dict, *, default: bool = False) -> tuple[Capability_Report, CountingProber]:
    """A foundation ``Capability_Report`` over a counted ``StaticProber``.

    Returns ``(report, prober)`` so a test can assert *which* capability ids the
    engine consulted and *how often* — the report caches, so a repeated id must
    reach the prober at most once.
    """
    prober = CountingProber(StaticProber(mapping, default=default))
    return Capability_Report(prober), prober


def _context(
    temp_dir: Any,
    *,
    options: Any,
    words: Any = (),
    duration: float = 0.0,
    capabilities: Any = None,
    clip_metadata: dict | None = None,
    deadline: float = math.inf,
    clip_id: str = "clip-1",
    time_base: Time_Base = TIME_BASE,
) -> Engine_Context:
    """A complete, frozen COMPOSE-stage :class:`Engine_Context` with a workspace.

    The workspace comes from the foundation's
    :func:`~worker.engines.artifacts.allocate_workspace` (never a hand-rolled
    directory), so every file the engine writes lands under
    ``<temp_dir>/engines/<job>/<clip>/kinetic_typography__<digest>``.

    Per-clip values reach the engine on ``clip_metadata`` — the channel the
    Pipeline really publishes at the COMPOSE hook (``hook_text``, ``clip_size``) —
    and never on ``deps``, which is the host's injected clock/logger/storage seam
    and carries none of them. Supplying them on ``deps`` is what let these tests
    pass while production read an always-empty mapping (task 12.4).
    """
    root = Path(temp_dir)
    workspace = allocate_workspace(root, JOB_ID, clip_id, ENGINE_ID, DIGEST)
    return Engine_Context(
        job_id=JOB_ID,
        clip_id=clip_id,
        engine_id=ENGINE_ID,
        stage=Engine_Stage.COMPOSE,
        source_path=root / "source.mp4",
        clip_path=root / "clip.mp4",
        time_base=time_base,
        clip_start=0.0,
        clip_end=float(duration),
        duration=float(duration),
        words=tuple(words),
        options=options,
        options_digest=DIGEST,
        workspace=workspace,
        capabilities=capabilities,
        deadline=deadline,
        clip_metadata=dict(clip_metadata or {}),
    )


def _written_files(ctx: Engine_Context) -> list[Path]:
    """Every file present in ``ctx``'s workspace, sorted."""
    root = ctx.workspace.root
    return sorted(path for path in root.rglob("*") if path.is_file())


def _style_lines(document: str) -> list[str]:
    """The ``Style:`` lines of an emitted ASS document, in order."""
    return [line for line in document.splitlines() if line.startswith("Style: ")]


def _fontname(style_line: str) -> str:
    """The ``Fontname`` column of one ``Style:`` line (field 1 of 23)."""
    return style_line.split(",")[1]


def _font_markers(markers: Any) -> list[str]:
    """The ``engine:kinetic_typography:degraded:font:<family>`` markers recorded."""
    prefix = f"engine:{ENGINE_ID}:degraded:font:"
    return [entry for entry in markers if entry.startswith(prefix)]


def _blanked(words: Any) -> list[FakeWord]:
    """The same timeline with every word's text replaced by whitespace (Req 3.5)."""
    return [
        FakeWord(word.start, word.end, _BLANK_TEXTS[index % len(_BLANK_TEXTS)])
        for index, word in enumerate(words)
    ]


# --------------------------------------------------------------------------- #
# Fresh-import evidence for Property 1 (task 9.4)                              #
# --------------------------------------------------------------------------- #
_FRESH: dict[str, Any] = {}


def _restore_registry(saved: Any) -> None:
    """Put the process-wide registry back exactly as it was found.

    Whatever state the registry was in when a test borrowed it is the state other
    tests expect — which is *not* necessarily "kinetic is registered": several
    modules in this suite call ``reset_registry()`` and leave the registry empty,
    so by the time this file runs inside the full suite it may legitimately hold
    nothing at all. Restoration therefore replays the captured records verbatim
    instead of re-registering the engine.
    """
    engine_registry.reset_registry()
    for record in saved:
        engine_registry.get_registry().register(record.engine, priority=record.priority)


def _fresh_registration_snapshot() -> dict[str, Any]:
    """Import a pristine copy of ``kinetic.py`` against an **empty** registry, once.

    Property 1 claims things about "a freshly imported process", including that
    the engine appears **exactly once** in the registry for the COMPOSE stage.
    Measuring that against the live process-wide registry would be meaningless
    (this module's own import already registered the engine, and other test
    modules reset the registry around their own assertions), so the measurement is
    taken here under controlled conditions:

    1. snapshot the live registrations;
    2. ``reset_registry()`` and assert the registry really is empty;
    3. execute a **fresh** copy of ``worker/engines/kinetic.py`` under a private
       module name — a genuinely new module object with new class objects, and
       ``sys.modules`` untouched — so its import-time ``register(...)`` call is
       the only thing that could have populated the registry;
    4. capture what the registry now holds;
    5. restore the snapshot exactly, so the process-wide registry is left in the
       state every other test expects (one ``kinetic_typography`` entry, holding
       the instance the real module registered at import).

    The work is done once and cached, so the 100 examples of the property all
    assert against the same captured evidence instead of re-importing 100 times
    and churning the shared registry.
    """
    if _FRESH:
        return _FRESH

    registry = engine_registry.get_registry()
    saved = registry.records()
    saved_ids = tuple(registry.ids())
    try:
        engine_registry.reset_registry()
        registry_was_empty = len(engine_registry.get_registry()) == 0

        module = _exec_fresh_module("_kinetic_fresh_p1")   # re-runs registration

        fresh = engine_registry.get_registry()
        _FRESH.update(
            module=module,
            registry_was_empty=registry_was_empty,
            ids=tuple(fresh.ids()),
            records=tuple(fresh.records()),
            compose=tuple(fresh.for_stage(Engine_Stage.COMPOSE)),
            stage_of=fresh.stage_of(module.ENGINE_ID),
            size=len(fresh),
        )
    finally:
        _restore_registry(saved)
    _FRESH.update(
        saved_ids=saved_ids,
        restored_ids=tuple(engine_registry.get_registry().ids()),
    )
    return _FRESH


# --------------------------------------------------------------------------- #
# Property 1 (task 9.4)                                                        #
# --------------------------------------------------------------------------- #
# Feature: kinetic-typography, Property 1: The engine's declared contract is exactly the
# pinned one — *For any* freshly imported process, `Kinetic_Typography_Engine` has
# `engine_id == "kinetic_typography"`, `stage is Engine_Stage.COMPOSE`, an integer
# `priority`, `"ffmpeg_filter:subtitles"` in `required_capabilities`, `requires_network is
# False`, `requires_model_download is False`, `max_media_passes == 0`, `produces_media is
# False`, a positive `time_budget_s`, `flag_field() == "kinetic_typography_enabled"`, and
# appears exactly once in the registry for the COMPOSE stage.
@settings(max_examples=100, deadline=None)
@given(
    inject_font_probe=st.booleans(),
    inject_keyword_planner=st.booleans(),
    inject_ass_writer=st.booleans(),
)
def test_p1_the_engines_declared_contract_is_exactly_the_pinned_one(
    inject_font_probe, inject_keyword_planner, inject_ass_writer
):
    """Validates: Requirements 1.1, 1.4, 1.5, 1.6, 1.7, 1.8, 15.2, 16.1

    The contract is asserted on the class **and** on an instance built with every
    combination of injected collaborators, because the declarations are the host's
    only input and dependency injection must not be able to change any of them.
    The registry clause is asserted against the fresh-import evidence captured by
    :func:`_fresh_registration_snapshot`.
    """
    snapshot = _fresh_registration_snapshot()
    module = snapshot["module"]
    engine_cls = module.Kinetic_Typography_Engine

    # Non-vacuity: the evidence really comes from a *new* module object registering
    # into an *empty* registry, not from this test module's own earlier import.
    assert module is not kinetic
    assert engine_cls is not Kinetic_Typography_Engine
    assert snapshot["registry_was_empty"] is True

    kwargs: dict[str, Any] = {}
    if inject_font_probe:
        kwargs["font_probe"] = Recording_Font_Probe()
    if inject_keyword_planner:
        kwargs["keyword_planner"] = lambda words, **_: set()
    if inject_ass_writer:
        kwargs["ass_writer"] = lambda path, text: None
    engine = engine_cls(**kwargs)

    # --- identity and stage (Reqs 1.1, 16.1) ------------------------------
    assert engine_cls.engine_id == "kinetic_typography"
    assert engine.engine_id == "kinetic_typography"
    assert module.ENGINE_ID == "kinetic_typography"
    assert engine_cls.stage is Engine_Stage.COMPOSE
    assert engine.stage is Engine_Stage.COMPOSE

    # --- ordering weight: a real integer, not a bool -----------------------
    assert isinstance(engine_cls.priority, int)
    assert not isinstance(engine_cls.priority, bool)

    # --- declared capability (Req 1.5) ------------------------------------
    assert "ffmpeg_filter:subtitles" in engine_cls.required_capabilities
    assert module.SUBTITLES_CAPABILITY == "ffmpeg_filter:subtitles"

    # --- cost declarations (Reqs 1.5, 1.6, 15.2) --------------------------
    assert engine_cls.requires_network is False
    assert engine_cls.requires_model_download is False
    assert engine_cls.max_media_passes == 0
    assert engine_cls.produces_media is False
    assert isinstance(engine_cls.time_budget_s, float)
    assert engine_cls.time_budget_s > 0.0

    # --- the Feature_Flag field (Reqs 1.8, 15.5) --------------------------
    assert engine_cls.flag_field() == "kinetic_typography_enabled"
    assert engine.flag_field() == "kinetic_typography_enabled"

    # --- exactly one COMPOSE registry entry, on a fresh registry ----------
    assert snapshot["ids"] == ("kinetic_typography",)
    assert snapshot["size"] == 1
    assert len(snapshot["compose"]) == 1
    assert snapshot["compose"][0].engine_id == "kinetic_typography"
    assert isinstance(snapshot["compose"][0], engine_cls)
    assert snapshot["stage_of"] is Engine_Stage.COMPOSE
    assert len(snapshot["records"]) == 1
    assert snapshot["records"][0].priority == engine_cls.priority

    # The process-wide registry was left exactly as it was found, whatever that
    # was — the fresh registration is borrowed, never leaked into the shared one.
    assert snapshot["restored_ids"] == snapshot["saved_ids"]
    assert tuple(engine_registry.get_registry().ids()) == snapshot["saved_ids"]
    # Whenever this module's own import is what registered the engine, that
    # registration is still the live one and still a COMPOSE-stage engine.
    live = engine_registry.get_registry().find(ENGINE_ID)
    if live is not None:
        assert isinstance(live, Kinetic_Typography_Engine)
        assert engine_registry.get_registry().stage_of(ENGINE_ID) is Engine_Stage.COMPOSE


# --------------------------------------------------------------------------- #
# Property 2 (task 9.5)                                                        #
# --------------------------------------------------------------------------- #
# Feature: kinetic-typography, Property 2: Applying contributes a subtitle-only compose
# fragment — *For all* Word_Timelines and Kinetic_Options values for which the engine
# returns `applied`, the `Compose_Contribution` has `engine_id == "kinetic_typography"`,
# `inputs == ()`, `audio_filters == ()`, `video_filters == ()`, `z_order == 100`, a
# `subtitle_path` that exists, and `Engine_Result.media is None`.
@settings(max_examples=100, deadline=None)
@given(timeline=st_word_timeline(), option_fields=st_kinetic_options())
def test_p2_applying_contributes_a_subtitle_only_compose_fragment(
    timeline, option_fields
):
    """Validates: Requirements 2.1, 2.3, 2.4, 12.4, 16.3

    The ``applied`` precondition is *forced*, not hoped for: captions are enabled,
    the timeline carries real words, ``ffmpeg_filter:subtitles`` is available and
    every font probes available (so no substitution can degrade the run), and the
    budget is unlimited. The status is then **asserted** to be ``applied`` on every
    example, which is what keeps the contribution clauses non-vacuous — a run that
    quietly degraded would fail here rather than skip the assertions.
    """
    words, duration = timeline
    opts = dataclasses.replace(
        Kinetic_Options.parse(option_fields), captions_enabled=True
    )
    engine = Kinetic_Typography_Engine(font_probe=Recording_Font_Probe())

    with tempfile.TemporaryDirectory() as temp_dir:
        report, prober = _report({SUBTITLES_CAPABILITY: True}, default=True)
        ctx = _context(
            temp_dir,
            options=opts,
            words=words,
            duration=duration,
            capabilities=report,
            clip_metadata={"hook_text": "watch this"},
        )
        result = engine.run(ctx)

        # --- the precondition, asserted (no font substitution, no degradation) ---
        assert result.status is Engine_Status.APPLIED, result.detail
        assert _font_markers(result.markers) == []

        # --- a subtitle-only compose fragment (Reqs 2.1, 2.3, 2.4) ----------
        contribution = result.contribution
        assert contribution is not None            # applied <=> owns the slot (Req 3.9)
        assert contribution.engine_id == "kinetic_typography"
        assert contribution.inputs == ()
        assert contribution.video_filters == ()
        assert contribution.audio_filters == ()
        assert contribution.z_order == KINETIC_Z_ORDER == 100
        assert contribution.subtitle_path is not None
        assert Path(contribution.subtitle_path).is_file()

        # --- no replacement media: this engine never re-encodes (Req 2.2) ---
        assert result.media is None
        assert engine.produces_media is False
        assert engine.max_media_passes == 0

        # --- exactly one file, inside the workspace, named kinetic.ass (16.3) ---
        files = _written_files(ctx)
        assert files == [ctx.workspace.path(ASS_NAME)]
        assert Path(contribution.subtitle_path) == files[0]
        assert files[0].name == ASS_NAME
        assert ctx.workspace.root.resolve() in files[0].resolve().parents

        # --- the artifact declares the same file (Reqs 12.2, 12.4, 12.7) -----
        assert len(result.artifacts) == 1
        artifact = result.artifacts[0]
        assert artifact.media_type == "subtitle"
        assert artifact.durable is opts.durable_subtitle
        assert Path(artifact.path) == files[0]

        # The document is a real, non-empty ASS file the compositor can hand to
        # libass — the contribution's promise is a *usable* subtitle path.
        document = files[0].read_text(encoding="utf-8")
        assert document.startswith("[Script Info]")
        assert document.endswith("\n")

        # The only capability oracle consulted is the injected prober, and no
        # capability was probed twice (the report caches).
        assert prober.count_for(SUBTITLES_CAPABILITY) == 1
        assert all(prober.count_for(cap) == 1 for cap in set(prober.calls))
        assert engine._font_probe.calls == []

        # Non-vacuity guard: a drawn timeline may legitimately plan *zero* cues
        # (Req 5.5 drops a cue shorter than MIN_WORD_S with its words), so the
        # same clause set is re-run on the fixed reference timeline, where the
        # contributed document must actually carry events.
        report, _prober = _report({SUBTITLES_CAPABILITY: True}, default=True)
        ref_ctx = _context(
            temp_dir,
            options=opts,
            words=REFERENCE_WORDS,
            duration=REFERENCE_DURATION,
            capabilities=report,
            clip_metadata={"hook_text": "watch this"},
            clip_id="clip-reference",
        )
        ref = engine.run(ref_ctx)
        assert ref.status is Engine_Status.APPLIED, ref.detail
        assert ref.contribution is not None
        assert ref.contribution.inputs == ()
        assert ref.contribution.video_filters == ()
        assert ref.contribution.audio_filters == ()
        assert ref.contribution.z_order == KINETIC_Z_ORDER
        assert ref.media is None
        ref_document = Path(ref.contribution.subtitle_path).read_text(encoding="utf-8")
        assert [
            line for line in ref_document.splitlines() if line.startswith("Dialogue: ")
        ]


# --------------------------------------------------------------------------- #
# Property 4 (task 9.6)                                                        #
# --------------------------------------------------------------------------- #
# Feature: kinetic-typography, Property 4: Gates return `skipped` and leave no
# contribution — *For all* Word_Timelines, when `ProcessingOptions.captions` is disabled or
# the rebased Word_Timeline contains no non-whitespace word, the result status is
# `skipped`, its `contribution is None`, its `markers == ()`, and no file was written.
@settings(max_examples=100, deadline=None)
@given(timeline=st_word_timeline(), option_fields=st_kinetic_options())
def test_p4_gates_return_skipped_and_leave_no_contribution(timeline, option_fields):
    """Validates: Requirements 3.4, 3.5

    **Both rungs are exercised on every example**, so neither can pass vacuously:
    rung 1 with captions disabled (twice over — once through a resolved
    ``Kinetic_Options`` and once through a real ``ProcessingOptions(captions=False)``
    projected by ``resolve_options``, which is the spelling the property names),
    and rung 2 with the same timeline blanked to whitespace-only words.

    The "no file was written" clause is likewise non-vacuous: a **control** run
    with both gates open, in a workspace of its own, must reach ``applied`` and
    write exactly one file — so the engine demonstrably *would* have written had
    the gates not stopped it.
    """
    words, duration = timeline
    drawn = Kinetic_Options.parse(option_fields)
    engine = Kinetic_Typography_Engine(font_probe=Recording_Font_Probe())

    def _assert_gated(ctx: Engine_Context, prober: CountingProber, label: str) -> None:
        result = engine.run(ctx)
        assert result.status is Engine_Status.SKIPPED, f"{label}: {result.detail}"
        assert result.contribution is None, label
        assert result.markers == (), label
        assert result.artifacts == (), label
        assert result.media is None, label
        assert _written_files(ctx) == [], label
        assert not ctx.workspace.path(ASS_NAME).exists(), label
        # A gate returns *before* any probe, so nothing was consulted either.
        assert prober.calls == [], label
        assert engine._font_probe.calls == [], label

    with tempfile.TemporaryDirectory() as temp_dir:
        # --- rung 1: captions disabled (Req 3.4) --------------------------
        report, prober = _report({SUBTITLES_CAPABILITY: True}, default=True)
        _assert_gated(
            _context(
                temp_dir,
                options=dataclasses.replace(drawn, captions_enabled=False),
                words=words,
                duration=duration,
                capabilities=report,
                clip_id="clip-captions-off",
            ),
            prober,
            "captions disabled",
        )

        # --- rung 1 again, through the real ProcessingOptions spelling -----
        report, prober = _report({SUBTITLES_CAPABILITY: True}, default=True)
        projected = engine.resolve_options(
            ProcessingOptions(captions=False, caption_preset=drawn.preset_name)
        )
        assert projected.captions_enabled is False
        _assert_gated(
            _context(
                temp_dir,
                options=projected,
                words=words,
                duration=duration,
                capabilities=report,
                clip_id="clip-processing-options-off",
            ),
            prober,
            "ProcessingOptions(captions=False)",
        )

        # --- rung 2: no non-whitespace word (Req 3.5) ----------------------
        blanked = _blanked(words)
        assert blanked and all(not word.text.strip() for word in blanked)
        report, prober = _report({SUBTITLES_CAPABILITY: True}, default=True)
        _assert_gated(
            _context(
                temp_dir,
                options=dataclasses.replace(drawn, captions_enabled=True),
                words=blanked,
                duration=duration,
                capabilities=report,
                clip_id="clip-blank-words",
            ),
            prober,
            "whitespace-only timeline",
        )

        # --- rung 2 again, with an empty timeline --------------------------
        report, prober = _report({SUBTITLES_CAPABILITY: True}, default=True)
        _assert_gated(
            _context(
                temp_dir,
                options=dataclasses.replace(drawn, captions_enabled=True),
                words=(),
                duration=duration,
                capabilities=report,
                clip_id="clip-no-words",
            ),
            prober,
            "empty timeline",
        )

        # --- control: gates open => applied, exactly one file written ------
        report, _prober = _report({SUBTITLES_CAPABILITY: True}, default=True)
        control_ctx = _context(
            temp_dir,
            options=dataclasses.replace(drawn, captions_enabled=True),
            words=REFERENCE_WORDS,
            duration=REFERENCE_DURATION,
            capabilities=report,
            clip_id="clip-control",
        )
        control = engine.run(control_ctx)
        assert control.status is Engine_Status.APPLIED, control.detail
        assert control.contribution is not None
        assert _written_files(control_ctx) == [control_ctx.workspace.path(ASS_NAME)]


# --------------------------------------------------------------------------- #
# Property 17 (task 9.7)                                                       #
# --------------------------------------------------------------------------- #
def _expected_ladder(opts: Kinetic_Options) -> tuple[str, ...]:
    """The font ladder ``(font_override, preset_font, "Arial")``, empties dropped.

    De-duplicated, which cannot change the outcome (a repeated family gives the
    same probe answer) and matches the shape ``st_font_availability`` reports.
    """
    ladder: list[str] = []
    for family in (opts.font_override, opts.preset_font, FALLBACK_FONT):
        if family and family not in ladder:
            ladder.append(family)
    return tuple(ladder) or (FALLBACK_FONT,)


def _expected_font(
    ladder: tuple[str, ...], availability: dict, default: bool
) -> tuple[str, bool]:
    """``(family, marked)`` the ladder must resolve to, computed independently.

    ``marked`` follows the ladder contract task 9.2 spells out: a marker is owed
    when the family used is not the one requested, **and also** when *nothing* on
    the ladder probed available — in that case the documented last rung is used
    even though it is not installed, so the look is degraded and the clip is
    handed back to the v0.8.0 caption path (Reqs 9.4, 9.7).

    Note that ``st_font_availability``'s ``expected_marked`` is computed as
    ``expected_font != ladder[0]`` alone, so it under-predicts in exactly one
    corner: nothing available **and** ``ladder[0] == "Arial"`` (an empty font
    override with ``preset_font == "Arial"``). The generator's docstring records
    that corner; the property below asserts both readings explicitly rather than
    quietly preferring one.
    """
    for family in ladder:
        if availability.get(f"font:{family}", default):
            return family, family != ladder[0]
    return FALLBACK_FONT, True


def _run_font_case(
    temp_dir: Any,
    opts: Kinetic_Options,
    availability: dict,
    default: bool,
    *,
    clip_id: str,
) -> tuple[Any, Engine_Context, CountingProber, Recording_Font_Probe, str]:
    """Run the engine once and return ``(result, ctx, prober, probe, document)``."""
    probe = Recording_Font_Probe()
    engine = Kinetic_Typography_Engine(font_probe=probe)
    # ``ffmpeg_filter:subtitles`` is always granted, so the run always reaches the
    # font ladder and the emitted document — the clauses below are never skipped.
    mapping = {**availability, SUBTITLES_CAPABILITY: True}
    report, prober = _report(mapping, default=default)
    ctx = _context(
        temp_dir,
        options=opts,
        words=REFERENCE_WORDS,
        duration=REFERENCE_DURATION,
        capabilities=report,
        clip_id=clip_id,
    )
    result = engine.run(ctx)
    document = ctx.workspace.path(ASS_NAME).read_text(encoding="utf-8")
    return result, ctx, prober, probe, document


def _assert_font_clauses(
    result: Any,
    prober: CountingProber,
    probe: Recording_Font_Probe,
    document: str,
    opts: Kinetic_Options,
    availability: dict,
    default: bool,
    label: str,
) -> tuple[str, bool]:
    """Every Property 17 clause, for one run. Returns ``(family, marked)``."""
    ladder = _expected_ladder(opts)
    expected, marked = _expected_font(ladder, availability, default)

    # --- exactly one Fontname in the Style: Default line (Reqs 9.3, 9.7) ---
    default_lines = [
        line for line in _style_lines(document) if line.startswith("Style: Default,")
    ]
    assert len(default_lines) == 1, label
    family = _fontname(default_lines[0])
    assert family == expected, f"{label}: {family!r} != {expected!r}"
    assert family in ladder, label
    # Every declared style names a ladder member — no other family can leak out.
    for line in _style_lines(document):
        assert _fontname(line) in ladder, f"{label}: {line}"
    assert result.plan["font"] == family, label

    # --- the requested style and Reveal_Mode survive substitution (Req 9.5) ---
    assert result.plan["style"] == opts.style, label
    assert result.plan["reveal"] == opts.reveal, label

    # --- at most one degraded:font: marker, naming the requested family (9.8) ---
    font_markers = _font_markers(result.markers)
    assert len(font_markers) <= 1, label
    assert bool(font_markers) is marked, f"{label}: {font_markers} vs marked={marked}"
    if font_markers:
        assert font_markers[0] == f"engine:{ENGINE_ID}:degraded:font:{ladder[0]}", label
        # A substituted font hands the clip back to the v0.8.0 caption path.
        assert result.status is Engine_Status.DEGRADED, label
        assert result.contribution is None, label

    # --- the injected probe is the only font oracle consulted (Reqs 9.1, 9.6) ---
    probed_fonts = [cap for cap in prober.calls if str(cap).startswith("font:")]
    assert probed_fonts == [f"font:{fam}" for fam in ladder[: len(probed_fonts)]], label
    assert probed_fonts, label                       # the ladder really was probed
    for cap in set(probed_fonts):
        assert prober.count_for(cap) == 1, label     # cached: one probe per family
    assert f"font:{family}" in probed_fonts, label
    assert probe.calls == [], label                  # no second oracle was consulted
    # No download, no network: the ladder is capability answers only.
    assert Kinetic_Typography_Engine.requires_network is False
    assert Kinetic_Typography_Engine.requires_model_download is False
    return family, marked


# Feature: kinetic-typography, Property 17: Exactly one font name, always from the ladder,
# marked once — *For every* Kinetic_Options value and *every* font availability
# combination, the emitted document contains exactly one `Fontname` value in its `Style:
# Default` line, that value is a member of `(font_override, preset_font, "Arial")`, the
# requested Kinetic_Style and Reveal_Mode are still emitted, at most one `degraded:font:`
# marker is recorded, and the injected probe is the only font oracle consulted.
@settings(max_examples=100, deadline=None)
@given(
    option_fields=st_kinetic_options(),
    ladder_case=st_font_availability(),
    noise=st_availability_map(),
)
def test_p17_exactly_one_font_name_always_from_the_ladder_marked_once(
    option_fields, ladder_case, noise
):
    """Validates: Requirements 9.1, 9.2, 9.3, 9.4, 9.5, 9.7, 9.8, 13.3

    Three runs per example, so **both** rungs of the ladder are exercised every
    time and the substitution branch is never left to chance:

    1. the **drawn** availability combination from ``st_font_availability`` (which
       may or may not substitute), with unrelated ``st_availability_map`` noise
       merged underneath it;
    2. a **forced substitution**: an override family that is explicitly
       unavailable while ``Arial`` is available, which must resolve down the
       ladder and record exactly one ``degraded:font:<requested>`` marker;
    3. a **forced non-substitution**: every family available, which must keep the
       requested family and record no font marker at all.

    Runs 2 and 3 are what make the marker clauses non-vacuous: on every single
    example one run hits a substitution and one run provably does not.
    """
    drawn = Kinetic_Options.parse(option_fields)
    opts = dataclasses.replace(
        drawn,
        font_override=ladder_case["font_override"],
        preset_font=ladder_case["preset_font"],
        captions_enabled=True,
    )
    # The generator's own ladder/expectation must agree with the resolved options:
    # this pins ``st_font_availability`` to the engine's ladder shape.
    assert _expected_ladder(opts) == ladder_case["ladder"]

    with tempfile.TemporaryDirectory() as temp_dir:
        # --- 1: the drawn availability combination ------------------------
        availability = {**noise, **ladder_case["availability"]}
        result, _ctx, prober, probe, document = _run_font_case(
            temp_dir, opts, availability, ladder_case["default"], clip_id="clip-drawn"
        )
        family, marked = _assert_font_clauses(
            result,
            prober,
            probe,
            document,
            opts,
            availability,
            ladder_case["default"],
            "drawn availability",
        )
        # The generator predicted the same font the engine resolved to, and the
        # same marker verdict once its one documented blind spot (nothing on the
        # ladder available) is accounted for — see :func:`_expected_font`.
        nothing_available = not ladder_case["available_families"]
        assert family == ladder_case["expected_font"]
        assert marked is (ladder_case["expected_marked"] or nothing_available)

        # --- 2: forced substitution (the requested family is missing) ------
        missing = "Definitely Not Installed"
        forced = dataclasses.replace(opts, font_override=missing)
        forced_availability = {
            f"font:{missing}": False,
            f"font:{opts.preset_font}": False,
            f"font:{FALLBACK_FONT}": True,
        }
        result, _ctx, prober, probe, document = _run_font_case(
            temp_dir, forced, forced_availability, False, clip_id="clip-substituted"
        )
        family, marked = _assert_font_clauses(
            result,
            prober,
            probe,
            document,
            forced,
            forced_availability,
            False,
            "forced substitution",
        )
        assert marked is True
        assert family == FALLBACK_FONT
        assert _font_markers(result.markers) == [
            f"engine:{ENGINE_ID}:degraded:font:{missing}"
        ]

        # --- 3: forced non-substitution (everything available) -------------
        result, _ctx, prober, probe, document = _run_font_case(
            temp_dir, opts, {}, True, clip_id="clip-available"
        )
        family, marked = _assert_font_clauses(
            result, prober, probe, document, opts, {}, True, "all fonts available"
        )
        assert marked is False
        assert family == _expected_ladder(opts)[0]
        assert _font_markers(result.markers) == []


# --------------------------------------------------------------------------- #
# Unit tests: import isolation and registry singleton (task 9.8)               #
# --------------------------------------------------------------------------- #
def _exec_fresh_module(name: str):
    """Execute a fresh copy of ``worker/engines/kinetic.py`` under a private ``name``.

    ``worker.engines.kinetic`` itself is left untouched in ``sys.modules``, so the
    already-imported real module — and every reference other test modules hold
    into it — is unaffected; only the private alias is added, and it is dropped
    again once the module body has run. (The alias has to be visible *during* the
    exec: ``dataclasses`` resolves the string annotations of a frozen dataclass
    through ``sys.modules[cls.__module__]``, which is exactly what the module's
    ``from __future__ import annotations`` makes necessary.)
    """
    spec = importlib.util.spec_from_file_location(name, Path(kinetic.__file__))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def test_module_imports_with_a_failing_which_and_font_probe(monkeypatch):
    """Validates: Requirements 1.4, 1.7 — import is safe with no binaries and no fonts.

    ``shutil.which`` and ``captions.font_available`` are patched to raise, which
    is what a host with no ffmpeg and an unusable font stack looks like. Importing
    the module must still succeed: every heavy dependency is reached through a
    lazy call made from ``plan``/``run``, never at module scope.
    """
    import shutil

    from worker import captions

    def _boom(*args, **kwargs):
        raise OSError("no binaries on this host")

    monkeypatch.setattr(shutil, "which", _boom)
    monkeypatch.setattr(captions, "font_available", _boom)

    registry = engine_registry.get_registry()
    saved = registry.records()
    saved_ids = tuple(registry.ids())
    try:
        module = _exec_fresh_module("_kinetic_import_isolation")
        # The module is fully usable: constants, the class, and a construction.
        assert module.ENGINE_ID == ENGINE_ID
        assert module.Kinetic_Typography_Engine().engine_id == ENGINE_ID
        # A pure emit still works with no fonts and no ffmpeg present.
        assert module.emit_ass(module.Kinetic_Plan.from_dict({})).endswith("\n")
    finally:
        _restore_registry(saved)
    assert tuple(engine_registry.get_registry().ids()) == saved_ids


def test_importing_the_module_twice_registers_the_engine_exactly_once():
    """Validates: Requirement 1.4 — registration is exactly once per registry.

    Two independent executions of the module (the moral equivalent of an import
    through two names, or a reload) must leave **one** registration, not two, and
    must not raise ``Engine_Registration_Error``. The process-wide registry is
    restored afterwards.
    """
    registry = engine_registry.get_registry()
    saved = registry.records()
    saved_ids = tuple(registry.ids())
    try:
        engine_registry.reset_registry()
        first = _exec_fresh_module("_kinetic_twice_a")
        assert len(engine_registry.get_registry()) == 1
        second = _exec_fresh_module("_kinetic_twice_b")

        fresh = engine_registry.get_registry()
        assert len(fresh) == 1
        assert fresh.ids() == [ENGINE_ID]
        assert len(fresh.for_stage(Engine_Stage.COMPOSE)) == 1
        # The first import owns the registration; the second one did not replace it.
        assert isinstance(fresh.find(ENGINE_ID), first.Kinetic_Typography_Engine)
        assert not isinstance(fresh.find(ENGINE_ID), second.Kinetic_Typography_Engine)
    finally:
        _restore_registry(saved)
    # The shared registry is left exactly as it was found (see _restore_registry).
    assert tuple(engine_registry.get_registry().ids()) == saved_ids


def test_an_os_error_from_the_ass_writer_yields_failed_with_no_contribution():
    """Validates: Requirement 12.5 — a write failure is reported, not raised.

    The injected ``ass_writer`` raises ``OSError``; ``run`` must return ``failed``
    with a ``"<Type>: <msg>"`` detail, no contribution, no artifact — and it must
    not leave a file behind.
    """
    writer = Refusing_Writer(OSError("disk on fire"))
    engine = Kinetic_Typography_Engine(
        font_probe=Recording_Font_Probe(), ass_writer=writer
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        report, _prober = _report({SUBTITLES_CAPABILITY: True}, default=True)
        ctx = _context(
            temp_dir,
            options=Kinetic_Options(captions_enabled=True),
            words=REFERENCE_WORDS,
            duration=REFERENCE_DURATION,
            capabilities=report,
        )
        result = engine.run(ctx)

        assert result.status is Engine_Status.FAILED
        assert result.detail == "OSError: disk on fire"
        assert result.contribution is None
        assert result.artifacts == ()
        assert result.media is None
        assert _written_files(ctx) == []
        # The writer really was reached (the failure is the write, not a gate).
        assert len(writer.calls) == 1
        assert Path(writer.calls[0][0]) == ctx.workspace.path(ASS_NAME)
