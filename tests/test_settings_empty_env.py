"""``FOO=`` in a ``.env`` means "not set", for every optional setting.

This is not a style question. ``.env.example`` ships 25 optional keys with an empty value, and the
README's quickstart is ``cp .env.example .env``, so the empty-value case is not an edge case — it
is what every new checkout runs. An environment variable that is present but empty is the string
``""``, not absent, so pydantic validated it rather than falling back to the field default.

For the 24 ``str | None`` keys that was invisible: ``""`` and ``None`` are both falsy and every
call site tests truthiness. ``INTERMEDIATE_CACHE_DIR`` was not invisible. It is ``Path | None``, so
an empty value became ``Path(".")`` — the process working directory — while its own description
promised ``<temp_dir>/intermediates``. ``worker.intermediate_cache.cache_dir()`` selects with
``settings.intermediate_cache_dir or ...``, and ``Path(".")`` is truthy, so the I3 cache (on by
default) wrote to the working directory. Inside the container that is ``/app``, owned by root while
the app runs as UID 10001, so the documented quickstart pointed a write-heavy cache at a directory
the application cannot write — and it slipped past the boot-time writability probe, because
``intermediate_cache_dir`` appears in neither ``_REQUIRED_DIR_FIELDS`` nor ``_OPTIONAL_DIR_FIELDS``.

The tests below assert the general rule rather than the one field that happened to hurt, because
the next ``Path | None`` or ``int | None`` setting someone adds would reintroduce it silently.
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest

from config import Settings


def _optional_field_names() -> list[str]:
    """Every field that admits ``None``, and so has a meaningful "unset" state."""
    return sorted(
        name
        for name, field in Settings.model_fields.items()
        if type(None) in get_args(field.annotation)
    )


def test_there_are_optional_fields_to_check():
    """Guard against the helper silently matching nothing and the tests passing vacuously."""
    names = _optional_field_names()
    assert len(names) > 20, f"only {len(names)} optional fields found; the filter is wrong"


@pytest.mark.parametrize("name", _optional_field_names())
def test_an_empty_value_is_read_as_unset(name, monkeypatch):
    """``FOO=`` must produce the field default, not ``""`` and not ``Path(".")``."""
    monkeypatch.setenv(name.upper(), "")
    # `_env_file=None` so a developer's own .env cannot supply a competing value and mask this.
    assert getattr(Settings(_env_file=None), name) is None


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_whitespace_only_is_also_unset(blank, monkeypatch):
    """Whitespace is what a hand-edited file produces, and it is not a value either."""
    monkeypatch.setenv("INTERMEDIATE_CACHE_DIR", blank)
    assert Settings(_env_file=None).intermediate_cache_dir is None


def test_the_intermediate_cache_falls_back_to_temp_dir(monkeypatch, tmp_path):
    """The concrete damage case: an empty value must not redirect the cache to the CWD.

    Asserts the resolved directory, not just the setting, because the defect lived in
    ``cache_dir()``'s ``or`` — the setting being falsy is the only thing that makes it fall back.
    """
    monkeypatch.setenv("INTERMEDIATE_CACHE_DIR", "")
    monkeypatch.setenv("TEMP_DIR", str(tmp_path))
    settings = Settings(_env_file=None)
    monkeypatch.setattr("worker.intermediate_cache.settings", settings)

    from worker.intermediate_cache import cache_dir

    resolved = cache_dir()
    assert resolved == tmp_path / "intermediates"
    assert resolved.resolve() != Path.cwd().resolve()


def test_an_explicit_value_still_wins(monkeypatch, tmp_path):
    """The normalisation must not swallow a real setting — otherwise it is not configurable."""
    target = tmp_path / "somewhere-else"
    monkeypatch.setenv("INTERMEDIATE_CACHE_DIR", str(target))
    assert Settings(_env_file=None).intermediate_cache_dir == target


def test_a_non_optional_field_keeps_its_empty_value(monkeypatch):
    """Only optional fields are normalised.

    ``app_name`` is a plain ``str``: emptying it is a mistake, and it should stay visible as one
    rather than being quietly replaced by the default.
    """
    monkeypatch.setenv("APP_NAME", "")
    assert Settings(_env_file=None).app_name == ""
