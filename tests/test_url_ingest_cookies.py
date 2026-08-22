"""URL ingest can authenticate, and says so when it cannot.

YouTube answers a growing share of requests with "Sign in to confirm you're not a bot". yt-dlp's
error names the fix -- pass cookies -- and there was no way to pass any: no `cookiefile`, no
`cookiesfrombrowser`, no setting. So the job failed with a wall of yt-dlp prose pointing at two wiki
pages and a pair of CLI flags this application does not expose, describing a fix the reader could
not apply.

The gate keys on the requesting IP rather than the video, which is why this is not an edge case: a
datacentre address is gated near-universally. **In a container `--cookies-from-browser` cannot work
at all**, there being no browser, so the file is the option that matters and the browser one is the
desktop convenience.

None of these tests reach the network. They assert the options handed to yt-dlp and the message
handed to the user, which is where both defects lived.
"""

from __future__ import annotations

import pytest

from worker import download as dl


@pytest.fixture(autouse=True)
def _no_ambient_cookies(monkeypatch):
    """Neither setting configured, unless a test says otherwise.

    A developer's own `.env` supplying one of these would make the "anonymous" assertions pass or
    fail for reasons unrelated to the test.
    """
    from config import settings

    monkeypatch.setattr(settings, "ytdlp_cookies_file", None, raising=False)
    monkeypatch.setattr(settings, "ytdlp_cookies_from_browser", None, raising=False)


def test_no_cookies_configured_adds_nothing():
    """Anonymous ingest must be untouched, or this feature changes every existing deployment."""
    assert dl.auth_opts() == {}


def test_a_cookie_file_is_passed_to_yt_dlp(tmp_path, monkeypatch):
    from config import settings

    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(settings, "ytdlp_cookies_file", jar, raising=False)

    assert dl.auth_opts() == {"cookiefile": str(jar)}


def test_a_missing_cookie_file_names_the_setting_and_the_path(tmp_path, monkeypatch):
    """yt-dlp's own error for this names neither, so a typo reads as an unrelated failure."""
    from config import settings

    monkeypatch.setattr(settings, "ytdlp_cookies_file", tmp_path / "absent.txt", raising=False)

    with pytest.raises(dl.DownloadError) as excinfo:
        dl.auth_opts()

    message = str(excinfo.value)
    assert "YTDLP_COOKIES_FILE" in message
    assert "absent.txt" in message


def test_a_user_path_is_expanded(tmp_path, monkeypatch):
    """`~/cookies.txt` is what a person types; yt-dlp does not expand it."""
    from config import settings

    home = tmp_path / "home"
    home.mkdir()
    jar = home / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(settings, "ytdlp_cookies_file", "~/cookies.txt", raising=False)

    assert dl.auth_opts()["cookiefile"] == str(jar)


def test_a_browser_becomes_the_tuple_yt_dlp_expects(monkeypatch):
    """yt-dlp wants (browser, profile, keyring, container), not a string."""
    from config import settings

    monkeypatch.setattr(settings, "ytdlp_cookies_from_browser", "Chrome", raising=False)

    assert dl.auth_opts() == {"cookiesfrombrowser": ("chrome", None, None, None)}


def test_a_browser_profile_is_split_off(monkeypatch):
    """A profile name can contain a space, so the split is on ':' and nothing else."""
    from config import settings

    monkeypatch.setattr(settings, "ytdlp_cookies_from_browser", "firefox:Profile 1", raising=False)

    assert dl.auth_opts() == {"cookiesfrombrowser": ("firefox", "Profile 1", None, None)}


def test_both_can_be_set_together(tmp_path, monkeypatch):
    """Not mutually exclusive: yt-dlp merges both jars, and refusing one would be a new rule."""
    from config import settings

    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(settings, "ytdlp_cookies_file", jar, raising=False)
    monkeypatch.setattr(settings, "ytdlp_cookies_from_browser", "edge", raising=False)

    opts = dl.auth_opts()
    assert opts["cookiefile"] == str(jar)
    assert opts["cookiesfrombrowser"] == ("edge", None, None, None)


# --- the message the user actually sees --------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        # The real text, including the Unicode right single quote yt-dlp emits.
        "ERROR: [youtube] abc: Sign in to confirm you\u2019re not a bot. Use --cookies-from-browser",
        # An ASCII apostrophe, in case a release changes it.
        "Sign in to confirm you're not a bot",
        # Reworded, since yt-dlp has changed this wording before.
        "Please sign in to confirm you are not a bot and try again",
    ],
)
def test_the_bot_check_is_recognised_however_it_is_worded(message):
    assert dl._is_bot_check(message)


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: [youtube] abc: Video unavailable",
        "HTTP Error 404: Not Found",
        "Requested format is not available",
        "Sign in to confirm your age",  # age gate: a different problem, a different fix
    ],
)
def test_other_failures_are_not_mistaken_for_the_bot_check(message):
    """Mislabelling a real failure would send the reader after cookies they do not need."""
    assert not dl._is_bot_check(message)


def test_the_hint_names_the_settings_rather_than_yt_dlp_flags():
    """The old behaviour relayed `--cookies-from-browser`, which this app does not expose."""
    assert "YTDLP_COOKIES_FILE" in dl.COOKIES_HINT
    assert "YTDLP_COOKIES_FROM_BROWSER" in dl.COOKIES_HINT
    assert "--cookies" not in dl.COOKIES_HINT


def test_a_gated_download_reports_the_hint_not_the_raw_error(tmp_path, monkeypatch):
    """End of the chain: the job's error field is what the UI shows."""

    class _Gated:
        def __init__(self, *_a, **_kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def extract_info(self, *_a, **_kw):
            raise RuntimeError("Sign in to confirm you\u2019re not a bot")

    fake = type("_M", (), {"YoutubeDL": _Gated})
    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", fake)
    monkeypatch.setattr(dl, "validate_public_url", lambda url, **_kw: url)

    with pytest.raises(dl.DownloadError) as excinfo:
        dl.download_video("https://www.youtube.com/watch?v=abc", tmp_path)

    message = str(excinfo.value)
    assert "YTDLP_COOKIES_FILE" in message
    assert "wiki" not in message.lower(), "yt-dlp's wiki links were relayed to the user again"


def test_a_gated_metadata_probe_reports_the_hint_too(monkeypatch):
    """The UI calls the probe first, so authenticating only the download looks broken."""

    class _Gated:
        def __init__(self, *_a, **_kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def extract_info(self, *_a, **_kw):
            raise RuntimeError("Sign in to confirm you\u2019re not a bot")

    fake = type("_M", (), {"YoutubeDL": _Gated})
    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", fake)
    monkeypatch.setattr(dl, "validate_public_url", lambda url, **_kw: url)

    with pytest.raises(dl.DownloadError) as excinfo:
        dl.fetch_metadata("https://www.youtube.com/watch?v=abc")

    assert "YTDLP_COOKIES_FILE" in str(excinfo.value)


def test_both_entry_points_send_the_cookies(tmp_path, monkeypatch):
    """A regression guard for the specific asymmetry that would be easy to reintroduce."""
    from config import settings

    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(settings, "ytdlp_cookies_file", jar, raising=False)

    seen: list[dict] = []

    class _Recorder:
        def __init__(self, opts):
            seen.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def extract_info(self, *_a, **_kw):
            raise RuntimeError("Video unavailable")

    fake = type("_M", (), {"YoutubeDL": _Recorder})
    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", fake)
    monkeypatch.setattr(dl, "validate_public_url", lambda url, **_kw: url)

    for call in (
        lambda: dl.fetch_metadata("https://example.com/v"),
        lambda: dl.download_video("https://example.com/v", tmp_path),
    ):
        with pytest.raises(dl.DownloadError):
            call()

    assert len(seen) == 2, "one of the two entry points did not construct a YoutubeDL"
    for opts in seen:
        assert opts.get("cookiefile") == str(jar)
