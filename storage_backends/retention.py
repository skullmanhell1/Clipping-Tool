"""Retention / cleanup.

Periodically removes finished clips and scratch files older than
``settings.retention_days`` from the active storage backend, keeping disk usage
(and S3 costs) bounded.

STUB ONLY.
"""

from __future__ import annotations

from config import settings
from storage_backends.base import BaseStorage


def cleanup_expired(storage: BaseStorage, prefix: str = "clips/") -> int:
    """Delete objects under ``prefix`` older than the retention window.

    Returns the number of objects removed.

    TODO(phase-storage): enumerate keys, check ages, and delete stale objects.
    """
    raise NotImplementedError
