"""Kinetic typography — determinism, locality and cost (spec task 11.1).

Covers **Property 19** from the kinetic-typography design: the engine's output is
byte-identical for equal inputs, produced entirely offline, free of environment
leakage, confined to the Pipeline ``temp_dir``, bounded in event count, and keyed
by an Options_Digest that is equal exactly for equal options.

The property drives the **real** engine end to end — ``resolve_options`` ->
``plan`` -> ``run`` -> the document on disk — with the real ``ass_writer`` and the
real ``caption_presets.plan_keywords`` (which is offline by construction: the
planner always passes ``client=None``, so no LLM call is possible). The only
injected collaborators are the foundation doubles from ``tests/fakes.py``
(``StaticProber`` behind a ``CountingProber``, wrapped in a real
``Capability_Report``), so nothing about the code under test is mocked.

How the three "no leakage" clauses are enforced
----------------------------------------------
* **No subprocess, no socket** — exactly as the design prescribes: every
  ``subprocess`` entry point, ``os.system``/``os.popen``/``os.fork``/
  ``os.posix_spawn`` and every ``socket`` constructor is patched to *raise* for
  the duration of both invocations, so a single attempt fails the test rather
  than being counted after the fact.
* **No wall-clock value** — the two invocations run with ``time.time`` /
  ``time.monotonic`` / ``time.perf_counter`` pinned to two values a million
  seconds apart (and, independently, at two different *real* wall-clock instants,
  which covers any clock API not named here). Byte-identical output across that
  gap is a stronger statement than searching the text for a timestamp: a clock
  reading cannot be embedded and still leave the bytes equal. Two lexical guards
  back it up — the content carries no ISO-8601-shaped date and no 10-or-more
  digit run (an epoch time).
* **No process identifier** — ``os.getpid`` likewise returns two different
  fabricated pids across the two invocations, so a pid cannot be embedded and
  leave the bytes equal. This is deliberately *not* implemented as
  ``str(os.getpid()) not in content``: a real pid is a short digit run that
  collides by coincidence with legitimate plan numbers (a font size, a margin, a
  ``PlayRes``), so that spelling would be simultaneously flaky and weaker.
* **No absolute path** — the two invocations use two different Pipeline
  ``temp_dir``s, so byte-identity already proves no path leaked; a regex for
  POSIX/UNC/drive-letter absolute paths and an explicit "neither temp dir string
  appears" check make the clause direct as well.

Temp directories come from :func:`tempfile.TemporaryDirectory` inside the
property body (the convention ``tests/test_engine_artifacts.py`` established and
``tests/test_kinetic_engine.py`` follows) rather than the function-scoped
``tmp_path`` fixture, because hypothesis runs many examples through one test
function and each example needs its own clean Pipeline ``temp_dir``.
``deadline=None`` is used for the same reason: this property touches the
filesystem.
"""
from __future__ import annotations

import ast
import contextlib
import dataclasses
import inspect
import math
import os
import re
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from tests.conftest import FakeWord
from tests.fakes import CountingProber, RecordingStorage, StaticProber
from tests.strategies import st_kinetic_options, st_time_base, st_word_timeline
from worker.engines.artifacts import allocate_workspace, artifact_key
from worker.engines.base import (
    Engine_Context,
    Engine_Stage,
    Engine_Status,
    options_digest,
)
from worker.engines.capabilities import Capability_Report
from worker.engines.host import Engine_Host
from worker.engines.kinetic import (
    ASS_NAME,
    ENGINE_ID,
    FALLBACK_FONT,
    KINETIC_STYLES,
    REVEAL_MODES,
    SUBTITLES_CAPABILITY,
    Kinetic_Options,
    Kinetic_Plan,
    Kinetic_Typography_Engine,
    emit_ass,
    plan_kinetic,
)
from worker.engines import kinetic as kinetic_module
from worker.engines.registry import Engine_Registry
from worker.engines.timebase import Time_Base

# The engine reaches ``worker.captions`` / ``worker.effects.caption_presets``
# lazily (module-scope imports of them would break the engine package's
# import-safety gate). Warm both here, at *test module* import time, so the first
# example never performs a lazy import while ``subprocess``/``socket`` are
# patched to raise — the patch is meant to catch the engine, not the import
# machinery of a module it legitimately reuses.
from worker import captions as _captions_warm  # noqa: F401
from worker.effects import caption_presets as _presets_warm  # noqa: F401

JOB_ID = "job-kinetic-determinism"

#: A fixed, fully-timed reference timeline: three Latin words inside a 3 s clip.
#: Used as the non-vacuity control, because a drawn timeline may legitimately
#: plan zero cues (Req 5.5 drops a cue shorter than ``MIN_WORD_S``).
REFERENCE_WORDS = (
    FakeWord(1.0, 1.3, "THIS"),
    FakeWord(1.3, 1.8, "CHANGED"),
    FakeWord(1.8, 2.2, "EVERYTHING"),
)
REFERENCE_DURATION = 3.0

#: The two fabricated environments the two invocations run in. Anything the
#: engine could read from the clock or the process would differ between them.
_ENV_A = {"clock": 1_000_000.5, "pid": 424_242}
_ENV_B = {"clock": 2_000_000.5, "pid": 131_313}

#: A POSIX absolute path (``/a/b``), a UNC share (``\\host\share``) or a Windows
#: drive-rooted path (``C:\dir``) — none may appear in an emitted document.
_ABSOLUTE_PATH = re.compile(r"(?:^|[\s,;:'\"(])(?:/[\w.\-]+){2,}|\\\\[\w.\-]+\\|[A-Za-z]:\\")

#: An ISO-8601-shaped date and a 10+ digit run (an epoch time), both of which a
#: wall-clock leak would plausibly look like.
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_LONG_DIGITS = re.compile(r"\d{10,}")

#: Legal ``Kinetic_Options.position`` values (``""`` inherits the Base_Preset).
_POSITION_VALUES = ("", "bottom", "center", "top")

#: Alternative values per non-boolean field, used by :func:`_perturbed` to prove
#: the Options_Digest changes when *any* field changes. Every candidate is inside
#: the field's declared bounds, so ``__post_init__`` cannot clamp it back to the
#: original (and :func:`_perturbed` asserts the field really did change anyway).
_FIELD_CANDIDATES: dict[str, tuple[Any, ...]] = {
    "style": KINETIC_STYLES,
    "reveal": REVEAL_MODES,
    "position": _POSITION_VALUES,
    "preset_name": ("karaoke", "hormozi", "minimal", "boxed"),
    "font_override": ("", "Impact", "Anton"),
    "preset_font": ("Arial", "Impact", "Anton"),
    "notes": ((), ("style_substituted",), ("position_substituted",)),
    "font_size": (84, 120, 200),
    "max_lines": (1, 2, 3, 4),
    "max_line_width": (6, 22, 80),
    "safe_area_x_pct": (0.0, 6.0, 25.0),
    "safe_area_y_pct": (0.0, 10.0, 40.0),
    "motion_duration_ms": (20, 120, 1000),
    "confidence_floor": (0.0, 0.5, 1.0),
    "hook_duration_s": (0.0, 2.5, 30.0),
    "hook_font_size": (12, 110, 400),
}


# --------------------------------------------------------------------------- #
# Harness                                                                       #
# --------------------------------------------------------------------------- #
def _report(mapping: dict, *, default: bool = True):
    """A real ``Capability_Report`` over a counted ``StaticProber``.

    Returns ``(report, prober)``. ``default=True`` grants every unlisted
    capability — in particular every ``font:<family>`` — so the font ladder
    resolves to its first rung and no substitution can degrade the run.
    """
    prober = CountingProber(StaticProber(mapping, default=default))
    return Capability_Report(prober), prober


def _context(
    temp_dir: Any,
    *,
    options: Kinetic_Options,
    words: Any,
    duration: float,
    time_base: Time_Base,
    seed: int,
    capabilities: Any,
    hook_text: str,
    clip_id: str = "clip-1",
) -> Engine_Context:
    """A complete, frozen COMPOSE-stage context whose workspace lives in ``temp_dir``.

    The workspace is allocated by the foundation
    (:func:`~worker.engines.artifacts.allocate_workspace`), keyed by the *real*
    Options_Digest of ``options`` — the same value Property 19's digest clause is
    about — so the file the engine writes is provably inside the Pipeline
    ``temp_dir`` this context was built against.

    ``hook_text`` is handed over on ``clip_metadata`` — the channel the Pipeline
    actually publishes at the COMPOSE hook — and never on ``deps``, which carries
    only the host's injected clock/logger/storage seam (task 12.4).
    """
    root = Path(temp_dir)
    digest = options_digest(options)
    workspace = allocate_workspace(root, JOB_ID, clip_id, ENGINE_ID, digest)
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
        options_digest=digest,
        seed=int(seed),
        workspace=workspace,
        capabilities=capabilities,
        deadline=math.inf,
        clip_metadata={"hook_text": hook_text},
    )


class _Refusing:
    """A callable that records the entry point it guarded and always raises."""

    def __init__(self, name: str, log: list) -> None:
        self.name = name
        self.log = log

    def __call__(self, *args: Any, **kwargs: Any):
        self.log.append(self.name)
        raise AssertionError(f"kinetic typography must not call {self.name}")


@contextlib.contextmanager
def _offline(*, clock: float, pid: int):
    """Run the body with no subprocess, no socket, and a fabricated clock/pid.

    Every process- and socket-creating entry point raises (the design's "patch
    ``subprocess.Popen`` / ``socket.socket`` to raise", widened to the whole
    family so no sibling spelling slips through). ``time.time`` /
    ``time.monotonic`` / ``time.perf_counter`` and ``os.getpid`` return the given
    fixed values, so two invocations can be run in two *different* fabricated
    environments and compared byte for byte.

    Yields the list of refusals that fired — asserted empty by the caller.
    """
    refused: list = []
    patched: list = []

    def _patch(module: Any, attr: str, value: Any) -> None:
        if not hasattr(module, attr):
            return
        patched.append((module, attr, getattr(module, attr)))
        setattr(module, attr, value)

    for module, attr in (
        (subprocess, "Popen"),
        (subprocess, "run"),
        (subprocess, "call"),
        (subprocess, "check_call"),
        (subprocess, "check_output"),
        (os, "system"),
        (os, "popen"),
        (os, "fork"),
        (os, "posix_spawn"),
        (os, "spawnv"),
        (socket, "socket"),
        (socket, "create_connection"),
        (socket, "socketpair"),
        (socket, "getaddrinfo"),
    ):
        _patch(module, attr, _Refusing(f"{module.__name__}.{attr}", refused))

    _patch(time, "time", lambda: float(clock))
    _patch(time, "monotonic", lambda: float(clock))
    _patch(time, "perf_counter", lambda: float(clock))
    _patch(time, "time_ns", lambda: int(clock * 1e9))
    _patch(time, "monotonic_ns", lambda: int(clock * 1e9))
    _patch(os, "getpid", lambda: int(pid))
    try:
        yield refused
    finally:
        for module, attr, original in reversed(patched):
            setattr(module, attr, original)


def _invoke(temp_dir: Any, *, env: dict, **context_kwargs):
    """One complete, independent invocation inside its own fabricated environment.

    A fresh engine (real writer, real keyword planner), a fresh capability report
    and a fresh workspace, run under :func:`_offline`. Returns
    ``(result, ctx, content, prober)`` where ``content`` is the **bytes** of the
    document that landed on disk.
    """
    engine = Kinetic_Typography_Engine()
    report, prober = _report({SUBTITLES_CAPABILITY: True}, default=True)
    ctx = _context(temp_dir, capabilities=report, **context_kwargs)
    with _offline(clock=env["clock"], pid=env["pid"]) as refused:
        result = engine.run(ctx)
        # Also exercise the planning hook and the emitter inside the same guard:
        # ``plan`` is the mapping the host records, and ``emit_ass`` of it must
        # reproduce the written document exactly (Req 11.10).
        planned = engine.plan(ctx)
        emitted = emit_ass(planned)
    assert refused == []                    # zero subprocesses, zero sockets
    path = ctx.workspace.path(ASS_NAME)
    return result, ctx, path.read_bytes(), emitted, planned, prober


def _files_under(root: Path) -> list[Path]:
    """Every file anywhere under ``root``, sorted."""
    return sorted(path for path in Path(root).rglob("*") if path.is_file())


def _events(content: str) -> list[tuple[str, str]]:
    """The ``(style, text)`` pair of every ``Dialogue:`` line, in order."""
    events: list[tuple[str, str]] = []
    for line in content.splitlines():
        if not line.startswith("Dialogue: "):
            continue
        fields = line[len("Dialogue: "):].split(",", 9)
        events.append((fields[3], fields[9] if len(fields) > 9 else ""))
    return events


def _assert_no_environment_leak(content: str, *, forbidden: tuple[str, ...]) -> None:
    """No absolute path, no ISO date, no epoch-shaped digit run, no fake ids."""
    for needle in forbidden:
        assert needle not in content, needle
    assert _ABSOLUTE_PATH.search(content) is None, "absolute path in document"
    assert _ISO_DATE.search(content) is None, "wall-clock date in document"
    assert _LONG_DIGITS.search(content) is None, "epoch-shaped digits in document"


def _perturbed(opts: Kinetic_Options, name: str) -> Kinetic_Options:
    """``opts`` with field ``name`` changed to a different **effective** value.

    Raises ``AssertionError`` when no candidate survives coercion as a genuine
    change, so the digest clause can never pass on a value that was silently
    clamped back to the original (which would make it vacuous).
    """
    current = getattr(opts, name)
    if isinstance(current, bool):
        candidates: tuple[Any, ...] = (not current,)
    else:
        candidates = _FIELD_CANDIDATES[name]
    for candidate in candidates:
        twin = dataclasses.replace(opts, **{name: candidate})
        if getattr(twin, name) != current:
            return twin
    raise AssertionError(f"no differing candidate for Kinetic_Options.{name}")


# --------------------------------------------------------------------------- #
# Property 19 (task 11.1)                                                       #
# --------------------------------------------------------------------------- #
# Feature: kinetic-typography, Property 19: Output is byte-identical, offline, and
# side-effect-free — *For the same* clip bounds, Word_Timeline, Kinetic_Options,
# Time_Base, and seed, two independent invocations produce byte-identical ASS content and
# equal Kinetic_Plan values; across those invocations zero subprocesses are created, zero
# sockets are opened, the ASS content contains no absolute path, no wall-clock value, and
# no process identifier; every file written resolves inside the Pipeline `temp_dir`; the
# emitted `Default` event count is at most the cue count plus one hook event; and the
# Options_Digest is equal for equal options and different for options differing in any
# field.
@settings(max_examples=100, deadline=None)
@given(
    timeline=st_word_timeline(),
    option_fields=st_kinetic_options(),
    time_base=st_time_base(),
    seed=st.integers(min_value=0, max_value=2 ** 32 - 1),
    hook_text=st.sampled_from(["", "watch this", "THE TRUTH ABOUT 2 THINGS"]),
)
def test_p19_output_is_byte_identical_offline_and_side_effect_free(
    timeline, option_fields, time_base, seed, hook_text
):
    """Validates: Requirements 2.2, 11.1, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8, 11.9,
    12.1, 12.3, 12.6, 14.6, 15.1, 15.5, 15.6, 16.4

    Every clause is checked on every example, and each is kept non-vacuous:

    * the two invocations are **asserted** ``applied`` (captions enabled, real
      words, ``ffmpeg_filter:subtitles`` granted, every font available, unlimited
      budget), so a document really was written both times;
    * they use **different** temp dirs, clocks and pids, so byte-identity is a
      statement about leakage, not about reading the same file twice;
    * the "no file outside ``temp_dir``" clause is measured by walking the whole
      Pipeline root, not just the expected path;
    * the event-count bound is re-checked on the fixed reference timeline, where
      the document provably carries one hook event and at least one cue event;
    * the digest clause perturbs **every** field and asserts each perturbation
      genuinely changed the field before asserting the digest changed.
    """
    words, duration = timeline
    # Two independently constructed but equal options values: equal inputs must
    # produce equal digests and equal output, whichever value object was used.
    opts_a = dataclasses.replace(
        Kinetic_Options.parse(option_fields), captions_enabled=True
    )
    opts_b = dataclasses.replace(
        Kinetic_Options.parse(dict(option_fields)), captions_enabled=True
    )
    assert opts_a == opts_b

    with tempfile.TemporaryDirectory() as temp_a, tempfile.TemporaryDirectory() as temp_b:
        shared = dict(
            words=words,
            duration=duration,
            time_base=time_base,
            seed=seed,
            hook_text=hook_text,
        )
        result_a, ctx_a, bytes_a, emitted_a, planned_a, prober_a = _invoke(
            temp_a, env=_ENV_A, options=opts_a, **shared
        )
        result_b, ctx_b, bytes_b, emitted_b, planned_b, prober_b = _invoke(
            temp_b, env=_ENV_B, options=opts_b, **shared
        )

        # --- the precondition, asserted (Reqs 12.1, 12.3) ------------------
        assert result_a.status is Engine_Status.APPLIED, result_a.detail
        assert result_b.status is Engine_Status.APPLIED, result_b.detail
        assert Path(temp_a).resolve() != Path(temp_b).resolve()
        assert ctx_a.workspace.path(ASS_NAME) != ctx_b.workspace.path(ASS_NAME)

        # --- byte-identical content (Reqs 11.1, 11.5, 11.7, 15.6) ----------
        assert bytes_a == bytes_b
        content = bytes_a.decode("utf-8")
        # The serialised plan round-trips back into the emitter byte-for-byte,
        # so the recorded plan and the written document are the same artifact
        # (Req 11.10) — and the emitter is pure in the same environment sense.
        assert emitted_a == emitted_b
        assert emitted_a.encode("utf-8") == bytes_a

        # --- equal Kinetic_Plan values (Reqs 11.1, 11.2) -------------------
        assert planned_a == planned_b
        assert result_a.plan == result_b.plan
        assert Kinetic_Plan.from_dict(planned_a) == Kinetic_Plan.from_dict(planned_b)
        assert Kinetic_Plan.from_dict(result_a.plan) == Kinetic_Plan.from_dict(
            result_b.plan
        )
        assert result_a.markers == result_b.markers

        # --- no environment leakage (Reqs 11.5, 11.7, 11.8, 11.9) ----------
        _assert_no_environment_leak(
            content,
            forbidden=(
                str(temp_a),
                str(temp_b),
                str(ctx_a.workspace.root),
                str(ctx_b.workspace.root),
                JOB_ID,
                ctx_a.clip_id,
                ASS_NAME,
                str(_ENV_A["pid"]),
                str(_ENV_B["pid"]),
                str(int(_ENV_A["clock"])),
                str(int(_ENV_B["clock"])),
            ),
        )

        # --- every written file is inside the Pipeline temp_dir (Req 16.4) --
        for temp_dir, ctx in ((temp_a, ctx_a), (temp_b, ctx_b)):
            root = Path(temp_dir).resolve()
            files = _files_under(temp_dir)
            assert files == [ctx.workspace.path(ASS_NAME)]
            assert files[0].name == ASS_NAME
            assert root in files[0].resolve().parents
            assert root in ctx.workspace.root.resolve().parents or (
                ctx.workspace.root.resolve() == root
            )
        # Nothing escaped into the working directory either.
        assert not (Path.cwd() / ASS_NAME).exists()

        # --- the event budget: cues + at most one hook (Reqs 2.2, 14.6) ----
        cue_count = len(result_a.plan["cues"])
        events = _events(content)
        styles = [style for style, _text in events]
        default_events = styles.count("Default")
        hook_events = styles.count("Hook")
        assert set(styles) <= {"Default", "Hook"}
        assert default_events <= cue_count
        assert hook_events <= 1
        assert len(events) <= cue_count + 1

        # --- the capability oracle was consulted, and cached (Reqs 15.1, 9.6) --
        for prober in (prober_a, prober_b):
            assert prober.count_for(SUBTITLES_CAPABILITY) >= 1
            assert all(prober.count_for(cap) == 1 for cap in set(prober.calls))

        # --- non-vacuity control: a timeline that provably emits events -----
        reference_opts = dataclasses.replace(
            opts_a, captions_enabled=True, hook_enabled=True
        )
        ref, ref_ctx, ref_bytes, _ref_emitted, ref_planned, _ref_prober = _invoke(
            temp_a,
            env=_ENV_A,
            options=reference_opts,
            words=REFERENCE_WORDS,
            duration=REFERENCE_DURATION,
            time_base=time_base,
            seed=seed,
            hook_text="watch this",
            clip_id="clip-reference",
        )
        assert ref.status is Engine_Status.APPLIED, ref.detail
        ref_content = ref_bytes.decode("utf-8")
        ref_events = _events(ref_content)
        ref_styles = [style for style, _text in ref_events]
        ref_cue_count = len(ref_planned["cues"])
        assert ref_cue_count >= 1
        assert ref_styles.count("Hook") == 1            # the hook event exists
        assert 1 <= ref_styles.count("Default") <= ref_cue_count
        assert len(ref_events) <= ref_cue_count + 1
        _assert_no_environment_leak(
            ref_content,
            forbidden=(str(temp_a), str(ref_ctx.workspace.root), JOB_ID, ASS_NAME),
        )

    # --- the Options_Digest (Reqs 11.3, 11.4, 12.6, 15.5) -----------------
    digest_a = options_digest(opts_a)
    assert digest_a == options_digest(opts_b)                   # equal options
    assert digest_a == options_digest(Kinetic_Options.parse(opts_a.to_dict()))
    assert len(digest_a) == 16
    assert digest_a == digest_a.lower()
    assert all(char in "0123456789abcdef" for char in digest_a)

    field_names = tuple(entry.name for entry in dataclasses.fields(Kinetic_Options))
    assert len(field_names) >= 20                    # the whole record is covered
    for name in field_names:
        twin = _perturbed(opts_a, name)
        assert twin != opts_a, name
        assert options_digest(twin) != digest_a, name


# --------------------------------------------------------------------------- #
# Determinism construction rules (task 11.2)                                    #
# --------------------------------------------------------------------------- #
# Single-execution unit tests (no hypothesis needed) for the four *construction*
# rules the design's determinism section states about how the engine is built,
# plus the durable-artifact key. Property 19 above proves the observable output
# is byte-identical; these tests prove the mechanisms that make it so, so a
# regression is caught at its cause rather than only at its symptom:
#
#   1. planning consults ``Engine_Context.rng()`` **zero** times (Req 11.3);
#   2. ``worker/engines/kinetic.py`` imports no ``time`` / ``datetime`` /
#      ``os.getpid`` / ``locale`` symbol (Req 11.5);
#   3. every mapping the emitter reads is consumed in ``sorted(...)`` order, so
#      dict insertion order cannot reach the document (Req 11.4);
#   4. keyword indices arriving as a ``set[int]`` are only membership-tested,
#      never iterated (Req 11.4);
#   5. a durable run persists through the Storage_Backend and records the
#      :func:`~worker.engines.artifacts.artifact_key` in the Engine_Result
#      (Req 12.7).

#: Modules whose mere import would put a clock, a locale, a process identifier
#: or an unseeded randomness source within reach of this module (Reqs 11.3, 11.5).
_FORBIDDEN_IMPORTS = frozenset(
    {
        "time",
        "datetime",
        "calendar",
        "locale",
        "os",
        "random",
        "secrets",
        "uuid",
        "platform",
        "getpass",
        "socket",
        "subprocess",
        "shutil",
        "tempfile",
    }
)

#: Everything ``worker/engines/kinetic.py`` is allowed to import, anywhere in the
#: file (module scope or inside a lazy accessor). Pinned as an allowlist rather
#: than a denylist so a *new* clock-bearing dependency also fails this test.
_ALLOWED_IMPORT_ROOTS = frozenset(
    {"__future__", "collections", "dataclasses", "math", "pathlib", "typing",
     "unicodedata", "worker"}
)

#: Attribute names that would betray a clock, a pid or a locale read even if the
#: owning module were reached without a plain ``import`` statement.
_FORBIDDEN_ATTRIBUTES = frozenset(
    {
        "getpid",
        "getppid",
        "monotonic",
        "monotonic_ns",
        "perf_counter",
        "perf_counter_ns",
        "process_time",
        "time_ns",
        "gmtime",
        "localtime",
        "strftime",
        "utcnow",
        "fromtimestamp",
        "setlocale",
        "getlocale",
        "getdefaultlocale",
        "getrandbits",
        "randrange",
        "uuid4",
    }
)

#: The emitter's own functions: none of them may iterate a mapping at all.
_EMITTER_FUNCTION_NAMES = (
    "emit_ass",
    "_cue_event",
    "_cue_text",
    "_word_span",
    "_style_span",
    "_text_lines",
    "_hook_event",
    "_plan_palette",
    "_caption_anchor",
    "_fallback_style_line",
    "_hook_style_line",
)

#: Calls that expose a mapping's *insertion* order.
_MAPPING_VIEWS = frozenset({"items", "keys", "values"})


def _kinetic_source_tree() -> tuple[str, ast.Module]:
    """The engine module's source text and parsed AST."""
    path = Path(kinetic_module.__file__)
    source = path.read_text(encoding="utf-8")
    return source, ast.parse(source, filename=str(path))


def _imported_roots(tree: ast.Module) -> set[str]:
    """Every module root imported **anywhere** in ``tree`` (lazy imports included)."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import has no module root of its own
                continue
            roots.add((node.module or "").split(".", 1)[0])
    return {root for root in roots if root}


def _function_nodes(tree: ast.Module) -> dict[str, ast.AST]:
    """``name -> node`` for every function/method defined in ``tree``.

    Methods are keyed by their bare name, which is unambiguous here (``to_dict``
    and ``from_dict`` appear on several records, so the *last* definition wins —
    the mapping is only used to look up specific emitter helpers by name).
    """
    found: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[node.name] = node
    return found


def _mapping_view_calls(node: ast.AST) -> list[str]:
    """The ``.items()`` / ``.keys()`` / ``.values()`` call sites inside ``node``."""
    return [
        child.func.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr in _MAPPING_VIEWS
    ]


def _calls_sorted(node: ast.AST) -> bool:
    """True when ``node`` contains at least one ``sorted(...)`` call."""
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == "sorted"
        for child in ast.walk(node)
    )


class _Refusing_Rng:
    """A counting ``Engine_Context.rng`` stand-in that records **and** refuses.

    Recording alone would be defeated by a caller that swallowed the exception;
    raising alone would be defeated by a caller that ignored the return value.
    Doing both means a single consultation is visible either way.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self):
        self.calls.append("rng")
        raise AssertionError("kinetic typography must not consult Engine_Context.rng()")


def _with_counted_rng(ctx: Engine_Context, probe: _Refusing_Rng) -> Engine_Context:
    """An equal :class:`Engine_Context` whose ``rng()`` is ``probe``.

    Subclassing the frozen foundation record (rather than hand-rolling a
    duck-typed stand-in) keeps every other context attribute genuinely real, so
    the engine is exercised against the same object the host would hand it.
    """
    values = {
        entry.name: getattr(ctx, entry.name)
        for entry in dataclasses.fields(ctx)
        if entry.init
    }

    class _Counted_Rng_Context(type(ctx)):  # type: ignore[misc]
        def rng(self):
            return probe()

    return _Counted_Rng_Context(**values)


class _Membership_Only_Set(set):
    """A ``set[int]`` that records membership tests and refuses to be iterated.

    ``caption_presets.plan_keywords`` returns a ``set[int]`` of flat word indices,
    whose iteration order is not part of any contract. Handing this container to
    the planner turns "indices are only membership-tested" into a mechanical fact:
    iterating (or reversing) it fails immediately, while every ``index in
    selected`` test is recorded in :attr:`tested`.
    """

    def __init__(self, values=()) -> None:
        super().__init__(values)
        self.tested: list[Any] = []

    def __contains__(self, item: Any) -> bool:  # type: ignore[override]
        self.tested.append(item)
        return super().__contains__(item)

    def __iter__(self):  # type: ignore[override]
        raise AssertionError("keyword indices must not be iterated (Req 11.4)")

    def __reversed__(self):
        raise AssertionError("keyword indices must not be iterated (Req 11.4)")


class _Recording_Keyword_Planner:
    """A ``caption_presets.plan_keywords``-shaped double returning fixed indices."""

    def __init__(self, indices) -> None:
        self.selection = _Membership_Only_Set(indices)
        self.calls: list[dict] = []

    def __call__(self, words, *, use_ai=False, client=None):
        self.calls.append(
            {"words": list(words), "use_ai": use_ai, "client": client}
        )
        return self.selection


def _reference_plan(**overrides) -> Kinetic_Plan:
    """The fixed reference timeline planned with default options plus ``overrides``."""
    opts = dataclasses.replace(Kinetic_Options(), **overrides)
    return plan_kinetic(
        REFERENCE_WORDS,
        REFERENCE_DURATION,
        Time_Base(fps=30.0),
        opts,
        font=FALLBACK_FONT,
        hook_text="watch this",
    )


def test_planning_never_consults_the_context_rng():
    """Validates: Requirements 11.3

    ``plan_kinetic`` takes no seed and no RNG, and neither ``plan`` nor ``run``
    consults ``Engine_Context.rng()`` — the only randomness source an engine is
    permitted — so the plan cannot vary with the seed. Asserted three ways: the
    counting/refusing RNG records zero calls, the planner's signature carries no
    randomness parameter, and two contexts differing **only** in ``seed`` produce
    byte-identical documents.
    """
    probe = _Refusing_Rng()
    with tempfile.TemporaryDirectory() as temp_dir:
        engine = Kinetic_Typography_Engine()
        report, _prober = _report({SUBTITLES_CAPABILITY: True}, default=True)
        base_ctx = _context(
            temp_dir,
            options=Kinetic_Options(hook_enabled=True),
            words=REFERENCE_WORDS,
            duration=REFERENCE_DURATION,
            time_base=Time_Base(fps=30.0),
            seed=1234,
            capabilities=report,
            hook_text="watch this",
        )
        ctx = _with_counted_rng(base_ctx, probe)

        planned = engine.plan(ctx)
        result = engine.run(ctx)

        # The precondition: the engine really did the work (a gate would make the
        # zero-calls assertion vacuous).
        assert result.status is Engine_Status.APPLIED, result.detail
        assert ctx.workspace.path(ASS_NAME).is_file()
        assert probe.calls == []
        assert planned["cues"], "the reference timeline must plan at least one cue"

        # Nothing seed-shaped reaches the pure planner.
        parameters = tuple(inspect.signature(plan_kinetic).parameters)
        assert parameters == (
            "words",
            "duration",
            "time_base",
            "opts",
            "font",
            "hook_text",
            "keyword_planner",
            "remaining",
            "play_res_x",
            "play_res_y",
        )
        assert not {"rng", "random", "seed"} & set(parameters)

        # ... and the seed the context *does* carry cannot change the output.
        low = _invoke(
            temp_dir,
            env=_ENV_A,
            options=Kinetic_Options(hook_enabled=True),
            words=REFERENCE_WORDS,
            duration=REFERENCE_DURATION,
            time_base=Time_Base(fps=30.0),
            seed=0,
            hook_text="watch this",
            clip_id="clip-seed-low",
        )
        high = _invoke(
            temp_dir,
            env=_ENV_A,
            options=Kinetic_Options(hook_enabled=True),
            words=REFERENCE_WORDS,
            duration=REFERENCE_DURATION,
            time_base=Time_Base(fps=30.0),
            seed=2 ** 32 - 1,
            hook_text="watch this",
            clip_id="clip-seed-high",
        )
        assert low[0].status is Engine_Status.APPLIED
        assert high[0].status is Engine_Status.APPLIED
        assert low[2] == high[2]            # byte-identical documents
        assert low[4] == high[4]            # equal serialised plans


def test_the_engine_module_imports_no_clock_locale_or_process_symbol():
    """Validates: Requirements 11.5

    ``worker/engines/kinetic.py`` imports no ``time``, ``datetime``, ``os`` (hence
    no ``os.getpid``), ``locale`` or unseeded-randomness symbol — anywhere in the
    file, lazy accessors included — so there is no wall-clock, locale or process
    value in reach to embed in a plan or a document. The import surface is pinned
    as an **allowlist**, so a newly added dependency that carries a clock fails
    here too.
    """
    source, tree = _kinetic_source_tree()
    roots = _imported_roots(tree)

    assert not (roots & _FORBIDDEN_IMPORTS), sorted(roots & _FORBIDDEN_IMPORTS)
    assert roots <= _ALLOWED_IMPORT_ROOTS, sorted(roots - _ALLOWED_IMPORT_ROOTS)
    # The scan is non-vacuous: the module really does import things.
    assert {"dataclasses", "math", "unicodedata", "worker"} <= roots

    # No attribute access betrays a clock/pid/locale reached another way.
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert not (attributes & _FORBIDDEN_ATTRIBUTES), sorted(
        attributes & _FORBIDDEN_ATTRIBUTES
    )

    # And none of those names is bound in the imported module's namespace.
    for name in sorted(_FORBIDDEN_IMPORTS):
        assert getattr(kinetic_module, name, None) is None, name

    # The lazy ``worker.captions`` accessor is the module's *only* function-scope
    # import, which is what keeps module import free of ``pydantic`` (Req 1.4).
    lazy = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and node.col_offset > 0
    ]
    assert [
        f"{getattr(node, 'module', '')}.{node.names[0].name}" for node in lazy
    ] == ["worker.captions"]
    assert "def _captions()" in source


def test_the_emitter_consumes_every_mapping_in_sorted_order():
    """Validates: Requirements 11.4

    The only mapping ``emit_ass`` reads is the plan's colour palette, and it can
    only ever see it in sorted key order: ``Kinetic_Plan.__post_init__`` normalises
    any supplied mapping through ``sorted(...)`` at the boundary, ``to_dict``
    re-emits every level in sorted key order, and the emitter itself never
    iterates a mapping at all (it reads by explicit key). Asserted behaviourally —
    reordering the mapping cannot change one byte of the document — and
    structurally, so the guarantee cannot be quietly removed.
    """
    plan = _reference_plan()
    assert plan.colors, "the reference plan must carry a palette"
    assert list(plan.colors) == sorted(plan.colors)

    # (a) A plan built from a *reversed* palette is normalised back to sorted
    #     order, and emits byte-identically.
    reversed_colors = {key: plan.colors[key] for key in reversed(list(plan.colors))}
    assert list(reversed_colors) != list(plan.colors)      # the input really differs
    twin = dataclasses.replace(plan, colors=reversed_colors)
    assert list(twin.colors) == sorted(twin.colors)
    assert emit_ass(twin) == emit_ass(plan)

    # (b) Every level of the serialised plan is in sorted key order, and feeding
    #     the emitter a shuffled serialisation changes nothing either.
    payload = plan.to_dict()
    assert list(payload) == sorted(payload)
    assert list(payload["colors"]) == sorted(payload["colors"])
    for cue in payload["cues"]:
        assert list(cue) == sorted(cue)
        for word in cue["words"]:
            assert list(word) == sorted(word)

    shuffled = {key: payload[key] for key in reversed(list(payload))}
    shuffled["colors"] = reversed_colors
    assert list(shuffled) != list(payload)
    assert emit_ass(shuffled) == emit_ass(payload) == emit_ass(plan)

    # (c) Structural: the emitter's own functions never touch a mapping view, and
    #     every function in the module that *does* iterate a mapping sorts it.
    _source, tree = _kinetic_source_tree()
    functions = _function_nodes(tree)
    for name in _EMITTER_FUNCTION_NAMES:
        assert name in functions, name
        assert _mapping_view_calls(functions[name]) == [], name

    iterating = {
        name: views
        for name, node in functions.items()
        if (views := _mapping_view_calls(node))
    }
    assert iterating, "expected at least one mapping-consuming helper"
    for name, views in sorted(iterating.items()):
        assert _calls_sorted(functions[name]), f"{name} iterates {views} unsorted"


def test_keyword_indices_are_only_membership_tested():
    """Validates: Requirements 11.4

    The keyword planner's ``set[int]`` of flat word indices is consumed **only**
    as ``index in selected`` against a positional index: the container is never
    iterated, sorted or unpacked, so its (unspecified) iteration order cannot
    reach the plan. Proved with a ``set`` subclass whose ``__iter__`` /
    ``__reversed__`` raise and whose ``__contains__`` records every test.
    """
    planner = _Recording_Keyword_Planner({1})
    opts = Kinetic_Options(highlight_keywords=True, keyword_ai=True)
    plan = plan_kinetic(
        REFERENCE_WORDS,
        REFERENCE_DURATION,
        Time_Base(fps=30.0),
        opts,
        font=FALLBACK_FONT,
        keyword_planner=planner,
    )

    words = [word for cue in plan.cues for word in cue.words]
    assert len(words) == len(REFERENCE_WORDS)              # nothing was dropped

    # The planner was consulted exactly once, offline (``client=None``), with the
    # surviving words in emission order.
    assert len(planner.calls) == 1
    call = planner.calls[0]
    assert call["client"] is None
    assert call["use_ai"] is True
    assert [word.text for word in call["words"]] == [
        word.text for word in REFERENCE_WORDS
    ]

    # Membership was tested once per positional index, in order.
    assert planner.selection.tested == list(range(len(words)))
    assert [word.emphasis for word in words] == [False, True, False]

    # A differently-spelled selection of the same indices plans identically, and
    # the emitted document is unchanged by how the container was built.
    twin_planner = _Recording_Keyword_Planner(frozenset({1}))
    twin = plan_kinetic(
        REFERENCE_WORDS,
        REFERENCE_DURATION,
        Time_Base(fps=30.0),
        opts,
        font=FALLBACK_FONT,
        keyword_planner=twin_planner,
    )
    assert twin == plan
    assert emit_ass(twin) == emit_ass(plan)


def test_a_durable_run_records_the_artifact_key_through_recording_storage():
    """Validates: Requirements 12.7

    With ``durable_subtitle`` set, the ASS document is persisted through the
    active Storage_Backend under
    ``engines/<job>/<clip>/kinetic_typography/kinetic.ass`` and the resulting key
    is recorded in the Engine_Result the caller holds. Driven through the real
    foundation path — ``Engine_Host.run_stage`` then ``finish_clip`` — with
    ``tests.fakes.RecordingStorage`` injected as the backend, so both the key and
    the recorded bytes are asserted rather than assumed.

    The flag-off case is asserted in the same test: a non-durable run stores
    nothing and records no key.
    """
    class _Options:
        """A minimal Processing_Options stand-in (the flat fields land in task 13.1)."""

        def __init__(self, *, durable: bool) -> None:
            self.kinetic_typography_enabled = True
            self.captions = True
            self.hook_title = False
            self.permissibility_mode = False
            self.caption_preset = "karaoke"
            self.durable_subtitle = durable

    def _run_clip(temp_dir: str, *, durable: bool, clip_id: str):
        storage = RecordingStorage()
        registry = Engine_Registry()
        registry.register(Kinetic_Typography_Engine())
        report, _prober = _report({SUBTITLES_CAPABILITY: True}, default=True)
        host = Engine_Host(
            _Options(durable=durable),
            job_id=JOB_ID,
            temp_dir=temp_dir,
            registry=registry,
            capabilities=report,
            storage=storage,
        )
        outcome = host.run_stage(
            Engine_Stage.COMPOSE,
            clip_id=clip_id,
            source=Path(temp_dir) / "source.mp4",
            clip_path=Path(temp_dir) / f"{clip_id}.mp4",
            clip_start=0.0,
            clip_end=REFERENCE_DURATION,
            duration=REFERENCE_DURATION,
            words=REFERENCE_WORDS,
        )
        result = outcome.results[0]
        assert result.status is Engine_Status.APPLIED, result.detail
        assert result.contribution is not None
        assert len(result.artifacts) == 1
        document = result.artifacts[0].path.read_bytes()
        markers = host.finish_clip(clip_id)
        return storage, outcome, markers, document

    with tempfile.TemporaryDirectory() as temp_dir:
        # --- durable: persisted, and the key is recorded -------------------
        storage, outcome, markers, document = _run_clip(
            temp_dir, durable=True, clip_id="clip-durable"
        )
        expected_key = artifact_key(JOB_ID, "clip-durable", ENGINE_ID, ASS_NAME)
        assert expected_key == f"engines/{JOB_ID}/clip-durable/{ENGINE_ID}/{ASS_NAME}"

        assert storage.saved_keys == [expected_key]
        assert [key for key, _path in storage.save_file_calls] == [expected_key]
        assert storage.exists(expected_key)
        assert storage.open(expected_key).read() == document
        assert markers == []                       # nothing failed to persist

        stored = outcome.results[0].artifacts[0]
        assert stored.durable is True
        assert stored.media_type == "subtitle"
        assert stored.name == ASS_NAME
        assert stored.storage_key == expected_key

        # --- flag off: nothing stored, no key recorded --------------------
        plain_storage, plain_outcome, plain_markers, _plain_document = _run_clip(
            temp_dir, durable=False, clip_id="clip-transient"
        )
        assert plain_storage.saved_keys == []
        assert plain_markers == []
        plain_artifact = plain_outcome.results[0].artifacts[0]
        assert plain_artifact.durable is False
        assert plain_artifact.storage_key == ""
