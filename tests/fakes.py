"""Lightweight fakes for HTTP clients and publishers used across tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class FakeResponse:
    def __init__(
        self,
        *,
        json_data: Any = None,
        headers: dict | None = None,
        status_code: int = 200,
        raise_exc: Exception | None = None,
    ):
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

    def put_object(self, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = Body if isinstance(Body, (bytes, bytearray)) else Body.read()
        return {}

    def upload_file(self, filename, Bucket, Key):
        with open(filename, "rb") as fh:
            self.objects[(Bucket, Key)] = fh.read()
        return {}

    def get_object(self, Bucket, Key):
        import io as _io

        return {"Body": _io.BytesIO(self.objects[(Bucket, Key)])}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise KeyError("404")
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)
        return {}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://s3.example.com/{Params['Bucket']}/{Params['Key']}?sig=abc"

    def get_paginator(self, name):
        objects = self.objects

        class _Paginator:
            def paginate(self, Bucket, Prefix=""):
                contents = [
                    {"Key": k, "ContentLength": len(v)}
                    for (b, k), v in objects.items()
                    if b == Bucket and k.startswith(Prefix)
                ]
                yield {"Contents": contents}

        return _Paginator()


class FakePublisher:
    """A configurable BasePublisher stand-in for scheduler/manager tests."""

    def __init__(
        self,
        name="fake",
        result=None,
        min_interval_seconds=0.0,
        configured=True,
        direct_publish=True,
        message="ok",
    ):
        self.name = name
        self.min_interval_seconds = min_interval_seconds
        self._result = result
        # ``configured``/``direct_publish`` default to True so every pre-existing
        # scheduler test is unaffected. They are settable because the real Instagram
        # and X publishers gate review_required on exactly these two flags, so
        # approve/retry behaviour cannot be tested without them.
        self._configured = configured
        self._direct_publish = direct_publish
        self._message = message
        self.published: list = []

    def status(self, account_id=""):
        from publishers.base import PublisherStatus

        return PublisherStatus(
            self.name,
            self._configured,
            self._configured,
            self._direct_publish,
            "ready" if self._configured else "not_configured",
            self._message,
            account_id,
            not self._direct_publish,
        )

    def publish(self, request):
        from publishers.base import PublishResult, PublishState

        self.published.append(request)
        if self._result is not None:
            return self._result
        return PublishResult(
            True,
            PublishState.PUBLISHED,
            self.name,
            url=f"https://example.com/{self.name}",
            external_id="ext123",
            message="ok",
        )


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
            [[tuple(b) for b in frame] for frame in script] if script is not None else None
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


# --------------------------------------------------------------------------- #
# AV engine test doubles (av-engines-foundation, task 3.4)                     #
#                                                                             #
# These doubles are the **reuse contract** for the queued audio-stem-separation #
# and kinetic-typography specs: they import these classes and their constructor #
# keywords verbatim, so treat the names and keywords as API, not style.         #
#                                                                             #
# Everything below is pure and offline: no ffmpeg, no OpenCV, no network, no    #
# real clock. Randomness, time and capability answers are all injected.         #
# --------------------------------------------------------------------------- #
from pathlib import Path as _Path

from storage_backends.base import BaseStorage as _BaseStorage
from storage_backends.base import normalize_key as _normalize_key
from worker.engines.base import (
    FLAG_SUFFIX as _FLAG_SUFFIX,
)
from worker.engines.base import (
    AV_Engine as _AV_Engine,
)
from worker.engines.base import (
    Compose_Contribution as _Compose_Contribution,
)
from worker.engines.base import (
    Engine_Result as _Engine_Result,
)
from worker.engines.base import (
    Engine_Stage as _Engine_Stage,
)
from worker.engines.base import (
    Engine_Status as _Engine_Status,
)


class FakeEngine(_AV_Engine):
    """A canned :class:`~worker.engines.base.AV_Engine` returning a preset result.

    Every keyword has a sensible default, so ``FakeEngine("x", Engine_Stage.AUDIO)``
    is a valid, enabled-able, applied-returning engine.

    Recording surface (what the host actually passed):

    * ``calls`` — the ``Engine_Context`` of every ``run`` invocation, in order;
    * ``contexts`` — alias of ``calls`` (both names are part of the contract);
    * ``run_count`` — ``len(calls)``;
    * ``plan_calls`` / ``resolve_calls`` — contexts / options seen by ``plan`` and
      ``resolve_options``, so purity and gating can be asserted too.

    The class-level declarations of ``AV_Engine`` (``engine_id``, ``stage``,
    ``priority``, ``required_capabilities``, ``optional_capabilities``,
    ``requires_network``, ``requires_model_download``, ``time_budget_s``,
    ``max_media_passes``, ``max_inputs``, ``produces_media``) are shadowed per
    instance, so a registry holding several ``FakeEngine``s sees genuinely
    different engines. ``max_inputs`` defaults to the number of inputs the canned
    ``contribution`` carries.
    """

    def __init__(
        self,
        engine_id="fake",
        stage=_Engine_Stage.AUDIO,
        *,
        status=_Engine_Status.APPLIED,
        markers=(),
        artifacts=(),
        contribution=None,
        plan=None,
        media=None,
        required_capabilities=(),
        optional_capabilities=(),
        requires_network=False,
        priority=100,
        requires_model_download=False,
        time_budget_s=30.0,
        detail="",
        max_inputs=None,
    ):
        self.engine_id = str(engine_id)
        self.stage = stage
        self.priority = priority
        self.required_capabilities = tuple(required_capabilities)
        self.optional_capabilities = tuple(optional_capabilities)
        self.requires_network = bool(requires_network)
        self.requires_model_download = bool(requires_model_download)
        self.time_budget_s = time_budget_s
        self.status = status
        self.markers = tuple(markers)
        self.artifacts = tuple(artifacts)
        self.contribution = contribution
        self.plan_payload = dict(plan) if plan else {}
        self.media = media
        self.detail = str(detail)
        # ``max_inputs`` defaults to however many inputs the canned contribution
        # actually carries, so a double that contributes inputs declares them (and
        # therefore gets a truthful ``Engine_Context.first_input_index``) without
        # every call site having to repeat the count.
        if max_inputs is None:
            inputs = getattr(contribution, "inputs", ()) or ()
            self.max_inputs = len(inputs)
        else:
            self.max_inputs = int(max_inputs)
        self.produces_media = media is not None
        self.calls: list = []
        self.plan_calls: list = []
        self.resolve_calls: list = []

    # --- recording helpers ------------------------------------------------
    @property
    def contexts(self) -> list:
        """The recorded ``Engine_Context`` objects (alias of :attr:`calls`)."""
        return list(self.calls)

    @property
    def run_count(self) -> int:
        return len(self.calls)

    @property
    def last_context(self):
        return self.calls[-1] if self.calls else None

    # --- AV_Engine contract -----------------------------------------------
    def flag_field(self) -> str:
        """``f"{engine_id}_enabled"`` for the **instance** id.

        ``AV_Engine.flag_field`` is a ``classmethod`` reading the ClassVar
        ``engine_id``, which real engines set at class level. These doubles carry
        their id per instance, so the override keeps ``is_enabled`` honest.
        """
        return f"{self.engine_id}{_FLAG_SUFFIX}"

    def resolve_options(self, options):
        """Pure pass-through: the caller's options object is returned unmutated."""
        self.resolve_calls.append(options)
        return options

    def plan(self, ctx):
        self.plan_calls.append(ctx)
        return dict(self.plan_payload)

    def run(self, ctx) -> _Engine_Result:
        self.calls.append(ctx)
        contribution = self.contribution
        if contribution is True:
            contribution = _Compose_Contribution(engine_id=self.engine_id)
        return _Engine_Result(
            engine_id=self.engine_id,
            status=self.status,
            markers=self.markers,
            artifacts=self.artifacts,
            contribution=contribution,
            plan=dict(self.plan_payload),
            media=self.media,
            detail=self.detail,
        )


class RaisingEngine(_AV_Engine):
    """An engine whose ``run`` always raises, exercising host failure isolation.

    ``plan`` and ``resolve_options`` stay well-behaved (so a test can reach ``run``
    through the host's normal ladder); set ``raise_in_plan=True`` to make the
    planning step raise the same exception instead.
    """

    def __init__(
        self,
        engine_id="raiser",
        stage=_Engine_Stage.AUDIO,
        exc=None,
        *,
        priority=100,
        required_capabilities=(),
        optional_capabilities=(),
        requires_network=False,
        raise_in_plan=False,
    ):
        self.engine_id = str(engine_id)
        self.stage = stage
        self.priority = priority
        self.required_capabilities = tuple(required_capabilities)
        self.optional_capabilities = tuple(optional_capabilities)
        self.requires_network = bool(requires_network)
        self.exc = exc if exc is not None else RuntimeError("boom")
        self._raise_in_plan = bool(raise_in_plan)
        self.calls: list = []
        self.plan_calls: list = []

    @property
    def contexts(self) -> list:
        return list(self.calls)

    @property
    def run_count(self) -> int:
        return len(self.calls)

    def flag_field(self) -> str:
        """See :meth:`FakeEngine.flag_field` — instance id, not the ClassVar."""
        return f"{self.engine_id}{_FLAG_SUFFIX}"

    def resolve_options(self, options):
        return options

    def plan(self, ctx):
        self.plan_calls.append(ctx)
        if self._raise_in_plan:
            raise self.exc
        return {}

    def run(self, ctx) -> _Engine_Result:
        self.calls.append(ctx)
        raise self.exc


class SlowEngine(_AV_Engine):
    """An engine that overruns its time budget against an **injected** clock.

    Deterministic by construction: instead of sleeping, ``run`` advances the
    :class:`FakeClock` it was given by ``overrun`` seconds past the point where the
    budget is exhausted, so budget/timeout assertions never depend on wall time.

    The clock is found in this order: the ``clock=`` constructor keyword, then
    ``ctx.deps["clock"]`` (the host's dependency-injection channel). With no
    advanceable clock available the engine still returns, recording the overrun it
    *would* have taken in ``Engine_Result.elapsed_s``.
    """

    def __init__(
        self,
        engine_id="slow",
        stage=_Engine_Stage.AUDIO,
        overrun=2.0,
        *,
        clock=None,
        priority=100,
        status=_Engine_Status.APPLIED,
        markers=(),
        time_budget_s=30.0,
        required_capabilities=(),
        optional_capabilities=(),
        requires_network=False,
    ):
        self.engine_id = str(engine_id)
        self.stage = stage
        self.priority = priority
        self.overrun = float(overrun)
        self.clock = clock
        self.status = status
        self.markers = tuple(markers)
        self.time_budget_s = time_budget_s
        self.required_capabilities = tuple(required_capabilities)
        self.optional_capabilities = tuple(optional_capabilities)
        self.requires_network = bool(requires_network)
        self.calls: list = []

    @property
    def contexts(self) -> list:
        return list(self.calls)

    @property
    def run_count(self) -> int:
        return len(self.calls)

    def _clock_for(self, ctx):
        if self.clock is not None:
            return self.clock
        deps = getattr(ctx, "deps", None)
        if isinstance(deps, dict):
            return deps.get("clock")
        return None

    def flag_field(self) -> str:
        """See :meth:`FakeEngine.flag_field` — instance id, not the ClassVar."""
        return f"{self.engine_id}{_FLAG_SUFFIX}"

    def resolve_options(self, options):
        return options

    def plan(self, ctx):
        return {"overrun": self.overrun}

    def run(self, ctx) -> _Engine_Result:
        self.calls.append(ctx)
        import math as _math

        clock = self._clock_for(ctx)
        elapsed = float(self.time_budget_s or 0.0) + self.overrun
        if clock is not None and hasattr(clock, "advance"):
            remaining = ctx.remaining(clock()) if hasattr(ctx, "remaining") else _math.inf
            if isinstance(remaining, (int, float)) and _math.isfinite(float(remaining)):
                # Overshoot the deadline by exactly ``overrun`` seconds.
                elapsed = float(remaining) + self.overrun
            clock.advance(elapsed)
        return _Engine_Result(
            engine_id=self.engine_id,
            status=self.status,
            markers=self.markers,
            detail="slow engine overran its budget",
            elapsed_s=elapsed,
        )


class FakeClock:
    """An advanceable monotonic-clock stub: ``clock()`` returns the current value.

    Substitutable for ``time.monotonic`` (``Engine_Host(clock=...)``) and readable
    through ``now``. ``advance(seconds)`` moves it forward (never backwards unless
    ``set`` is used explicitly), and ``readings`` records every value handed out so
    a test can assert how often the host consulted the clock.
    """

    def __init__(self, start=0.0):
        self.now = float(start)
        self.readings: list = []

    def __call__(self) -> float:
        self.readings.append(self.now)
        return self.now

    # Convenience aliases so the same double can stand in for either name.
    def monotonic(self) -> float:
        return self()

    def time(self) -> float:
        return self()

    def advance(self, seconds=1.0) -> float:
        """Move the clock forward by ``seconds`` (clamped at zero) and return it."""
        self.now += max(0.0, float(seconds))
        return self.now

    def set(self, value) -> float:
        """Force the clock to ``value`` (allowed to move backwards, for edge cases)."""
        self.now = float(value)
        return self.now

    @property
    def call_count(self) -> int:
        return len(self.readings)


def _capability_status(capability_id, available, detail=""):
    """Build a ``Capability_Status``-shaped record.

    ``worker.engines.capabilities`` lands in a later task, so the real class is
    imported lazily and a field-compatible fallback is used until then: the probers
    below are importable today and return the genuine record once it exists.
    """
    try:  # pragma: no cover - exercised once capabilities.py lands
        from worker.engines.capabilities import Capability_Status
    except Exception:
        return _Fallback_Capability_Status(str(capability_id), bool(available), str(detail))
    return Capability_Status(str(capability_id), bool(available), str(detail))


class _Fallback_Capability_Status:
    """Minimal ``Capability_Status`` stand-in (``capability_id``/``available``/``detail``)."""

    __slots__ = ("capability_id", "available", "detail")

    def __init__(self, capability_id, available, detail=""):
        self.capability_id = capability_id
        self.available = available
        self.detail = detail

    def to_dict(self) -> dict:
        return {
            "capability_id": self.capability_id,
            "available": self.available,
            "detail": self.detail,
        }

    def __eq__(self, other) -> bool:
        return (
            isinstance(other, _Fallback_Capability_Status)
            and other.capability_id == self.capability_id
            and other.available == self.available
            and other.detail == self.detail
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Capability_Status(capability_id={self.capability_id!r}, "
            f"available={self.available!r}, detail={self.detail!r})"
        )


class StaticProber:
    """A ``Prober`` answering from a fixed ``{capability_id: bool}`` mapping.

    Ids absent from the mapping report ``default`` (``False``, i.e. unavailable, so
    engines gate off unless a test explicitly grants a capability). Every probed id
    is appended to ``calls``.
    """

    def __init__(self, mapping=None, *, default=False):
        self.mapping = dict(mapping or {})
        self.default = bool(default)
        self.calls: list = []

    def __call__(self, capability_id):
        self.calls.append(capability_id)
        available = self.mapping.get(capability_id, self.default)
        detail = "static prober" if available else "static prober: unavailable"
        return _capability_status(capability_id, bool(available), detail)


class CountingProber:
    """A ``Prober`` decorator counting how often each id reaches ``inner``.

    ``counts`` maps id -> invocation count, ``calls`` keeps the ids in order, and
    ``total`` is the overall count — which is what proves ``Capability_Report``
    probes each id at most once.
    """

    def __init__(self, inner=None):
        self.inner = inner if inner is not None else StaticProber()
        self.counts: dict = {}
        self.calls: list = []

    def __call__(self, capability_id):
        self.calls.append(capability_id)
        self.counts[capability_id] = self.counts.get(capability_id, 0) + 1
        return self.inner(capability_id)

    def count_for(self, capability_id) -> int:
        return self.counts.get(capability_id, 0)

    @property
    def total(self) -> int:
        return len(self.calls)


class RaisingProber:
    """A ``Prober`` that always raises, exercising probe error handling.

    The report must wrap the exception as an unavailable status carrying the
    exception class name in ``detail`` rather than propagating it.
    """

    def __init__(self, exc=None):
        self.exc = exc if exc is not None else RuntimeError("probe failed")
        self.calls: list = []

    def __call__(self, capability_id):
        self.calls.append(capability_id)
        raise self.exc


class RecordingStorage(_BaseStorage):
    """An in-memory :class:`~storage_backends.base.BaseStorage` that records keys.

    * ``saved_keys`` — every key passed to ``save`` **or** ``save_file``, in call
      order (duplicates preserved), so artifact-key ordering can be asserted;
    * ``save_file_calls`` — the ``(key, path)`` pairs of ``save_file`` specifically;
    * ``objects`` — the stored bytes, keyed by the *normalised* key;
    * ``fail_on`` — keys (raw or normalised) whose persistence raises ``exc``
      (``OSError`` by default), for durable-artifact failure paths.

    A failed key is still appended to ``saved_keys`` (the attempt happened) but is
    never stored.
    """

    name = "recording"

    def __init__(self, *, fail_on=(), exc=None):
        self.fail_on = set(fail_on or ())
        self.exc = exc
        self.objects: dict = {}
        self.saved_keys: list = []
        self.save_file_calls: list = []
        self.deleted: list = []

    # --- internals --------------------------------------------------------
    def _should_fail(self, key) -> bool:
        return key in self.fail_on or _normalize_key(key) in {
            _normalize_key(k) for k in self.fail_on
        }

    def _fail(self, key):
        if self.exc is not None:
            raise self.exc
        raise OSError(f"RecordingStorage: refusing to save {key!r}")

    # --- BaseStorage ------------------------------------------------------
    def save(self, key, data) -> str:
        self.saved_keys.append(key)
        if self._should_fail(key):
            self._fail(key)
        payload = data if isinstance(data, (bytes, bytearray)) else data.read()
        self.objects[_normalize_key(key)] = bytes(payload)
        return self.url(key)

    def save_file(self, key, path) -> str:
        self.saved_keys.append(key)
        self.save_file_calls.append((key, str(path)))
        if self._should_fail(key):
            self._fail(key)
        with open(path, "rb") as fh:
            self.objects[_normalize_key(key)] = fh.read()
        return self.url(key)

    def open(self, key):
        import io as _io

        return _io.BytesIO(self.objects[_normalize_key(key)])

    def url(self, key) -> str:
        return f"memory://recording/{_normalize_key(key)}"

    def delete(self, key) -> None:
        self.deleted.append(key)
        self.objects.pop(_normalize_key(key), None)

    def exists(self, key) -> bool:
        return _normalize_key(key) in self.objects

    def list(self, prefix="") -> list:
        wanted = _normalize_key(prefix) if prefix else ""
        return sorted(k for k in self.objects if k.startswith(wanted))

    def size(self, key) -> int:
        return len(self.objects.get(_normalize_key(key), b""))

    # --- convenience ------------------------------------------------------
    def key_order(self) -> list:
        """The recorded keys in call order (alias of :attr:`saved_keys`)."""
        return list(self.saved_keys)

    def path_for(self, key) -> _Path:
        """A pseudo-path for ``key``, for assertions that need a ``Path``."""
        return _Path("memory") / _normalize_key(key)


# --------------------------------------------------------------------------- #
# Audio stem-inpainting test doubles (audio-stem-inpainting, task 2.2)         #
#                                                                              #
# These follow the naming/pattern of ``FakeDiarizationBackend`` /               #
# ``RaisingDiarizationBackend`` above: a canned "happy path" double that        #
# records every call, plus narrow variants for each failure mode.               #
#                                                                              #
# Everything here is stdlib-only and offline by construction:                   #
#                                                                              #
#   * **No numeric stack** — WAVs are written with ``wave`` + ``struct`` only;  #
#     no numpy, torch, scipy, soundfile or librosa is imported.                 #
#   * **No ffmpeg, no network, no model file** — nothing is shelled out and no  #
#     socket is opened, so the suite stays fast, offline and CPU-only           #
#     (Req 19.5, 19.7).                                                        #
#   * **No import of ``worker.engines.stems``** — that module lands in epic 4.  #
#     These doubles must remain importable before it exists, so they never      #
#     reference it, and the ``Backend_Stem`` names below are spelled locally.   #
#                                                                              #
# ``fmt`` is DUCK-TYPED on purpose. ``separate(..., fmt=...)`` is documented to #
# receive a ``worker.engines.stems.Audio_Format``, which does not exist yet, so #
# these doubles read ``fmt.sample_rate`` / ``fmt.channels`` defensively through #
# :func:`read_audio_format` (``getattr`` with defaults, mappings also accepted, #
# non-positive/non-numeric values falling back to the defaults). The very same  #
# double therefore works today with an ad-hoc stub (``SimpleNamespace``, a      #
# plain dict, or even ``None``) and unchanged once ``Audio_Format`` lands.      #
# --------------------------------------------------------------------------- #
import json as _json
import struct as _struct
import subprocess as _subprocess
import wave as _wave
from collections import namedtuple as _namedtuple
from collections.abc import Mapping as _Mapping

from worker.ffmpeg_utils import FFmpegError as _FFmpegError

#: The ``Backend_Stem`` vocabulary a four-stem separator emits, sorted. These are
#: the *backend* names (``htdemucs``' own), **not** the engine's ``STEM_NAMES``;
#: the caller maps them through ``STEM_MAPPING`` (``drums``/``bass`` -> ``music``).
BACKEND_STEM_NAMES = ("bass", "drums", "other", "vocals")

#: Fallbacks used when ``fmt`` does not carry a usable value (see the note above).
FAKE_SAMPLE_RATE = 48000
FAKE_CHANNELS = 2
#: 16-bit signed PCM: the only width these doubles write.
FAKE_SAMPLE_WIDTH = 2
#: Duration used when neither the double nor the source WAV implies one.
FAKE_DURATION_S = 0.5
#: Default and full-scale peaks for the synthetic waveform.
FAKE_PEAK = 8000
FAKE_FULL_SCALE_PEAK = 32767

#: One recorded ``Separator_Backend.separate`` call. A ``namedtuple`` so tests can
#: either unpack it positionally (``source, dest_dir, fmt, seed, timeout_s = call``)
#: or read fields by name (``call.seed``).
Separate_Call = _namedtuple("Separate_Call", "source dest_dir fmt seed timeout_s")

#: One recorded command-runner invocation: the argv as a tuple of ``str`` and the
#: explicit subprocess timeout it was given (``None`` when the caller omitted it).
Command_Call = _namedtuple("Command_Call", "argv timeout_s")


def read_audio_format(fmt, *, sample_rate=FAKE_SAMPLE_RATE, channels=FAKE_CHANNELS):
    """Defensively read ``(sample_rate, channels)`` off a duck-typed ``fmt``.

    Accepts anything: a future ``worker.engines.stems.Audio_Format``, a
    ``SimpleNamespace``, a mapping, or ``None``. Attributes are read with
    ``getattr`` (mapping keys with ``get``), and any value that is missing,
    non-numeric, non-finite, zero or negative falls back to the supplied default —
    ``wave`` refuses to write a stream with a non-positive rate or channel count,
    and hostile ``st_audio_format`` draws include exactly those values.
    """

    def _read(name, default):
        if isinstance(fmt, _Mapping):
            value = fmt.get(name, None)
        else:
            value = getattr(fmt, name, None)
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            # ``OverflowError`` is not hypothetical: ``int(float("inf"))`` raises it, and
            # ``st_audio_format`` draws exactly that for ``sample_rate``/``channels``.
            return int(default)
        return number if number > 0 else int(default)

    return _read("sample_rate", sample_rate), _read("channels", channels)


def _triangle_pcm(n_frames, channels, *, period, peak):
    """A deterministic integer triangle wave as little-endian 16-bit PCM bytes.

    Integer-only arithmetic (no ``math``, no floats), so the same bytes are
    produced on every platform and run; identical for every channel of a frame.
    """
    n_frames = max(0, int(n_frames))
    channels = max(1, int(channels))
    period = max(2, int(period))
    peak = max(0, min(FAKE_FULL_SCALE_PEAK, int(peak)))
    half = max(1, period // 2)
    samples = []
    for i in range(n_frames):
        pos = i % period
        if pos < half:
            value = -peak + (2 * peak * pos) // half
        else:
            value = peak - (2 * peak * (pos - half)) // half
        value = max(-FAKE_FULL_SCALE_PEAK, min(FAKE_FULL_SCALE_PEAK, value))
        samples.extend([value] * channels)
    if not samples:
        return b""
    return _struct.pack("<%dh" % len(samples), *samples)


def _silence_pcm(n_frames, channels):
    """Digital silence: exactly zero-valued 16-bit frames (all-zero bytes)."""
    return b"\x00" * (max(0, int(n_frames)) * max(1, int(channels)) * FAKE_SAMPLE_WIDTH)


def write_pcm_wav(path, pcm, *, sample_rate, channels):
    """Write ``pcm`` (little-endian 16-bit frames) to ``path`` with ``wave``."""
    path = _Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _wave.open(str(path), "wb") as wf:
        wf.setnchannels(max(1, int(channels)))
        wf.setsampwidth(FAKE_SAMPLE_WIDTH)
        wf.setframerate(max(1, int(sample_rate)))
        wf.writeframes(pcm)
    return path


def read_pcm_wav(path):
    """Read ``path`` with ``wave``, returning ``(sample_rate, channels, frames, pcm)``.

    Returns ``None`` when the file is missing or is not a readable WAV, which is
    how these doubles stay usable with non-audio and truncated inputs.
    """
    try:
        with _wave.open(str(path), "rb") as wf:
            return (
                wf.getframerate(),
                wf.getnchannels(),
                wf.getnframes(),
                wf.readframes(wf.getnframes()),
            )
    except Exception:
        return None


class Fake_Separator_Backend:
    """A canned ``Separator_Backend`` writing synthetic per-stem WAVs offline.

    Implements the protocol designed for the stem engine::

        backend_id: str
        requires_network: bool
        separate(source, dest_dir, *, fmt, seed, timeout_s) -> Mapping[str, Path]

    and returns ``{Backend_Stem name: wav path}`` (``vocals``/``drums``/``bass``/
    ``other`` by default — the caller is what maps those through ``STEM_MAPPING``).
    Every written WAV is 16-bit PCM at the *requested* ``fmt`` sample rate and
    channel count (Req 4.6), so a test can reopen it with ``wave`` and assert the
    format was preserved.

    Recording surface — ``separate`` appends a :class:`Separate_Call` to ``calls``
    before doing any work, so the ``source``, ``dest_dir``, the ``fmt`` object
    itself, the ``seed`` drawn from ``ctx.rng()`` and the ``timeout_s`` derived from
    ``ctx.remaining()`` can all be asserted; ``seeds``, ``timeouts``,
    ``call_count`` and ``last_call`` are conveniences over it.

    Constructor keywords:

    * ``backend_id`` — the reported id (``"fake"``).
    * ``stems`` — the Backend_Stem names to emit, in any order, including unknown
      names (for the mapping's "unknown backend stem" case).
    * ``requires_network`` — what the permissibility rung consults; see
      :class:`Network_Separator_Backend` for the ``True`` variant.
    * ``sum_to_input`` — when true the stems sum back to the input **exactly,
      sample for sample**: the whole signal goes into ``sum_stem`` and every other
      stem is digital silence (all-zero frames), so the additive-decomposition
      invariant holds by construction with no arithmetic at all. When ``source``
      is a readable WAV already at ``fmt``, its frames are copied verbatim, so the
      sum equals the *input* byte for byte (``copied_source[-1]`` records that).
    * ``duration_s`` — force the output length; by default the length is taken
      from the source WAV (rescaled to the requested rate) and falls back to
      ``FAKE_DURATION_S`` for a missing/non-audio source.
    * ``peak`` — waveform amplitude (``FAKE_FULL_SCALE_PEAK`` for full scale,
      ``0`` for silence).
    * ``silent`` — write digital silence for every stem.
    """

    def __init__(
        self,
        backend_id="fake",
        *,
        stems=BACKEND_STEM_NAMES,
        requires_network=False,
        sum_to_input=False,
        sum_stem="vocals",
        duration_s=None,
        peak=FAKE_PEAK,
        silent=False,
    ):
        self.backend_id = str(backend_id)
        self.requires_network = bool(requires_network)
        self.stems = tuple(stems)
        self.sum_to_input = bool(sum_to_input)
        self.sum_stem = str(sum_stem)
        self.duration_s = None if duration_s is None else float(duration_s)
        self.peak = int(peak)
        self.silent = bool(silent)
        self.calls: list = []
        #: Per call: whether the input WAV's frames were copied verbatim.
        self.copied_source: list = []

    # --- recording helpers ------------------------------------------------
    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_call(self):
        return self.calls[-1] if self.calls else None

    @property
    def seeds(self) -> list:
        return [call.seed for call in self.calls]

    @property
    def timeouts(self) -> list:
        return [call.timeout_s for call in self.calls]

    # --- internals --------------------------------------------------------
    def _frame_count(self, source, sample_rate):
        """Frames to write: explicit duration, else the source's, else the default."""
        if self.duration_s is not None:
            return max(0, int(round(self.duration_s * sample_rate)))
        info = read_pcm_wav(source)
        if info is not None:
            src_rate, _src_channels, src_frames, _pcm = info
            if src_rate == sample_rate:
                return src_frames
            return max(0, int(round(src_frames * (sample_rate / float(src_rate or 1)))))
        return max(0, int(round(FAKE_DURATION_S * sample_rate)))

    def _scale_frames(self, n_frames):
        """Hook for variants that deliberately write the wrong length."""
        return n_frames

    def _period_for(self, stem, seed):
        """A per-stem, per-seed wave period, so stems are distinguishable."""
        index = self.stems.index(stem) if stem in self.stems else 0
        try:
            seed_int = int(seed)
        except (TypeError, ValueError):
            seed_int = 0
        return 16 + 4 * index + (abs(seed_int) % 8)

    # --- Separator_Backend contract ---------------------------------------
    def separate(self, source, dest_dir, *, fmt, seed, timeout_s):
        self.calls.append(Separate_Call(_Path(source), _Path(dest_dir), fmt, seed, timeout_s))
        sample_rate, channels = read_audio_format(fmt)
        n_frames = self._scale_frames(self._frame_count(source, sample_rate))
        dest = _Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)

        source_pcm = None
        if self.sum_to_input:
            info = read_pcm_wav(source)
            if info is not None:
                src_rate, src_channels, src_frames, pcm = info
                if (src_rate, src_channels, src_frames) == (sample_rate, channels, n_frames):
                    source_pcm = pcm
        self.copied_source.append(source_pcm is not None)

        out: dict = {}
        for stem in self.stems:
            if self.silent:
                pcm = _silence_pcm(n_frames, channels)
            elif self.sum_to_input:
                if stem == self.sum_stem:
                    pcm = (
                        source_pcm
                        if source_pcm is not None
                        else _triangle_pcm(
                            n_frames,
                            channels,
                            period=self._period_for(stem, seed),
                            peak=self.peak,
                        )
                    )
                else:
                    pcm = _silence_pcm(n_frames, channels)
            else:
                pcm = _triangle_pcm(
                    n_frames, channels, period=self._period_for(stem, seed), peak=self.peak
                )
            out[stem] = write_pcm_wav(
                dest / f"{stem}.wav", pcm, sample_rate=sample_rate, channels=channels
            )
        return out


class Raising_Separator_Backend:
    """A ``Separator_Backend`` whose ``separate`` always raises.

    Mirrors :class:`RaisingDiarizationBackend`: the call is recorded *first* (so a
    test can still assert the seed and timeout the engine passed), then ``exc`` is
    raised — the engine must convert that into ``Engine_Status.failed`` and must
    not leave a partial Stem_Set behind. ``after`` lets the first *N* calls succeed
    via a delegate, so retry/degradation ladders can be exercised too.
    """

    def __init__(
        self, backend_id="raiser", exc=None, *, requires_network=False, after=0, delegate=None
    ):
        self.backend_id = str(backend_id)
        self.requires_network = bool(requires_network)
        self.exc = exc if exc is not None else RuntimeError("separator backend unavailable")
        self.after = max(0, int(after))
        self.delegate = delegate if delegate is not None else Fake_Separator_Backend()
        self.calls: list = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_call(self):
        return self.calls[-1] if self.calls else None

    def separate(self, source, dest_dir, *, fmt, seed, timeout_s):
        self.calls.append(Separate_Call(_Path(source), _Path(dest_dir), fmt, seed, timeout_s))
        if len(self.calls) <= self.after:
            return self.delegate.separate(source, dest_dir, fmt=fmt, seed=seed, timeout_s=timeout_s)
        raise self.exc


class Truncating_Separator_Backend(Fake_Separator_Backend):
    """A ``Separator_Backend`` whose stems have the **wrong duration**.

    Everything else is honest — the WAVs are readable, at the requested
    ``fmt.sample_rate`` / ``fmt.channels`` — only the length is wrong, which is
    exactly the integrity failure the caller's verification must catch and report
    as failed (Req 4.6, 14.2). ``scale`` multiplies the frame count (``0.5`` by
    default, i.e. half the audio; use ``2.0`` for an over-long output) and
    ``drop_frames`` subtracts a fixed number of frames on top. The result is
    clamped to at least one frame so the file is still a valid WAV.
    """

    def __init__(self, backend_id="truncating", *, scale=0.5, drop_frames=0, **kwargs):
        super().__init__(backend_id, **kwargs)
        self.scale = float(scale)
        self.drop_frames = int(drop_frames)

    def _scale_frames(self, n_frames):
        return max(1, int(round(n_frames * self.scale)) - self.drop_frames)


class Missing_Stem_Backend(Fake_Separator_Backend):
    """A ``Separator_Backend`` that omits one or more Backend_Stems.

    The omitted names are simply absent from the returned mapping (no empty file,
    no zero-length WAV), so the caller must synthesise digital silence for the
    affected Stem_Name and report ``stem_missing:<stem_name>`` (Req 4.3).
    ``missing=("bass", "drums")`` reproduces the ffmpeg adapter's two-stem shape;
    ``missing=BACKEND_STEM_NAMES`` returns an empty mapping.
    """

    def __init__(self, backend_id="missing-stem", *, missing=("other",), **kwargs):
        self.missing = tuple(missing)
        stems = kwargs.pop("stems", BACKEND_STEM_NAMES)
        kept = tuple(stem for stem in stems if stem not in self.missing)
        super().__init__(backend_id, stems=kept, **kwargs)


class Network_Separator_Backend(Fake_Separator_Backend):
    """A ``Separator_Backend`` declaring ``requires_network = True``.

    Nothing about it touches the network — it writes the same synthetic WAVs as
    :class:`Fake_Separator_Backend`. It exists so the permissibility rung can be
    tested: under a ``local_only``/network-forbidden policy the engine must refuse
    or degrade *before* calling ``separate``, which is proved by ``calls`` staying
    empty (Req 16.3).
    """

    def __init__(self, backend_id="network", **kwargs):
        kwargs.pop("requires_network", None)
        super().__init__(backend_id, requires_network=True, **kwargs)


class Recording_Command_Runner:
    """A recording ``Command_Runner`` — ``runner(argv, timeout_s) -> CompletedProcess``.

    Matches the injectable runner the stem engine takes
    (``Callable[[Sequence[str], float], subprocess.CompletedProcess]``) and never
    executes anything, so no ffmpeg/ffprobe binary is needed.

    Recording (Req 19.1): every invocation appends a :class:`Command_Call` holding
    the argv as a tuple of ``str`` and the explicit subprocess timeout, so tests
    can assert the filtergraph, the flags, the input ordering, that a timeout was
    always passed (Req 15.4) and how many media passes were spent (Req 2.6).

    Replay:

    * ``ffprobe`` invocations (argv[0] basename contains ``ffprobe``, or the argv
      carries ``-show_entries``) return canned JSON on ``stdout``. By default that
      is one audio stream built from ``sample_rate``/``channels``/``codec``/
      ``start_time``/``duration``; ``has_audio=False`` yields ``{"streams": []}``
      (the "no audio stream" case, Req 4.8), and ``probe_json`` overrides it with a
      mapping, a raw ``str``, or a sequence consumed one per probe call.
    * every other invocation returns ``responses[i]`` when supplied, else a
      ``CompletedProcess(returncode=returncode, stdout=stdout)``. A response entry
      may be a ``CompletedProcess`` (returned as-is), a ``str``/``bytes``
      (used as ``stdout``), or a mapping/list (JSON-encoded onto ``stdout``).

    Failure injection at a chosen call index (0-based, counting *all* calls):

    * ``fail_at=1`` raises ``exc`` — :class:`worker.ffmpeg_utils.FFmpegError` by
      default — on the second call;
    * ``timeout_at=0`` raises ``subprocess.TimeoutExpired(argv, timeout)`` on the
      first call.

    Both accept an ``int`` or any iterable of ints. The failing call is still
    recorded (the attempt happened) before the exception is raised.
    """

    def __init__(
        self,
        *,
        responses=None,
        stdout="",
        stderr="",
        returncode=0,
        probe_json=None,
        sample_rate=FAKE_SAMPLE_RATE,
        channels=FAKE_CHANNELS,
        codec="pcm_s16le",
        start_time=0.0,
        duration=FAKE_DURATION_S,
        has_audio=True,
        fail_at=None,
        exc=None,
        timeout_at=None,
    ):
        self.responses = list(responses) if responses is not None else []
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = int(returncode)
        self.sample_rate = sample_rate
        self.channels = channels
        self.codec = codec
        self.start_time = start_time
        self.duration = duration
        self.has_audio = bool(has_audio)
        self.probe_json = probe_json
        self.fail_at = self._indices(fail_at)
        self.timeout_at = self._indices(timeout_at)
        self.exc = exc if exc is not None else _FFmpegError("recorded runner: forced failure")
        self.calls: list = []

    # --- internals --------------------------------------------------------
    @staticmethod
    def _indices(value):
        if value is None:
            return frozenset()
        if isinstance(value, int) and not isinstance(value, bool):
            return frozenset({int(value)})
        return frozenset(int(v) for v in value)

    def _default_probe_payload(self) -> dict:
        if not self.has_audio:
            return {"streams": [], "format": {"duration": str(self.duration)}}
        return {
            "streams": [
                {
                    "sample_rate": str(self.sample_rate),
                    "channels": self.channels,
                    "codec_name": self.codec,
                    "start_time": str(self.start_time),
                }
            ],
            "format": {"duration": str(self.duration)},
        }

    def _probe_stdout(self, probe_index) -> str:
        payload = self.probe_json
        if payload is None:
            payload = self._default_probe_payload()
        elif isinstance(payload, (list, tuple)) and payload:
            payload = payload[min(probe_index, len(payload) - 1)]
        if isinstance(payload, (str, bytes)):
            return payload.decode() if isinstance(payload, bytes) else payload
        return _json.dumps(payload)

    @staticmethod
    def _is_probe(argv) -> bool:
        if not argv:
            return False
        return "ffprobe" in _Path(argv[0]).name or "-show_entries" in argv

    def _completed(self, argv, entry):
        if isinstance(entry, _subprocess.CompletedProcess):
            return entry
        if isinstance(entry, bytes):
            entry = entry.decode()
        if isinstance(entry, str):
            return _subprocess.CompletedProcess(list(argv), 0, entry, "")
        return _subprocess.CompletedProcess(list(argv), 0, _json.dumps(entry), "")

    # --- Command_Runner contract ------------------------------------------
    def __call__(self, cmd, timeout_s=None, *, timeout=None):
        argv = tuple(str(part) for part in cmd)
        effective_timeout = timeout_s if timeout_s is not None else timeout
        index = len(self.calls)
        self.calls.append(Command_Call(argv, effective_timeout))

        if index in self.timeout_at:
            raise _subprocess.TimeoutExpired(list(argv), effective_timeout or 0.0)
        if index in self.fail_at:
            raise self.exc

        if self._is_probe(argv):
            return _subprocess.CompletedProcess(
                list(argv), 0, self._probe_stdout(len(self.probe_calls) - 1), ""
            )
        if index < len(self.responses):
            return self._completed(argv, self.responses[index])
        return _subprocess.CompletedProcess(list(argv), self.returncode, self.stdout, self.stderr)

    # --- recording helpers ------------------------------------------------
    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def argvs(self) -> list:
        """Every recorded argv, in call order."""
        return [call.argv for call in self.calls]

    #: Alias of :attr:`argvs` — both spellings are part of the contract.
    @property
    def commands(self) -> list:
        return self.argvs

    @property
    def timeouts(self) -> list:
        return [call.timeout_s for call in self.calls]

    @property
    def last_call(self):
        return self.calls[-1] if self.calls else None

    @property
    def probe_calls(self) -> list:
        return [call for call in self.calls if self._is_probe(call.argv)]

    @property
    def ffmpeg_calls(self) -> list:
        """The non-probe calls, i.e. the media passes (Req 2.6, 15.9)."""
        return [call for call in self.calls if not self._is_probe(call.argv)]

    def calls_matching(self, token) -> list:
        """Recorded calls whose argv contains ``token`` as a whole part or substring."""
        return [call for call in self.calls if any(token in part for part in call.argv)]

    def saw(self, token) -> bool:
        return bool(self.calls_matching(token))

    def reset(self) -> None:
        self.calls.clear()


class Seam_Note_Fixtures:
    """Named ``notes`` tuples for the Seam cases, valid and hostile (Req 6.4-6.6).

    The engine reads Seams **only** from ``Engine_Context.notes``, as
    ``"filler_seam:<float>"`` strings published by the host. Each attribute below
    is a ready-made ``notes`` tuple; :attr:`DURATION` is the clip duration the
    in/out-of-bounds cases are written against, and :attr:`EXPECTED` maps a
    fixture name to the seam values that must survive parsing at that duration
    (in note order), so a test can assert "these and nothing else".

    Valid: :attr:`EMPTY`, :attr:`SINGLE`, :attr:`MANY`, :attr:`ADJACENT`,
    :attr:`AT_ZERO`, :attr:`AT_DURATION`, :attr:`UNSORTED`.
    Hostile: :attr:`MALFORMED_PREFIX`, :attr:`MALFORMED_VALUE`,
    :attr:`NON_FINITE`, :attr:`NEGATIVE`, :attr:`OUT_OF_BOUNDS`,
    :attr:`DUPLICATES`, :attr:`OTHER_ENGINE_NOTES`, and :attr:`MIXED` /
    :attr:`ALL_HOSTILE` which interleave the two so the survivors must be picked
    out individually rather than the whole tuple being rejected.
    """

    #: Clip duration the bounds cases assume.
    DURATION = 10.0

    # --- valid ------------------------------------------------------------
    EMPTY: tuple = ()
    SINGLE = ("filler_seam:1.234",)
    MANY = ("filler_seam:1.000", "filler_seam:2.500", "filler_seam:7.750")
    #: Two seams closer together than any repair window, so their windows merge.
    ADJACENT = ("filler_seam:4.000", "filler_seam:4.010")
    AT_ZERO = ("filler_seam:0.000",)
    AT_DURATION = ("filler_seam:10.000",)
    #: Valid but out of order — parsing must not assume sortedness.
    UNSORTED = ("filler_seam:8.000", "filler_seam:0.500", "filler_seam:3.250")

    # --- hostile ----------------------------------------------------------
    #: Right-ish shape, wrong prefix / no value at all.
    MALFORMED_PREFIX = (
        "filler_seam",
        "filler_seam:",
        "filler_seams:1.000",
        "fillerseam:1.000",
        "seam:1.000",
        ":1.000",
        "FILLER_SEAM:1.000",
        "filler_seam:1.000:2.000",
    )
    #: Correct prefix, unparsable value.
    MALFORMED_VALUE = (
        "filler_seam:abc",
        "filler_seam:1,5",
        "filler_seam: ",
        "filler_seam:1.0s",
        "filler_seam:0x10",
    )
    NON_FINITE = (
        "filler_seam:nan",
        "filler_seam:inf",
        "filler_seam:-inf",
        "filler_seam:NaN",
        "filler_seam:Infinity",
    )
    NEGATIVE = ("filler_seam:-0.001", "filler_seam:-1.500")
    #: Beyond ``DURATION``.
    OUT_OF_BOUNDS = ("filler_seam:10.001", "filler_seam:99.000", "filler_seam:1e9")
    DUPLICATES = ("filler_seam:2.000", "filler_seam:2.000", "filler_seam:2.000")
    #: Another engine's notes — never read as Seams, never rejected wholesale.
    OTHER_ENGINE_NOTES = (
        "kinetic_word:1.000",
        "filler_removed:3",
        "broll:keyword=ocean",
        "degraded:python_pkg:demucs",
        "stem_missing:other",
        "",
    )
    #: Valid seams interleaved with hostile notes: only the valid ones survive.
    MIXED = (
        "kinetic_word:0.500",
        "filler_seam:1.500",
        "filler_seam:nan",
        "filler_seam:-2.000",
        "filler_seams:3.000",
        "filler_seam:3.000",
        "filler_seam:99.000",
        "filler_seam:abc",
        "filler_seam:3.000",
        "stem_missing:music",
    )
    #: Every hostile family at once, with no valid seam anywhere.
    ALL_HOSTILE = (
        MALFORMED_PREFIX
        + MALFORMED_VALUE
        + NON_FINITE
        + NEGATIVE
        + OUT_OF_BOUNDS
        + OTHER_ENGINE_NOTES
    )

    #: fixture name -> the seam values that must survive at :attr:`DURATION`,
    #: in the order the notes appear (duplicates included, so a test can assert
    #: either the raw survivors or their de-duplicated/sorted form).
    EXPECTED = {
        "EMPTY": (),
        "SINGLE": (1.234,),
        "MANY": (1.0, 2.5, 7.75),
        "ADJACENT": (4.0, 4.01),
        "AT_ZERO": (0.0,),
        "AT_DURATION": (10.0,),
        "UNSORTED": (8.0, 0.5, 3.25),
        "MALFORMED_PREFIX": (),
        "MALFORMED_VALUE": (),
        "NON_FINITE": (),
        "NEGATIVE": (),
        "OUT_OF_BOUNDS": (),
        "DUPLICATES": (2.0, 2.0, 2.0),
        "OTHER_ENGINE_NOTES": (),
        "MIXED": (1.5, 3.0, 3.0),
        "ALL_HOSTILE": (),
    }

    @classmethod
    def cases(cls) -> dict:
        """``{fixture name: notes tuple}`` for every fixture in :attr:`EXPECTED`."""
        return {name: getattr(cls, name) for name in cls.EXPECTED}

    @classmethod
    def expected_for(cls, name) -> tuple:
        """The surviving seam values for fixture ``name`` at :attr:`DURATION`."""
        return cls.EXPECTED[name]

    @staticmethod
    def note(value) -> str:
        """Build one well-formed note: ``note(1.5) == "filler_seam:1.500"``."""
        return f"filler_seam:{float(value):.3f}"

    @classmethod
    def notes_for(cls, values) -> tuple:
        """Build a well-formed ``notes`` tuple from an iterable of seam seconds."""
        return tuple(cls.note(v) for v in values)
