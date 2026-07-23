"""X (Twitter) publisher.

Posts clips as native video via the X API using the API key/secret from
configuration.

STUB ONLY.
"""

from __future__ import annotations

from config import settings
from publishers.base import BasePublisher, PublishRequest, PublishResult


class XPublisher(BasePublisher):
    """Publish clips to X. TODO(phase-publish): implement media upload + post."""

    name = "x"

    def is_configured(self) -> bool:  # noqa: D102
        return bool(settings.x_api_key and settings.x_api_secret)

    def publish(self, request: PublishRequest) -> PublishResult:  # noqa: D102
        raise NotImplementedError
