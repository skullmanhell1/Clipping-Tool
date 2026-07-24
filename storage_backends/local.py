"""Local filesystem storage backend.

Stores objects under ``settings.storage_root``; a storage key maps directly to a
relative path beneath that root. ``url`` returns an app-served path (the API
mounts ``clips_dir`` at ``/clips``), so a ``clips/<job>/<file>`` key resolves to
``/clips/<job>/<file>`` in the browser.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import BinaryIO

from config import settings
from storage_backends.base import BaseStorage, Data, normalize_key


class LocalStorage(BaseStorage):
    """Filesystem-backed :class:`BaseStorage` implementation."""

    name = "local"

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or settings.storage_root)

    def _path(self, key: str) -> Path:
        """Resolve a storage ``key`` to an absolute path beneath the root."""
        return self.root / normalize_key(key)

    def save(self, key: str, data: Data) -> str:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, (bytes, bytearray)):
            dest.write_bytes(bytes(data))
        else:
            with dest.open("wb") as out:
                shutil.copyfileobj(data, out)
        return str(dest)

    def save_file(self, key: str, path: str | Path) -> str:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = Path(path)
        # Avoid copying a file onto itself (local backend often already has it).
        if src.resolve() != dest.resolve():
            shutil.copyfile(src, dest)
        return str(dest)

    def open(self, key: str) -> BinaryIO:
        return self._path(key).open("rb")

    def url(self, key: str) -> str:
        # Served by the app's /clips (and other) static mounts.
        return "/" + normalize_key(key)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list(self, prefix: str = "") -> list[str]:
        base = self._path(prefix) if prefix else self.root
        if not base.exists():
            return []
        root = self.root
        out: list[str] = []
        if base.is_file():
            return [base.relative_to(root).as_posix()]
        for p in base.rglob("*"):
            if p.is_file():
                out.append(p.relative_to(root).as_posix())
        return sorted(out)

    def size(self, key: str) -> int:
        p = self._path(key)
        return p.stat().st_size if p.is_file() else 0
