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


def test_every_baseline_module_entry_is_still_dead():
    """The ratchet. Wire a module up and its baseline entry must go with it.

    Without this the baseline rots into a list of things that used to be broken, and the next reader
    cannot tell which entries are still true — so they trust none of them, which is the same as not
    having the file.

    **Deliberately not parametrised, unlike its settings counterpart below.** `KNOWN_UNWIRED` is now
    empty -- A15 was the last entry -- and `pytest.mark.parametrize` over an empty sequence produces
    a *skipped* test. This suite has no skips by design, and a skip here would be the worst possible
    reading: the ratchet reporting "not run" at the exact moment the debt reaches zero, which looks
    identical to the ratchet having been switched off. Asserting over the dict in one test keeps the
    empty case a genuine pass.
    """
    revived = [name for name in sorted(cw.KNOWN_UNWIRED) if name not in UNWIRED]

    assert revived == [], (
        f"{revived} are now imported outside tests/, so delete their KNOWN_UNWIRED entries in "
        "scripts/check_wired.py -- the baseline may only shrink"
    )


def test_every_baseline_setting_entry_is_still_dead():
    """The settings half of the ratchet. Not parametrised, for the reason given above.

    `KNOWN_UNREAD` is also empty now: four of the thirteen were plumbed and the other eight retired.
    Both baselines being empty is the intended end state, and neither ratchet may express that as a
    skip.
    """
    revived = [name for name in sorted(cw.KNOWN_UNREAD) if name not in UNREAD]

    assert revived == [], (
        f"{revived} are now read outside config.py, so delete their KNOWN_UNREAD entries in "
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


def test_the_checker_does_not_count_itself_as_a_user(tmp_path, monkeypatch):
    """It named every baseline setting as a string constant, so it read itself clean.

    The settings scan counts string constants as reads — `getattr(settings, "caption_safe_area", "")`
    is idiomatic here — and `KNOWN_UNREAD` named all fourteen. Before the exclusion the check reported
    a spotless tree, which is the worst way for a gate to fail.

    **Rewritten to prove that on a fixture tree rather than on the real repository.** The previous
    version asserted `UNREAD` was non-empty, using the existence of real debt as a proxy for "the scan
    is not reading itself". That proxy was valid only while the debt existed: clearing the last entry
    made this test fail even though the exclusion it guards was working perfectly. A test that breaks
    when the thing it *wants* finally happens is measuring the wrong quantity — and the honest fix is
    to construct the confusion deliberately, which also makes the test independent of the tree's
    state forever.

    The fixture is the exact shape of the original bug: `checker.py` mentions `orphan` as a string
    constant and nothing else reads it. Excluded, `orphan` is correctly reported unread; counted as a
    user, it vanishes and the gate goes quiet.
    """
    (tmp_path / "worker").mkdir()
    (tmp_path / "worker" / "__init__.py").write_text("")
    (tmp_path / "worker" / "pipeline.py").write_text("VALUE = 1\n")
    (tmp_path / "config.py").write_text(
        "class Settings:\n    orphan: str = ''\n    used: str = ''\n"
    )
    (tmp_path / "worker" / "reader.py").write_text("from config import settings\nsettings.used\n")
    # The self-reference: a file whose only mention of `orphan` is the string constant.
    checker = tmp_path / "checker.py"
    checker.write_text('KNOWN_UNREAD = {"orphan": "reason"}\n')

    monkeypatch.setattr(cw, "ROOT", tmp_path)
    monkeypatch.setattr(cw, "PACKAGES", ("worker",))

    monkeypatch.setattr(cw, "SELF", checker)
    assert not cw._is_user(checker), "the checker counted itself as a user of the tree"
    assert "orphan" in cw.unread_settings(), (
        "an unread setting went unreported while the checker excluded itself"
    )
    assert "used" not in cw.unread_settings()

    # And the failure the exclusion exists to prevent: counted as a user, the gate reads clean.
    monkeypatch.setattr(cw, "SELF", tmp_path / "nonexistent.py")
    assert "orphan" not in cw.unread_settings(), (
        "the fixture does not reproduce the original bug, so the assertion above proves nothing"
    )
