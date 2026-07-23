"""Whop publisher.

Uploads/attaches finished clips to a Whop product/community using the Whop API
key from configuration.

STUB ONLY.
"""

from __future__ import annotations

from config import settings
from publishers.base import BasePublisher, PublishRequest, PublishResult


class WhopPublisher(BasePublisher):
    """Publish clips to Whop. TODO(phase-publish): implement the Whop API calls."""

    name = "whop"

    def is_configured(self) -> bool:  # noqa: D102
        return bool(settings.whop_api_key)

    def publish(self, request: PublishRequest) -> PublishResult:  # noqa: D102
        raise NotImplementedError
