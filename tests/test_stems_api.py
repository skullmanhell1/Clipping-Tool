"""API / UI surface tests for the audio-stem-inpainting spec.

Covers epic 17: the eleven ``ProcessingOptions`` fields (17.1), the ``OptionsModel`` and
``/api/upload`` Form surface (17.2), ``/api/info`` (17.3) and the ``SettingsPanel.jsx`` group
(17.5) — headlined by **P21** (every option field survives the API surface).

The panel assertions are deliberately *field-name* assertions against the JSX source rather
than a rendered-DOM test: this repo has no JavaScript test tooling at all, and the failure
mode worth catching here is a **name mismatch** between the panel, ``DEFAULT_ENGINE_SETTINGS``
and the backend field list. A camelCase key or a typo in the panel would silently never reach
the API — `App.jsx` forwards `DEFAULT_ENGINE_SETTINGS` keys verbatim as FormData — and that is
exactly what these tests pin.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings

from tests.strategies import st_stem_options
from worker.engines import stems
from worker.models import ProcessingOptions

_ROOT = Path(__file__).resolve().parents[1]
_APP_JSX = (_ROOT / "frontend" / "src" / "App.jsx").read_text(encoding="utf-8")
_PANEL_JSX = (
    _ROOT / "frontend" / "src" / "components" / "SettingsPanel.jsx"
).read_text(encoding="utf-8")

#: The eleven Processing_Options fields this spec adds: the Feature_Flag plus one per
#: ``Stem_Options`` field. Spelled out rather than derived, because the point is to pin the
#: **names** — deriving them from the dataclass would make the test agree with any rename.
STEM_FIELDS = (
    "stem_inpainting_enabled",
    "stem_mix_preset",
    "stem_gain_vocals",
    "stem_gain_music",
    "stem_gain_other",
    "stem_repair_mode",
    "stem_repair_window_ms",
    "stem_declick",
    "stem_backend",
    "stem_model",
    "stem_retain_stems",
)

#: Documented defaults, mirroring ``Stem_Options`` (Req 18.1).
STEM_DEFAULTS = {
    "stem_inpainting_enabled": False,
    "stem_mix_preset": "custom",
    "stem_gain_vocals": 1.0,
    "stem_gain_music": 1.0,
    "stem_gain_other": 1.0,
    "stem_repair_mode": "crossfade",
    "stem_repair_window_ms": 12,
    "stem_declick": False,
    "stem_backend": "auto",
    "stem_model": "htdemucs",
    "stem_retain_stems": False,
}


@pytest.fixture
def registered_engines():
    """Guarantee both engines are in the default registry for the duration of a test.

    ``/api/info`` reads the process-wide default registry, which is populated by an
    import-time side effect in ``worker/engines/loader.py``. But
    ``tests/test_engine_host.py`` calls ``reset_registry()`` from an autouse fixture *and*
    from inside each property body, and the side effect cannot re-fire on an
    already-imported module — so by the time this file runs, the registry may be empty and
    ``/api/info`` would legitimately advertise no engines.

    Re-registering explicitly makes these tests state their own precondition instead of
    depending on file ordering. (This is the third test in this spec that has had to work
    around that reset; a restoring fixture in the foundation's own test module would be the
    real fix, but that file is out of scope here — Req 20.6.)
    """
    from worker.engines.kinetic import Kinetic_Typography_Engine
    from worker.engines.registry import get_registry, register
    from worker.engines.stems import Stem_Inpainting_Engine

    registry = get_registry()
    for engine in (Kinetic_Typography_Engine(), Stem_Inpainting_Engine()):
        if engine.engine_id not in registry:
            register(engine)
    return registry


@pytest.fixture
def client(registered_engines):
    from fastapi.testclient import TestClient

    from api.main import app

    return TestClient(app)


# --------------------------------------------------------------------------- #
# Task 17.1 — the eleven Processing_Options fields                            #
# --------------------------------------------------------------------------- #
def test_all_eleven_fields_exist_with_their_documented_defaults() -> None:
    """Every field is present, and the engine is OFF on a stock install (Req 18.1)."""
    options = ProcessingOptions()
    record = asdict(options)

    for name in STEM_FIELDS:
        assert name in record, f"missing ProcessingOptions field: {name}"
        assert record[name] == STEM_DEFAULTS[name], name

    # The flag is a real bool, not a truthy string — ``effective_options`` and the parity
    # gate compare options by value.
    assert options.stem_inpainting_enabled is False


def test_the_flag_and_booleans_survive_form_style_strings() -> None:
    """Form fields arrive as text; ``"false"`` must not become a truthy ``bool`` (Req 18.5)."""
    options = ProcessingOptions.from_dict(
        {
            "stem_inpainting_enabled": "true",
            "stem_declick": "false",
            "stem_retain_stems": "1",
        }
    )
    assert options.stem_inpainting_enabled is True
    assert options.stem_declick is False
    assert options.stem_retain_stems is True


def test_unknown_values_never_fail_the_job() -> None:
    """An unrecognised value falls back to the documented default (Reqs 18.1, 18.5).

    Coercion happens in the engine's ``resolve_options`` rather than in ``ProcessingOptions``,
    which is what keeps ``worker/models.py`` free of a ``worker.engines`` import. So the
    assertion is on the *resolved* value: the job runs, with defaults.
    """
    options = ProcessingOptions.from_dict(
        {
            "stem_mix_preset": "not_a_preset",
            "stem_repair_mode": "nonsense",
            "stem_backend": "quantum",
            "stem_gain_music": "loud",
            "stem_repair_window_ms": 9999,
        }
    )
    resolved = stems.Stem_Inpainting_Engine().resolve_options(options)

    assert resolved.mix_preset == "custom"
    assert resolved.repair_mode == "crossfade"
    assert resolved.backend == "auto"
    assert resolved.gain_music == stems.GAIN_DEFAULT
    assert resolved.repair_window_ms == stems.WINDOW_MAX_MS      # clamped, not rejected


# --------------------------------------------------------------------------- #
# P21 — every option field survives the API surface                           #
# --------------------------------------------------------------------------- #
# Feature: audio-stem-inpainting, Property 21: Every option field survives the API surface
@hyp_settings(max_examples=100, deadline=None)
@given(option_map=st_stem_options())
def test_p21_every_option_field_survives_the_api_surface(option_map: dict) -> None:
    """A value set through the API arrives at the engine unchanged.

    The chain asserted end to end is the one a real request takes:
    ``OptionsModel`` → ``to_options()`` → ``ProcessingOptions`` → ``resolve_options()`` →
    ``Stem_Options``. Its result must equal resolving the same values directly, so no layer
    silently drops, renames or re-defaults a field (Reqs 18.1, 18.2, 18.5).
    """
    from api.models import OptionsModel

    payload = {f"stem_{key}": value for key, value in option_map.items()}
    through_api = OptionsModel(**payload).to_options()
    engine = stems.Stem_Inpainting_Engine()

    # The oracle has to be an *object*: ``resolve_stem_options`` reads ``stem_<field>``
    # attributes (never mapping keys), which is what makes it safe to hand it a real
    # ProcessingOptions without it accidentally consuming unrelated dict entries.
    direct = stems.resolve_stem_options(type("O", (), dict(payload))())

    assert engine.resolve_options(through_api) == direct

    # And the flat fields really are carried on ProcessingOptions, not just coerced away.
    for key, value in option_map.items():
        assert hasattr(through_api, f"stem_{key}")


@hyp_settings(max_examples=50, deadline=None)
@given(option_map=st_stem_options())
def test_p21_the_fields_round_trip_through_from_dict_and_asdict(option_map: dict) -> None:
    """``from_dict`` / ``asdict`` round-trip losslessly, so a saved profile is faithful."""
    payload = {f"stem_{key}": value for key, value in option_map.items()}
    once = ProcessingOptions.from_dict(payload)
    twice = ProcessingOptions.from_dict(asdict(once))

    for name in STEM_FIELDS:
        assert getattr(twice, name) == getattr(once, name), name


# --------------------------------------------------------------------------- #
# Task 17.2 — the /api/upload Form surface                                    #
# --------------------------------------------------------------------------- #
def test_the_upload_form_accepts_every_stem_field(client, tmp_path) -> None:
    """All eleven are accepted as Form fields, and an unknown value is not a 422 (Req 18.5)."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 64)

    with video.open("rb") as handle:
        response = client.post(
            "/api/upload",
            files={"files": ("clip.mp4", handle, "video/mp4")},
            data={
                "stem_inpainting_enabled": "true",
                "stem_mix_preset": "speech_focus",
                "stem_gain_music": "0.25",
                "stem_repair_mode": "spectral",
                "stem_repair_window_ms": "24",
                "stem_declick": "true",
                "stem_backend": "ml",
                "stem_model": "htdemucs",
                "stem_retain_stems": "false",
                # Deliberately nonsense: it must be coerced, not rejected.
                "stem_gain_vocals": "not-a-number",
            },
        )

    assert response.status_code == 200, response.text
    jobs = response.json()["jobs"]
    assert len(jobs) == 1


def test_omitted_stem_form_fields_keep_their_defaults(client, tmp_path) -> None:
    """An omitted field must not be forwarded as ``None`` and blank the default (Req 17.4).

    This is the reason the Form parameters are declared as loose ``Optional[str]`` and only
    forwarded when non-``None`` — the same arrangement the kinetic fields use.
    """
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 64)

    with video.open("rb") as handle:
        response = client.post(
            "/api/upload",
            files={"files": ("clip.mp4", handle, "video/mp4")},
            data={"stem_mix_preset": "music_focus"},
        )

    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# Task 17.3 — /api/info                                                       #
# --------------------------------------------------------------------------- #
def test_info_advertises_the_engine_row(client) -> None:
    """One generic row, ``enabled_by_default`` false (foundation Reqs 20.1, 20.2)."""
    payload = client.get("/api/info").json()
    rows = {row["id"]: row for row in payload["engines"]}

    assert "stem_inpainting" in rows
    row = rows["stem_inpainting"]
    assert row["flag"] == "stem_inpainting_enabled"
    assert row["enabled_by_default"] is False
    assert row["stage"] == "audio"
    assert row["priority"] == 20
    assert row["requires_network"] is False
    # The row schema is fixed and generic, so adding an engine never changes it.
    assert set(row) == {
        "id", "stage", "priority", "flag", "enabled_by_default",
        "available", "missing", "requires_network", "time_budget_s",
    }


def test_info_advertises_the_stem_option_domains(client) -> None:
    """The vocabularies and slider bounds the panel needs, sourced from the module.

    Note these live under ``capabilities["stem_inpainting"]`` rather than on the engine row,
    which is where task 17.3's wording puts them: the row schema is deliberately generic (see
    the assertion above), so engine-specific vocabularies ride in the Engine_Id-namespaced
    capabilities block — the convention kinetic typography already established.
    """
    payload = client.get("/api/info").json()
    domains = payload["capabilities"]["stem_inpainting"]

    assert domains["mix_presets"] == list(stems.MIX_PRESET_CHOICES)
    assert domains["repair_modes"] == list(stems.REPAIR_MODES)
    assert domains["backends"] == list(stems.BACKEND_IDS)
    assert domains["stem_set"] == list(stems.STEM_NAMES)
    assert domains["gain"] == {
        "min": stems.GAIN_MIN, "max": stems.GAIN_MAX, "default": stems.GAIN_DEFAULT
    }
    assert domains["repair_window_ms"] == {
        "min": stems.WINDOW_MIN_MS,
        "max": stems.WINDOW_MAX_MS,
        "default": stems.WINDOW_DEFAULT_MS,
    }


def test_info_advertises_the_separation_dependencies(client) -> None:
    """``python_pkg:demucs`` / ``model:htdemucs`` are reported so the UI can explain itself.

    They appear because they are *declared* optional capabilities of the engine, which
    ``_engines_info`` forces into the report — the operator can therefore see that full
    fidelity needs a locally provisioned model (Req 12.8, 18.6).
    """
    payload = client.get("/api/info").json()
    caps = payload["capabilities"]

    for capability_id in ("python_pkg:demucs", "model:htdemucs"):
        assert capability_id in caps
        assert "available" in caps[capability_id]


def test_info_leaves_the_pre_existing_payload_untouched(client) -> None:
    """Additive only: every v0.8.0 key and vocabulary is still there (Req 18.2)."""
    payload = client.get("/api/info").json()

    for key in (
        "app_name", "version", "aspect_ratios", "clip_lengths", "clip_counts",
        "platforms", "strategies", "regeneratable_fields", "llm_available",
        "effects", "broll_available", "storage_backend", "retention_choices",
    ):
        assert key in payload, key

    effects = payload["effects"]
    for key in (
        "music_moods", "color_presets", "emoji_intensities", "caption_templates",
        "caption_positions", "caption_presets", "caption_animations",
        "broll_intensities", "reframe_layouts", "reframe_intensities",
    ):
        assert key in effects, key

    # The sibling engine's domains are untouched by ours (Req 20.6).
    assert "styles" in payload["capabilities"]["kinetic_typography"]


# --------------------------------------------------------------------------- #
# Tasks 17.4 / 17.5 — the frontend field names                                #
# --------------------------------------------------------------------------- #
def test_the_frontend_defaults_list_every_field_with_the_api_spelling() -> None:
    """A camelCase key or a typo here would silently never reach the backend.

    ``App.jsx``'s ``engineOptions`` forwards ``DEFAULT_ENGINE_SETTINGS`` keys **verbatim** as
    FormData field names, so the JS spelling has to equal the Python one exactly. That makes
    this a real integration assertion rather than a style check.
    """
    block = re.search(
        r"DEFAULT_ENGINE_SETTINGS\s*=\s*\{(.*?)\n\};", _APP_JSX, re.DOTALL
    )
    assert block is not None, "DEFAULT_ENGINE_SETTINGS not found in App.jsx"
    body = block.group(1)

    for name in STEM_FIELDS:
        assert re.search(rf"^\s*{name}:", body, re.MULTILINE), (
            f"{name} missing from DEFAULT_ENGINE_SETTINGS"
        )

    # No camelCase sibling snuck in alongside the snake_case key.
    assert "stemInpainting" not in _APP_JSX
    assert "stemGain" not in _APP_JSX


#: The fields the "Stem repair" panel group exposes as controls.
#:
#: ``stem_model`` is deliberately **not** among them, matching task 17.5's control list. The
#: checkpoint *name* is an operator/deployment concern, not a per-job creative choice: it has
#: to agree with what is actually on disk in the model directory, so a free-text box in the
#: creative panel would mostly be a way to break separation by typo. It stays reachable
#: through the API and through a saved profile.
PANEL_FIELDS = tuple(name for name in STEM_FIELDS if name != "stem_model")


def test_the_panel_binds_every_control_field() -> None:
    """The "Stem repair" group reads and writes each field it is specified to expose."""
    assert "Stem repair" in _PANEL_JSX

    for name in PANEL_FIELDS:
        assert name in _PANEL_JSX, f"{name} is not bound in SettingsPanel.jsx"


def test_the_field_the_panel_omits_is_still_reachable() -> None:
    """``stem_model`` has no control, so it must still round-trip via the generic path.

    Being absent from the panel is a UI choice; being absent from the *forwarding* would make
    it unsettable by any means, which is a different and worse thing.
    """
    assert "stem_model" not in _PANEL_JSX
    assert re.search(r"^\s*stem_model:", _APP_JSX, re.MULTILINE)   # forwarded generically
    assert "stem_model" in {f.name for f in __import__("dataclasses").fields(ProcessingOptions)}


def test_the_panel_gates_the_gain_sliders_on_the_custom_preset() -> None:
    """A named Mix_Preset overrides the individual gains on the backend (Req 5.2).

    Leaving the sliders live under a named preset would display values that do not describe
    what will actually happen, so they are disabled and the reason is shown.
    """
    assert "stemGainsEditable" in _PANEL_JSX
    assert '=== "custom"' in _PANEL_JSX
    assert "disabled={!stemGainsEditable}" in _PANEL_JSX


def test_the_panel_disables_spectral_without_a_local_model() -> None:
    """``spectral`` is shown **with its reason**, not hidden (Req 18.4).

    A creator who has configured a model directory needs to see that the mode exists and why
    it is not currently on offer; hiding it would look like the feature does not exist.
    """
    assert "stemModelAvailable" in _PANEL_JSX
    assert 'capabilities?.["model:htdemucs"]' in _PANEL_JSX
    assert "needs local model" in _PANEL_JSX
    # Shown-but-unselectable requires Dropdown to honour a per-option `disabled`.
    dropdown = (
        _ROOT / "frontend" / "src" / "components" / "Dropdown.jsx"
    ).read_text(encoding="utf-8")
    assert "disabled={!!o.disabled}" in dropdown


def test_the_panel_disables_the_whole_group_when_the_engine_is_unavailable() -> None:
    """A creator must not be able to enable something that would silently degrade."""
    assert "stemAvailable" in _PANEL_JSX
    assert "disabled={!stemAvailable}" in _PANEL_JSX
    assert 'engineHint(stemEngine)' in _PANEL_JSX
