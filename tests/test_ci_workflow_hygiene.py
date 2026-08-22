"""CI checks the things it claims to check, on a runtime that still exists.

Two failures in this repository's history motivate this file, and both were invisible to the suite.

**The first PR here failed on "Set up Node".** The frontend was scaffolded with `package-lock.json`
left to be "generated on first npm install", and `actions/setup-node` was configured with
`cache: npm` and `cache-dependency-path: frontend/package-lock.json`. A missing cache-dependency
path is a *hard* failure, not a cache miss, so the job died with

    Some specified paths were not resolved, unable to cache dependencies.

which names neither the lockfile nor the fact that `npm ci` cannot run without one.

**The second and third PRs failed on "Import & boot smoke test"** — a step that never booted
anything. It ran `from api.main import app`, and FastAPI does not run the lifespan on import, so the
entire startup path was unexercised in CI. That is the direct reason a `render.yaml` which could not
boot at all shipped: the gate that would have caught it executed nowhere.

Both are now fixed, and this file exists so they stay fixed. In keeping with the working agreement,
the boot smoke is **run** rather than read — the sibling `tests/test_ci_skip_gate.py` opens with the
observation that asserting strings appear in the workflow would not have caught its defect either,
because every string was already correct.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
import yaml

from config import BASE_DIR

WORKFLOWS = sorted((BASE_DIR / ".github" / "workflows").glob("*.yml"))

#: The first major of each action that runs natively on Node 24, verified against each action's own
#: `runs.using` rather than inferred from the version number.
#:
#: Node 20 is deprecated on GitHub-hosted runners and every action still targeting it is currently
#: "being forced to run on Node.js 24" — it works today and breaks when the substitution is
#: withdrawn. Pinning to the first node24 major fixes that with the smallest behavioural delta.
#:
#: `actions/upload-artifact` is the trap: **v5 is still node20**, so the obvious +1 bump from v4
#: does not clear the deprecation. That is exactly the kind of detail a version bump done by
#: pattern-matching gets wrong, which is why the floor is recorded per action.
NODE24_FLOOR = {
    "actions/checkout": 5,
    "actions/setup-node": 5,
    "actions/setup-python": 6,
    "actions/cache": 5,
    "actions/upload-artifact": 6,
    "github/codeql-action": 4,
}


def _steps(workflow) -> list[tuple[str, str, dict]]:
    """Every step in a workflow as ``(job_name, step_name, step)``."""
    parsed = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    out = []
    for job_name, job in (parsed.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            out.append((job_name, str(step.get("name") or step.get("uses") or "?"), step))
    return out


def _action_uses() -> list[tuple[str, str]]:
    """Every ``uses:`` reference across all workflows, as ``(workflow_name, ref)``."""
    found = []
    for workflow in WORKFLOWS:
        for _job, _name, step in _steps(workflow):
            if "uses" in step:
                found.append((workflow.name, str(step["uses"])))
    return found


def test_workflows_exist_and_parse():
    """A guard on every other test here, all of which would pass vacuously on an empty list."""
    assert WORKFLOWS, "no workflow files found"
    assert _action_uses(), "no `uses:` references found - the parser is looking in the wrong place"


@pytest.mark.parametrize(("workflow", "ref"), _action_uses(), ids=lambda v: str(v))
def test_no_action_targets_the_deprecated_node_runtime(workflow, ref):
    """Every pinned action major runs natively on Node 24.

    Not a style preference. Actions targeting Node 20 are being run on Node 24 by substitution
    today, so this is a breakage with a date on it rather than a live failure — and the annotation
    that reports it appears on *passing* runs too, which is how it went unnoticed through several
    releases of CI changes.
    """
    action = ref.split("@")[0]
    version = ref.split("@")[1] if "@" in ref else ""
    # Sub-actions like `github/codeql-action/init` are governed by their repository's floor.
    owner_repo = "/".join(action.split("/")[:2])
    floor = NODE24_FLOOR.get(owner_repo)
    if floor is None:
        pytest.fail(
            f"{workflow}: {ref} is an action with no recorded Node 24 floor. Check its "
            f"`runs.using` and add it to NODE24_FLOOR, rather than assuming it is fine."
        )
    assert version.startswith("v"), f"{workflow}: {ref} is not pinned to a major tag"
    major = int(version[1:].split(".")[0])
    assert major >= floor, (
        f"{workflow}: {ref} targets Node 20, which is deprecated on GitHub runners. "
        f"{owner_repo} first ships Node 24 in v{floor}."
    )


def test_the_frontend_checks_for_the_lockfile_before_setting_up_node():
    """Ordering is the whole point, so it is asserted rather than the mere presence of a step.

    After `setup-node` the damage is done: `cache: npm` has already turned a missing lockfile into
    a message about caching. The guard is only useful in front of it.
    """
    ci = BASE_DIR / ".github" / "workflows" / "ci.yml"
    frontend = [(name, step) for job, name, step in _steps(ci) if job == "frontend"]
    assert frontend, "no frontend job found in ci.yml"

    lock_guard = next(
        (i for i, (_n, s) in enumerate(frontend) if "package-lock.json" in str(s.get("run", ""))),
        None,
    )
    setup_node = next(
        (
            i
            for i, (_n, s) in enumerate(frontend)
            if str(s.get("uses", "")).startswith("actions/setup-node")
        ),
        None,
    )
    assert lock_guard is not None, (
        "the frontend job does not check that frontend/package-lock.json exists. Without it a "
        "missing lockfile fails on Set up Node with 'unable to cache dependencies', which is how "
        "the first PR in this repository failed."
    )
    assert setup_node is not None, "the frontend job does not set up Node"
    assert lock_guard < setup_node, (
        "the lockfile check runs after Set up Node, where it is useless - setup-node has already "
        "failed on the unresolved cache path by then."
    )


def test_the_backend_boot_step_runs_the_lifespan_rather_than_an_import():
    """The specific regression: a step named "boot" that performs an import.

    Pinned on the *script* rather than on the absence of a string, because there are many ways to
    write an import test and only one thing that makes this step meaningful — entering the
    lifespan. If the boot smoke is ever replaced, it should be replaced by something that also
    starts the app.
    """
    ci = BASE_DIR / ".github" / "workflows" / "ci.yml"
    backend_runs = [str(step.get("run", "")) for job, _name, step in _steps(ci) if job == "backend"]
    joined = "\n".join(backend_runs)
    assert "scripts/boot_smoke.py" in joined, (
        "the backend job no longer runs scripts/boot_smoke.py. FastAPI does not run the lifespan "
        "on import, so an import-only check leaves the whole startup path - directory creation, "
        "the writability proof, the security gate, the sweeper - unexercised in CI."
    )


def test_the_boot_smoke_actually_boots():
    """Run it, do not read it.

    This is the assertion that has teeth. It starts the application through its real lifespan,
    serves two endpoints, and confirms a misconfigured production environment is refused at
    startup — the last of which is the difference between "the gate function raises" (already
    covered) and "the app will not start", which is what actually protects a deployment.

    A subprocess because the script is an entry point and `config.settings` is built from the
    environment at import time; driving it in-process would test a mock of the thing being checked.
    """
    result = subprocess.run(
        [sys.executable, "scripts/boot_smoke.py"],
        capture_output=True,
        text=True,
        cwd=str(BASE_DIR),
        timeout=600,
    )
    assert result.returncode == 0, (
        f"scripts/boot_smoke.py failed:\n{result.stdout}\n{result.stderr}"
    )
    assert "lifespan + /healthz" in result.stdout
    assert "production gate         refuses to boot when misconfigured" in result.stdout
