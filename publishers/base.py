"""Publisher interface.

Every platform publisher implements :class:`BasePublisher` so the pipeline can
publish a finished clip to any destination through one uniform API.

STUB ONLY.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PublishRequest:
    """Everything a publisher needs to upload a single clip."""

    video_path: Path
    title: str = ""
    description: str = ""
    hashtags: list[str] = field(default_factory=list)


@dataclass
class PublishResult:
    """Outcome of a publish attempt."""

    success: bool
    url: str = ""
    error: str = ""


class BasePublisher(ABC):
    """Abstract base class for all platform publishers."""

    #: Human-readable platform name, overridden by subclasses.
    name: str = "base"

    @abstractmethod
    def is_configured(self) -> bool:
        """Return ``True`` when the required credentials are present."""
        raise NotImplementedError

    @abstractmethod
    def publish(self, request: PublishRequest) -> PublishResult:
        """Publish a clip described by ``request`` and return the result."""
        raise NotImplementedError
