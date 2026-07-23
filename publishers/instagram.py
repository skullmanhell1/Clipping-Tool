"""Instagram publisher.

Publishes clips as Reels via the Instagram Graph API using an access token from
configuration.

STUB ONLY.
"""

from __future__ import annotations

from config import settings
from publishers.base import BasePublisher, PublishRequest, PublishResult


class InstagramPublisher(BasePublisher):
    """Publish clips to Instagram. TODO(phase-publish): implement Reels upload."""

    name = "instagram"

    def is_configured(self) -> bool:  # noqa: D102
        return bool(settings.instagram_access_token)

    def publish(self, request: PublishRequest) -> PublishResult:  # noqa: D102
        raise NotImplementedError
