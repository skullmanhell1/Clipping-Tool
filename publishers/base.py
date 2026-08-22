"""Common contracts shared by every publishing integration."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any


class PublishState(str, Enum):
    QUEUED = "queued"
    SCHEDULED = "scheduled"
    UPLOADING = "uploading"
    PUBLISHED = "published"
    DRAFT = "draft"
    PRIVATE = "private"
    REVIEW_REQUIRED = "review_required"
    FAILED = "failed"
    #: The process died between the platform call and the record of it, so what happened is
    #: genuinely unknown (S4).
    #:
    #: Distinct from FAILED, and the distinction is the whole point: a FAILED attempt is safe to
    #: retry, and this one is **not** — the post may well exist. A stale ``uploading`` row used to
    #: be unreachable by every path (``due_attempts`` selects only queued/scheduled, and
    #: ``/retry``, ``/approve``, ``/reschedule`` and ``/cancel`` all refused it), so the attempt
    #: was silently lost *and* the audit trail was wrong. Naming the uncertainty lets a person
    #: check the platform and decide, which is the only correct resolution available.
    UNKNOWN = "unknown"


#: Query/body parameter names whose values must never reach a log, a database or an API response.
_SECRET_PARAM_NAMES = (
    "access_token",
    "refresh_token",
    "client_secret",
    "api_key",
    "apikey",
    "token",
    "key",
    "password",
    "secret",
)

#: `name=value` in a query string or form body, for the parameters above.
_SECRET_IN_URL = re.compile(
    r"(?i)\b(" + "|".join(_SECRET_PARAM_NAMES) + r")=([^&\s\"'<>]+)",
)

#: A bearer/OAuth credential in a header echoed into an error message.
_SECRET_IN_HEADER = re.compile(r"(?i)\b(bearer|oauth|basic)\s+([A-Za-z0-9._~+/=-]{8,})")


def status_of(exc: BaseException) -> int | None:
    """The HTTP status behind ``exc``, or ``None`` when it was not an HTTP failure.

    Every publisher catches bare ``Exception`` and records ``str(exc)``, which is why
    ``retry.classify``'s precise status-code path was never reached in production — the code was
    right there on the exception object and nobody read it. Kept here so all five publishers use
    one implementation, and written defensively because it runs inside an ``except``.
    """
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


def redact(text: str) -> str:
    """Remove credentials from text before it is stored or returned.

    Every publisher's ``except`` block records ``str(exc)``, and an ``httpx.HTTPStatusError``
    message **embeds the full request URL**. Instagram passed its access token as a query
    parameter, so a single 4xx on the container-status poll produced

        Client error '400 Bad Request' for url 'https://graph.facebook.com/v25.0/17…?
        fields=status_code&access_token=EAAG…'

    which `manager._execute` then wrote into both the ``error`` column and ``result_json``, and
    which ``GET /api/history`` serves. A long-lived credential that only ever needed to be in a
    header ended up persisted in ``history.db``, in every backup of it, and rendered in the
    dashboard.

    Applied at the ``PublishResult`` boundary rather than at each call site, because there are five
    publishers and the sixth would forget. It is a net, not a substitute for keeping secrets out of
    URLs — Instagram's poll now uses a header too.
    """
    if not text:
        return text
    cleaned = _SECRET_IN_URL.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    return _SECRET_IN_HEADER.sub(lambda m: f"{m.group(1)} [REDACTED]", cleaned)


@dataclass
class PublisherStatus:
    platform: str
    configured: bool
    available: bool
    direct_publish: bool
    state: str
    message: str
    account_id: str = ""
    requires_approval: bool = False
    #: PB4: when the current access token expires, as a unix timestamp, or ``None``.
    #:
    #: ``None`` covers two different situations and the difference is in ``token_kind``: a
    #: publisher that mints tokens on demand has nothing to expire, whereas one using a static
    #: long-lived token has an expiry nobody here can see. Reporting both as "no expiry" would
    #: tell an operator their Instagram token is fine right up until the day it is not.
    token_expires_at: float | None = None
    #: ``refreshable`` (exchanged from a refresh token), ``static`` (a long-lived token the
    #: operator pasted in), or ``none`` (no token-based auth).
    token_kind: str = "static"  # noqa: S105 - names a *kind* of credential (static/refreshable/none), not one

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PublishRequest:
    """Platform-neutral upload request."""

    video_path: Path
    title: str = ""
    description: str = ""
    hashtags: list[str] = field(default_factory=list)
    hook_text: str = ""
    cta: str = ""
    mentions: list[str] = field(default_factory=list)
    account_id: str = ""
    campaign_id: str = ""
    target_type: str = ""
    target_id: str = ""
    mode: str = "auto"  # auto | review
    scheduled_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def caption(self) -> str:
        parts = [self.description.strip()]
        if self.cta.strip():
            parts.append(self.cta.strip())
        tags = " ".join([*self.mentions, *self.hashtags]).strip()
        if tags:
            parts.append(tags)
        return "\n\n".join(p for p in parts if p)


@dataclass
class PublishResult:
    success: bool
    state: PublishState
    platform: str
    url: str = ""
    external_id: str = ""
    error: str = ""
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    #: The platform's HTTP status, when the failure was an HTTP one (PB5).
    #:
    #: ``retry.classify`` has always accepted a status code and decided purely from it when given
    #: one — but no publisher populated one and ``manager._schedule_retry`` called
    #: ``should_retry(count, error)`` with two arguments, so **every production retry decision was
    #: made by substring-matching an exception message**, which is the fallback that module's own
    #: docstring apologises for. The precise path was dead code, and its unit tests passed because
    #: they call ``classify`` directly.
    status_code: int | None = None
    #: Whether the platform may already have accepted the post despite this attempt failing (S2).
    #:
    #: Set when a failure occurs *after* an irreversible step in a multi-request flow — X's
    #: FINALIZE, Instagram's ``media_publish``, YouTube's PUT of the file bytes. Retrying such an
    #: attempt re-runs the flow from step one, and a timeout on the last call of a successful
    #: upload therefore produces a **second post**. ``_schedule_retry`` refuses to auto-retry these
    #: and routes them to a human instead, because a duplicate post cannot be undone by this tool
    #: and a missed post can be re-published by one click.
    side_effect_possible: bool = False

    def __post_init__(self) -> None:
        # Redacted here so no publisher can leak a credential into the record by forgetting to.
        # `error` is stored in a column and served by /api/history; `raw` is stored as JSON.
        self.error = redact(self.error)
        self.message = redact(self.message)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        return data


class PublisherError(RuntimeError):
    pass


class BasePublisher(ABC):
    name: str = "base"
    min_interval_seconds: float = 1.0

    @abstractmethod
    def status(self, account_id: str = "") -> PublisherStatus:
        raise NotImplementedError

    def is_configured(self) -> bool:
        return self.status().configured

    @abstractmethod
    def publish(self, request: PublishRequest) -> PublishResult:
        raise NotImplementedError

    # ----------------------------------------------------------------- PB4 --
    def refresh_credentials(self, account_id: str = "") -> bool:
        """Renew this publisher's access token. Return whether anything was renewed.

        The default is ``False`` and that is the honest answer for four of the five publishers:
        TikTok, Instagram and X authenticate with a long-lived token the operator pasted into
        config, and Whop with an API key. None of those can be renewed from here - when they
        expire, a human has to obtain a new one. Returning ``False`` says exactly that, and lets
        the scheduler skip a refresh it knows cannot help rather than retrying a 401 into the cap.

        Only YouTube overrides this, because only YouTube was given a refresh token.
        """
        return False

    def invalidate_credentials(self, account_id: str = "") -> None:
        """Discard any cached token so the next publish obtains a new one."""
        return None

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)
