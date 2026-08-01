#!/usr/bin/env bash
# Re-resolve requirements.txt / requirements-dev.txt into their lockfiles.
#
# Run this whenever a range in requirements.txt or requirements-dev.txt changes, and commit the
# resulting .lock files. CI checks that the locks are in step with the inputs
# (`--check`), so a range edited without re-locking fails the build rather than
# silently making the lock a description of the past.
#
# The locks carry `--hash=` for every package, which means pip verifies the artefact it
# downloaded is the one that was resolved. Without hashes a lockfile pins a *name and version*,
# which a compromised or re-uploaded index entry can still satisfy with different bytes.
#
# Deliberately not locked: requirements-ml.txt. torch's wheels differ per platform and per CUDA
# build, and the documented CPU-only install path passes `--extra-index-url` to select a
# different set entirely — a single lockfile cannot describe all of those, and one that pinned
# the default CUDA wheels would break the smaller install the docs recommend.
#
#   scripts/lock_requirements.sh            # rewrite the lockfiles
#   scripts/lock_requirements.sh --check    # exit 1 if they are out of date
set -euo pipefail

cd "$(dirname "$0")/.."

# Pinned so the lock does not change because the resolver did. `uv` is used rather than
# pip-compile because pip-tools 7.6 is incompatible with pip >= 26 (it imports
# `pip._internal.utils.compat.stdlib_pkgs`, which was removed).
UV="${UV:-uv}"
if ! command -v "$UV" >/dev/null 2>&1; then
    echo "uv is not installed. Install it with: pip install uv" >&2
    exit 1
fi

# The runtime interpreter, matching the Dockerfile's python:3.11-slim and
# `target-version = "py311"` in pyproject.toml. Resolving on a different minor would pick
# different wheels for anything with an `python_version` marker.
PYTHON_VERSION=3.11

compile_one() {
    local input="$1" output="$2"
    "$UV" pip compile \
        --quiet \
        --generate-hashes \
        --python-version "$PYTHON_VERSION" \
        --output-file "$output" \
        "$input"
}

CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

status=0
for pair in "requirements.txt:requirements.lock" "requirements-dev.txt:requirements-dev.lock"; do
    input="${pair%%:*}"
    output="${pair##*:}"

    if [ "$CHECK" -eq 1 ]; then
        tmp="$(mktemp)"
        trap 'rm -f "$tmp"' EXIT
        compile_one "$input" "$tmp"
        # Compared in Python rather than with `diff`. diffutils is not present in every slim
        # image — including this project's own dev sandbox — and a gate that silently reports
        # "out of date" because a coreutils binary is missing is worse than no gate: the
        # obvious response is to stop trusting it.
        if ! python3 - "$output" "$tmp" <<'PY'; then
import sys

def body(path):
    """The requirement lines, without the header uv writes (it records the output path)."""
    with open(path, encoding="utf-8") as handle:
        return [line.rstrip("\n") for line in handle if not line.lstrip().startswith("#")]

committed, fresh = body(sys.argv[1]), body(sys.argv[2])
if committed == fresh:
    sys.exit(0)

only_committed = [line for line in committed if line not in set(fresh)]
only_fresh = [line for line in fresh if line not in set(committed)]
for line in only_committed[:20]:
    print(f"  committed only: {line.strip()}", file=sys.stderr)
for line in only_fresh[:20]:
    print(f"  re-resolved only: {line.strip()}", file=sys.stderr)
sys.exit(1)
PY
            echo "$output is out of date with $input. Run scripts/lock_requirements.sh" >&2
            status=1
        else
            echo "$output is in step with $input"
        fi
        rm -f "$tmp"
        trap - EXIT
    else
        compile_one "$input" "$output"
        echo "wrote $output ($(grep -c '^[a-zA-Z0-9]' "$output") packages)"
    fi
done

exit "$status"
