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



# --------------------------------------------------------------------------- #
# B-roll test doubles (Tier 1 — Feature B)
# --------------------------------------------------------------------------- #
class SpyAssetProvider:
    """An ``AssetProvider`` stand-in that records every ``search`` call.

    Configure a single ``result`` returned for any keyword, and/or a per-keyword
    ``results`` mapping (values may be ``None`` to simulate a miss). ``has_key``
    lets the same double act as a keyed/unkeyed external provider.
    """

    def __init__(self, name="spy", result=None, results=None, has_key=True):
        self.name = name
        self._result = result
        self._results = results or {}
        self._has_key = has_key
        self.searches: list = []

    @property
    def has_key(self) -> bool:
        return self._has_key

    def search(self, keyword):
        self.searches.append(keyword)
        if keyword in self._results:
            return self._results[keyword]
        return self._result


class RecordingDownloader:
    """A call-recording ``downloader(keyword, api_key, base_url, cache_dir)``.

    Returns the configured ``result`` (an ``AssetRef`` or ``None``) and appends
    each invocation to ``calls`` so tests can assert no external download
    occurred under ``local_only`` / permissibility / disabled sourcing.
    """

    def __init__(self, result=None):
        self._result = result
        self.calls: list[dict] = []

    def __call__(self, keyword, api_key, base_url, cache_dir):
        self.calls.append(
            {
                "keyword": keyword,
                "api_key": api_key,
                "base_url": base_url,
                "cache_dir": cache_dir,
            }
        )
        return self._result



# --------------------------------------------------------------------------- #
# Speaker-diarisation test doubles (speaker-diarization-reframe)
# --------------------------------------------------------------------------- #
class FakeDiarizationBackend:
    """A canned ``DiarizationBackend`` returning preset ``(label, start, end)``
    spans and recording every ``assign`` call.

    Implements the ``worker.diarization.DiarizationBackend`` protocol so it can
    be injected into ``diarize_source`` for offline, deterministic tests.
    """

    def __init__(self, spans=None):
        self._spans = list(spans or [])
        self.calls: list[tuple] = []

    def assign(self, words, duration):
        self.calls.append((list(words), duration))
        return list(self._spans)


class RaisingDiarizationBackend:
    """A ``DiarizationBackend`` whose ``assign`` always raises, exercising the
    diariser's degradation-to-offline fallback path."""

    def __init__(self, exc=None):
        self._exc = exc or RuntimeError("diarisation backend unavailable")
        self.calls: list[tuple] = []

    def assign(self, words, duration):
        self.calls.append((list(words), duration))
        raise self._exc



# --------------------------------------------------------------------------- #
# Speaker-reframe face-detection test double (speaker-diarization-reframe)
# --------------------------------------------------------------------------- #
class FakeFaceDetector:
    """A canned face detector callable ``detector(frame) -> list[(x, y, w, h)]``.

    Injectable as the ``detector`` argument of
    ``worker.effects.reframe.detect_faces`` so the sampling path runs offline
    with no cv2. Two modes:

      - ``boxes`` set -> the SAME list of ``(x, y, w, h)`` tuples is returned on
        every call (a static per-frame detection).
      - ``script`` set -> a list of per-frame box lists is cycled through, one
        entry consumed per call (wrapping around when exhausted), so successive
        sampled frames can yield different detections.

    With neither configured the detector returns ``[]`` (the "no faces"
    variant). Every call's frame argument is appended to ``calls`` so tests can
    assert the wiring was exercised.
    """

    def __init__(self, boxes=None, script=None):
        self._boxes = [tuple(b) for b in boxes] if boxes is not None else None
        self._script = (
            [[tuple(b) for b in frame] for frame in script]
            if script is not None
            else None
        )
        self.calls: list = []

    def __call__(self, frame):
        idx = len(self.calls)
        self.calls.append(frame)
        if self._script is not None:
            if not self._script:
                return []
            return list(self._script[idx % len(self._script)])
        if self._boxes is not None:
            return list(self._boxes)
        return []



# --------------------------------------------------------------------------- #
# Speaker-reframe frame-sampler test double (speaker-diarization-reframe)
# --------------------------------------------------------------------------- #
class CannedSampler:
    """A canned ``sampler(video) -> list[list[FaceBox]]`` for ``apply_speaker_reframe``.

    ``apply_speaker_reframe`` accepts an injected ``sampler`` that, given the
    video path, returns the per-frame face boxes directly (bypassing cv2 /
    ffmpeg frame decode). This double returns the same preset per-frame
    :class:`~worker.effects.reframe.FaceBox` lists on every call and records
    each invocation's ``video`` argument in ``calls`` so tests can assert the
    wiring was exercised fully offline.
    """

    def __init__(self, per_frame_boxes):
        self._per_frame = list(per_frame_boxes)
        self.calls: list = []

    def __call__(self, video):
        self.calls.append(video)
        return [list(frame) for frame in self._per_frame]
