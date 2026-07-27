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
from worker.engines.base import (
    FLAG_SUFFIX as _FLAG_SUFFIX,
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
