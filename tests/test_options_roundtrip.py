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


# --- Unit tests: defaults OFF and existing fields untouched (task 1.6) ------
def test_new_features_default_off():
    """Validates: Requirements 16.2, 22.1 — new features default disabled."""
    o = ProcessingOptions()
    assert o.broll is False
    assert o.visual_selection is False
    assert o.permissibility_mode is False
    assert o.caption_keyword_highlight is False
    assert o.caption_keyword_ai is False
    assert o.caption_emoji is False
    # Sourcing / external features off by default.
    assert o.asset_sourcing_mode == "off"
    assert o.broll_provider == ""
    assert o.broll_intensity == "standard"
    assert o.caption_preset == "karaoke"
    assert o.caption_animation == ""
    assert o.selection_prompt == ""


def test_preexisting_fields_keep_v060_defaults():
    """Validates: Requirements 22.1 — v0.6.0 fields/defaults unchanged."""
    o = ProcessingOptions()
    assert o.aspect == "9:16"
    assert o.captions is True
    assert o.caption_template == "karaoke"
    assert o.caption_position == "bottom"
    assert o.emoji == "off"
    assert o.music == ""
    assert o.clip_length == "auto"
    assert o.num_clips == "auto"
    assert o.strategy == "ai"
    assert o.metadata is True


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
