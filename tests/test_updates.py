"""Tests for the update-check logic."""
from __future__ import annotations

from updates import UpdateChecker, is_newer, parse_version


def test_parse_version_tolerant():
    assert parse_version("v1.2.3") == (1, 2, 3)
    assert parse_version("0.6.0-rc1") == (0, 6, 0)
    assert parse_version("2.0") == (2, 0)
    assert parse_version("garbage") == (0,)


def test_is_newer():
    assert is_newer("0.6.0", "0.5.0") is True
    assert is_newer("v0.6.1", "0.6.0") is True
    assert is_newer("0.5.0", "0.5.0") is False
    assert is_newer("0.4.0", "0.5.0") is False


def test_checker_reports_update(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "update_check_enabled", True)
    checker = UpdateChecker(http_get=lambda url: {"tag_name": "v99.0.0",
                                                  "html_url": "http://x/rel"})
    result = checker.check(force=True)
    assert result["latest"] == "v99.0.0"
    assert result["update_available"] is True
    assert result["html_url"] == "http://x/rel"


def test_checker_no_update_when_same(monkeypatch):
    from config import settings
    from updates import get_current_version
    monkeypatch.setattr(settings, "update_check_enabled", True)
    current = get_current_version()
    checker = UpdateChecker(http_get=lambda url: {"tag_name": current})
    assert checker.check(force=True)["update_available"] is False


def test_checker_disabled(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "update_check_enabled", False)
    checker = UpdateChecker(http_get=lambda url: {"tag_name": "v99.0.0"})
    result = checker.check(force=True)
    assert result["update_available"] is False
    assert result["latest"] is None


def test_checker_handles_fetch_failure(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "update_check_enabled", True)

    def boom(url):
        raise RuntimeError("network down")

    checker = UpdateChecker(http_get=boom)
    result = checker.check(force=True)  # must not raise
    assert result["update_available"] is False
