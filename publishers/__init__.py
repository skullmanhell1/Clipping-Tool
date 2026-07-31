"""Publisher registry and platform capability discovery."""

from publishers.base import (
    BasePublisher,
    PublisherStatus,
    PublishRequest,
    PublishResult,
    PublishState,
)
from publishers.instagram import InstagramPublisher
from publishers.tiktok import TikTokPublisher
from publishers.whop import WhopPublisher
from publishers.x import XPublisher
from publishers.youtube import YouTubePublisher

PUBLISHER_TYPES = {
    "whop": WhopPublisher,
    "youtube": YouTubePublisher,
    "tiktok": TikTokPublisher,
    "instagram": InstagramPublisher,
    "x": XPublisher,
}


def build_publishers():
    return {name: cls() for name, cls in PUBLISHER_TYPES.items()}


__all__ = [
    "BasePublisher",
    "PublishRequest",
    "PublishResult",
    "PublishState",
    "PublisherStatus",
    "build_publishers",
]
