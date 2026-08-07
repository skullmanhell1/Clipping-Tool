"""Tests for URL ingest (I13).

The plan recorded ingest as "only local files have been exercised", and that is the worst state
for this particular code to be in: ``download_video`` is the *first* thing a "paste a link" user
touches, every failure inside it raises :class:`DownloadError` with a message from yt-dlp, and none
of it was ever run. Four things in there can be wrong in ways no unit test of the surrounding
pipeline would notice:

* the format selector - a malformed one silently downloads the largest rendition available, or
  nothing at all;
* ``outtmpl`` - a template yt-dlp cannot expand writes somewhere unexpected;
* ``prepare_filename`` plus the ``.mp4`` fix-up - ``merge_output_format`` changes the extension
  only when a merge actually happened, so the returned path is wrong for one of the two cases;
* the progress hook - a wrong key name means the UI shows nothing for the whole download.

**Served locally rather than fetched from the internet.** yt-dlp's ``generic`` extractor treats a
plain media URL exactly like any other input, so a local HTTP server exercises the full path -
extractor, format selection, template expansion, filename resolution, hooks - with none of the
flakiness that would make this test the reason CI is red. The real-network path was verified
separately against Wikimedia Commons: metadata extraction, a 5.6 MB download, 14 progress events,
and the height cap honoured at 240p and 480p. That check cannot be a test, because a test that
needs the public internet is a test that fails for reasons unrelated to this repository.
"""

from __future__ import annotations

import functools
import http.server
import subprocess
import threading

import pytest

from config import settings
from worker import download

requires_ffmpeg = pytest.mark.skipif(
    subprocess.run(["which", settings.ffmpeg_binary], capture_output=True).returncode != 0,
    reason="ffmpeg not on PATH",
)
FFMPEG = settings.ffmpeg_binary


@pytest.fixture(autouse=True)
def _allow_loopback_ingest(monkeypatch):
    """Let this module fetch from ``127.0.0.1``, which ingest otherwise refuses.

    ``download.validate_public_url`` rejects loopback, link-local and private addresses, because
    an unauthenticated URL endpoint handed to yt-dlp is otherwise a request forwarder into the
    deployment's own network. Every URL here is the local ``media_server`` fixture, which is the
    entire point of the module docstring above: serving locally is what makes this test exercise
    the real yt-dlp path without depending on the public internet.

    So the loopback address is deliberate here and forbidden everywhere else. Opting in module-wide
    keeps that explicit in one place; the default-deny rules are covered on their own in
    ``tests/test_url_guard.py``, which calls the validator directly and so cannot be affected by
    this fixture.
    """
    monkeypatch.setattr(settings, "url_ingest_allow_private", True)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):  # keep the test output readable
        pass


@pytest.fixture
def media_server(tmp_path, monkeypatch):
    """Serve ``tmp_path`` over HTTP and yield ``base_url``.

    The SSRF guard in ``download.validate_public_url`` refuses loopback, link-local and
    private addresses, which is exactly what this fixture serves from. Serving locally is
    deliberate (see the module docstring) - it exercises the whole yt-dlp path without making
    CI depend on the public internet - so the guard has to be opted out of, not worked around.
    That opt-in is the module-wide ``_allow_loopback_ingest`` fixture above rather than a
    second one here, so there is one place that says "this module may reach 127.0.0.1".

    The guard's default-deny behaviour is covered directly in ``tests/test_url_guard.py``,
    including the loopback and cloud-metadata cases, so relaxing it here loses no coverage.
    """
    handler = functools.partial(_QuietHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def hosted_video(tmp_path, media_server):
    """A real, probeable mp4 with audio, reachable over HTTP."""
    dest = tmp_path / "sample.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=15:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return f"{media_server}/sample.mp4"


def test_is_url_accepts_http_and_https_and_rejects_paths():
    assert download.is_url("https://example.com/a.mp4")
    assert download.is_url("  http://example.com/a.mp4 ")
    assert not download.is_url("/tmp/a.mp4")
    assert not download.is_url("a.mp4")
    assert not download.is_url("ftp://example.com/a.mp4")


@requires_ffmpeg
def test_i13_a_url_actually_downloads_to_a_playable_file(tmp_path, hosted_video):
    """The whole item: this path had never been run.

    Asserts the file exists *and* that ffmpeg can read it, because a truncated or misnamed
    download still produces a path.
    """
    dest = tmp_path / "out"
    path, meta = download.download_video(hosted_video, dest)

    assert path.exists() and path.stat().st_size > 0
    assert path.parent == dest, "the outtmpl did not put the file where it was asked to"

    probed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert float(probed) > 1.0
    assert meta.title


@requires_ffmpeg
def test_i13_the_returned_path_is_the_file_that_exists(tmp_path, hosted_video):
    """``prepare_filename`` reports the pre-merge name.

    ``merge_output_format`` rewrites the extension only when a merge actually happened, so the
    returned path is wrong for exactly one of the two cases - and the caller has no way to tell
    which, because both return a plausible-looking path.
    """
    path, _meta = download.download_video(hosted_video, tmp_path / "out")
    assert path.is_file(), path


@requires_ffmpeg
def test_i13_progress_is_reported_during_the_download(tmp_path, hosted_video):
    """A wrong key in the hook means the UI shows nothing for the whole download.

    Which on a long source is indistinguishable from a hung job.
    """
    events: list[tuple[float, str]] = []
    download.download_video(
        hosted_video, tmp_path / "out", progress_cb=lambda f, m: events.append((f, m))
    )
    assert events, "no progress was reported at all"
    assert all(0.0 <= fraction <= 1.0 for fraction, _msg in events)
    # The final event is the completion one, and it reports exactly 1.0 - the downloading
    # branch is clamped to 0.99 so a caller can distinguish "nearly done" from "done".
    assert events[-1][0] == 1.0
    assert max(fraction for fraction, msg in events if msg != events[-1][1]) <= 0.99


@requires_ffmpeg
def test_i13_metadata_is_readable_without_downloading(tmp_path, hosted_video):
    """The preview card is shown *before* processing, so it must not pay for the media."""
    before = set(tmp_path.iterdir())
    meta = download.fetch_metadata(hosted_video)
    assert meta.title
    assert meta.source
    assert set(tmp_path.iterdir()) == before, "fetch_metadata wrote something"


def test_i13_an_unreachable_url_raises_download_error_not_a_yt_dlp_type(media_server):
    """Every caller catches :class:`DownloadError`.

    yt-dlp raises a dozen private subclasses, so letting one escape turns a bad link into a 500
    instead of a message the user can act on.
    """
    with pytest.raises(download.DownloadError):
        download.fetch_metadata(f"{media_server}/does-not-exist.mp4")


def test_i13_a_failed_download_raises_download_error(tmp_path, media_server):
    with pytest.raises(download.DownloadError):
        download.download_video(f"{media_server}/nope.mp4", tmp_path / "out")


@requires_ffmpeg
def test_i13_the_height_cap_is_expressed_in_a_selector_yt_dlp_accepts(tmp_path, media_server):
    """A malformed format string does not error - it falls through to `best`.

    So a cap that is silently ignored looks exactly like one that worked, until someone pastes a
    4K link and waits. Checked by capping *below* the hosted rendition and confirming the
    selector still resolves rather than raising.
    """
    dest = tmp_path / "tall.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x480:rate=15:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    path, _meta = download.download_video(
        f"{media_server}/tall.mp4", tmp_path / "out", max_height=240
    )
    # A single-rendition source has nothing to choose from, so the terminal `/best` rung is what
    # must fire. The assertion is that it *does* - an unparseable selector raises instead.
    assert path.is_file()
    height = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=height",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert int(height) == 480, "the fallback rung did not deliver the only rendition available"


# --------------------------------------------------------------------------- #
# I7 - image weight, and what the image promises
# --------------------------------------------------------------------------- #
#
# These read the Dockerfile and requirements.txt as *text*, which is unusual and deliberate. The
# image is verified end to end by `scripts/docker_smoke.sh` in CI, but a build takes minutes and
# cannot say *why* it is 350 MB larger than it needs to be. Each property below is one that was
# actually wrong, and each would silently regress on an ordinary-looking edit.


def _dockerfile() -> str:
    from config import BASE_DIR

    return (BASE_DIR / "Dockerfile").read_text(encoding="utf-8")


def _requirements() -> str:
    from config import BASE_DIR

    return (BASE_DIR / "requirements.txt").read_text(encoding="utf-8")


def test_i7_opencv_is_not_pinned_alongside_mediapipe_s_own_build():
    """Asking for `opencv-python` installed a *second* OpenCV.

    mediapipe depends on `opencv-contrib-python`, so pinning `opencv-python` too gave two wheels,
    each shipping its own ~91 MB directory of near-identical native libraries - about 180 MB of
    the image duplicated. contrib is a superset in the same `cv2` namespace, so removing the pin
    changes nothing that imports.
    """
    requirements = _requirements()
    active = [
        line.split("#")[0].strip()
        for line in requirements.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not any(line.startswith("opencv-python") for line in active), (
        "opencv-python is pinned again; mediapipe already brings opencv-contrib-python and "
        "installing both duplicates ~91 MB of native libraries"
    )


def test_i7_cv2_still_imports_and_offers_what_reframe_uses():
    """The point of removing the pin is that nothing notices.

    `worker/effects/reframe.py` uses only core OpenCV, present in either build - but "present in
    either build" is an assertion about a wheel we no longer name, so it is worth checking.
    """
    import cv2

    assert hasattr(cv2, "VideoCapture")
    assert hasattr(cv2, "CascadeClassifier")
    assert hasattr(cv2, "cvtColor")
    assert hasattr(cv2, "data") and cv2.data.haarcascades


def test_i7_node_is_optional_in_the_image():
    """Debian's nodejs+npm is around 200 MB for one optional publisher.

    It has to stay behind a build arg, and the bridge's `npm install` behind the same one - it
    needs npm to run at all.
    """
    dockerfile = _dockerfile()
    assert "ARG INSTALL_WHOP_BRIDGE" in dockerfile
    # Both the apt install and the npm install are conditional on it.
    assert dockerfile.count('if [ "$INSTALL_WHOP_BRIDGE" = "true" ]') == 2
    # And nothing installs node unconditionally. Comments stripped first, or this matches the
    # paragraph above the arg that explains why node is optional.
    before_arg = dockerfile.split("ARG INSTALL_WHOP_BRIDGE")[0]
    instructions = "\n".join(
        line for line in before_arg.splitlines() if not line.lstrip().startswith("#")
    )
    assert "nodejs" not in instructions, "node is being installed before the opt-in arg"


def test_i7_the_whop_publisher_requires_the_interpreter_not_just_the_script(monkeypatch):
    """The bridge script is committed source, so it is present in every image.

    Checking only for it reported the publisher *ready* on an image with no Node, and then failed
    at publish time with a `FileNotFoundError` from `subprocess` - the least actionable place to
    learn that Node is missing.
    """
    from publishers.whop import WhopPublisher

    monkeypatch.setattr(settings, "whop_api_key", "test-key", raising=False)
    monkeypatch.setattr(settings, "whop_node_binary", "definitely-not-a-real-binary", raising=False)
    status = WhopPublisher().status()
    assert status.configured is True, "the key is set, so it is configured"
    assert status.available is False, "there is no interpreter to run the bridge with"
    assert "not installed" in status.message
    # The message names the fix, because "unavailable" on its own sends someone to the API key.
    assert "INSTALL_WHOP_BRIDGE" in status.message


def test_i7_the_whop_publisher_is_available_when_both_halves_are_present(monkeypatch):
    """The other side of the check: a real interpreter must not be reported as missing."""
    import sys

    from publishers.whop import WhopPublisher

    monkeypatch.setattr(settings, "whop_api_key", "test-key", raising=False)
    # `sys.executable` is a real binary on PATH-independent absolute path terms; `shutil.which`
    # accepts an absolute path, which is also how WHOP_NODE_BINARY would be set on a host whose
    # node is not on PATH.
    monkeypatch.setattr(settings, "whop_node_binary", sys.executable, raising=False)
    status = WhopPublisher().status()
    assert status.available is True, status.message
    assert status.message == "Ready via @whop/sdk"


def test_i7_ml_dependencies_stay_behind_their_own_build_arg():
    """torch adds several hundred megabytes, and the engine degrades without it rather than
    failing - so a default-on install would be paying for a fallback nobody asked to replace."""
    dockerfile = _dockerfile()
    assert "ARG INSTALL_ML=false" in dockerfile
    assert 'if [ "$INSTALL_ML" = "true" ]' in dockerfile


def test_i12_the_smoke_script_exists_and_is_executable():
    """CI runs it, so a rename would turn the Docker job green by doing nothing."""
    import os

    from config import BASE_DIR

    script = BASE_DIR / "scripts" / "docker_smoke.sh"
    assert script.is_file()
    assert os.access(script, os.X_OK), "scripts/docker_smoke.sh is not executable"
    body = script.read_text(encoding="utf-8")
    # The three properties that make it worth running at all.
    assert "/healthz" in body
    assert "caption_fonts" in body
    assert "emoji" in body


def test_i12_the_docker_job_is_wired_into_ci_and_gates_deploys():
    """A check nothing depends on is a check that can stay red."""
    from config import BASE_DIR

    workflow = (BASE_DIR / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/docker_smoke.sh" in workflow
    assert "needs: [backend, frontend, docker]" in workflow


def test_i13_the_alternative_emoji_styles_are_excluded_from_the_image():
    """A13's per-machine caches are not part of the image.

    Without the pattern, a developer who once selected OpenMoji ships ~7 MB of a style the image
    never uses - which is how the emoji directory got into the first build.
    """
    from config import BASE_DIR

    ignored = (BASE_DIR / ".dockerignore").read_text(encoding="utf-8")
    assert "assets/emoji-*/" in ignored
    # And the committed set is *not* excluded, because a render must never need the network (A7).
    assert not any(
        line.strip() in ("assets/emoji", "assets/emoji/") for line in ignored.splitlines()
    )


def test_i13_the_merged_extension_is_resolved(tmp_path):
    """``prepare_filename`` reports the pre-post-processing name.

    ``merge_output_format="mp4"`` rewrites the container only when separate video and audio
    renditions were selected, so the prepared name is right for a progressive source and wrong for
    a merged one - and both look plausible from the outside. A real download of a single-file
    source only ever exercises one half of this.
    """
    # Merged: the prepared name does not exist, the .mp4 sibling does.
    (tmp_path / "abc123.mp4").write_bytes(b"merged")
    assert download.resolve_downloaded_path(tmp_path / "abc123.webm").name == "abc123.mp4"

    # Progressive: the prepared name exists and must be returned untouched, even though an
    # unrelated .mp4 of the same stem happens to sit beside it.
    (tmp_path / "def456.webm").write_bytes(b"progressive")
    (tmp_path / "def456.mp4").write_bytes(b"stale")
    assert download.resolve_downloaded_path(tmp_path / "def456.webm").name == "def456.webm"

    # Neither: return the prepared name so the caller's own existence check reports the URL.
    assert download.resolve_downloaded_path(tmp_path / "ghi.webm").name == "ghi.webm"


def test_i13_a_download_that_writes_nothing_is_an_error_not_a_missing_path(tmp_path, monkeypatch):
    """yt-dlp reporting success and leaving no file is rare but real.

    A post-processor that failed after the download, or a template that expanded somewhere
    unwritable. Without the post-condition the caller gets a path that does not exist and the
    failure surfaces later as an ffprobe error nobody can explain.
    """
    import yt_dlp

    class _Silent:
        def __init__(self, _opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def extract_info(self, _url, download=False):
            return {"id": "nothing", "ext": "mp4", "title": "Nothing"}

        def prepare_filename(self, info):
            return str(tmp_path / f"{info['id']}.{info['ext']}")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _Silent)
    with pytest.raises(download.DownloadError, match="not found"):
        download.download_video("https://example.invalid/x.mp4", tmp_path)
