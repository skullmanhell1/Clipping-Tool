"""Tests for the CORS origin/credentials pairing.

The app hard-coded ``allow_credentials=True`` while ``cors_origins`` defaulted to ``*``.
That combination is forbidden by the CORS specification and browsers reject the
response, so the default configuration disabled every credentialed cross-origin request
while looking like it permitted them.

Note these assert on :class:`config.Settings` rather than on live response headers.
The middleware is constructed once at import time from the settings values, so a
per-test monkeypatch cannot reach it — the decision itself is what is worth pinning, and
it now lives in a property with a single caller.
"""

from __future__ import annotations

import pytest

from config import Settings


def _settings(origins: str, **extra) -> Settings:
    """A Settings instance with an explicit origin string.

    ``_env_file=None`` keeps a developer's local ``.env`` from deciding the outcome.
    """
    return Settings(cors_origins=origins, _env_file=None, **extra)


@pytest.mark.parametrize("origins", ["*", "* ", "https://a.example.com,*"])
def test_a_wildcard_origin_disables_credentials(origins):
    """Wildcard and credentials are mutually exclusive, including when mixed in.

    The mixed case matters: a list that merely *contains* ``*`` still makes Starlette
    emit the wildcard, so the presence of a specific origin alongside it is not enough
    to make credentials safe or functional.
    """
    settings = _settings(origins)
    assert settings.cors_allow_wildcard is True
    assert settings.cors_allow_credentials is False


@pytest.mark.parametrize(
    "origins",
    [
        "https://app.example.com",
        "https://app.example.com,https://admin.example.com",
        " https://app.example.com , https://admin.example.com ",
    ],
)
def test_explicit_origins_enable_credentials(origins):
    """An explicit allow-list is the configuration in which credentials work."""
    settings = _settings(origins)
    assert settings.cors_allow_wildcard is False
    assert settings.cors_allow_credentials is True
    assert all(origin.startswith("https://") for origin in settings.cors_origins_list)


def test_origins_are_split_and_stripped():
    """Whitespace around a comma-separated entry does not create a bogus origin."""
    settings = _settings(" https://a.example.com ,https://b.example.com , ")
    assert settings.cors_origins_list == [
        "https://a.example.com",
        "https://b.example.com",
    ]


def test_the_shipped_default_is_the_wildcard_without_credentials():
    """Pins the out-of-the-box pairing, which is the case that was broken.

    A wildcard default is reasonable for local development; silently combining it with
    credentials was not.
    """
    settings = Settings(_env_file=None)
    assert settings.cors_origins_list == ["*"]
    assert settings.cors_allow_credentials is False


def test_the_app_passes_the_derived_value_to_the_middleware():
    """The middleware is wired to the property, not to a literal ``True``.

    Without this the property could be correct while the app kept ignoring it.
    """
    from starlette.middleware.cors import CORSMiddleware

    import api.main as api_main
    from config import settings as app_settings

    cors = [m for m in api_main.app.user_middleware if m.cls is CORSMiddleware]
    assert len(cors) == 1, "expected exactly one CORS middleware"
    assert cors[0].kwargs["allow_credentials"] == app_settings.cors_allow_credentials
