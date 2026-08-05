"""Common contracts shared by every publishing integration."""

from __future__ import annotations

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
    token_kind: str = (
        "static"  # noqa: S105 - names a *kind* of credential (static/refreshable/none), not one
    )

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
