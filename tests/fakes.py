"""Lightweight fakes for HTTP clients and publishers used across tests."""
from __future__ import annotations

from typing import Any, Callable, Optional


class FakeResponse:
    def __init__(self, *, json_data: Any = None, headers: Optional[dict] = None,
                 status_code: int = 200, raise_exc: Optional[Exception] = None):
        self._json = json_data if json_data is not None else {}
        self.headers = headers or {}
        self.status_code = status_code
        self._raise = raise_exc

    def json(self):
        return self._json

    def raise_for_status(self):
        if self._raise is not None:
            raise self._raise


class FakeHTTPClient:
    """Routes (method, url) to responses via a handler; records every call."""

    def __init__(self, handler: Callable[[str, str, dict], FakeResponse]):
        self._handler = handler
        self.calls: list[dict] = []

    def _record(self, method: str, url: str, kwargs: dict) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._handler(method, url, kwargs)

    def post(self, url, **kwargs):
        return self._record("POST", url, kwargs)

    def put(self, url, **kwargs):
        return self._record("PUT", url, kwargs)

    def get(self, url, **kwargs):
        return self._record("GET", url, kwargs)


class FakeS3Client:
    """Minimal in-memory stand-in for a boto3 S3 client (no network/boto3)."""

    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, Bucket, Key, Body):  # noqa: N803
        self.objects[(Bucket, Key)] = Body if isinstance(Body, (bytes, bytearray)) else Body.read()
        return {}

    def upload_file(self, filename, Bucket, Key):  # noqa: N803
        with open(filename, "rb") as fh:
            self.objects[(Bucket, Key)] = fh.read()
        return {}

    def get_object(self, Bucket, Key):  # noqa: N803
        import io as _io

        return {"Body": _io.BytesIO(self.objects[(Bucket, Key)])}

    def head_object(self, Bucket, Key):  # noqa: N803
        if (Bucket, Key) not in self.objects:
            raise KeyError("404")
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.objects.pop((Bucket, Key), None)
        return {}

    def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803
        return f"https://s3.example.com/{Params['Bucket']}/{Params['Key']}?sig=abc"

    def get_paginator(self, name):
        objects = self.objects

        class _Paginator:
            def paginate(self, Bucket, Prefix=""):  # noqa: N803
                contents = [
                    {"Key": k, "ContentLength": len(v)}
                    for (b, k), v in objects.items()
                    if b == Bucket and k.startswith(Prefix)
                ]
                yield {"Contents": contents}

        return _Paginator()


class FakePublisher:
    """A configurable BasePublisher stand-in for scheduler/manager tests."""

    def __init__(self, name="fake", result=None, min_interval_seconds=0.0):
        self.name = name
        self.min_interval_seconds = min_interval_seconds
        self._result = result
        self.published: list = []

    def status(self, account_id=""):
        from publishers.base import PublisherStatus

        return PublisherStatus(self.name, True, True, True, "ready", "ok", account_id)

    def publish(self, request):
        from publishers.base import PublishResult, PublishState

        self.published.append(request)
        if self._result is not None:
            return self._result
        return PublishResult(True, PublishState.PUBLISHED, self.name,
                             url=f"https://example.com/{self.name}", external_id="ext123",
                             message="ok")
