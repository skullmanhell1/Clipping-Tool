"""Transient-failure classification and backoff for publish attempts (PB5).

A publish attempt had exactly one chance. Any failure - an expired token, a 500 from
TikTok, a DNS hiccup, a rate-limit response - wrote ``state=failed`` and stopped, and the
only way forward was a human pressing Retry in the dashboard. That makes a network blip
indistinguishable from a rejected video, and it means an overnight batch is decided by whichever
minute the upload happened to land in.

Two rules shape everything here.

**Retrying is only ever correct for failures that might succeed unchanged.** A 429 or a 503 will
probably pass in thirty seconds. A clip that is too long, a caption the platform rejected, a
revoked permission - those will fail identically forever, and retrying them burns quota and
delays the operator learning that something is actually wrong. So the default is *not* to retry:
an error has to be recognised as transient to earn one.

**Automatic retry must stay separate from human review.** ``review_required`` means a person has
to decide something; re-queueing it automatically would either loop forever or silently escalate
a review-mode submission into a live post. Only ``failed`` attempts are eligible, which is the
same invariant ``/approve`` and ``/retry`` already keep in the API.
"""

from __future__ import annotations

import random
import re
from typing import Optional

from config import settings

#: HTTP statuses worth retrying.
#:
#: 408 request timeout, 425 too early, 429 rate limited, and the 5xx family - all of which mean
#: "not now" rather than "not ever". Notably absent: 400, 401, 403, 404, 413, 422. A 401 is
#: interesting because it *can* be transient (an expired token), but the fix is a refresh rather
#: than a wait, so it is handled by the token path (PB4) and only retried once a refresh has
#: actually happened - retrying a 401 on a revoked credential would spin until the cap.
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Substrings that mark a transient failure in an error string.
#:
#: Matched against the *text* because that is what survives into the attempt record: publishers
#: catch broad exceptions and store ``str(exc)``, so by the time the manager sees a failure the
#: original exception type is gone. Fixing that properly means changing all five publishers'
#: error handling; recognising the text is what can be done without touching them, and the
#: status-code path above is the precise one used wherever a code is available.
_TRANSIENT_PATTERNS: tuple[str, ...] = (
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "connection aborted",
    "connection error",
    "temporarily unavailable",
    "temporary failure",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "too many requests",
    "rate limit",
    "rate-limit",
    "try again",
    "server disconnected",
    "remote end closed",
    "broken pipe",
    "name or service not known",
    "nodename nor servname",
    "dns",
    "ssl",
    "eof occurred",
)

#: Substrings that mark a *permanent* failure even if a transient pattern also matches.
#:
#: Checked first, and it has to be: platform error bodies are chatty, and one that says
#: "video too long, please try again with a shorter clip" contains "try again" while being the
#: least retryable error there is. Without this precedence a rejected clip would be uploaded
#: five times before failing.
_PERMANENT_PATTERNS: tuple[str, ...] = (
    "too long",
    "too large",
    "file size",
    "unsupported",
    "invalid_grant",
    "invalid grant",
    "revoked",
    "not authorized",
    "unauthorized_client",
    "permission",
    "forbidden",
    "no longer exists",
    "rejected before upload",
    "duplicate",
    "already published",
    "quota exceeded",
)

#: Pulls an HTTP status out of an httpx error string, e.g.
#: "Server error '503 Service Unavailable' for url ...".
_STATUS_RE = re.compile(r"\b([1-5]\d{2})\b")


def classify(error: str, status_code: Optional[int] = None) -> bool:
    """Whether ``error`` looks worth retrying unchanged.

    ``status_code`` wins when supplied, since it is unambiguous. Otherwise the text is checked
    for permanent markers first (see :data:`_PERMANENT_PATTERNS` for why the order matters), then
    for transient ones, then for an embedded HTTP status.

    An empty or unrecognised error is treated as **permanent**. Retrying something we cannot
    identify is how a broken configuration turns into a retry loop that hides it.
    """
    if status_code is not None:
        return int(status_code) in RETRYABLE_STATUS_CODES

    text = (error or "").strip().lower()
    if not text:
        return False
    if any(pattern in text for pattern in _PERMANENT_PATTERNS):
        return False
    if any(pattern in text for pattern in _TRANSIENT_PATTERNS):
        return True

    found = _STATUS_RE.search(text)
    if found:
        return int(found.group(1)) in RETRYABLE_STATUS_CODES
    return False


def max_attempts() -> int:
    """How many *total* tries one attempt record may take, including the first."""
    return max(1, int(getattr(settings, "publish_max_retries", 3)) + 1)


def backoff_seconds(retry_count: int, *, jitter: Optional[float] = None) -> float:
    """Seconds to wait before retry number ``retry_count`` (1-based).

    Exponential from a configurable base, capped, with jitter added rather than subtracted so a
    delay is never shorter than the backoff it is derived from.

    The jitter is the point of this function, not a decoration. Every attempt in a batch that
    fails against the same platform outage becomes due at the same instant, and without jitter
    they retry in lockstep forever - the retry storm that caused the outage's second spike. It is
    proportional (up to 25% of the delay), so it stays meaningful as the delay grows.

    ``jitter`` may be supplied as a 0..1 fraction to make the result deterministic in tests.
    """
    base = max(1.0, float(getattr(settings, "publish_retry_base_seconds", 30.0)))
    ceiling = max(base, float(getattr(settings, "publish_retry_max_seconds", 3600.0)))
    step = max(1, int(retry_count))

    delay = base * (2 ** (step - 1))
    delay = min(delay, ceiling)
    fraction = random.random() if jitter is None else max(0.0, min(1.0, float(jitter)))  # noqa: S311 - retry jitter; spreading retries needs no cryptographic randomness
    return delay + delay * 0.25 * fraction


def should_retry(retry_count: int, error: str, status_code: Optional[int] = None) -> bool:
    """Whether an attempt that has already retried ``retry_count`` times should go again."""
    if retry_count + 1 >= max_attempts():
        return False
    return classify(error, status_code)


def exhausted_message(retry_count: int, error: str) -> str:
    """The error text recorded when retries run out.

    Keeps the original error *and* says how many tries it took, because "failed after 4 attempts
    over 2 hours" and "failed immediately" call for completely different responses from whoever
    reads the dashboard, and the bare platform error cannot distinguish them.
    """
    return f"{error} (gave up after {retry_count + 1} attempts)"
