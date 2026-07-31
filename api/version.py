"""The application's version, read from the ``VERSION`` file.

Its own module because three unrelated places need it and none of them can import the others:
``api.main`` passes it to ``FastAPI(version=...)``, ``api.routers.system`` reports it from
``/api/info``, and ``api.main.fallback_index_html`` shows it on the no-frontend-built page.
``system`` previously read it back off the constructed ``app`` object, which is not available to a
router without importing ``api.main`` — a cycle.
"""

from __future__ import annotations

from pathlib import Path


def _read_version() -> str:
    """Read the semantic version from the VERSION file (fallback to a default).

    Never raises. This is called at import time, so a container with an unreadable filesystem
    reports ``0.0.0`` rather than failing to boot — the version is diagnostic information, and
    refusing to start over it would trade a cosmetic problem for an outage.
    """
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text(
            encoding="utf-8"
        ).strip() or "0.0.0"
    except OSError:
        return "0.0.0"


APP_VERSION = _read_version()
