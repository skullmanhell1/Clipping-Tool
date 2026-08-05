"""``.env.example`` is kept in step with :class:`config.Settings`.

``config.Settings``' own docstring says "See ``.env.example`` for the full list", so that
file is a documented contract, not a sample. It had drifted badly: 26 of 93 settings were
undocumented, and one documented key — ``PUBLISH_DEFAULT_INTERVAL_SECONDS`` — no longer
existed at all, so an operator setting it would silently get no effect (``Settings`` is
configured with ``extra="ignore"``).

Drift is invisible without a check like this, which is exactly why it happened. Both
directions matter: a missing key hides a feature, and a stale key actively misleads.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"

#: Matches an assignment line, ignoring commented-out lines. Only upper-case names are
#: considered, which is the convention every real key follows.
_ASSIGNMENT = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)


def _documented_keys() -> set[str]:
    """Environment variable names assigned in ``.env.example``, lower-cased."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    return {match.group(1).lower() for match in _ASSIGNMENT.finditer(text)}


def _setting_names() -> set[str]:
    """Every field on :class:`config.Settings`."""
    return set(Settings.model_fields)


def test_every_setting_is_documented():
    """No setting is reachable only by reading the source.

    A configurable behaviour nobody can discover is effectively not configurable. The
    failure message lists the specific names so the fix is mechanical.
    """
    missing = sorted(name.upper() for name in _setting_names() - _documented_keys())
    assert not missing, f"{len(missing)} setting(s) missing from .env.example: {missing}"


def test_no_documented_key_is_stale():
    """Every key in ``.env.example`` corresponds to a real setting.

    ``Settings`` uses ``extra=\"ignore\"``, so an unknown key is accepted and discarded
    without complaint. Documenting one therefore produces a control that appears to work
    and does nothing — worse than omitting it. This is what happened when
    ``publish_default_interval_seconds`` was renamed.
    """
    stale = sorted(name.upper() for name in _documented_keys() - _setting_names())
    assert not stale, f"{len(stale)} key(s) in .env.example are not settings: {stale}"


def test_the_example_file_parses_as_env_assignments():
    """Sanity check on the file itself, so the two tests above cannot pass vacuously.

    If a syntax change made the regex match nothing, both set differences would collapse
    and the checks would silently stop testing anything.
    """
    documented = _documented_keys()
    assert len(documented) > 50, f"only found {len(documented)} keys; parser likely broken"


@pytest.mark.parametrize(
    "name",
    [
        # A representative spread rather than an exhaustive list: a secret, a path, a
        # numeric ceiling, a float and a delimited string, so a formatting change that
        # breaks one shape is caught.
        "openai_api_key",
        "jobs_db",
        "max_upload_bytes",
        "visual_selection_weight",
        "allowed_upload_extensions",
    ],
)
def test_representative_settings_are_present(name):
    """Named spot checks, which give a clearer failure than a set difference."""
    assert name in _documented_keys()
