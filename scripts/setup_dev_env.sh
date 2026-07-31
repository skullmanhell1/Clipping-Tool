#!/usr/bin/env bash
# Install what the test suite needs beyond `pip install -r requirements-dev.txt`.
#
# The suite has no skips by design: a skipped test is not a passing test, and an earlier ffmpeg gap
# went unnoticed for several releases precisely because ~90 tests quietly stopped running and CI
# reported green. So the system-level dependencies are not optional, and getting them wrong looks
# like success.
#
# Three of them, each for a specific reason:
#
#   ffmpeg + ffprobe   Most of this project is a filter graph. Without them the real-binary
#                      capability tests cannot run, and those exist to catch the probe bugs every
#                      mocked test misses.
#   Liberation fonts   The documented terminal rung of the caption font chain, and the one face
#                      both the Dockerfile and CI install by name.
#   libGL + glib2      `import cv2` raises `libGL.so.1: cannot open shared object file` without
#                      them, so every vision path silently takes its degraded branch even though
#                      the packages are installed. CI installed opencv and got no coverage from it
#                      for a while for exactly this reason.
#
# It also installs the bundled caption faces the way the Dockerfile does (A2). libass reaches them
# through `fontsdir` regardless, but the capability probe reads the *system* font list, so without
# this `font_available()` disagrees with what will actually render.
#
# Idempotent: safe to re-run, and cheap when there is nothing to do.
#
# Usage:  bash scripts/setup_dev_env.sh
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE="${FFMPEG_CACHE_DIR:-$REPO/.devtools/bin}"
mkdir -p "$CACHE"

install_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update >/dev/null 2>&1
    apt-get install -y --no-install-recommends \
      fonts-liberation fontconfig libgl1 libglib2.0-0 xz-utils >/dev/null 2>&1
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y liberation-sans-fonts liberation-serif-fonts liberation-mono-fonts \
      fontconfig mesa-libGL mesa-libGLES glib2 xz >/dev/null 2>&1
  else
    echo "note: no apt-get or dnf found - install the Liberation fonts, fontconfig," >&2
    echo "      libGL and glib2 by hand, or ~90 tests will skip and CI will fail." >&2
  fi
}

if ! fc-list : family 2>/dev/null | grep -qi liberation; then
  install_packages
fi

# ffmpeg: several distributions ship no usable package, so fall back to a static build. Cached
# under .devtools/ so a re-run is a copy rather than a 42 MB download.
if ! command -v ffmpeg >/dev/null 2>&1; then
  if [ ! -x "$CACHE/ffmpeg" ]; then
    if command -v apt-get >/dev/null 2>&1 && apt-get install -y ffmpeg >/dev/null 2>&1; then
      :
    else
      echo "fetching a static ffmpeg build..."
      tmp="$(mktemp -d)"
      curl -sSL -o "$tmp/ff.tar.xz" \
        https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz \
        && tar xf "$tmp/ff.tar.xz" -C "$tmp" \
        && d="$(find "$tmp" -maxdepth 1 -type d -name 'ffmpeg-*-amd64-static' | head -1)" \
        && install -m755 "$d/ffmpeg" "$d/ffprobe" "$CACHE/"
      rm -rf "$tmp"
    fi
  fi
  [ -x "$CACHE/ffmpeg" ] && install -m755 "$CACHE/ffmpeg" "$CACHE/ffprobe" /usr/local/bin/ 2>/dev/null
fi

# The bundled caption faces, registered with fontconfig as the Dockerfile does (A2).
if [ -d "$REPO/assets/fonts" ] && command -v fc-cache >/dev/null 2>&1; then
  mkdir -p /usr/share/fonts/clipping-tool 2>/dev/null \
    && cp "$REPO"/assets/fonts/*.ttf /usr/share/fonts/clipping-tool/ 2>/dev/null
  fc-cache -f >/dev/null 2>&1
fi

# --- report, so a partial setup is visible rather than discovered as a skip later -------
printf 'ffmpeg      %s\n' "$(ffmpeg -version 2>/dev/null | head -1 || echo 'MISSING - the real-binary tests will skip and CI will fail')"
printf 'liberation  %s family/families\n' "$(fc-list : family 2>/dev/null | tr ',' '\n' | sed 's/^ *//' | sort -u | grep -c -i '^liberation')"
printf 'bundled     %s (1 = Anton resolves to itself, which C1 broke once)\n' \
  "$(fc-match Anton 2>/dev/null | grep -c Anton)"
printf 'opencv      %s\n' "$("$REPO/.venv/bin/python" -c 'import cv2; print(cv2.__version__)' 2>&1 | tail -1)"
