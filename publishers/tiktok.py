"""TikTok publisher.

Uploads clips via the TikTok Content Posting API using an access token from
configuration.

STUB ONLY.
"""

from __future__ import annotations

from config import settings
from publishers.base import BasePublisher, PublishRequest, PublishResult


class TikTokPublisher(BasePublisher):
    """Publish clips to TikTok. TODO(phase-publish): implement the upload flow."""

    name = "tiktok"

    def is_configured(self) -> bool:  # noqa: D102
        return bool(settings.tiktok_access_token)

    def publish(self, request: PublishRequest) -> PublishResult:  # noqa: D102
        raise NotImplementedError
