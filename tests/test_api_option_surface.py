"""The API's option surface is kept in step with the options it forwards.

``ProcessingOptions`` is the single source of truth for what a job can be asked to do. Three
separate surfaces restate parts of it - ``OptionsModel`` for JSON bodies, the ``/api/upload``
form signature, and ``/api/info``'s advertised vocabularies - and every one of them had
drifted, silently, because FastAPI and pydantic are *designed* to drop what they do not
recognise. Nothing 422s, nothing logs, and the control simply does nothing:

* **13 fields were missing.** The whole U6 brand kit (font, CTA, both colours, logo and its
  position/scale/opacity) reached no path at all, while the UI cheerfully sent all eight. So
  did ``profile``, which ``/api/profiles/builtin`` advertises. ``subtitle_sidecar`` worked for
  an uploaded file and was dropped for the same job submitted as a URL. And
  ``loudness_normalise``, ``music_duck`` and ``trim_silence`` - which default ON, so a caller
  could not observe them failing to switch on, only failing to switch OFF.
* **10 declared defaults contradicted the dataclass**, all of them the U1 default-on effects.
  A bare ``POST /api/jobs/url`` therefore produced a static centre crop with no zoom, no hook
  title, no fades, no progress bar and no emoji - quietly undoing the change whose entire
  point was that shipping those off made the tool look worse than it is. Watch-folder jobs got
  the same treatment, because ``WatchToggleRequest.options`` defaults to ``OptionsModel()``.
* **8 of 14 caption presets were rejected.** ``/api/info`` publishes every key of
  ``BUILTIN_PRESETS`` and the UI renders a swatch for each, while a hand-written six-item
  tuple substituted ``karaoke`` for the other eight.

These tests are the contract, in the same spirit as ``tests/test_config_documentation.py``
for ``.env.example``: comparing two representations of one fact, in both directions, so drift
fails a build instead of a render.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from api.main import OptionsModel, upload
from worker.effects.caption_presets import BUILTIN_PRESETS
from worker.models import ProcessingOptions


def _option_fields() -> list[str]:
    return [f.name for f in dataclasses.fields(ProcessingOptions)]


def _option_defaults() -> dict[str, object]:
    blank = ProcessingOptions()
    return {name: getattr(blank, name) for name in _option_fields()}


# --------------------------------------------------------------------------- #
# 1. Every option is reachable, on every path
# --------------------------------------------------------------------------- #
def test_every_processing_option_is_accepted_in_a_json_body():
    """``/api/jobs/url``, ``/api/jobs/batch`` and both watch endpoints go through this model.

    A field absent here is not an error: pydantic drops the key and the caller is told
    nothing, so the symptom is a setting that visibly moves in the UI and changes no pixels.
    """
    missing = [name for name in _option_fields() if name not in OptionsModel.model_fields]
    assert missing == [], (
        f"{len(missing)} option(s) cannot be set through a JSON body and would be silently "
        f"discarded: {missing}"
    )


def test_every_processing_option_is_accepted_by_the_upload_form():
    """``/api/upload`` matches form fields by name; an unrecognised one is dropped, not 422'd."""
    params = set(inspect.signature(upload).parameters)
    missing = [name for name in _option_fields() if name not in params]
    assert missing == [], (
        f"{len(missing)} option(s) cannot be set on an upload and would be silently "
        f"discarded: {missing}"
    )


def test_the_model_declares_no_field_that_is_not_a_real_option():
    """The other direction, which is how a renamed option leaves a decoy behind.

    ``ProcessingOptions.from_dict`` ignores unknown keys, so a stale field here would accept a
    value, validate it, and drop it - the most convincing possible way to do nothing.
    """
    stale = [name for name in OptionsModel.model_fields if name not in set(_option_fields())]
    assert stale == [], f"{stale} are accepted by the API but are not processing options"


# --------------------------------------------------------------------------- #
# 2. One source of truth for a default
# --------------------------------------------------------------------------- #
def test_the_models_declared_defaults_match_the_dataclass():
    """A default restated in two places is a default that will disagree in two places.

    This is not cosmetic even though ``to_options`` no longer reads these values: the model's
    defaults are what FastAPI publishes in ``openapi.json``, so a wrong one here is generated
    client code and documentation that lie about what a request will do.
    """
    declared = OptionsModel().model_dump()
    truth = _option_defaults()
    drift = {
        name: (declared[name], truth[name])
        for name in declared
        if name in truth and declared[name] != truth[name]
    }
    assert drift == {}, (
        "the API advertises different defaults from the ones it applies "
        f"(field: api_value vs real_value): {drift}"
    )


def test_an_empty_request_yields_the_dataclass_defaults():
    """Behavioural half of the above, and the reason ``exclude_unset`` is load-bearing.

    Asserted on the *resolved* options rather than on the model, because that is what the
    worker receives. A bare URL job must produce the same clip the UI's defaults would.
    """
    resolved = OptionsModel().to_options()
    for name, expected in _option_defaults().items():
        assert getattr(resolved, name) == expected, (
            f"a request that mentioned nothing changed {name!r} to "
            f"{getattr(resolved, name)!r}; the API's defaults have diverged again"
        )


def test_the_u1_effects_are_on_for_a_caller_who_asks_for_nothing():
    """Stated separately because it is the user-visible consequence, and it regressed once.

    These default on deliberately (U1): shipping them off made the tool look worse than it is
    capable of. An API-first caller, and every watch-folder job, was getting the pre-U1 look.
    """
    bare = OptionsModel().to_options()
    for name in (
        "reframe",
        "zoom",
        "transitions",
        "hook_title",
        "fades",
        "progress_bar",
        "caption_keyword_highlight",
        "caption_emoji",
        "visual_selection",
    ):
        assert getattr(bare, name) is True, f"{name} should default on for a bare request"
    assert bare.emoji == "standard"


# --------------------------------------------------------------------------- #
# 3. Named profiles
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["podcast", "gaming", "talking_head", "educational"])
def test_a_built_in_profile_actually_expands(name):
    """``/api/profiles/builtin`` advertises these; they must do something when asked for.

    ``from_dict`` has always expanded a bundle, but ``model_dump()`` emitted every field, so
    every one counted as an explicit override and the bundle was overridden in full.
    """
    from worker.models import BUILTIN_PROFILES

    resolved = OptionsModel(profile=name).to_options()
    expected = BUILTIN_PROFILES[name].settings
    assert expected, f"profile {name!r} has no opinions to apply"

    real_fields = {f.name for f in dataclasses.fields(ProcessingOptions)}
    for key, value in expected.items():
        if key not in real_fields:
            continue
        assert getattr(resolved, key) == value, (
            f"profile {name!r} did not apply {key}={value!r} (got {getattr(resolved, key)!r})"
        )


def test_an_explicit_value_still_beats_the_profile():
    """The precedence `from_dict` documents, which `exclude_unset` must not invert."""
    resolved = OptionsModel(profile="gaming", caption_preset="minimal").to_options()
    assert resolved.caption_preset == "minimal"
    # ...while the rest of the bundle still applies.
    assert resolved.kinetic_typography_enabled is True


# --------------------------------------------------------------------------- #
# 4. Advertised vocabularies are accepted vocabularies
# --------------------------------------------------------------------------- #
def test_every_advertised_caption_preset_survives_a_request():
    """``/api/info`` publishes ``BUILTIN_PRESETS.keys()`` and the UI renders one swatch each.

    Asserts the **resolved** preset, not the requested one: the failure mode was a request for
    ``sticker`` coming back as ``karaoke`` plus a ``caption_preset_substituted`` marker, which
    is a fallback doing its job on a value that should never have reached it.
    """
    for name in BUILTIN_PRESETS:
        resolved = OptionsModel(caption_preset=name).to_options().caption_preset
        assert resolved == name, (
            f"the API advertises caption preset {name!r} but a request for it resolves to "
            f"{resolved!r}"
        )


def test_an_unknown_caption_preset_still_falls_back():
    """Widening the accepted set must not remove the guard for a genuinely bad value."""
    assert OptionsModel(caption_preset="nonsense").to_options().caption_preset == "karaoke"


# --------------------------------------------------------------------------- #
# 5. The fourth surface: the UI's own defaults
# --------------------------------------------------------------------------- #
#: Fields the UI deliberately spells differently from the dataclass. Each is a *UI encoding*
#: rather than drift, so each is listed with its reason rather than the check being loosened.
_UI_ENCODINGS = {
    # One control carries both the target language and whether to translate, so the UI's
    # sentinel is the string "auto" where the dataclass uses None. `resolveLanguage` in
    # api.js turns it back into (language, translate) on the way out.
    "language",
    # Empty text inputs. The dataclass wants None for "no range"; an <input> has "" and the
    # coercion happens in ProcessingOptions.from_dict.
    "range_start",
    "range_end",
}


def _ui_default_settings() -> dict[str, object]:
    """``DEFAULT_SETTINGS`` from ``App.jsx``, parsed for the literals it declares.

    Reading the JSX is the same technique ``tests/test_stems_api.py`` uses, and for the same
    reason: it is the only thing that compares the two languages' spellings of one fact
    against each other. Values that are not simple literals are skipped rather than guessed.
    """
    import pathlib
    import re

    src = (pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src" / "App.jsx").read_text(
        encoding="utf-8"
    )
    match = re.search(r"const DEFAULT_SETTINGS = \{(.*?)\n\};", src, re.S)
    assert match, "DEFAULT_SETTINGS not found in App.jsx"

    out: dict[str, object] = {}
    for name, raw in re.findall(r"^  ([a-z_][a-z0-9_]*):\s*(.+?),?$", match.group(1), re.M):
        text = raw.strip().rstrip(",")
        if text == "true":
            out[name] = True
        elif text == "false":
            out[name] = False
        elif text.startswith('"') and text.endswith('"'):
            out[name] = text[1:-1]
        else:
            try:
                out[name] = float(text) if "." in text else int(text)
            except ValueError:
                continue  # an expression or object; not a literal to compare
    return out


def test_the_uis_defaults_match_the_dataclass():
    """The settings panel must open showing what the tool will actually do.

    This was the most user-visible copy of the same drift: all ten U1 effects were declared
    ``false`` here, so the panel opened with reframe, zoom, punch-in, hook title, fades, the
    progress bar, emoji and keyword highlighting all off. Since the UI forwards every field
    explicitly, that is also what got rendered - so *every* user who never opened the panel
    got the pre-U1 look, which is precisely the outcome U1 existed to prevent.

    Only fields the UI actually declares are compared; the engine bundles live in
    ``DEFAULT_ENGINE_SETTINGS`` and the publish fields in a separate blob.
    """
    ui = _ui_default_settings()
    truth = _option_defaults()

    drift = {
        name: (value, truth[name])
        for name, value in ui.items()
        if name in truth and name not in _UI_ENCODINGS and value != truth[name]
    }
    assert drift == {}, (
        "the settings panel opens with different values from the ones the worker applies "
        f"(field: ui_value vs real_value): {drift}"
    )


def test_the_ui_encoding_exemptions_are_all_real_fields():
    """So the exemption list above cannot quietly outlive the fields it excuses."""
    truth = _option_defaults()
    stale = sorted(name for name in _UI_ENCODINGS if name not in truth)
    assert stale == [], f"{stale} are exempted but are not processing options"
