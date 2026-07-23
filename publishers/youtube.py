"""YouTube publisher.

Uploads clips as YouTube Shorts/videos via the YouTube Data API using OAuth 2.0
credentials (client id/secret + refresh token) from configuration.

STUB ONLY.
"""

from __future__ import annotations

from config import settings
from publishers.base import BasePublisher, PublishRequest, PublishResult


class YouTubePublisher(BasePublisher):
    """Publish clips to YouTube. TODO(phase-publish): implement OAuth + upload."""

    name = "youtube"

    def is_configured(self) -> bool:  # noqa: D102
        return all(
            [
                settings.youtube_client_id,
                settings.youtube_client_secret,
                settings.youtube_refresh_token,
            ]
        )

    def publish(self, request: PublishRequest) -> PublishResult:  # noqa: D102
        raise NotImplementedError
