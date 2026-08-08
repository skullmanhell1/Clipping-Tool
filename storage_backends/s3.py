"""S3 (or S3-compatible) storage backend.

A drop-in replacement for :class:`LocalStorage`, selected with
``STORAGE_BACKEND=s3`` and configured via the ``S3_*`` settings. Objects are
stored under an optional key prefix in the bucket; ``url`` returns a presigned
GET URL so clips remain private by default.

The boto3 client is created lazily (and can be injected for tests), so importing
this module never requires boto3 or network access.
"""

from __future__ import annotations

import io
from typing import BinaryIO

from config import settings
from storage_backends.base import BaseStorage, Data, normalize_key


class S3Storage(BaseStorage):
    """S3-backed :class:`BaseStorage` implementation."""

    name = "s3"

    def __init__(
        self, client=None, bucket: str | None = None, prefix: str = "", url_expiry: int = 3600
    ) -> None:
        self.bucket = bucket or settings.s3_bucket
        self.prefix = prefix.strip("/")
        self.url_expiry = url_expiry
        self._client = client  # injectable; otherwise built lazily

    # -- client / key helpers --------------------------------------------
    @property
    def client(self):
        if self._client is None:
            import boto3  # imported lazily

            self._client = boto3.client(
                "s3",
                region_name=settings.s3_region,
                aws_access_key_id=settings.s3_access_key_id,
                aws_secret_access_key=settings.s3_secret_access_key,
                endpoint_url=settings.s3_endpoint_url,
            )
        return self._client

    def _key(self, key: str) -> str:
        k = normalize_key(key)
        return f"{self.prefix}/{k}" if self.prefix else k

    # -- operations -------------------------------------------------------
    def save(self, key: str, data: Data) -> str:
        body = data if isinstance(data, (bytes, bytearray)) else data.read()
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=body)
        return self.url(key)

    def save_file(self, key: str, path) -> str:
        self.client.upload_file(str(path), self.bucket, self._key(key))
        return self.url(key)

    def open(self, key: str) -> BinaryIO:
        resp = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        return io.BytesIO(resp["Body"].read())

    def url(self, key: str) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._key(key)},
            ExpiresIn=self.url_expiry,
        )

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except Exception:
            return False

    def list(self, prefix: str = "") -> list[str]:
        full_prefix = self._key(prefix) if prefix else (self.prefix or "")
        paginator = self.client.get_paginator("list_objects_v2")
        strip = f"{self.prefix}/" if self.prefix else ""
        out: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                k = obj["Key"]
                out.append(k[len(strip) :] if strip and k.startswith(strip) else k)
        return sorted(out)

    def size(self, key: str) -> int:
        try:
            resp = self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return int(resp.get("ContentLength", 0))
        except Exception:
            return 0
