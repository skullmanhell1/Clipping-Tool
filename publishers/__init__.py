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

#: Platform names that mean one of :data:`PUBLISHER_TYPES` under a different spelling.
#:
#: ``youtube_shorts`` is the case that forces this to exist. It is a first-class key in
#: ``best_times.PLATFORM_WINDOWS`` -- deliberately, because Shorts genuinely peaks at a different
#: hour than YouTube proper -- and in ``output_profiles``, where it selects the vertical profile.
#: But no *publisher* is registered under it, and ``PublishManager.submit`` skipped any platform it
#: did not recognise with a bare ``continue``.
#:
#: So a stored ``youtube_shorts`` preference produced a scheduling suggestion, produced a vertical
#: render, and then published **nothing at all** -- no attempt row, no error, no marker. The user
#: pressed Publish and the platform silently did not happen. Resolving the alias here is what makes
#: the three layers agree, and it is the reason the alias could be kept "so a stored preference does
#: not break" without that actually being true.
PLATFORM_ALIASES = {
    "youtube_shorts": "youtube",
}


def resolve_platform(name: str) -> str:
    """The registered publisher name for ``name``, resolving known aliases.

    Unknown names are returned normalised but unchanged, so the caller still gets to decide what an
    unroutable platform means rather than having it silently become something else.
    """
    key = (name or "").strip().lower()
    return PLATFORM_ALIASES.get(key, key)


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
