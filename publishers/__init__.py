"""Publishers package.

Uniform integrations for auto-publishing finished clips to external platforms
(Whop, YouTube, TikTok, Instagram, X). All implementations are stubs and share
the :class:`publishers.base.BasePublisher` interface.
"""

from publishers.base import BasePublisher, PublishRequest, PublishResult

__all__ = ["BasePublisher", "PublishRequest", "PublishResult"]
