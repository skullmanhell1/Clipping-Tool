"""One implementation of the per-word ASS animation span.

There were two, and they were required to agree **byte for byte**.

``captions.build_word_span`` and ``engines.kinetic._style_span`` both emitted the animation tags
for a single word, and four styles — ``none``, ``pop``, ``typewriter`` and ``karaoke_fill`` — had
to produce identical output because a Kinetic_Plan at ``reveal="cumulative"`` is specified to
render the v0.8.0 caption look exactly. That agreement was maintained by two separately-written
f-strings and a Hypothesis property test comparing them
(``tests/test_kinetic_ass.py::test_p8_shared_styles_reproduce_build_word_span_semantics``).

The property test is a good test. It is not a substitute for there being one implementation: it
can only report that the two copies have diverged, after they have, and the tags it compares are
dense enough (``{\\fscx60\\fscy60\\t(0,120,\\fscx100\\fscy100)}``) that a divergence is far easier
to introduce than to read. In particular the ``+120`` and ``+30`` ramps were written out twice
each and are deliberately *not* ``motion_ms``, so the obvious "tidy-up" of routing them through
the duration parameter would have been wrong in both files independently.

**This module owns the span shapes; the callers own everything around them.** The two callers do
not have the same input contract, and that asymmetry is why the split falls here:

* ``captions.build_word_span`` receives a raw word. It masks profanity, escapes the text and
  computes ``rel_ms`` itself, then decorates the span with the punch (C10), the pill (C9) and
  either the highlight wrap or the low-confidence dim (T7).
* ``kinetic._style_span`` receives text the planner has already escaped and a ``rel_ms`` it has
  already computed, and its caller adds the emoji and the word-by-word reveal gate.

Neither of those belongs here. What is shared is exactly the mapping from a style name plus a few
numbers to a tag string, which is what this module is.

**Import-safe by construction: this module imports nothing at all.** ``worker.engines.kinetic``
must be importable without pulling ``config`` and therefore ``pydantic``
(``tests/test_engines_base.py::test_every_engine_module_imports_without_heavy_dependencies``
proves it in a fresh interpreter), which is why that module reaches ``worker.captions`` through a
lazy ``_captions()`` accessor. A shared helper living in ``captions`` would have been unusable
from kinetic at module scope; a shared helper with no imports is usable from both.
"""

from __future__ import annotations

#: Peak scale of the ``bounce`` overshoot, as an ASS ``\fscx``/``\fscy`` percentage.
#:
#: Above 100 on purpose — the word passes through its final size and settles back, which is what
#: makes it read as a bounce rather than a grow. Re-exported by ``worker.engines.kinetic`` under
#: its original name, because that is where it was public.
BOUNCE_OVERSHOOT = 118

#: Start scale of the ``bounce`` ramp.
BOUNCE_START_SCALE = 55

#: Start scale of the ``pop`` ramp.
POP_START_SCALE = 60

#: The ``pop`` scale ramp, in milliseconds.
#:
#: A fixed literal, **not** the caller's ``motion_ms``. ``pop`` and ``typewriter`` reproduce
#: v0.8.0 output, and v0.8.0 had no configurable motion duration — so making these follow
#: ``motion_ms`` would change the rendered result for every existing configuration, which is the
#: one thing the shared styles exist to prevent.
POP_RAMP_MS = 120

#: The ``typewriter`` alpha reveal, in milliseconds. Fixed for the same reason as
#: :data:`POP_RAMP_MS`.
TYPEWRITER_RAMP_MS = 30

#: The styles that predate the kinetic engine and must render identically in both callers.
#:
#: ``none`` is absent because it is the fall-through rather than a branch: every caller returns the
#: plain escaped word for a style it does not recognise, and ``none`` is simply the commonest
#: unrecognised style.
LEGACY_ANIMATIONS = frozenset({"pop", "typewriter", "karaoke_fill"})

#: Every style :func:`animation_span` knows. The three beyond :data:`LEGACY_ANIMATIONS` are
#: kinetic-only; a caption preset cannot select them (``caption_presets.VALID_ANIMATIONS`` is the
#: legacy four), which is why ``build_word_span`` gates on ``LEGACY_ANIMATIONS`` rather than
#: passing any style straight through.
KINETIC_ANIMATIONS = LEGACY_ANIMATIONS | frozenset({"bounce", "highlight_sweep", "slide_up"})


def animation_span(
    style: str,
    escaped: str,
    *,
    rel_ms: int,
    duration_s: float = 0.0,
    motion_ms: int = 0,
    primary: str = "&H00FFFFFF",
    highlight: str = "&H0000E5FF",
) -> str:
    """The ASS-tagged span for one word, for ``style``.

    Args:
        style: The animation name. An unrecognised style returns ``escaped`` unchanged, which is
            how ``none`` is handled and what makes an unknown style degrade to a plain word rather
            than to an exception.
        escaped: The word's text, **already escaped**. This function does no escaping: its two
            callers escape at different points (the kinetic planner does it upstream), and doing
            it here as well would double-escape one of them.
        rel_ms: The word's onset relative to its *cue* start, in milliseconds. This is the offset
            libass ``\\t`` expects — not an absolute timestamp.
        duration_s: The word's own duration in seconds. Used only by ``karaoke_fill``, which needs
            it in centiseconds for ``\\kf``.
        motion_ms: The configurable motion duration, used only by ``bounce`` and
            ``highlight_sweep``. ``pop`` and ``typewriter`` ignore it deliberately — see
            :data:`POP_RAMP_MS`.
        primary: Fill colour in ASS ``&HAABBGGRR`` form, used only by ``highlight_sweep``.
        highlight: Emphasis colour, used only by ``highlight_sweep``.

    Returns:
        A well-formed span: every ``{`` opened is closed in the same expression, and because
        ``escaped`` is pre-escaped no transcript text can unbalance the braces.
    """
    rel = int(rel_ms)

    if style == "karaoke_fill":
        # Centiseconds, floored at 1: `\kf0` is a sweep with no duration, which libass renders as
        # an un-swept word - so a zero-length word would silently lose its karaoke fill.
        dur_cs = max(1, int(round(duration_s * 100)))
        return f"{{\\kf{dur_cs}}}{escaped}"

    if style == "pop":
        return (
            f"{{\\fscx{POP_START_SCALE}\\fscy{POP_START_SCALE}"
            f"\\t({rel},{rel + POP_RAMP_MS},"
            f"\\fscx100\\fscy100)}}{escaped}"
        )

    if style in ("typewriter", "slide_up"):
        # `slide_up` carries the event-level `\move` (added by the kinetic emitter) plus this
        # per-word alpha gate, so its words still appear on beat rather than all at once.
        return (
            f"{{\\alpha&HFF&\\t({rel},{rel + TYPEWRITER_RAMP_MS},\\alpha&H00&)}}{escaped}"
        )

    duration = int(motion_ms)

    if style == "bounce":
        half = duration // 2
        return (
            f"{{\\fscx{BOUNCE_START_SCALE}\\fscy{BOUNCE_START_SCALE}"
            f"\\t({rel},{rel + half},"
            f"\\fscx{BOUNCE_OVERSHOOT}\\fscy{BOUNCE_OVERSHOOT})"
            f"\\t({rel + half},{rel + duration},\\fscx100\\fscy100)}}{escaped}"
        )

    if style == "highlight_sweep":
        return (
            f"{{\\c{highlight}&\\t({rel},{rel + duration},"
            f"\\c{primary}&)}}{escaped}"
        )

    return escaped
