#!/usr/bin/env bash
# I12: build the Docker image and verify it actually serves the app.
#
# The plan recorded this as "never been run", and a Dockerfile that has never been built is a
# deployment story rather than a deployment. Three things can be wrong in ways nothing else
# catches, because each of them is invisible from a working checkout:
#
#   1. the build fails (a missing system package, an apt name change);
#   2. the build succeeds and the app cannot start (a dependency only present in the dev venv);
#   3. both succeed and an *asset* is missing, because `.dockerignore` excluded it. The font
#      chain has already broken once this way (C1), and the symptom - captions in a substituted
#      face - is visible only in rendered output.
#
# So this checks the built image serves `/healthz`, serves the built SPA (which lives only in
# the frontend build stage), and can enumerate the bundled fonts *through the API* rather than
# from the filesystem - `fc-match` succeeding proves fontconfig registration, not that the app's
# own manifest and files both arrived.
#
# Bounded, not detached: it starts the server inside the container, probes it, and exits, so it
# is a check that finishes rather than a service someone has to remember to stop.
#
# Usage:
#   scripts/docker_smoke.sh            # build, then verify
#   scripts/docker_smoke.sh --no-build # verify an already-built clipping-tool:smoke
set -euo pipefail

IMAGE="${IMAGE:-clipping-tool:smoke}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "${1:-}" != "--no-build" ]; then
  echo "==> building $IMAGE"
  docker build -t "$IMAGE" "$ROOT"
fi

echo "==> booting $IMAGE and probing it"
docker run --rm --entrypoint sh "$IMAGE" -c '
uvicorn api.main:app --host 127.0.0.1 --port 8000 > /tmp/uv.log 2>&1 &
SERVER=$!
# <<"PY", quoted. An unquoted heredoc is shell-expanded, and this one is Python source, so
# backticks in it were command substitutions: the comment below mentioning fc-match had been
# silently running `fc-match Anton` in the container on every smoke run, and any $ would have
# been interpolated away. Quoting the delimiter passes the program through literally.
#
# Double quotes around the delimiter, not single: the whole program already sits inside a
# single-quoted sh -c string, and an apostrophe anywhere in here closes that quote and hands the
# remainder of the file to the outer shell. Double quotes disable heredoc expansion just as well.
python - <<"PY"
import json, time, urllib.request

def get(path, tries=60):
    last = None
    for _ in range(tries):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=2) as r:
                return r.status, r.headers.get("content-type", ""), r.read()
        except Exception as exc:
            last = exc
            time.sleep(0.5)
    raise AssertionError(f"{path} never responded: {last}")

status, _ctype, body = get("/healthz")
assert status == 200, (path, status)
print("healthz:", status, body[:120].decode("utf-8", "replace"))

# The SPA exists only in the frontend build stage, so this is the one check that the
# multi-stage COPY landed.
status, ctype, body = get("/")
assert status == 200 and b"<div id=\"root\"" in body, "the built SPA is not served from the image"
print("spa:", status, ctype.split(";")[0], len(body), "bytes")

info = json.loads(get("/api/info")[2])
effects = info.get("effects") or {}

fonts = [f["name"] for f in effects.get("caption_fonts", [])]
# Through the API, not the filesystem: `fc-match Anton` succeeding only proves the Dockerfile
# registered the faces with fontconfig. This proves the *manifest* and the *files* both arrived
# under /app, which is what the picker and libass fontsdir actually read.
assert "Anton" in fonts, f"bundled fonts unreachable through the API: {fonts}"
print("caption_fonts:", len(fonts), "->", fonts[:4], "...")

presets = effects.get("caption_presets") or []
assert len(presets) >= 8, f"caption presets missing: {presets}"
print("caption_presets:", len(presets))

# Emoji assets are committed precisely so a render needs no network (A7); an image without
# them renders every clip with the overlay silently absent.
import os
count = len([n for n in os.listdir("/app/assets/emoji") if n.endswith(".png")])
assert count >= 300, f"only {count} emoji vendored in the image"
print("emoji vendored:", count)

# ffmpeg is installed from the Debian archive, so the base image tag decides its version. The
# Dockerfile pins `python:3.11-slim-bookworm` rather than `-slim` for exactly that reason, and
# printing the version here is what makes a change to it visible: filter defaults move between
# major versions, so an ffmpeg jump can alter rendered output with no code change at all.
#
# Reported, not asserted. Pinning an exact version here would fail the build the day Debian ships
# a security patch, which is a change we want -- and a gate that fires on wanted changes gets
# deleted. The number is here so a shift shows up as a diff in the smoke log.

import subprocess
ffmpeg = subprocess.run(
    ["ffmpeg", "-version"], capture_output=True, text=True, check=True
).stdout.splitlines()[0]
assert ffmpeg.startswith("ffmpeg version"), ffmpeg
print("ffmpeg:", ffmpeg.split(" Copyright")[0])

print("I12 OK")
PY
RC=$?
kill $SERVER 2>/dev/null || true
if [ "$RC" -ne 0 ]; then echo "--- uvicorn log ---"; tail -40 /tmp/uv.log; fi
exit $RC
' 2>&1 | grep -v 'level=warning'
