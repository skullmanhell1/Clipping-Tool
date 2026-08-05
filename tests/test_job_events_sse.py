"""``GET /api/jobs/events`` — the Server-Sent Events job-progress stream (Phase 5.5).

Why these tests are shaped unusually
------------------------------------
Every other API test here uses ``fastapi.testclient.TestClient``. It cannot test this endpoint:
it reads the response body to completion before returning, so a request to an endless stream never
comes back — the first attempt hung for 45 seconds without printing a status line, which looks like
a broken endpoint and is not.

``httpx.ASGITransport`` was the obvious alternative and is no better. It waits for
``http.response.complete`` before returning a response, so it hangs in ``__aenter__`` — the stream
never opens at all. Measured, not assumed: a probe reached "client open" and then timed out at 40s
without ever reporting a status code.

So the coverage is split three ways, by what each layer can actually break:

* **Protocol** — driven against the async generator directly, with a stub request. Fast,
  deterministic, no sockets. This is where snapshot/incremental/heartbeat/escaping live.
* **Routing, headers, auth** — the HTTP surface, tested without reading a body: route resolution
  through Starlette's router, headers off the response object, and ``TestClient`` for the 401s
  (a rejection has a finite body, so ``TestClient`` is fine there).
* **Streaming for real** — one test against a live uvicorn server on an ephemeral port. This is
  the only layer that can catch the failure that actually matters and that neither of the others
  can see: ``ClipsAuthMiddleware`` is a ``BaseHTTPMiddleware``, and that class is a known hazard
  for streaming responses. If it buffered, every test above would still pass and the endpoint
  would deliver one lump at the end of the render.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routers.jobs import job_events
from config import settings
from worker.jobs import get_manager
from worker.models import ClipResult, Job, JobStatus, ProcessingOptions

# Long enough not to fail a correct implementation on a slow box, short enough that a broken
# stream fails the run instead of stalling it.
READ_TIMEOUT = 15.0


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _job(job_id: str, **fields) -> Job:
    job = Job(
        id=job_id,
        input_type="url",
        source=f"https://example.com/{job_id}",
        options=ProcessingOptions(),
    )
    for key, value in fields.items():
        setattr(job, key, value)
    get_manager().store.add(job)
    return job


class _StubRequest:
    """Just enough ``Request`` for the generator, which only calls ``is_disconnected``.

    Duck-typed rather than a real ``starlette.requests.Request`` because building one that reports
    a disconnect on demand means driving ``receive`` through an immediately-cancelled scope, which
    tests Starlette's plumbing rather than this endpoint. ``disconnect_after`` is the number of
    checks to answer "still here" before reporting the client has gone.
    """

    def __init__(self, disconnect_after: int | None = None) -> None:
        self.checks = 0
        self._disconnect_after = disconnect_after

    async def is_disconnected(self) -> bool:
        self.checks += 1
        if self._disconnect_after is None:
            return False
        return self.checks > self._disconnect_after


def _parse(block: str) -> tuple[str, dict | None] | None:
    """Turn one SSE frame into ``(event_name, payload)``; heartbeats become ``(":ping", None)``."""
    block = block.strip()
    if not block:
        return None
    if block.startswith(":"):
        return (":ping", None)
    name = None
    data = None
    for line in block.split("\n"):
        if line.startswith("event:"):
            name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data = json.loads(line[len("data:") :].strip())
    return (name, data) if name else None


async def _drive(want: int, mutate=None, request: _StubRequest | None = None) -> list:
    """Collect ``want`` frames from the endpoint's generator.

    ``mutate`` runs concurrently: the property under test is that a change made *while a client is
    connected* reaches it, so it has to happen after the stream is established.
    """
    request = request or _StubRequest()
    response = await job_events(request)  # type: ignore[arg-type]
    frames: list = []
    task = asyncio.ensure_future(mutate()) if mutate else None

    async def read() -> None:
        async for chunk in response.body_iterator:
            parsed = _parse(chunk if isinstance(chunk, str) else chunk.decode())
            if parsed:
                frames.append(parsed)
            if len(frames) >= want:
                return

    try:
        await asyncio.wait_for(read(), timeout=READ_TIMEOUT)
    finally:
        if task:
            task.cancel()
        await response.body_iterator.aclose()
    return frames


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fast_stream(monkeypatch):
    """Shrink the loop's timings so the protocol tests run in milliseconds.

    The defaults (0.5s poll, 15s heartbeat) are right for a browser and wrong for a test suite;
    without this the heartbeat test alone would take fifteen seconds.
    """
    monkeypatch.setattr(settings, "job_events_poll_interval_seconds", 0.02)
    monkeypatch.setattr(settings, "job_events_heartbeat_seconds", 1.0)


# --------------------------------------------------------------------------- #
# Protocol                                                                    #
# --------------------------------------------------------------------------- #
def test_first_frame_is_a_snapshot_of_every_job():
    _job("sse-snap-1")
    _job("sse-snap-2")
    frames = _run(_drive(want=1))
    name, payload = frames[0]
    assert name == "snapshot"
    assert {"sse-snap-1", "sse-snap-2"} <= {job["id"] for job in payload["jobs"]}


def test_a_progress_write_is_delivered_as_an_incremental_frame():
    """The whole point of the endpoint."""
    _job("sse-live", status=JobStatus.QUEUED)

    async def mutate():
        await asyncio.sleep(0.1)
        get_manager().store.update(
            "sse-live", progress=0.42, stage="Rendering clip", status=JobStatus.PROCESSING
        )

    frames = _run(_drive(want=2, mutate=mutate))
    names = [name for name, _ in frames]
    assert names[0] == "snapshot"
    assert "jobs" in names, f"no incremental frame arrived; got {names}"
    incremental = next(payload for name, payload in frames if name == "jobs")
    updated = {job["id"]: job for job in incremental["jobs"]}["sse-live"]
    assert updated["progress"] == 0.42
    assert updated["stage"] == "Rendering clip"
    assert updated["status"] == "processing"


def test_an_incremental_frame_carries_only_the_jobs_that_changed():
    """The reason this endpoint exists at all.

    The poll loop it replaces refetched every job — with every clip and the full ~100-field options
    object — twice a second, whether or not anything had moved. If an incremental frame re-sent
    unchanged jobs, this would be a more complicated way to do the same work.
    """
    _job("sse-quiet")
    _job("sse-busy")

    async def mutate():
        await asyncio.sleep(0.1)
        get_manager().store.update("sse-busy", progress=0.5)

    frames = _run(_drive(want=2, mutate=mutate))
    incremental = next(payload for name, payload in frames if name == "jobs")
    ids = {job["id"] for job in incremental["jobs"]}
    assert ids == {"sse-busy"}, f"unchanged jobs were re-sent: {ids}"


def test_an_idle_stream_sends_a_heartbeat_rather_than_nothing():
    """An SSE connection that sends nothing looks dead to anything in the middle.

    nginx's default ``proxy_read_timeout`` is 60s and load balancers are typically similar, so a
    stream that stayed quiet through a long analysis pass would be closed under it.
    """
    _job("sse-idle", status=JobStatus.COMPLETED)
    frames = _run(_drive(want=2))
    assert frames[0][0] == "snapshot"
    assert (":ping", None) in frames[1:], f"no heartbeat; got {[n for n, _ in frames]}"


def test_a_newline_in_clip_text_does_not_split_the_frame():
    """``transcript_text`` contains newlines, and a bare newline ends an SSE frame early.

    Without JSON escaping a client would parse the rest of one job as a new event. This is a real
    payload, not a hypothetical one.
    """
    job = _job("sse-newline")
    job.clips = [
        ClipResult(
            id="clip-nl",
            filename="a.mp4",
            start=0.0,
            end=1.0,
            duration=1.0,
            title="t",
            transcript_text="line one\nline two\n\nline four",
        )
    ]
    get_manager().store.update("sse-newline", clips=job.clips)
    frames = _run(_drive(want=1))
    found = next(j for j in frames[0][1]["jobs"] if j["id"] == "sse-newline")
    assert found["clips"][0]["transcript_text"] == "line one\nline two\n\nline four"


def test_the_stream_stops_when_the_client_goes_away():
    """Checked every tick, rather than relying on the generator being closed.

    Starlette only notices a vanished client when it next tries to write, so a stream with nothing
    to say would otherwise sit in its loop indefinitely after the tab closed — holding a
    connection and a task for a client that no longer exists.
    """
    _job("sse-gone", status=JobStatus.COMPLETED)
    request = _StubRequest(disconnect_after=2)

    async def go():
        response = await job_events(request)  # type: ignore[arg-type]
        frames = []
        async for chunk in response.body_iterator:
            parsed = _parse(chunk if isinstance(chunk, str) else chunk.decode())
            if parsed:
                frames.append(parsed)
        return frames

    # No `want` cap and no timeout cushion beyond this: if the disconnect check did not end the
    # loop, the generator never returns and this call does not either.
    frames = _run(asyncio.wait_for(go(), timeout=READ_TIMEOUT))
    assert frames, "expected at least the snapshot before the disconnect"
    assert request.checks == 3, f"loop did not stop on the third check: {request.checks}"


# --------------------------------------------------------------------------- #
# Routing, headers, auth                                                      #
# --------------------------------------------------------------------------- #
def test_job_events_is_not_shadowed_by_the_job_id_route():
    """``/api/jobs/events`` must not bind as ``/api/jobs/{job_id}``.

    Starlette matches in registration order, so with the parameterised route declared first,
    ``events`` would be read as a job id and this endpoint would answer ``404 Job not found`` — a
    failure indistinguishable from a genuinely missing job. Asserted through the router rather
    than over HTTP because resolving the path is exactly the thing in question.
    """
    # `app.routes` is not the route list on this FastAPI version — it holds `_IncludedRouter`
    # wrappers, so it reports 14 entries with no `endpoint` attribute rather than the 54 real
    # routes. `iter_route_contexts` is the accessor that flattens them.
    from fastapi.routing import iter_route_contexts

    candidates = [
        context
        for context in iter_route_contexts(app.routes)
        if "GET" in (getattr(context, "methods", None) or set())
        and context.path_regex.match("/api/jobs/events")
    ]
    assert candidates, "/api/jobs/events resolves to nothing"
    winner = candidates[0].endpoint
    assert winner is job_events, (
        f"/api/jobs/events resolves to {getattr(winner, '__name__', winner)!r}, not the SSE "
        "endpoint — /api/jobs/{job_id} is registered first and is shadowing it"
    )
    # Both routes really do match the path, so the ordering is load-bearing rather than incidental.
    assert {getattr(c.endpoint, "__name__", None) for c in candidates} == {
        "job_events",
        "get_job",
    }, "expected exactly job_events and get_job to match this path"


def test_stream_sets_the_headers_that_stop_intermediaries_buffering():
    """A response buffered anywhere in the path delivers nothing until it ends.

    ``X-Accel-Buffering: no`` is nginx's documented opt-out (ignored by everything else) and
    ``Cache-Control: no-cache`` stops a cache holding the whole response.
    """
    response = _run(job_events(_StubRequest()))  # type: ignore[arg-type]
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"


def test_the_stream_requires_the_token_like_every_other_route(monkeypatch):
    """Inherited from the app-level dependency, and worth pinning.

    A streaming route is the sort of thing that gets special-cased into an exemption to make a
    client work. This asserts it was not. A rejection has a finite body, so ``TestClient`` is
    usable here where it is not for a successful stream.
    """
    monkeypatch.setattr(settings, "api_auth_token", "sse-secret")
    with TestClient(app) as client:
        assert client.get("/api/jobs/events").status_code == 401


def test_the_stream_does_not_accept_a_query_string_token(monkeypatch):
    """Deliberately *not* added to ``api.security._QUERY_TOKEN_PATHS``.

    ``EventSource`` cannot set headers, which is the usual reason to allow ``?token=``. The
    frontend reads this with ``fetch`` instead, precisely so that allowance stays limited to the
    read-only media paths a browser genuinely cannot authenticate another way — a token in the URL
    of a connection that stays open for a whole render would sit in access logs for its lifetime.
    """
    monkeypatch.setattr(settings, "api_auth_token", "sse-secret")
    with TestClient(app) as client:
        assert client.get("/api/jobs/events?token=sse-secret").status_code == 401


# --------------------------------------------------------------------------- #
# Streaming through the real server                                           #
# --------------------------------------------------------------------------- #
def test_frames_arrive_incrementally_through_the_real_middleware_stack():
    """The one property no in-process client can check.

    ``ClipsAuthMiddleware`` is a ``BaseHTTPMiddleware``, which is a known hazard for streaming
    responses — it pumps the body through an anyio memory stream. If it buffered, every other test
    in this file would still pass while the UI received one lump of frames at the end of the
    render, which is the exact failure this endpoint exists to avoid.

    A raw socket rather than a client library, so nothing between the test and uvicorn can buffer
    and make a buffered response look streamed. An ephemeral port, so a busy CI box cannot collide.
    """
    import uvicorn

    _job("sse-real", status=JobStatus.QUEUED)

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (server.started and server.servers):
            time.sleep(0.05)
        assert server.started and server.servers, "uvicorn did not start"
        port = server.servers[0].sockets[0].getsockname()[1]

        def mutate():
            for index in range(3):
                time.sleep(0.4)
                get_manager().store.update(
                    "sse-real", progress=0.1 * (index + 1), status=JobStatus.PROCESSING
                )

        threading.Thread(target=mutate, daemon=True).start()

        sock = socket.create_connection(("127.0.0.1", port), timeout=READ_TIMEOUT)
        sock.settimeout(READ_TIMEOUT)
        sock.sendall(
            b"GET /api/jobs/events HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Accept: text/event-stream\r\n\r\n"
        )
        started = time.monotonic()
        arrivals: list[float] = []
        raw = b""
        try:
            while time.monotonic() - started < READ_TIMEOUT and len(arrivals) < 3:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                raw += chunk
                for line in chunk.split(b"\n"):
                    if line.strip().startswith(b"event:"):
                        arrivals.append(time.monotonic() - started)
        finally:
            sock.close()

        assert b"Job not found" not in raw, "route was shadowed by /api/jobs/{job_id}"
        assert b"text/event-stream" in raw.lower()
        assert (
            b"transfer-encoding: chunked" in raw.lower()
        ), "response was not chunked, so it was buffered somewhere in the stack"
        assert len(arrivals) >= 3, f"only {len(arrivals)} frames arrived: {raw[:400]!r}"
        # The assertion that catches buffering: frames must be spread out in time. A buffered
        # response delivers them all at once, so the spread would be ~0.
        assert (
            arrivals[-1] - arrivals[0] > 0.3
        ), f"frames arrived together ({arrivals}) — the stack buffered the stream"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
