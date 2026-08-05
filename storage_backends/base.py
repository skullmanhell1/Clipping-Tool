"""Storage backend interface.

Abstracts where source videos, intermediate artefacts, and finished clips live
so the pipeline works **identically** against the local filesystem or S3. Select
the active backend via ``settings.storage_backend`` and :func:`get_storage`.

A storage *key* is a POSIX-style relative path (e.g. ``"clips/<job>/<file>"``).
The same key works against either backend; only :meth:`url` differs (a local
path served by the app vs. an S3/CDN URL).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO

#: A runtime alias, so it uses `|` rather than `Union` but is still evaluated at import.
Data = bytes | BinaryIO


class BaseStorage(ABC):
    """Abstract object storage interface."""

    #: Short identifier for the backend (``"local"`` / ``"s3"``).
    name: str = "base"

    @abstractmethod
    def save(self, key: str, data: Data) -> str:
        """Persist ``data`` (bytes or a readable binary stream) under ``key``.

        Returns a locator (a local path or an object URL).
        """

    def save_file(self, key: str, path: str | Path) -> str:
        """Persist an on-disk file at ``path`` under ``key``.

        Default implementation streams the file through :meth:`save`; backends
        may override for efficiency.
        """
        with open(path, "rb") as fh:
            return self.save(key, fh)

    @abstractmethod
    def open(self, key: str) -> BinaryIO:
        """Open the object stored at ``key`` for binary reading."""

    @abstractmethod
    def url(self, key: str) -> str:
        """Return a retrievable URL/path for ``key``."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete the object at ``key`` (a no-op if it does not exist)."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Return whether an object exists at ``key``."""

    @abstractmethod
    def list(self, prefix: str = "") -> list[str]:
        """List keys, optionally filtered by ``prefix`` (sorted)."""

    @abstractmethod
    def size(self, key: str) -> int:
        """Return the object size in bytes (0 if missing)."""


def normalize_key(key: str) -> str:
    """Normalise a storage key to a safe, POSIX-style relative path.

    Strips leading slashes and collapses ``.``/``..`` segments so a key can
    never escape the storage root.
    """
    parts: list[str] = []
    for segment in str(key).replace("\\", "/").split("/"):
        if segment in ("", ".", ".."):
            continue
        parts.append(segment)
    return "/".join(parts)
