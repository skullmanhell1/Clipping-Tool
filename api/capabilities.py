"""What this deployment can actually do.

Every helper here answers a question about *optional* capability: which engines loaded, which
b-roll providers have credentials, whether an LLM is reachable, what a caption preset contains.
They live together, and away from the routes, because three separate consumers ask them --
``/api/info``, the ``/api/jobs/{id}/clips/{id}/regenerate`` route (which must not offer an LLM
rewrite when no LLM is configured), and ``main.fallback_index_html`` (which renders a diagnostic
page when the built frontend is missing, and so runs in exactly the situation where something is
already wrong).

Each returns a value rather than raising: a deployment missing an optional dependency has to
report that it is missing, not fail to answer at all.
"""

from __future__ import annotations

from typing import Any

from config import settings


def _engines_info() -> tuple[list[dict[str, object]], dict[str, Any]]:
    """Return the ``(engines, capabilities)`` pair advertised by ``/api/info``.

    Reqs 20.1/20.2/20.6 — additive only: one row per registered AV engine in the
    registry's deterministic order, plus the serialisable Capability_Report.

    The report is consulted **only** for capability ids that registered engines
    actually declare, so with no engine registered this returns ``([], {})``
    having performed **zero** capability probes (Req 20.2). Never raises: a
    broken engine declaration must not take ``/api/info`` down.
    """
    try:
        from worker.engines.registry import get_registry

        engines = get_registry().all()
    except Exception:
        return [], {}
    if not engines:
        # No engine registered => nothing to probe (Reqs 20.2, 20.6).
        return [], {}

    from worker.engines.capabilities import get_report

    report = get_report()
    rows: list[dict[str, object]] = []
    for engine in engines:
        try:
            required = list(getattr(engine, "required_capabilities", ()) or ())
            optional = list(getattr(engine, "optional_capabilities", ()) or ())
            missing = report.missing(required)
            for capability_id in optional:
                # Declared by this engine, so it belongs in the advertised
                # report even when it is only an optional degradation.
                report.available(capability_id)
            stage = getattr(engine, "stage", None)
            rows.append(
                {
                    "id": str(getattr(engine, "engine_id", "")),
                    "stage": getattr(stage, "value", stage),
                    "priority": getattr(engine, "priority", 100),
                    "flag": engine.flag_field(),
                    "enabled_by_default": False,
                    "available": not missing,
                    "missing": missing,
                    "requires_network": bool(getattr(engine, "requires_network", False)),
                    "time_budget_s": getattr(engine, "time_budget_s", None),
                }
            )
        except Exception:
            continue
    capabilities = report.to_dict()
    _add_engine_option_domains(rows, capabilities)
    return rows, capabilities


def _add_engine_option_domains(
    rows: list[dict[str, object]], capabilities: dict[str, Any]
) -> None:
    """Advertise engine-specific option domains inside the ``capabilities`` block.

    The per-engine row stays generic (`id`/`stage`/`priority`/`flag`/
    `enabled_by_default`/`available`/`missing`/`requires_network`/
    `time_budget_s`), so a new engine never changes that schema. Engine-specific
    option vocabularies therefore ride in ``capabilities`` under an
    Engine_Id-namespaced key — capability ids are always ``<kind>:<name>``, so a
    bare engine id can never collide with one.

    Kinetic typography advertises its supported Kinetic_Style and Reveal_Mode
    values (Reqs 17.2, 17.3), and stem inpainting its Mix_Preset / Repair_Mode /
    backend vocabularies plus the numeric bounds the UI's sliders need — both
    **imported** from their engine module so the endpoint cannot drift from it.

    Note the audio-stem spec's task 17.3 asks for these on the engine *row*
    (``engines.stem_inpainting.mix_presets`` and friends). They are placed here
    instead, deliberately: the per-engine row schema is fixed and generic so that
    adding an engine never changes it, which is the rule this function exists to
    uphold. The availability facts that task also asks for are already covered —
    the row carries ``available``/``missing``, and each engine's *optional*
    capability ids (``python_pkg:demucs``, ``model:htdemucs``) are forced into the
    report by :func:`_engines_info`, so they appear in the top-level
    ``capabilities`` map under their own ``<kind>:<name>`` keys.

    Each block is emitted only when that engine is actually registered, so the
    no-engine-registered payload stays empty, and each is independently guarded:
    one engine module failing to import must not cost the other its domains, and
    must not take ``/api/info`` down.
    """
    if any(row.get("id") == "kinetic_typography" for row in rows):
        try:
            from worker.engines.kinetic import ENGINE_ID, KINETIC_STYLES, REVEAL_MODES

            capabilities[ENGINE_ID] = {
                "styles": list(KINETIC_STYLES),
                "reveal_modes": list(REVEAL_MODES),
            }
        except Exception:
            pass

    if any(row.get("id") == "stem_inpainting" for row in rows):
        try:
            from worker.engines.stems import (
                BACKEND_IDS,
                ENGINE_ID,
                GAIN_DEFAULT,
                GAIN_MAX,
                GAIN_MIN,
                MIX_PRESET_CHOICES,
                REPAIR_MODES,
                STEM_NAMES,
                WINDOW_DEFAULT_MS,
                WINDOW_MAX_MS,
                WINDOW_MIN_MS,
            )

            capabilities[ENGINE_ID] = {
                "mix_presets": list(MIX_PRESET_CHOICES),
                "repair_modes": list(REPAIR_MODES),
                "backends": list(BACKEND_IDS),
                "stem_set": list(STEM_NAMES),
                "gain": {
                    "min": GAIN_MIN,
                    "max": GAIN_MAX,
                    "default": GAIN_DEFAULT,
                },
                "repair_window_ms": {
                    "min": WINDOW_MIN_MS,
                    "max": WINDOW_MAX_MS,
                    "default": WINDOW_DEFAULT_MS,
                },
            }
        except Exception:
            pass


def _available_broll_providers() -> list[str]:
    """Return configured external b-roll providers ([] when none configured)."""
    return [settings.broll_provider] if settings.broll_provider else []


def _llm_available_safe() -> bool:
    """Return whether an LLM is configured (never raises)."""
    try:
        from worker.llm_client import llm_available

        return llm_available()
    except Exception:
        return False


def _preset_detail(preset) -> dict:
    """A caption preset serialised for the UI, with web-usable colours (U5).

    The preset's own ``to_dict`` keeps ASS ``&HAABBGGRR`` colours, which is right for the
    renderer and unusable in a browser: no colour input or CSS property accepts one. The hex
    equivalents are *added* rather than substituted, so the API still reports exactly what the
    renderer will use.
    """
    from worker import branding

    data = preset.to_dict()
    colors = data.get("colors") or {}
    data["colors_hex"] = {
        key: branding.ass_to_hex(value)
        for key, value in colors.items()
        if branding.ass_to_hex(value)
    }
    return data
