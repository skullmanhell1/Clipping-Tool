#!/usr/bin/env python3
"""Find modules and settings that nothing outside the tests uses.

The failure mode this exists to catch: **a module can be complete, correct, fully tested, and never
called, and the suite will not tell you.** `worker/word_spans.py` (C23) plus
`cue_constraints.apply_constraints` (C24) and `choose_break` (C25) all shipped that way -- roughly
600 lines with property-test coverage, three documented settings, and no effect on a single rendered
frame. Every gate in the project was green. Two real defects were hiding in that dead code and
surfaced within minutes of wiring it up.

A unit test of a pure function cannot tell whether anything calls it, and
`tests/test_config_documentation.py` proves a setting is *documented*, not that it is *read*. So
neither existing gate can see this, and both will stay green while a feature does nothing.

Why an AST walk rather than grep
--------------------------------
The obvious shell one-liner is wrong in a way that looks right. Matching ``import <name>`` misses
``from worker import cue_constraints, script_support, text_metrics``, because the module name is not
adjacent to the ``import`` keyword -- which reports nearly every module in this project as unwired.
The first version of this check did exactly that and produced eighteen false positives. Parsing is
the only way to resolve comma lists, aliases, ``from . import x`` and dotted forms uniformly.

Deliberately not a linter plugin: this is a *reachability* question about one repository's layout,
not a style rule, and expressing it as ~150 readable lines beats configuring something general.

Limits, stated rather than implied
----------------------------------
This proves a module is *imported* by non-test code, not that the import is *reached at runtime*. A
call sitting behind a condition that is never true still counts as wired. It is a floor, not a proof
-- but it is the floor that was missing, and it catches the case that actually happened.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Packages whose modules are expected to be reachable from production code.
#:
#: `scripts/` is excluded on purpose: those are entry points, run by a human or by CI, and nothing
#: importing them is the normal case rather than a defect.
#:
#: `evaluation/` is excluded for the same reason and it is worth being explicit, because five of its
#: modules do trip this check. `caption_timing`, `fidelity`, `golden_render`, `preference` and `sync`
#: are *instruments* -- M9 to M12 -- run against the pipeline by hand or by CI, not called by it.
#: An instrument nothing imports is doing its job; a pipeline feature nothing imports is dead. Those
#: are different questions and folding them together would bury the second in the first.
PACKAGES = ("worker", "publishers")

#: Directories that never count as a *user* of a module.
#:
#: `tests/` is the whole point -- a test importing the module under test is exactly the evidence that
#: misleads here.
NON_USERS = ("tests", ".venv", "build", "dist", "node_modules", ".git")

#: Modules that are legitimately imported by nothing, with the reason.
#:
#: An allowlist rather than silence, so the next person sees the claim and can challenge it. Anything
#: added here needs a reason that is about *this* module, not "it is not called yet" -- that belongs
#: in :data:`KNOWN_UNWIRED`, which is debt rather than design.
ALLOWED: dict[str, str] = {
    "worker.engines.kinetic": (
        "registered by side-effect import in worker/engines/loader.py, which the registry "
        "discovers; there is deliberately no direct import of the engine class"
    ),
}

#: Dead code this check found on the day it was written, so ``--check`` can gate *new* dead code
#: immediately instead of waiting for the backlog to be cleared.
#:
#: A ratchet, not an allowlist, and the difference is enforced: `tests/test_check_wired.py` asserts
#: every name here is **still** unwired, so wiring one up without deleting its entry fails the suite.
#: The list can only shrink. Without that assertion a baseline becomes a list of things that used to
#: be broken, which is worse than no baseline because it reads as current.
#:
#: Each of these is a shipped, tested feature with no effect on output. That is the point of the file.
KNOWN_UNWIRED: dict[str, str] = {
    "worker.effects.sfx": (
        "A15 sound effects. Nothing imports it and `sfx_volume` is read by nothing; `SFX_MODE=off` "
        "is documented as the default, but there is no path that would honour any other value"
    ),
    "worker.caption_placement": (
        "V15 keeping captions off the speaker's mouth. Nothing imports it, so "
        "`caption_avoid_faces` cannot take effect -- the collision it describes still ships"
    ),
}

#: ``Settings`` fields nothing reads, same ratchet, same enforcement.
#:
#: Two kinds here and the distinction matters when clearing them. Some are inert *because* their
#: module is unwired (`stabilise_strength`, `sfx_volume`) and will fix themselves when it is wired.
#: The rest are documented environment variables that were never plumbed anywhere -- a reader setting
#: `API_PORT` or `REDIS_URL` today gets silence, which is worse than an unsupported option because it
#: looks supported.
KNOWN_UNREAD: dict[str, str] = {
    "sfx_volume": "A15; inert until worker.effects.sfx is wired",
    "face_detector_backend": (
        "documented as the detector used 'when a job does not specify one', but "
        "`resolve_detector` is only ever called with the per-job option, so this default is "
        "never consulted"
    ),
    "api_host": "never plumbed; the server is started by an explicit uvicorn invocation",
    "api_port": "never plumbed; as api_host",
    "redis_url": "never plumbed; commented out in docker-compose.yml and read by no code",
    "rq_queue_name": "never plumbed; the in-process executor is the only live job path",
    "use_inprocess_fallback": "never plumbed; the in-process path is unconditional",
    "public_base_url": "never plumbed",
    "music_default_volume": "never plumbed; the per-job option carries the value instead",
    "background_color": "never plumbed; ffmpeg_utils takes its own default parameter",
    "background_style": "never plumbed; as background_color",
    "x_api_key": "never plumbed; publishers/x.py references neither this nor x_api_secret",
    "x_api_secret": "never plumbed; as x_api_key",
}


#: This file, which must never count as a user of anything.
#:
#: It named every setting in :data:`KNOWN_UNREAD` as a string constant, the settings scan counts
#: string constants as reads, and so the baseline made all fourteen look read -- the check reported a
#: clean tree because it was reading itself. A self-referential gate that always passes is the worst
#: possible outcome for a gate, so the exclusion is explicit rather than incidental.
SELF = Path(__file__).resolve()


def _is_user(path: Path) -> bool:
    if path.resolve() == SELF:
        return False
    parts = path.relative_to(ROOT).parts
    return not any(part in NON_USERS for part in parts)


def _module_name(path: Path) -> str:
    relative = path.relative_to(ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(path: Path) -> set[str]:
    """Every module name ``path`` imports, in fully-qualified form.

    Both halves of a ``from X import Y`` are recorded: ``Y`` may be a submodule (``from worker import
    word_spans``) or a symbol (``from worker.word_spans import apply_hygiene``), and which one it is
    cannot be known without resolving the package. Recording both is safe because a symbol name that
    happens to collide with a module name only ever makes this check *less* likely to report a
    problem, and a false negative here is a missed warning while a false positive is noise that gets
    the whole check switched off.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()

    found: set[str] = set()
    package = _module_name(path).rsplit(".", 1)[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # Relative: `from . import x` inside worker/ means worker.x.
                base = package
                for _ in range(node.level - 1):
                    base = base.rsplit(".", 1)[0] if "." in base else ""
                module = f"{base}.{node.module}" if node.module else base
            else:
                module = node.module or ""
            if module:
                found.add(module)
            for alias in node.names:
                found.add(f"{module}.{alias.name}" if module else alias.name)
    return found


def unwired_modules() -> list[str]:
    """Modules in :data:`PACKAGES` that no non-test file imports."""
    candidates: set[str] = set()
    for package in PACKAGES:
        for path in sorted((ROOT / package).rglob("*.py")):
            if path.name == "__init__.py" or "__pycache__" in path.parts:
                continue
            candidates.add(_module_name(path))

    used: set[str] = set()
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in path.parts or not _is_user(path):
            continue
        module = _module_name(path)
        for imported in _imports(path):
            if imported != module:
                used.add(imported)

    return sorted(name for name in candidates - used if name not in ALLOWED)


def unread_settings() -> list[str]:
    """``Settings`` fields that nothing outside ``config.py`` reads.

    Matched by attribute access and by name inside ``getattr``, because this project reaches settings
    both ways -- ``settings.min_cue_seconds`` and ``getattr(settings, "caption_safe_area", "")`` are
    both idiomatic here.
    """
    config = ROOT / "config.py"
    tree = ast.parse(config.read_text(encoding="utf-8"))
    fields: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            for statement in node.body:
                if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                    fields.add(statement.target.id)

    read: set[str] = set()
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in path.parts or not _is_user(path):
            continue
        # `config.py` is scanned for *reads* even though it declares the fields, because a computed
        # property is a legitimate consumer: `api_auth_token_value` reads `self.api_auth_token`, and
        # excluding the file reported that field as unread. Declarations cannot be mistaken for reads
        # -- a field is an `AnnAssign` whose target is a `Name`, and only `Attribute` nodes count
        # below.
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                read.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                read.add(node.value)
    return sorted(fields - read)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero on anything unwired that is not in the recorded baseline (for CI)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="also list the recorded baseline, which --check tolerates",
    )
    args = parser.parse_args()

    modules = unwired_modules()
    settings_ = unread_settings()
    new_modules = [name for name in modules if name not in KNOWN_UNWIRED]
    new_settings = [name for name in settings_ if name not in KNOWN_UNREAD]

    for name in new_modules:
        print(f"UNWIRED MODULE: {name} - imported by nothing outside tests/")
    for name in new_settings:
        print(f"UNREAD SETTING: {name} - documented but read by nothing outside config.py")

    if args.all:
        for name in modules:
            if name in KNOWN_UNWIRED:
                print(f"known unwired module: {name} - {KNOWN_UNWIRED[name]}")
        for name in settings_:
            if name in KNOWN_UNREAD:
                print(f"known unread setting: {name} - {KNOWN_UNREAD[name]}")

    if new_modules or new_settings:
        print(
            f"\n{len(new_modules)} unwired module(s) and {len(new_settings)} unread setting(s) "
            "are not in the recorded baseline.\n"
            "A module nothing imports has no effect on output, however well tested it is.\n"
            "Wire it up, or -- if it is reachable by a mechanism this cannot see, such as a "
            "registry side-effect import -- add it to ALLOWED in scripts/check_wired.py with the "
            "reason."
        )
        return 1 if args.check else 0

    print(
        f"no new dead code: {len(modules)} known unwired module(s), "
        f"{len(settings_)} known unread setting(s), 0 new"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
