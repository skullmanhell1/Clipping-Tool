#!/usr/bin/env bash
# Create the annotated git tag for the version in VERSION.
#
# Why this exists: the CHANGELOG documents eleven releases and the repository has **zero tags**, so
# the release history is prose. There is no commit you can check out to get v0.9.0, and when
# `/api/info` reports v0.11.0 there is no way to tell which commit that build came from — which is
# the first question a bug report needs answered.
#
# The eleven historical tags are deliberately not backfilled: the commit each was released at is
# not recoverable from the repository, and a tag placed on a guess is worse than a missing one. A
# missing tag is visibly absent; a wrong tag is a false claim that tooling will believe.
#
#   scripts/release_tag.sh            # create the tag locally
#   scripts/release_tag.sh --push     # create it and push it
#
# It refuses rather than guesses:
#   - VERSION must be a bare major.minor.patch
#   - CHANGELOG.md must have a released section for exactly that version
#   - the tag must not already exist
#   - the working tree must be clean, so the tag names a reviewable commit
#   - HEAD must be on the default branch, because a tag on a feature branch points at a commit
#     that may never be merged
set -euo pipefail

cd "$(dirname "$0")/.."

PUSH=0
[ "${1:-}" = "--push" ] && PUSH=1

VERSION="$(tr -d '[:space:]' < VERSION)"
TAG="v${VERSION}"

if ! printf '%s' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "VERSION is '${VERSION}', which is not a bare major.minor.patch." >&2
    exit 1
fi

# The CHANGELOG section is the release notes. Requiring it here is what stops a tag existing for a
# version nobody wrote up — `tests/test_release_metadata.py` enforces the same agreement in CI.
if ! grep -Eq "^## \[${VERSION}\] - [0-9]{4}-[0-9]{2}-[0-9]{2}" CHANGELOG.md; then
    echo "CHANGELOG.md has no released section '## [${VERSION}] - YYYY-MM-DD'." >&2
    echo "Move the [Unreleased] entries under a dated heading for ${VERSION} first." >&2
    exit 1
fi

if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
    echo "${TAG} already exists (at $(git rev-parse --short "${TAG}")). Bump VERSION." >&2
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "The working tree is dirty. A tag has to name a commit someone can review:" >&2
    git status --short >&2
    exit 1
fi

DEFAULT_BRANCH="${DEFAULT_BRANCH:-main}"
CURRENT="$(git rev-parse --abbrev-ref HEAD)"
if [ "$CURRENT" != "$DEFAULT_BRANCH" ]; then
    echo "On '${CURRENT}', not '${DEFAULT_BRANCH}'. Tagging a feature branch points the release at" >&2
    echo "a commit that may never be merged. Merge first, or set DEFAULT_BRANCH." >&2
    exit 1
fi

# Annotated, not lightweight: an annotated tag carries the tagger, the date and a message, and is
# what `git describe` reports. A lightweight tag is a bare pointer with no record of who made it.
NOTES="$(awk -v v="## [${VERSION}]" '
    index($0, v) == 1 { printing = 1; next }
    printing && /^## \[/ { exit }
    printing { print }
' CHANGELOG.md)"

git tag -a "${TAG}" -m "${TAG}" -m "${NOTES}"
echo "created ${TAG} at $(git rev-parse --short HEAD)"

if [ "$PUSH" -eq 1 ]; then
    git push origin "refs/tags/${TAG}"
    echo "pushed ${TAG}"
else
    echo "not pushed. Push with: git push origin refs/tags/${TAG}"
fi
