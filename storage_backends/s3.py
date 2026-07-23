"""S3 storage backend.

Stores objects in an S3 (or S3-compatible) bucket configured via the ``S3_*``
settings. Drop-in replacement for :class:`LocalStorage`.

STUB ONLY.
"""

from __future__ import annotations

from typing import BinaryIO

from config import settings
from storage_backends.base import BaseStorage


class S3Storage(BaseStorage):
    """S3-backed :class:`BaseStorage` implementation.

    TODO(phase-storage): initialise a boto3 client from ``settings.s3_*`` and
    implement the object operations below.
    """

    def __init__(self) -> None:
        self.bucket = settings.s3_bucket

    def save(self, key: str, data: BinaryIO | bytes) -> str:  # noqa: D102
        raise NotImplementedError

    def open(self, key: str) -> BinaryIO:  # noqa: D102
        raise NotImplementedError

    def url(self, key: str) -> str:  # noqa: D102
        raise NotImplementedError

    def delete(self, key: str) -> None:  # noqa: D102
        raise NotImplementedError

    def exists(self, key: str) -> bool:  # noqa: D102
        raise NotImplementedError

    def list(self, prefix: str = "") -> list[str]:  # noqa: D102
        raise NotImplementedError
