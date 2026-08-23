#!/usr/bin/env python3
"""Mutation testing: break the code on purpose and check a test notices.

A passing suite proves the tests agree with the code. It does not prove they would *disagree* with
the wrong code, and for most of what this project does that is the distinction that matters: a
ranking change produces a plausible ordering, an emoji on the wrong word still renders, a caption in
a substituted font still encodes. None of those raise, so a test that merely exercises the path
passes either way.

The workflow is: apply one small, deliberately-wrong change; run the tests that claim to cover it;
and record whether anything failed.

* **CAUGHT** - a test failed. The behaviour is genuinely pinned.
* **ESCAPED** - everything passed. Either the tests do not cover the behaviour, or the mutation was
  *equivalent* and changed nothing observable. Both are findings, and they need different fixes: the
  first wants a test, the second wants the redundancy removed from the code.

Every escape found in practice has pointed at something real - a leaked file descriptor on each
unreadable font, an overlay box that was only even-sided at one frame width, a compositor wiring
that could be replaced with an empty list while every unit test still passed, and two cases of the
same fact being stated in two places so that changing one had no effect.

Usage
-----
One inline mutation::

    scripts/mutate.py --file worker/discourse.py \\
        --old 'if self.question:\\n            return 0.25' \\
        --new 'if self.question:\\n            return 0.5' \\
        -- pytest tests/test_selection_transcript.py

A batch from a spec file::

    scripts/mutate.py --spec tests/mutations/selection.json
    scripts/mutate.py --spec tests/mutations/selection.json --only s7_unanswered_neutral
    scripts/mutate.py --spec tests/mutations/selection.json --list

Spec format (JSON)::

    {
      "command": ["pytest", "tests/test_selection_transcript.py", "-q", "-x"],
      "mutations": [
        {
          "name": "s7_unanswered_neutral",
          "file": "worker/discourse.py",
          "old": "if self.question:\\n            return 0.25",
          "new": "if self.question:\\n            return 0.5",
          "why": "an unanswered question must score below a structureless passage"
        },
        {
          "name": "au9_mode_guard",
          "file": "worker/effects/sfx.py",
          "old": "...", "new": "...",
          "equivalent": true,
          "why": "documented equivalent mutant: the guard is redundant with the table below"
        }
      ]
    }

Exit status is ``0`` when every mutation was caught (or was declared equivalent and escaped as
expected), and ``1`` otherwise - so this can gate a branch.

Notes on the implementation, each of which is a bug this replaced
----------------------------------------------------------------
* **The anchor must match exactly once.** A substring appearing twice makes the mutation ambiguous,
  and patching the first occurrence silently tests something other than what was intended. Zero
  matches means the code moved and the mutation is stale, which reads as a false CAUGHT if the run
  happens to fail for another reason.
* **Backups are taken once, in memory, before anything runs.** An earlier version of this re-read
  the backup from disk between mutations, and refreshing it mid-batch produced false ESCAPEs that
  cost real time to understand.
* **Restore happens in ``finally`` and on a signal.** An interrupted run must not leave a
  deliberately-broken file in the working tree, because the next thing anyone does is run the tests
  and believe the result.
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

CAUGHT = "CAUGHT"
ESCAPED = "ESCAPED"
STALE = "STALE-ANCHOR"


@dataclass
class Mutation:
    """One deliberately-wrong edit and why it should be noticed."""

    name: str
    file: str
    old: str
    new: str
    why: str = ""
    #: Declared equivalent: it changes no observable behaviour, so escaping is the correct outcome.
    #: Use sparingly and always with ``why`` - an undocumented "equivalent" is how a real gap gets
    #: marked as expected.
    equivalent: bool = False

    @property
    def path(self) -> Path:
        return REPO_ROOT / self.file


@dataclass
class Result:
    mutation: Mutation
    status: str
    output: str = ""

    @property
    def ok(self) -> bool:
        """Whether this outcome is the expected one."""
        if self.status == STALE:
            return False
        if self.mutation.equivalent:
            return self.status == ESCAPED
        return self.status == CAUGHT


@dataclass
class _Backups:
    """In-memory snapshots, taken once, restored on any exit path."""

    contents: dict[Path, str] = field(default_factory=dict)

    def remember(self, path: Path) -> None:
        if path not in self.contents:
            self.contents[path] = path.read_text(encoding="utf-8")

    def restore(self) -> None:
        for path, text in self.contents.items():
            try:
                if path.read_text(encoding="utf-8") != text:
                    path.write_text(text, encoding="utf-8")
            except OSError:
                path.write_text(text, encoding="utf-8")


def _apply(mutation: Mutation, backups: _Backups) -> bool:
    """Apply ``mutation``. Returns ``False`` when the anchor is not uniquely present."""
    path = mutation.path
    if not path.is_file():
        print(f"  {STALE}: {mutation.file} does not exist")
        return False
    backups.remember(path)
    text = backups.contents[path]
    occurrences = text.count(mutation.old)
    if occurrences != 1:
        print(f"  {STALE}: anchor matched {occurrences} times in {mutation.file} (needs exactly 1)")
        return False
    path.write_text(text.replace(mutation.old, mutation.new), encoding="utf-8")
    return True


#: Ceiling on one mutated test run, in seconds.
#:
#: A mutation can introduce an infinite loop, and an unbounded harness then hangs for ever on it
#: rather than reporting it. Expiry counts as CAUGHT below, which is the right reading: a mutation
#: that makes the suite stop terminating *has* been detected by the suite.
MUTATION_RUN_TIMEOUT_S = 3600.0


def _run_tests(command: Sequence[str]) -> tuple[bool, str]:
    """Run ``command``; return ``(something_failed, tail_of_output)``."""
    try:
        proc = subprocess.run(
            list(command),
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=MUTATION_RUN_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return True, f"the test command did not finish within {MUTATION_RUN_TIMEOUT_S:g}s"
    output = (proc.stdout or "") + (proc.stderr or "")
    # The exit status is the signal, not a grep of the summary line: a collection error, an internal
    # error or a non-zero exit for any other reason all mean the tests noticed something, and a
    # summary-line grep misses every one of those.
    return proc.returncode != 0, output


def run(
    mutations: Sequence[Mutation],
    command: Sequence[str],
    *,
    verbose: bool = False,
) -> list[Result]:
    backups = _Backups()
    results: list[Result] = []

    def _on_signal(_signum, _frame):
        backups.restore()
        print("\ninterrupted - working tree restored", file=sys.stderr)
        raise SystemExit(130)

    previous = {sig: signal.signal(sig, _on_signal) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for mutation in mutations:
            backups.restore()  # start each mutation from clean source
            if not _apply(mutation, backups):
                results.append(Result(mutation, STALE))
                continue
            failed, output = _run_tests(command)
            status = CAUGHT if failed else ESCAPED
            results.append(Result(mutation, status, output))
            marker = "ok " if results[-1].ok else "!! "
            note = " (equivalent, expected)" if mutation.equivalent and status == ESCAPED else ""
            print(f"  {marker}{status:8} {mutation.name}{note}")
            if verbose and status == ESCAPED and not mutation.equivalent:
                print("      last lines of the passing run:")
                for line in output.strip().splitlines()[-3:]:
                    print(f"      | {line}")
    finally:
        backups.restore()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return results


def _load_spec(path: Path) -> tuple[list[Mutation], list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    command = data.get("command") or ["pytest", "-q", "-x"]
    mutations = [
        Mutation(
            name=entry["name"],
            file=entry["file"],
            old=entry["old"],
            new=entry["new"],
            why=entry.get("why", ""),
            equivalent=bool(entry.get("equivalent", False)),
        )
        for entry in data.get("mutations", [])
    ]
    return mutations, list(command)


def _report(results: Sequence[Result]) -> int:
    caught = [r for r in results if r.status == CAUGHT]
    escaped = [r for r in results if r.status == ESCAPED and not r.mutation.equivalent]
    expected = [r for r in results if r.status == ESCAPED and r.mutation.equivalent]
    stale = [r for r in results if r.status == STALE]

    print()
    print(
        f"{len(caught)} caught, {len(escaped)} escaped, "
        f"{len(expected)} equivalent-as-declared, {len(stale)} stale"
    )
    for result in stale:
        print(f"  STALE   {result.mutation.name} - the anchor no longer matches; update or drop it")
    for result in escaped:
        why = f" - expected: {result.mutation.why}" if result.mutation.why else ""
        print(f"  ESCAPED {result.mutation.name}{why}")
    if escaped:
        print()
        print(
            "An escape is either a missing test or an equivalent mutant. If the behaviour really "
            "cannot be observed, remove the redundancy from the code rather than declaring the "
            "mutation equivalent - a second source of truth is the thing that made it unobservable."
        )
    return 0 if not escaped and not stale else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--spec", type=Path, help="JSON file of mutations to run as a batch")
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="NAME",
        help="run only these mutations from the spec (repeatable)",
    )
    parser.add_argument("--list", action="store_true", help="list the spec's mutations and exit")
    parser.add_argument("--file", help="file to mutate (inline mode)")
    parser.add_argument("--old", help="exact text to replace; must occur exactly once")
    parser.add_argument("--new", help="replacement text")
    parser.add_argument("--why", default="", help="what should notice this")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="show test output for an escape"
    )
    parser.add_argument(
        "command", nargs="*", help="test command, after `--` (default: pytest -q -x)"
    )
    args = parser.parse_args(argv)

    if args.spec:
        mutations, command = _load_spec(args.spec)
        if args.only:
            wanted = set(args.only)
            unknown = wanted - {m.name for m in mutations}
            if unknown:
                parser.error(f"no such mutation(s) in the spec: {sorted(unknown)}")
            mutations = [m for m in mutations if m.name in wanted]
        if args.command:
            command = list(args.command)
    elif args.file and args.old is not None and args.new is not None:
        mutations = [Mutation("inline", args.file, args.old, args.new, args.why)]
        command = list(args.command) or ["pytest", "-q", "-x"]
    else:
        parser.error("give either --spec, or --file with --old and --new")
        return 2

    if args.list:
        for mutation in mutations:
            flag = " [equivalent]" if mutation.equivalent else ""
            print(f"{mutation.name}{flag}\n    {mutation.file}: {mutation.why or '(no note)'}")
        return 0

    if not mutations:
        print("no mutations to run")
        return 0

    print(f"running {len(mutations)} mutation(s) against: {' '.join(command)}")
    results = run(mutations, command, verbose=args.verbose)
    return _report(results)


if __name__ == "__main__":
    raise SystemExit(main())
