"""Kinetic typography — engine-surface properties (spec task 3.6).

Covers **Property 10** from the kinetic-typography design (an unrecognised
Kinetic_Style falls back once, and names it) plus the vocabulary pin that keeps
``tests/strategies.py`` and ``worker/engines/kinetic.py`` from drifting apart.

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

from hypothesis import given, settings
from hypothesis import strategies as st

from tests import strategies
from tests.strategies import st_options_mapping
from worker.engines import kinetic
from worker.engines.kinetic import DEFAULT_STYLE, KINETIC_STYLES, Kinetic_Options
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


def _carrier(hostile, style):
    """A Processing_Options carrying ``style`` plus the drawn hostile noise.

    Every key of the hostile mapping is attached as an attribute, so resolution
    is proved to ignore option keys it does not know while still reading the one
    key it does (``kinetic_style``). ``kinetic_style`` is set last so the noise
    can never shadow it.
    """
    options = ProcessingOptions()
    for key, value in hostile.items():
        if isinstance(key, str) and key != "kinetic_style":
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
