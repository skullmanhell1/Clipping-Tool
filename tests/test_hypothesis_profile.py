"""The Hypothesis profile disables per-example deadlines suite-wide.

Hypothesis defaults to a 200 ms per-example deadline. 43 of this suite's property tests
carried a bare ``@settings(max_examples=100)`` and therefore inherited it — including the
ones that touch the filesystem or shell out to ffmpeg. On a loaded CI runner that produced
``DeadlineExceeded`` failures which were **intermittent**: the same commit could pass on
one run and fail on another, with nothing in the diff to explain the difference.

A deadline is a latency assertion, not a correctness one. This pins that the profile stays
in force, because the failure it prevents is invisible locally and expensive to re-diagnose
from a red build.
"""

from __future__ import annotations

from hypothesis import HealthCheck, settings


def test_the_active_profile_has_no_deadline():
    """The suite-wide default is ``deadline=None``."""
    assert settings().deadline is None


def test_a_bare_settings_decorator_inherits_no_deadline():
    """``@settings(max_examples=100)`` must not silently reintroduce the 200 ms limit.

    This is the case that mattered: 37 tests use exactly this form. ``settings(...)``
    fills unspecified fields from the active profile, which is what makes fixing this on
    the profile sufficient rather than having to edit every decorator.
    """
    assert settings(max_examples=100).deadline is None


def test_a_settings_decorator_with_health_checks_inherits_no_deadline():
    """The other unprotected form — six tests suppress a health check only."""
    derived = settings(
        max_examples=100, suppress_health_check=[HealthCheck.filter_too_much]
    )
    assert derived.deadline is None


def test_an_explicit_deadline_is_still_respected():
    """The profile sets a default, it does not forbid an explicit deadline.

    A test that genuinely wants to assert latency can still opt in, so this change
    removes noise without removing the capability.
    """
    from datetime import timedelta

    assert settings(deadline=timedelta(milliseconds=500)).deadline == timedelta(
        milliseconds=500
    )
