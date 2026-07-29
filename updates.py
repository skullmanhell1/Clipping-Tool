"""Update checking.

Compares the running version (the ``VERSION`` file) against the latest GitHub
release and reports whether an update is available. Results are cached so the UI
can poll cheaply, and every failure mode degrades to "no update" rather than
raising.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Optional

from config import BASE_DIR, settings

_VERSION_FILE = BASE_DIR / "VERSION"
_CACHE_TTL = 3600  # seconds


def get_current_version() -> str:
    """Return the running version from the ``VERSION`` file (``0.0.0`` if absent)."""
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def parse_version(value: str) -> tuple[int, ...]:
    """Parse a semver-ish string into a comparable tuple.

    Tolerates a leading ``v`` and any pre-release/build suffix (``1.2.3-rc1``
    -> ``(1, 2, 3)``). Non-numeric input yields ``(0,)``.
    """
    if not value:
        return (0,)
    cleaned = value.strip().lstrip("vV")
    # maxsplit passed by keyword: Python 3.13 deprecates giving it positionally, and
    # since the suite treats warnings as errors that would fail every run after a
    # Python upgrade rather than at the point the code was written.
    core = re.split(r"[-+]", cleaned, maxsplit=1)[0]
    parts: list[int] = []
    for piece in core.split("."):
        m = re.match(r"\d+", piece)
        parts.append(int(m.group()) if m else 0)
    return tuple(parts) or (0,)


def is_newer(latest: str, current: str) -> bool:
    """Return whether ``latest`` is a strictly newer version than ``current``."""
    return parse_version(latest) > parse_version(current)


class UpdateChecker:
    """Caches the latest-release lookup and computes update availability."""

    def __init__(self, http_get=None) -> None:
        self._http_get = http_get  # injectable for tests
        self._lock = threading.Lock()
        self._cache: Optional[dict[str, Any]] = None
        self._fetched_at = 0.0

    def _fetch_latest(self) -> Optional[dict[str, Any]]:
        """Fetch the latest release from GitHub. Returns ``None`` on any failure."""
        url = f"https://api.github.com/repos/{settings.github_repo}/releases/latest"
        try:
            if self._http_get is not None:
                return self._http_get(url)
            import httpx

            resp = httpx.get(url, timeout=8, follow_redirects=True,
                             headers={"Accept": "application/vnd.github+json"})
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            return None
        return None

    def check(self, force: bool = False) -> dict[str, Any]:
        """Return ``{current, latest, update_available, html_url, checked_at, ...}``."""
        current = get_current_version()
        result = {
            "current": current,
            "latest": None,
            "update_available": False,
            "html_url": f"https://github.com/{settings.github_repo}/releases",
            "checked_at": None,
            "enabled": settings.update_check_enabled,
        }
        if not settings.update_check_enabled:
            return result

        with self._lock:
            fresh = (self._cache is not None
                     and (time.time() - self._fetched_at) < _CACHE_TTL)
            if force or not fresh:
                data = self._fetch_latest()
                self._cache = data
                self._fetched_at = time.time()
            data = self._cache

        if data:
            latest = str(data.get("tag_name") or data.get("name") or "").strip()
            result["latest"] = latest or None
            result["html_url"] = data.get("html_url") or result["html_url"]
            result["checked_at"] = self._fetched_at
            if latest:
                result["update_available"] = is_newer(latest, current)
        return result


_checker: Optional[UpdateChecker] = None
_lock = threading.Lock()


def get_update_checker() -> UpdateChecker:
    """Return the shared update-checker singleton."""
    global _checker
    with _lock:
        if _checker is None:
            _checker = UpdateChecker()
        return _checker
