"""Local filesystem storage backend.

Stores objects under ``settings.storage_root``. Keys are treated as relative
paths beneath the root.

STUB ONLY.
"""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from config import settings
from storage_backends.base import BaseStorage


class LocalStorage(BaseStorage):
    """Filesystem-backed :class:`BaseStorage` implementation."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or settings.storage_root)

    def _path(self, key: str) -> Path:
        """Resolve a storage ``key`` to an absolute path beneath the root."""
        return self.root / key

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
