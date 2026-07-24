"""Common contracts shared by every publishing integration."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional


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
    scheduled_at: Optional[datetime] = None
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

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)
