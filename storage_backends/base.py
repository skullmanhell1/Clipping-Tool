"""Storage backend interface.

Abstracts where source videos, intermediate artefacts, and finished clips live
so the pipeline works identically against the local filesystem or S3. Select the
active backend via ``settings.storage_backend`` and :func:`get_storage`.

STUB ONLY.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO


class BaseStorage(ABC):
    """Abstract object storage interface."""

    @abstractmethod
    def save(self, key: str, data: BinaryIO | bytes) -> str:
        """Persist ``data`` under ``key`` and return a locator (path or URL)."""
        raise NotImplementedError

    @abstractmethod
    def open(self, key: str) -> BinaryIO:
        """Open the object stored at ``key`` for reading."""
        raise NotImplementedError

    @abstractmethod
    def url(self, key: str) -> str:
        """Return a retrievable URL/path for ``key``."""
        raise NotImplementedError

    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete the object stored at ``key``."""
        raise NotImplementedError

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Return whether an object exists at ``key``."""
        raise NotImplementedError

    @abstractmethod
    def list(self, prefix: str = "") -> list[str]:
        """List keys, optionally filtered by ``prefix``."""
        raise NotImplementedError
