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


# --- The file must be loadable by Docker, not only by python-dotenv ----------------------
#
# The tests above prove every setting is *documented*. They cannot prove the documented value
# *parses*, and that gap shipped a broken quickstart: `cp .env.example .env` followed by
# `docker compose up` died with 17 pydantic validation errors, because this file used inline
# trailing comments and Docker does not strip them.
#
# Two loaders read this file and they disagree:
#
#   python-dotenv (via pydantic-settings, when the app runs directly)
#       strips inline `#` comments, and strips surrounding quotes.
#   Docker `env_file` / `--env-file`
#       strips neither. Everything after the first `=` is the value, to end of line.
#
# So the container saw `OUTPUT_SHORT_SIDE=1080           # 720 | 1080 | 1440 | 2160` and tried
# to parse that whole string as an int. Of the 39 affected lines, 17 failed loudly. The other
# 22 were string-typed and pydantic **accepted them**, which is the worse half: `WHISPER_MODEL`
# became `small              # tiny | base | small | medium | large-v3`, a model name that does
# not exist, and `FACE_DETECTOR_BACKEND` and `CAPTION_MODE` likewise took values matching no
# branch. Fixing only the loud 17 would have produced a container that booted and misbehaved.
#
# These two tests are the ratchet. They are deliberately textual: reproducing them by loading
# the file would mean choosing one of the two loaders, and the defect lives precisely in the
# disagreement between them.

#: An assignment whose value is followed by whitespace and then `#`. The whitespace matters —
#: `#` with no space before it is part of the value (a hex colour, a URL fragment), which is
#: why this is not simply a search for `#` after `=`.
_INLINE_COMMENT = re.compile(r"^\s*([A-Z][A-Z0-9_]*)=[^#\n]*?[ \t]+#", re.MULTILINE)

#: An assignment whose value opens with a quote. Docker keeps the quote characters.
_QUOTED_VALUE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)=[\"']", re.MULTILINE)


def test_no_value_carries_an_inline_comment():
    """Comments belong on their own line above the setting.

    Docker's `env_file` reads to end of line, so an inline comment becomes part of the value.
    """
    offenders = sorted(m.group(1) for m in _INLINE_COMMENT.finditer(ENV_EXAMPLE.read_text("utf-8")))
    assert not offenders, (
        f"{len(offenders)} setting(s) in .env.example have an inline comment: {offenders}. "
        "Docker's env_file does not strip them, so the comment becomes part of the value — "
        "numeric settings fail validation and string settings silently absorb it. "
        "Move the comment to its own line above the assignment."
    )


def test_no_value_is_quoted():
    """Quotes are not stripped by Docker, so they end up inside the value.

    `APP_NAME="AI Video Clipper"` reached the container as `"AI Video Clipper"`, quotes and all,
    and a value containing spaces needs no quoting in either loader.
    """
    offenders = sorted(m.group(1) for m in _QUOTED_VALUE.finditer(ENV_EXAMPLE.read_text("utf-8")))
    assert not offenders, (
        f"{len(offenders)} setting(s) in .env.example are quoted: {offenders}. "
        "Docker's env_file keeps the quote characters in the value. Values with spaces are "
        "fine unquoted in both loaders."
    )
