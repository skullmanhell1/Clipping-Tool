"""VERSION, the CHANGELOG and the git tags have to agree, or none of them means anything.

The repository documents eleven releases in `CHANGELOG.md` and has **zero git tags**, so the
history exists in prose only: there is no commit you can check out to get v0.9.0, and no way to
tell which commit a running `v0.11.0` was built from. `/api/info` reports that version to the UI
and `fallback_index_html` prints it on the diagnostic page, so it is the number an operator quotes
in a bug report — against a commit nobody can identify.

Backfilling the missing tags is deliberately **not** attempted. The commit each of those eleven
versions was released at is not recoverable from what is in the repository, and a tag placed on a
guess is worse than a missing one: a missing tag is visibly absent, while a wrong tag is a claim.
`scripts/release_tag.sh` starts tagging from the next release instead.

What is enforceable today is that the three sources cannot drift, which is what this module does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
CHANGELOG = ROOT / "CHANGELOG.md"

#: `major.minor.patch`, no pre-release or build metadata. Narrower than full semver on purpose:
#: `api/version.py` hands this string to `FastAPI(version=...)` and the UI prints it verbatim, so
#: the shape is part of the interface.
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

#: A released section heading: `## [1.2.3] - 2026-07-29`. `## [Unreleased]` deliberately does not
#: match — it is where work accumulates before it has a number.
RELEASED_HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)


def declared_version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def changelog_releases() -> list[tuple[str, str]]:
    """`[(version, date)]` in the order they appear, newest first."""
    return RELEASED_HEADING.findall(CHANGELOG.read_text(encoding="utf-8"))


def test_the_version_file_holds_one_bare_semver():
    version = declared_version()
    assert SEMVER.match(version), (
        f"VERSION is {version!r}. It is served from /api/info and printed in the UI, so it has to "
        "be a bare major.minor.patch — no leading 'v', no suffix."
    )


def test_the_version_matches_the_newest_released_changelog_entry():
    """So a release cannot ship with a CHANGELOG that describes the previous one.

    This is the drift that actually happens: VERSION is bumped as part of cutting a release and
    the CHANGELOG section is written afterwards, or the other way round. Either order leaves a
    window where the two disagree, and nothing else notices.
    """
    releases = changelog_releases()
    assert releases, "CHANGELOG.md has no released sections matching '## [x.y.z] - YYYY-MM-DD'"
    newest, _date = releases[0]
    assert declared_version() == newest, (
        f"VERSION says {declared_version()} but the newest released CHANGELOG section is {newest}. "
        "Bump both together, or move the entry under [Unreleased]."
    )


def test_the_changelog_still_has_an_unreleased_section():
    """Its absence means the next change has nowhere to be recorded, so it will not be."""
    assert "## [Unreleased]" in CHANGELOG.read_text(encoding="utf-8")


def test_released_versions_are_unique():
    versions = [version for version, _ in changelog_releases()]
    duplicates = sorted({v for v in versions if versions.count(v) > 1})
    assert not duplicates, f"CHANGELOG has more than one section for {duplicates}"


def test_released_versions_descend():
    """Newest first, so `releases[0]` is a meaningful thing to compare VERSION against.

    Compared as integer tuples rather than strings, because "0.11.0" sorts below "0.9.0"
    lexically — which is exactly the mistake that would make an out-of-order CHANGELOG look
    sorted.
    """
    versions = [tuple(int(part) for part in v.split(".")) for v, _ in changelog_releases()]
    assert versions == sorted(versions, reverse=True), (
        "CHANGELOG sections are not in descending version order: "
        f"{['.'.join(map(str, v)) for v in versions]}"
    )


def test_release_dates_do_not_move_backwards():
    """A later version dated before an earlier one means one of the two dates is wrong."""
    dated = changelog_releases()
    dates = [date for _version, date in dated]
    assert dates == sorted(
        dates, reverse=True
    ), f"release dates are not in descending order: {dated}"


def test_the_app_reports_the_declared_version():
    """`api/version.py` is what /api/info and the fallback page read.

    It has a `0.0.0` fallback for an unreadable VERSION file — deliberate, so a container with a
    broken filesystem reports a wrong version rather than failing to boot. This asserts the
    non-fallback path actually works, because a silent permanent `0.0.0` would look like the
    fallback doing its job.
    """
    from api.version import APP_VERSION, _read_version

    assert APP_VERSION == declared_version()
    assert _read_version() == declared_version()
    assert APP_VERSION != "0.0.0", "the version fallback is being used; VERSION is unreadable"


@pytest.mark.parametrize("script", ["scripts/release_tag.sh", "scripts/lock_requirements.sh"])
def test_the_release_scripts_are_executable(script):
    """A script committed without the bit set fails in CI with 'Permission denied'."""
    path = ROOT / script
    assert path.is_file(), f"{script} is missing"
    assert path.stat().st_mode & 0o111, f"{script} is not executable"
