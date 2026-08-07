"""Options serialization / defaults tests for the Tier 1 Creator Output Upgrade.

Covers the new ``ProcessingOptions`` fields added in task 1.1:

- Property 25 (P25): new option fields round-trip through ``from_dict``/``asdict``.
- Property 26 (P26): malformed / unknown option values apply documented defaults
  and never raise.
- Unit tests: all new visual/audio/rights features default OFF, pre-existing
  v0.6.0 fields keep their defaults, and ``effective_options`` enforces
  permissibility mode.
"""

from __future__ import annotations

from dataclasses import asdict

from hypothesis import given, settings
from hypothesis import strategies as st

from worker.models import ProcessingOptions, effective_options

# --- Known value sets / documented defaults for the new enum-like fields ----
CAPTION_PRESETS = ("karaoke", "boxed", "minimal", "pop", "typewriter", "hormozi")
CAPTION_ANIMATIONS = ("", "none", "pop", "typewriter", "karaoke_fill")
BROLL_INTENSITIES = ("off", "subtle", "standard", "heavy")
ASSET_SOURCING_MODES = ("off", "local_only", "local_then_external")

# Documented defaults applied when a value is unknown / malformed.
ENUM_DEFAULTS = {
    "caption_preset": "karaoke",
    "caption_animation": "",
    "broll_intensity": "standard",
    "asset_sourcing_mode": "off",
}

# The full set of fields introduced by the Tier 1 upgrade (task 1.1).
NEW_FIELDS = (
    "caption_preset",
    "caption_animation",
    "caption_keyword_highlight",
    "caption_keyword_ai",
    "caption_emoji",
    "broll",
    "broll_intensity",
    "asset_sourcing_mode",
    "broll_provider",
    "selection_prompt",
    "visual_selection",
    "permissibility_mode",
)


# --- Strategies -------------------------------------------------------------
def _valid_new_options_dicts():
    """Dicts covering the new fields with *valid* values for enum-like ones."""
    return st.fixed_dictionaries(
        {
            "caption_preset": st.sampled_from(CAPTION_PRESETS),
            "caption_animation": st.sampled_from(CAPTION_ANIMATIONS),
            "caption_keyword_highlight": st.booleans(),
            "caption_keyword_ai": st.booleans(),
            "caption_emoji": st.booleans(),
            "broll": st.booleans(),
            "broll_intensity": st.sampled_from(BROLL_INTENSITIES),
            "asset_sourcing_mode": st.sampled_from(ASSET_SOURCING_MODES),
            "broll_provider": st.text(max_size=20),
            "selection_prompt": st.text(max_size=40),
            "visual_selection": st.booleans(),
            "permissibility_mode": st.booleans(),
        }
    )


# --- Property 25 ------------------------------------------------------------
# Feature: tier1-creator-output-upgrade, Property 25: New option fields round-trip
@settings(max_examples=100)
@given(_valid_new_options_dicts())
def test_p25_new_option_fields_round_trip(data):
    """Validates: Requirements 16.4

    For any options dict, parsing then serialising (``asdict``) and parsing the
    result again preserves every new field without loss.
    """
    first = ProcessingOptions.from_dict(data)
    # ProcessingOptions has no to_dict(); use dataclasses.asdict as the
    # serialised form, then round-trip it back through from_dict.
    serialised = asdict(first)
    second = ProcessingOptions.from_dict(serialised)

    for f in NEW_FIELDS:
        assert getattr(second, f) == getattr(first, f), f"field {f} not preserved"


# --- Property 26 ------------------------------------------------------------
def _enum_field_junk():
    """Arbitrary (mostly out-of-set) values for the enum-like fields."""
    return st.one_of(
        st.text(max_size=20),
        st.integers(),
        st.booleans(),
        st.none(),
        st.floats(allow_nan=False, allow_infinity=False),
    )


# Feature: tier1-creator-output-upgrade, Property 26: Malformed or unknown option values apply documented defaults
@settings(max_examples=100)
@given(
    caption_preset=_enum_field_junk(),
    caption_animation=_enum_field_junk(),
    broll_intensity=_enum_field_junk(),
    asset_sourcing_mode=_enum_field_junk(),
    unknown=st.dictionaries(
        st.text(min_size=1, max_size=10).filter(
            lambda k: k not in ProcessingOptions.__dataclass_fields__
        ),
        st.text(max_size=10),
        max_size=3,
    ),
)
def test_p26_malformed_values_apply_defaults(
    caption_preset, caption_animation, broll_intensity, asset_sourcing_mode, unknown
):
    """Validates: Requirements 16.5, 22.4

    Malformed / unrecognised values for the enum-like fields (and any unknown
    keys) never cause ``from_dict`` to raise, and out-of-set values fall back to
    the documented default.
    """
    data = {
        "caption_preset": caption_preset,
        "caption_animation": caption_animation,
        "broll_intensity": broll_intensity,
        "asset_sourcing_mode": asset_sourcing_mode,
    }
    data.update(unknown)

    opts = ProcessingOptions.from_dict(data)  # must not raise

    for field_name, known in (
        ("caption_preset", CAPTION_PRESETS),
        ("caption_animation", CAPTION_ANIMATIONS),
        ("broll_intensity", BROLL_INTENSITIES),
        ("asset_sourcing_mode", ASSET_SOURCING_MODES),
    ):
        value = getattr(opts, field_name)
        # Resolved value is always a member of the known set.
        assert value in known
        # Out-of-set inputs specifically resolve to the documented default.
        if data[field_name] not in known:
            assert value == ENUM_DEFAULTS[field_name]


# --- Unit tests: the shipped defaults, pinned (task 1.6, U1) ----------------
#
# These tests pinned "every new feature defaults off", which was the right call while the
# features were being staged: each spec landed without disturbing existing behaviour.
#
# U1 is the deliberate end of that staging. A default run enabled only captions, 9:16, the
# ``ai`` strategy and metadata, so out of the box the product produced a static centre crop
# with plain captions and every feature that makes short-form video look modern had to be
# found one checkbox at a time. The defaults below are the shipped ones, still pinned - the
# value of these tests is that a default cannot drift unnoticed, not that it must be False.
def test_output_shaping_features_default_on():
    """U1: the features that decide how a clip *looks* are on out of the box."""
    o = ProcessingOptions()
    assert o.reframe is True  # V1: was a centre crop that decapitated speakers
    assert o.zoom is True
    assert o.transitions is True
    assert o.fades is True
    assert o.hook_title is True  # V12
    assert o.progress_bar is True
    assert o.emoji == "standard"
    assert o.caption_keyword_highlight is True
    assert o.caption_emoji is True
    assert o.visual_selection is True


def test_features_whose_assets_do_not_exist_yet_default_off():
    """The three exceptions, each because enabling it today makes output *worse*.

    Not oversights - turning any of these on now would add degradation, a drone, or a
    silent component swap. Each becomes a default when the work it waits on lands.
    """
    o = ProcessingOptions()
    # A14/A15: audio.py synthesises two sine waves per mood; assets/music is empty.
    assert o.music == ""
    # A18/A21: broll matches filename stems against assets/broll, which is empty.
    assert o.broll is False
    assert o.asset_sourcing_mode == "off"
    assert o.broll_provider == ""
    # An AV engine that takes ownership of the caption layer; belongs to a profile (U2).
    assert o.kinetic_typography_enabled is False
    assert o.stem_inpainting_enabled is False
    # Removes content rather than restyling it, and cuts hard on sparse-speech footage.
    assert o.filler_removal is False


def test_costly_or_policy_features_default_off():
    """Defaults must not silently spend money or narrow what the tool will do."""
    o = ProcessingOptions()
    assert o.caption_keyword_ai is False  # an LLM call per clip
    assert o.permissibility_mode is False  # a restriction, not a feature
    assert o.diarization is False  # needs the ML extras to do better than degrade
    assert o.speaker_reframe is False


def test_preexisting_fields_keep_v060_defaults():
    """Validates: Requirements 22.1 — the v0.6.0 fields whose defaults U1 did not touch."""
    o = ProcessingOptions()
    assert o.aspect == "9:16"
    assert o.captions is True
    assert o.caption_template == "karaoke"
    assert o.caption_position == "bottom"
    assert o.caption_preset == "karaoke"
    assert o.caption_animation == ""
    assert o.selection_prompt == ""
    assert o.broll_intensity == "standard"
    assert o.clip_length == "auto"
    assert o.num_clips == "auto"
    assert o.strategy == "ai"
    assert o.metadata is True


def test_all_off_helper_covers_every_effect():
    """The test helper that disables every effect must not fall behind the model.

    ``tests/conftest.options_all_off`` is how a dozen tests say "nothing enabled" now that
    the defaults are on. If a new default-on effect is not listed there, those tests keep
    passing while quietly testing something else.
    """
    from tests.conftest import assert_effects_off_is_exhaustive

    assert_effects_off_is_exhaustive()


def test_effective_options_permissibility_mode():
    """Validates: Requirements 16.2, 22.1 — permissibility forces safe defaults.

    With permissibility mode on, added audio is disabled and asset sourcing is
    forced to local_only regardless of the requested mode; the input object is
    not mutated (pure).
    """
    o = ProcessingOptions(
        permissibility_mode=True,
        music="upbeat",
        asset_sourcing_mode="local_then_external",
    )
    eff = effective_options(o)
    assert eff.music == ""
    assert eff.asset_sourcing_mode == "local_only"
    # Purity: original unchanged.
    assert o.music == "upbeat"
    assert o.asset_sourcing_mode == "local_then_external"


def test_effective_options_noop_when_permissibility_off_and_no_external():
    """A plain off-sourcing config is returned unchanged by effective_options."""
    o = ProcessingOptions()
    eff = effective_options(o)
    assert eff.asset_sourcing_mode == "off"
    assert eff.music == ""


# ===========================================================================
# v0.8.0 — Speaker Diarisation & Multi-Speaker Reframe (task 1.4 / 1.5)
# ===========================================================================
# New ProcessingOptions fields added in task 1.1:
#   diarization: bool = False
#   speaker_reframe: bool = False
#   reframe_layout: str = "follow_active"   (follow_active | split_screen)
#   reframe_intensity: str = "standard"     (subtle | standard | heavy)

# Known value sets / documented defaults for the new enum-like fields.
REFRAME_LAYOUTS = ("follow_active", "split_screen")
REFRAME_INTENSITIES = ("subtle", "standard", "heavy")
REFRAME_ENUM_DEFAULTS = {
    "reframe_layout": "follow_active",
    "reframe_intensity": "standard",
}

# The set of fields introduced by the v0.8.0 upgrade (task 1.1).
V080_FIELDS = (
    "diarization",
    "speaker_reframe",
    "reframe_layout",
    "reframe_intensity",
)


def _boolish():
    """Arbitrary truthy/falsy inputs accepted by the bool-coercion path."""
    return st.one_of(
        st.booleans(),
        st.sampled_from(["true", "false", "1", "0", "yes", "no", "on", "off", ""]),
        st.integers(),
        st.none(),
        st.text(max_size=8),
    )


def _reframe_enum_value(valid):
    """Sometimes a valid enum member, sometimes arbitrary/garbage input."""
    return st.one_of(
        st.sampled_from(valid),
        st.text(max_size=20),
        st.integers(),
        st.booleans(),
        st.none(),
        st.floats(allow_nan=False, allow_infinity=False),
    )


def _v080_options_dicts():
    """Dicts covering the new v0.8.0 fields, mixing valid + garbage values."""
    return st.fixed_dictionaries(
        {
            "diarization": _boolish(),
            "speaker_reframe": _boolish(),
            "reframe_layout": _reframe_enum_value(REFRAME_LAYOUTS),
            "reframe_intensity": _reframe_enum_value(REFRAME_INTENSITIES),
        }
    )


# --- Property 25 ------------------------------------------------------------
# Feature: speaker-diarization-reframe, Property 25: New option fields round-trip and unknown values apply defaults
@settings(max_examples=100)
@given(_v080_options_dicts())
def test_p25_v080_fields_round_trip_and_defaults(data):
    """Validates: Requirements 17.3, 17.4, 18.5

    For any options dict, parsing then serialising via ``dataclasses.asdict``
    and parsing the result again preserves ``diarization``, ``speaker_reframe``,
    ``reframe_layout`` and ``reframe_intensity`` without loss; and any
    malformed / unrecognised ``reframe_layout`` / ``reframe_intensity`` value
    applies the documented default (``follow_active`` / ``standard``) without
    raising.
    """
    first = ProcessingOptions.from_dict(data)  # must not raise
    # ProcessingOptions has no to_dict(); dataclasses.asdict is the serialised
    # form, round-tripped back through from_dict.
    serialised = asdict(first)
    second = ProcessingOptions.from_dict(serialised)

    # Round-trip preserves every new field without loss.
    for f in V080_FIELDS:
        assert getattr(second, f) == getattr(first, f), f"field {f} not preserved"

    # Bool toggles always coerce to real booleans.
    assert isinstance(first.diarization, bool)
    assert isinstance(first.speaker_reframe, bool)

    # Enum-like fields always resolve to a known member, and out-of-set inputs
    # fall back specifically to the documented default.
    for field_name, known in (
        ("reframe_layout", REFRAME_LAYOUTS),
        ("reframe_intensity", REFRAME_INTENSITIES),
    ):
        value = getattr(first, field_name)
        assert value in known
        if data[field_name] not in known:
            assert value == REFRAME_ENUM_DEFAULTS[field_name]


# --- Unit tests: defaults OFF and toggles independent (task 1.5) -----------
def test_v080_fields_default_off():
    """Validates: Requirements 16.1, 16.2, 16.3, 17.1 — new fields default OFF.

    A default-constructed ``ProcessingOptions`` and ``from_dict({})`` both
    disable diarisation / speaker reframe and use the documented layout /
    intensity defaults.
    """
    for o in (ProcessingOptions(), ProcessingOptions.from_dict({})):
        assert o.diarization is False
        assert o.speaker_reframe is False
        assert o.reframe_layout == "follow_active"
        assert o.reframe_intensity == "standard"


def test_v080_toggles_are_independent():
    """Validates: Requirements 16.1, 16.2 — the two toggles are independent.

    Setting one of ``diarization`` / ``speaker_reframe`` does not affect the
    other.
    """
    diar_only = ProcessingOptions.from_dict({"diarization": True})
    assert diar_only.diarization is True
    assert diar_only.speaker_reframe is False

    reframe_only = ProcessingOptions.from_dict({"speaker_reframe": True})
    assert reframe_only.speaker_reframe is True
    assert reframe_only.diarization is False


def test_v080_additions_do_not_disturb_v070_defaults():
    """Validates: Requirements 16.3, 17.1 — pre-existing fields keep defaults.

    Enabling only the new v0.8.0 toggles leaves every pre-existing v0.7.0
    field at its default, confirming the new fields are the only additions.
    """
    base = ProcessingOptions()
    o = ProcessingOptions(diarization=True, speaker_reframe=True)

    # Every field other than the four new ones keeps its v0.7.0 default.
    for name in ProcessingOptions.__dataclass_fields__:
        if name in V080_FIELDS:
            continue
        assert getattr(o, name) == getattr(base, name), f"field {name} changed"

    # Representative sanity checks on a spread of existing defaults. The subject here is
    # that the v0.8.0 toggles add fields without disturbing their neighbours, so these
    # track the shipped defaults rather than asserting they are off; U1 turned several on
    # deliberately, and the per-default contract lives in the three tests above.
    assert base.aspect == "9:16"
    assert base.captions is True
    assert base.reframe is True  # U1/V1
    assert base.caption_template == "karaoke"
    assert base.caption_preset == "karaoke"
    assert base.emoji == "standard"  # U1
    assert base.music == ""  # still off: A14 has not shipped real beds
    assert base.permissibility_mode is False


# ===========================================================================
# Advanced AV engines foundation (task 12.4)
# ===========================================================================
# This spec registers no engines, so it adds no ProcessingOptions fields. What
# it *does* fix is the Feature_Flag convention every sibling engine spec relies
# on: `<engine_id>_enabled`, absent-or-False on a fresh instance, surviving the
# `from_dict` / `asdict` round-trip untouched.

import math  # noqa: E402

from tests.fakes import FakeEngine  # noqa: E402
from tests.strategies import st_engine_id, st_options_mapping  # noqa: E402
from worker.engines.base import FLAG_SUFFIX, AV_Engine, Engine_Stage  # noqa: E402
from worker.engines.capabilities import reset_report  # noqa: E402
from worker.engines.registry import get_registry, register, reset_registry  # noqa: E402


def _options_equal(a: ProcessingOptions, b: ProcessingOptions) -> bool:
    """Field-wise equality treating two NaN floats as equal.

    `ProcessingOptions` is a plain dataclass, so `==` compares field values —
    and a NaN that survives coercion (e.g. a float NaN handed to `range_start`)
    would make an otherwise faithful round-trip compare unequal purely because
    `nan != nan`. Round-tripping such a value is still lossless, so NaN-vs-NaN
    counts as preserved here.
    """
    fa, fb = asdict(a), asdict(b)
    if fa.keys() != fb.keys():
        return False
    for name, left in fa.items():
        right = fb[name]
        if (
            isinstance(left, float)
            and isinstance(right, float)
            and math.isnan(left)
            and math.isnan(right)
        ):
            continue
        if left != right:
            return False
    return True


# Feature: av-engines-foundation, Property 35: Engine option fields round-trip through ProcessingOptions
@settings(max_examples=100)
@given(mapping=st_options_mapping(), engine_id=st_engine_id())
def test_p35_engine_option_fields_round_trip(mapping, engine_id):
    """Validates: Requirements 9.1, 9.2, 9.3, 23.4

    For any (hostile) options mapping,
    `ProcessingOptions.from_dict(asdict(from_dict(m))) == from_dict(m)`; every
    engine Feature_Flag is off on a fresh instance; and `flag_field()` equals
    `f"{engine_id}_enabled"` for every registered engine.

    The module-level default registry and capability report are reset **inside**
    the property body (not only in a fixture), because a fixture runs once per
    test while hypothesis runs this body once per example — state from example N
    must not leak into example N+1. The `finally` block guarantees the default
    registry is empty again for every other test in the suite.
    """
    # --- round-trip through the serialised form ---------------------------
    first = ProcessingOptions.from_dict(mapping)  # must not raise
    second = ProcessingOptions.from_dict(asdict(first))
    assert _options_equal(second, first)

    # --- every engine Feature_Flag defaults OFF --------------------------
    fresh = ProcessingOptions()
    for name in ProcessingOptions.__dataclass_fields__:
        if name.endswith(FLAG_SUFFIX):
            assert getattr(fresh, name) is False, f"engine flag {name} defaults on"

    # --- flag_field() is the `<engine_id>_enabled` convention ------------
    reset_registry()
    reset_report()
    try:
        register(FakeEngine(engine_id, Engine_Stage.AUDIO))
        registry = get_registry()
        assert len(registry) == 1
        for engine in registry.all():
            expected = f"{engine.engine_id}_enabled"
            assert engine.flag_field() == expected == f"{engine.engine_id}{FLAG_SUFFIX}"
            # A registered engine's flag is absent-or-off on fresh options, so
            # every engine is disabled until the user opts in (Reqs 9.2, 23.4).
            assert getattr(fresh, engine.flag_field(), False) is False
            # ...and the mapping-derived options never turn it on either.
            assert getattr(first, engine.flag_field(), False) is False

        # The inherited classmethod default derives the same name from a
        # class-level `engine_id` (what real engines declare).
        declared = type(
            "_Flag_Field_Probe_Engine",
            (AV_Engine,),
            {"engine_id": engine_id, "stage": Engine_Stage.AUDIO},
        )
        assert declared.flag_field() == f"{engine_id}_enabled"
    finally:
        reset_registry()
        reset_report()
    assert len(get_registry()) == 0
