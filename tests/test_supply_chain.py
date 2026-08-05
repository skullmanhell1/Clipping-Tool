"""Drift pins for the supply-chain controls added in the deployment phase.

Every assertion here corresponds to a defect that was actually present, and each one is silent: a
dependency resolving to a different version than the one that was tested does not announce itself,
and neither does a lockfile that has stopped being used. These are cheap text assertions against the
manifests, in the same spirit as the parity guards elsewhere in this suite — the point is that a
later edit which undoes one of them fails a test instead of quietly shipping.

The findings they pin:

* ``npm install`` in the Dockerfile, which may resolve a newer version than the lockfile records and
  rewrite the lock while doing it — so the image could ship a dependency tree no test run had seen.
* ``"@whop/sdk": "latest"``, i.e. a third-party package running in the API's container, version
  chosen by what had been published that morning.
* No Python lockfile at all, so the same commit built twice produced two dependency sets.
* Direct requirements with no upper bound.
* ``pillow`` held on 10.4.0 by a ``<11.0`` ceiling while pip-audit reported 17 vulnerabilities in it,
  all fixed above that ceiling.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

DOCKERFILE = (ROOT / "Dockerfile").read_text()
COMPOSE_TEXT = (ROOT / "docker-compose.yml").read_text()
CI_TEXT = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
REQUIREMENTS = (ROOT / "requirements.txt").read_text()

#: The requirement files that must have a committed, hash-pinned lock beside them.
LOCKED_PAIRS = (
    ("requirements.txt", "requirements.lock"),
    ("requirements-dev.txt", "requirements-dev.lock"),
)

#: Direct requirements allowed to have no upper bound, with the reason recorded here so that adding
#: to this tuple is a visible decision rather than a quiet edit.
#:
#: ``yt-dlp`` exists to track sites that change their players without notice. A ceiling would convert
#: "someone must review an upgrade" into "URL ingest silently stops working", which is a worse
#: outcome from the same mechanism. Reproducibility is not given up for it: the lock still pins the
#: exact version and hash.
UNBOUNDED_BY_DESIGN = ("yt-dlp",)


def _direct_requirements(text: str) -> dict[str, str]:
    """Map requirement name -> version specifier for the direct requirements in a file."""
    found: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.match(r"^([A-Za-z0-9_.\-]+)\s*(\[[^\]]*\])?\s*(.*)$", line)
        if match:
            found[match.group(1).lower()] = match.group(3).strip()
    return found


# --------------------------------------------------------------------------- #
# Lockfiles                                                                   #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("source", "lock"), LOCKED_PAIRS)
def test_a_lock_exists_for_each_requirements_file(source: str, lock: str) -> None:
    assert (ROOT / lock).is_file(), f"{lock} is missing; regenerate it (see {source})"


@pytest.mark.parametrize(("source", "lock"), LOCKED_PAIRS)
def test_every_locked_requirement_is_exact_and_hashed(source: str, lock: str) -> None:
    """A lock is only a control if it pins *and* verifies.

    An exact version stops an accidental upgrade; the hash is what stops a substituted artifact on
    the index. And because ``pip --require-hashes`` rejects the whole file when any single
    requirement lacks a hash, one unhashed line does not degrade the lock — it disables it.
    """
    text = (ROOT / lock).read_text()
    requirements = [
        line for line in text.splitlines() if line and not line.startswith((" ", "\t", "#", "-"))
    ]
    assert requirements, f"{lock} pins nothing"

    unpinned = [line for line in requirements if "==" not in line]
    assert not unpinned, f"{lock} has requirements that are not pinned with ==: {unpinned[:5]}"

    # Each requirement's hashes are the indented continuation lines that follow it.
    blocks = re.split(r"\n(?=[^\s#-])", text)
    missing = [
        block.splitlines()[0]
        for block in blocks
        if "==" in block.splitlines()[0] and "--hash=sha256:" not in block
    ]
    assert not missing, f"{lock} has requirements with no hash: {missing[:5]}"


def test_the_image_installs_from_the_lock_with_hashes_enforced() -> None:
    """Without ``--require-hashes`` the lock is a suggestion.

    pip will happily install a pinned version whose artifact does not match the recorded digest
    unless this flag is present, which is the difference between reproducibility and integrity.
    """
    assert "requirements.lock" in DOCKERFILE, "the image does not reference requirements.lock"
    assert re.search(
        r"pip install[^\n]*--require-hashes[^\n]*-r requirements\.lock", DOCKERFILE
    ), "the image must install requirements.lock with --require-hashes"


def test_ci_installs_from_the_lock_rather_than_the_ranges() -> None:
    """A green run has to mean a specific dependency set was green."""
    assert re.search(
        r"pip install[^\n]*--require-hashes[^\n]*-r requirements-dev\.lock", CI_TEXT
    ), "CI must install requirements-dev.lock with --require-hashes"


def test_ci_fails_when_a_lock_is_stale() -> None:
    """A lock that no longer matches its requirements is worse than none: it looks authoritative.

    The same argument as ``black --check`` — the check is what makes the artefact trustworthy.
    """
    assert "Locks match their requirements files" in CI_TEXT


# --------------------------------------------------------------------------- #
# Version bounds                                                              #
# --------------------------------------------------------------------------- #
def test_every_direct_requirement_has_an_upper_bound() -> None:
    """An unbounded requirement accepts the next major release sight unseen."""
    offenders = [
        f"{name}{spec}"
        for name, spec in _direct_requirements(REQUIREMENTS).items()
        if name not in UNBOUNDED_BY_DESIGN and "<" not in spec and "==" not in spec
    ]
    assert not offenders, (
        f"unbounded direct requirements: {offenders}. Add an upper bound, or add the name to "
        "UNBOUNDED_BY_DESIGN with the reason."
    )


def test_pillow_is_above_the_versions_that_fixed_its_advisories() -> None:
    """Guards the specific regression of lowering this ceiling back.

    ``Pillow>=10.3,<11.0`` resolved to 10.4.0, in which pip-audit reports 17 vulnerabilities — all
    fixed in 12.1.1/12.2.0/12.3.0, so all of them *unreachable* without crossing that ceiling. A
    large share are in the font and image parsers, and this app opens media derived from arbitrary
    downloaded video and ships a directory of font files.

    Asserted on the floor rather than trusting the audit step alone, because the audit needs network
    access and a current advisory database, whereas this is true offline and forever.
    """
    spec = _direct_requirements(REQUIREMENTS)["pillow"]
    floor = re.search(r">=\s*(\d+)\.(\d+)", spec)
    assert floor, f"pillow has no lower bound: {spec!r}"
    major, minor = int(floor.group(1)), int(floor.group(2))
    assert (major, minor) >= (12, 3), (
        f"pillow floor is {major}.{minor}; must be >= 12.3, which is where the last of the 17 "
        "advisories against 10.4.0 was fixed"
    )


# --------------------------------------------------------------------------- #
# npm                                                                         #
# --------------------------------------------------------------------------- #
def test_the_dockerfile_never_uses_npm_install() -> None:
    """``npm ci`` installs exactly the lockfile; ``npm install`` may resolve past it and rewrite it.

    Both npm stages had this, and the second one mattered more: it installed a package declared as
    ``"latest"``.
    """
    offenders = [
        line.strip()
        for line in DOCKERFILE.splitlines()
        if re.search(r"\bnpm\s+install\b", line) and not line.strip().startswith("#")
    ]
    assert not offenders, f"use `npm ci` instead of `npm install`: {offenders}"
    assert "npm ci" in DOCKERFILE


@pytest.mark.parametrize("manifest", ["frontend/package.json", "publisher_bridge/package.json"])
def test_no_package_declares_a_floating_version(manifest: str) -> None:
    """``"latest"`` is not a version; it is "whatever was published most recently".

    ``publisher_bridge`` declared ``"@whop/sdk": "latest"``, so every image build installed an
    unreviewed third-party package — running in the same container as the API — selected by the
    calendar.
    """
    data = json.loads((ROOT / manifest).read_text())
    floating = {
        name: spec
        for section in ("dependencies", "devDependencies")
        for name, spec in (data.get(section) or {}).items()
        if spec in ("latest", "*", "next", "") or str(spec).startswith("http")
    }
    assert not floating, f"{manifest} declares floating versions: {floating}"


@pytest.mark.parametrize("directory", ["frontend", "publisher_bridge"])
def test_every_npm_package_has_a_committed_lockfile(directory: str) -> None:
    """``npm ci`` fails outright without one, so the Dockerfile depends on these being present."""
    assert (
        ROOT / directory / "package-lock.json"
    ).is_file(), f"{directory}/package-lock.json is missing, so `npm ci` cannot run there"


def test_the_whop_lock_agrees_with_its_manifest() -> None:
    """``npm ci`` refuses to install when the manifest and lock have drifted.

    Pinning the manifest without regenerating the lock would therefore break the optional
    ``INSTALL_WHOP_BRIDGE=true`` build — and only that build, which nothing else here exercises.
    """
    manifest = json.loads((ROOT / "publisher_bridge" / "package.json").read_text())
    lock = json.loads((ROOT / "publisher_bridge" / "package-lock.json").read_text())
    declared = manifest["dependencies"]["@whop/sdk"]
    assert lock["packages"][""]["dependencies"]["@whop/sdk"] == declared
    assert lock["packages"]["node_modules/@whop/sdk"]["version"] == declared


# --------------------------------------------------------------------------- #
# Image and compose                                                          #
# --------------------------------------------------------------------------- #
def test_the_image_asserts_ffmpeg_is_usable_rather_than_merely_installed() -> None:
    """``apt-get install ffmpeg`` says nothing about which ffmpeg arrived.

    The exact Debian version is deliberately not pinned — Debian rotates point releases out of the
    archive, so an exact pin makes the image unbuildable weeks later. What the pin was *for* is
    asserted instead: a major-version floor, and the presence of ``subtitles`` (libass), without
    which burned captions cannot render at all.
    """
    assert "FFMPEG_MIN_MAJOR" in DOCKERFILE, "the image does not assert an ffmpeg version floor"
    assert re.search(
        r"-filters[^\n]*subtitles", DOCKERFILE
    ), "the image does not assert that ffmpeg has the subtitles filter (libass)"


def test_ci_proves_opencv_and_ffmpeg_actually_work() -> None:
    """Installed is not the same as importable.

    ``import cv2`` needs libgl1 at runtime; without it every vision path caught the ImportError and
    silently took its degraded branch, so CI installed opencv and got no coverage from it.
    """
    assert "import cv2" in CI_TEXT, "CI does not assert that opencv actually imports"
    assert "subtitles" in CI_TEXT, "CI does not assert that ffmpeg has libass"


def test_compose_does_not_restate_the_images_start_command() -> None:
    """Two definitions of the start-up line is one too many.

    ``docker-compose.yml`` repeated the Dockerfile's ``CMD`` verbatim, so changing it in the
    Dockerfile left compose silently running the old one.
    """
    compose = yaml.safe_load(COMPOSE_TEXT)
    app = compose["services"]["app"]
    assert "command" not in app, (
        "docker-compose.yml sets `command:`, which duplicates the Dockerfile CMD; the image knows "
        "how to start itself"
    )


def test_the_audit_step_blocks_and_reads_the_lock() -> None:
    """A step that cannot fail reports nothing — the lesson of ``ruff check . || true``.

    Auditing the lock rather than the ranges is the substance: ranges answer "could a safe version
    satisfy this?", when the question is "is the version we ship safe?".
    """
    audit = CI_TEXT.split("Audit Python dependencies", 1)
    assert len(audit) == 2, "the pip-audit step is gone"
    step = audit[1]
    assert (
        "--requirement requirements.lock" in step
    ), "pip-audit must audit the lock, not the ranges"
    # `continue-on-error` would appear within this step's block, before the next job/step at column 2.
    assert (
        "continue-on-error" not in step.split("\n  frontend:")[0]
    ), "the pip-audit step is non-blocking again"


# --------------------------------------------------------------------------- #
# Automation                                                                  #
# --------------------------------------------------------------------------- #
def test_dependabot_covers_every_dependency_surface() -> None:
    """Pinning without an upgrade path is how ``pillow`` reached 17 open advisories.

    A gap in any one ecosystem is invisible, so each is asserted by name rather than by count.
    """
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    covered = {(entry["package-ecosystem"], entry["directory"]) for entry in config["updates"]}
    for expected in (
        ("pip", "/"),
        ("npm", "/frontend"),
        ("npm", "/publisher_bridge"),
        ("github-actions", "/"),
        ("docker", "/"),
    ):
        assert expected in covered, f"dependabot does not cover {expected}"


def test_a_release_workflow_exists_and_is_driven_by_the_version_file() -> None:
    """The repo had zero tags and zero releases while shipping an update checker.

    ``updates.py`` asks GitHub for ``releases/latest``, so with no releases that call 404s and the
    "update available" banner can never appear — a documented, tested, API-exposed feature that was
    inert. Driven by ``VERSION`` because that is already the source of truth the app reports.
    """
    workflow = ROOT / ".github" / "workflows" / "release.yml"
    assert workflow.is_file(), "no release workflow, so no release can ever be published"
    config = yaml.safe_load(workflow.read_text())
    # PyYAML parses a bare `on:` key as the boolean True, which is why this is not `config["on"]`.
    triggers = config.get("on") or config.get(True)
    assert (
        "VERSION" in triggers["push"]["paths"]
    ), "the release is not triggered by VERSION changing"


def test_codeql_analyses_both_languages() -> None:
    """Neither ``pip-audit`` nor ``npm audit`` can see a taint path through this repo's own code.

    Which is the shape that matters here: the app takes a caller's URL and hands it to yt-dlp, and
    builds ffmpeg arguments influenced by request data.
    """
    workflow = ROOT / ".github" / "workflows" / "codeql.yml"
    assert workflow.is_file()
    config = yaml.safe_load(workflow.read_text())
    languages = config["jobs"]["analyze"]["strategy"]["matrix"]["language"]
    assert "python" in languages
    assert any("javascript" in language for language in languages)
