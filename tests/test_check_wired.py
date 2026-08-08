"""The dead-code check, and the property that keeps its baseline honest.

`scripts/check_wired.py` exists because three caption features shipped implemented, tested and never
called — `worker/word_spans.py`, `cue_constraints.apply_constraints` and `choose_break` — with no
importer outside their own test modules. A unit test of a pure function cannot tell whether anything
calls it, and `test_config_documentation.py` proves a setting is *documented*, not *read*, so neither
existing gate could see it.

The test that matters most here is `test_every_baseline_entry_is_still_dead`. A baseline that is
allowed to keep entries for things somebody has since fixed becomes a list of historical problems
that reads as current, which is worse than having no baseline. Enforcing that the list can only
shrink is what makes it a ratchet rather than an excuse.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load():
    """Import the script by path; `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location(
        "check_wired", ROOT / "scripts" / "check_wired.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_wired"] = module
    spec.loader.exec_module(module)
    return module


cw = _load()

#: Computed once at import. Each call walks and parses every `.py` file in the repository, and the
#: two ratchet tests below are parametrised over 18 baseline entries -- calling per test took 32
#: seconds for work whose answer cannot change during a run.
#:
#: The fixture tests further down deliberately call the functions directly instead, because they
#: monkeypatch `ROOT` to a toy tree and need a fresh walk of it.
UNWIRED = cw.unwired_modules()
UNREAD = cw.unread_settings()


def test_the_tree_has_no_dead_code_outside_the_baseline():
    """The gate itself. A new unwired module or unread setting fails here, not in review."""
    unexpected_modules = [m for m in UNWIRED if m not in cw.KNOWN_UNWIRED]
    unexpected_settings = [s for s in UNREAD if s not in cw.KNOWN_UNREAD]

    assert unexpected_modules == [], (
        "these modules are imported by nothing outside tests/, so they have no effect on output: "
        f"{unexpected_modules}"
    )
    assert unexpected_settings == [], (
        "these settings are documented but read by nothing, so setting them does nothing: "
        f"{unexpected_settings}"
    )


@pytest.mark.parametrize("name", sorted(cw.KNOWN_UNWIRED))
def test_every_baseline_module_entry_is_still_dead(name):
    """The ratchet. Wire a module up and its baseline entry must go with it.

    Without this the baseline rots into a list of things that used to be broken, and the next reader
    cannot tell which entries are still true — so they trust none of them, which is the same as not
    having the file.
    """
    assert name in UNWIRED, (
        f"{name} is now imported outside tests/, so delete its KNOWN_UNWIRED entry in "
        "scripts/check_wired.py -- the baseline may only shrink"
    )


@pytest.mark.parametrize("name", sorted(cw.KNOWN_UNREAD))
def test_every_baseline_setting_entry_is_still_dead(name):
    assert name in UNREAD, (
        f"{name} is now read outside config.py, so delete its KNOWN_UNREAD entry in "
        "scripts/check_wired.py -- the baseline may only shrink"
    )


def test_it_finds_a_module_that_is_only_imported_by_its_own_test(tmp_path, monkeypatch):
    """The detection itself, on a tree small enough to reason about completely.

    Built rather than asserted against the real repository, because a test that only checks the real
    tree passes for as long as the real tree happens to be clean and proves nothing about whether the
    check works.
    """
    (tmp_path / "worker").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "worker" / "__init__.py").write_text("")
    (tmp_path / "worker" / "live.py").write_text("VALUE = 1\n")
    (tmp_path / "worker" / "dead.py").write_text("VALUE = 2\n")
    (tmp_path / "worker" / "pipeline.py").write_text("from worker import live\n")
    (tmp_path / "tests" / "test_dead.py").write_text("from worker import dead\n")

    monkeypatch.setattr(cw, "ROOT", tmp_path)
    monkeypatch.setattr(cw, "PACKAGES", ("worker",))
    monkeypatch.setattr(cw, "ALLOWED", {})
    monkeypatch.setattr(cw, "SELF", tmp_path / "nonexistent.py")

    unwired = cw.unwired_modules()
    # Membership rather than equality: `pipeline` is the fixture's entry point and nothing imports it
    # either, which is correct for a toy tree and irrelevant to what this test claims.
    assert "worker.dead" in unwired
    assert "worker.live" not in unwired


def test_a_comma_form_import_counts_as_wiring(tmp_path, monkeypatch):
    """The bug that made the first version of this check useless.

    A shell one-liner matching `import <name>` misses `from worker import a, b, c`, because the module
    name is not adjacent to the keyword. That version reported eighteen modules as unwired, nearly the
    whole package, and would have been switched off as noise within a day.
    """
    (tmp_path / "worker").mkdir()
    (tmp_path / "worker" / "__init__.py").write_text("")
    for name in ("alpha", "beta", "gamma"):
        (tmp_path / "worker" / f"{name}.py").write_text("VALUE = 1\n")
    (tmp_path / "worker" / "pipeline.py").write_text("from worker import alpha, beta, gamma\n")

    monkeypatch.setattr(cw, "ROOT", tmp_path)
    monkeypatch.setattr(cw, "PACKAGES", ("worker",))
    monkeypatch.setattr(cw, "ALLOWED", {})
    monkeypatch.setattr(cw, "SELF", tmp_path / "nonexistent.py")

    unwired = cw.unwired_modules()
    assert [n for n in unwired if n != "worker.pipeline"] == []


def test_a_relative_import_counts_as_wiring(tmp_path, monkeypatch):
    """`from . import x` and `from .x import y` resolve to the same module as the absolute forms."""
    (tmp_path / "worker").mkdir()
    (tmp_path / "worker" / "__init__.py").write_text("")
    (tmp_path / "worker" / "alpha.py").write_text("VALUE = 1\n")
    (tmp_path / "worker" / "beta.py").write_text("VALUE = 1\n")
    (tmp_path / "worker" / "pipeline.py").write_text(
        "from . import alpha\nfrom .beta import VALUE\n"
    )

    monkeypatch.setattr(cw, "ROOT", tmp_path)
    monkeypatch.setattr(cw, "PACKAGES", ("worker",))
    monkeypatch.setattr(cw, "ALLOWED", {})
    monkeypatch.setattr(cw, "SELF", tmp_path / "nonexistent.py")

    unwired = cw.unwired_modules()
    assert "worker.alpha" not in unwired
    assert "worker.beta" not in unwired


def test_the_checker_does_not_count_itself_as_a_user():
    """It named every baseline setting as a string constant, so it read itself clean.

    The settings scan counts string constants as reads — `getattr(settings, "caption_safe_area", "")`
    is idiomatic here — and `KNOWN_UNREAD` names all fourteen. Before the exclusion the check reported
    a spotless tree, which is the worst way for a gate to fail.
    """
    assert not cw._is_user(ROOT / "scripts" / "check_wired.py")
    assert UNREAD, "the settings scan found nothing, which means it is reading itself"
