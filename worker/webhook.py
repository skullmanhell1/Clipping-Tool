"""Outbound job-completion webhook (Phase 7).

Every integration with this tool has had to poll. `GET /api/jobs` twice a second is what the UI
did until Phase 5 replaced it with SSE, and anything *scripted* - an n8n flow, a cron job that
uploads finished clips somewhere, a Slack notifier - still has no choice but to ask repeatedly
whether anything has happened. One outbound request on a terminal transition removes that.

**Fired from the single ``finally`` in ``JobManager._execute``.** Every terminal path - completed,
failed, cancelled - passes through it exactly once, so there is one delivery per job and no
outcome can be added later that silently skips notification. Attempting it at each of the three
`store.update` sites would have been three call sites to keep in step, and the failure mode is a
missing notification, which nobody notices.

**Delivery is synchronous, on the worker thread, with a short bounded timeout.** That is a
deliberate trade and worth stating, because a background thread is the obvious alternative:

* The job is already finished when this runs, so the only cost is delaying the *next* job's
  start. With ``max_workers=1`` that is real but bounded and predictable - at most
  ``job_webhook_timeout_seconds`` per job, which is noise against a render measured in minutes.
* A thread would avoid even that, and introduce a worse question: whether a delivery in flight
  survives process shutdown. A daemon thread loses it silently; a non-daemon thread blocks
  shutdown. Neither is better than a bounded wait.
* Staying on the worker thread also keeps the observability job context, so a delivery failure
  logs against the job it belongs to. ``contextvars`` do not cross ``threading.Thread``, so a
  background sender would have had to re-enter the context or log unattributed lines.

**One attempt, and no delivery guarantee is claimed.** A non-2xx response or a timeout is logged
with what came back and then dropped. Retrying properly needs durable queue state - which
attempts are outstanding, how many times, with what backoff - and that is `publishers/retry.py`'s
job for publishing, where the work is worth the machinery. Pretending to guarantee delivery with
an in-process retry loop would be the worse outcome: it would block the worker for longer and
still lose everything on restart.

**A delivery failure is not a degradation marker.** Optional features in this pipeline fail into
a marker on ``ClipResult.effects_applied``, and this deliberately does not: the webhook is a
job-level integration and says nothing about the rendered output, so recording it against a clip
would claim the clip is somehow lesser when it is byte-identical either way.

**The URL is not SSRF-checked, on purpose.** ``worker/download.validate_public_url`` exists
because *callers* supply URLs to ingest. This one comes from the deployment's own environment,
and the overwhelmingly common target is a service on the same host or LAN - n8n on
``localhost:5678``, a container on a compose network. Refusing private addresses here would break
the main use case to protect against an operator attacking their own machine. The scheme *is*
checked, because that is a typo guard rather than a security boundary: a ``file://`` URL should
fail loudly rather than being handed to an HTTP client.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any
from urllib.parse import urlsplit

from config import settings
from worker import metrics as process_metrics

logger = logging.getLogger(__name__)

#: Schemes an HTTP client can meaningfully be handed. A typo guard, not a security boundary.
_ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Header carrying the HMAC of the body, when a secret is configured.
SIGNATURE_HEADER = "X-Clipping-Signature"

#: Header naming the event, so a receiver can route without parsing the body.
EVENT_HEADER = "X-Clipping-Event"


def configured_events() -> frozenset[str]:
    """Terminal job statuses the operator wants delivered.

    Parsed from a comma-separated setting rather than offered as a list of booleans, because the
    set is small and an operator writing ``failed`` alone is the common case: the interesting
    event is usually the one that needs a human.
    """
    raw = str(settings.job_webhook_events or "")
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def _target() -> str | None:
    """The configured URL, or ``None`` when webhooks are off or the URL is unusable.

    An unusable URL is reported once, here, rather than at delivery time - an operator who
    mistyped the scheme should find out from a log line naming the setting, not from silence.
    """
    url = (settings.job_webhook_url or "").strip()
    if not url:
        return None
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        logger.warning(
            "JOB_WEBHOOK_URL has scheme %r; only http and https can be delivered to, so no "
            "webhook will be sent",
            scheme or "none",
        )
        return None
    if not parts.netloc:
        logger.warning("JOB_WEBHOOK_URL %r has no host, so no webhook will be sent", url)
        return None
    return url


def sign(body: bytes, secret: str) -> str:
    """The signature for ``body``, as ``sha256=<hex>``.

    Prefixed with the algorithm because that is what GitHub, Stripe and Shopify all do, so a
    receiver written against any of their examples works here unchanged - and because an
    unprefixed hex string cannot be migrated later without breaking every receiver.
    """
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def build_payload(job: Any) -> dict[str, Any]:
    """The delivered body for ``job``.

    Deliberately *not* ``job.to_dict()``. That carries every clip's ``transcript_text`` and the
    full ~100-field options object, which on a ten-clip job is hundreds of kilobytes of mostly
    irrelevant text in a request the receiver has to accept before it can decide it does not care.
    This is the summary a receiver acts on - what happened, to which job, how many clips, where
    they are - plus the job id, which is the key to every detail endpoint if more is wanted.

    ``clips`` carries the filename and URL rather than the media: a webhook that inlined video
    would be unusable, and the URL already requires the API token to fetch.
    """
    status = getattr(job.status, "value", str(job.status))
    clips = list(getattr(job, "clips", None) or [])
    return {
        "event": f"job.{status}",
        "job_id": job.id,
        "status": status,
        "batch_id": getattr(job, "batch_id", None),
        "title": getattr(job, "title", "") or "",
        "input_type": getattr(job, "input_type", ""),
        "source": getattr(job, "source", ""),
        "duration": getattr(job, "duration", None),
        "stage": getattr(job, "stage", ""),
        # Present and null on success. A receiver branching on `status` never reads it; one
        # logging a single line reads it unconditionally, and a missing key would raise there.
        "error": getattr(job, "error", None),
        "clip_count": len(clips),
        "clips": [
            {
                "id": getattr(clip, "id", ""),
                "filename": getattr(clip, "filename", ""),
                "title": getattr(clip, "title", ""),
                "duration": getattr(clip, "duration", None),
                "video_url": getattr(clip, "video_url", ""),
                # The degradation contract, surfaced where an integration can act on it: a clip
                # carrying `music_degraded:synthesised` rendered fine but not as asked.
                "effects_applied": list(getattr(clip, "effects_applied", None) or []),
            }
            for clip in clips
        ],
        "stage_timings": list(getattr(job, "stage_timings", None) or []),
        # Phase 7's other half. `cost_usd` inside is null when no rate is configured, which is
        # not the same as zero - see worker/llm_cost.py.
        "llm_usage": dict(getattr(job, "llm_usage", None) or {}),
        "created_at": getattr(job, "created_at", None),
        "updated_at": getattr(job, "updated_at", None),
        "sent_at": time.time(),
    }


def notify(job: Any, *, client: Any = None) -> bool:
    """Deliver the terminal-state webhook for ``job``. Returns whether it was sent.

    Total by construction. This runs in the ``finally`` of the job body, so anything it raises
    would replace the job's real outcome with a delivery error - turning a successful render into
    a failure because a notifier was unreachable. Every failure path returns ``False`` and logs.

    ``client`` is injectable so tests need no network, matching how every publisher in
    ``publishers/`` takes its ``httpx.Client``.
    """
    try:
        url = _target()
        if url is None:
            return False
        status = getattr(job.status, "value", str(job.status))
        events = configured_events()
        if status not in events:
            # Not an error: an operator asking only for failures is the common configuration.
            logger.debug("job webhook skipped, %r not in JOB_WEBHOOK_EVENTS", status)
            return False

        payload = build_payload(job)
        # Separators without spaces, and sort_keys, so the bytes signed are the bytes sent and
        # are reproducible. A receiver verifying the HMAC must hash the raw body it received;
        # re-serialising the parsed JSON would give different bytes and fail every check.
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            EVENT_HEADER: payload["event"],
            "User-Agent": "clipping-tool-webhook/1",
        }
        secret = (settings.job_webhook_secret or "").strip()
        if secret:
            headers[SIGNATURE_HEADER] = sign(body, secret)

        timeout = max(0.1, float(settings.job_webhook_timeout_seconds))
        if client is None:
            import httpx

            client = httpx.Client(timeout=timeout)
            owns_client = True
        else:
            owns_client = False
        try:
            response = client.post(url, content=body, headers=headers)
        finally:
            if owns_client:
                client.close()

        code = int(getattr(response, "status_code", 0))
        if 200 <= code < 300:
            logger.info("job webhook delivered (%s) for %s", code, payload["event"])
            process_metrics.count_webhook("delivered")
            return True
        # Logged rather than retried, and the body is included truncated: a receiver's error
        # message is usually the only thing that says why, and the whole body could be a page.
        logger.warning(
            "job webhook rejected with %s: %s",
            code,
            str(getattr(response, "text", ""))[:200],
        )
        process_metrics.count_webhook("rejected")
        return False
    except Exception as exc:
        # Deliberately broad. The point of this handler is that no failure here - DNS, TLS,
        # timeout, a client library raising something undocumented - can change the outcome of a
        # render that has already finished.
        logger.warning("job webhook could not be delivered: %s", exc)
        # `error` is kept distinct from `rejected` because they need different responses: a
        # rejection means the receiver is up and refused the payload, an error means it was never
        # reached. A single `failed` count would hide which.
        #
        # The paths that send nothing at all - no URL configured, or a URL whose scheme or host is
        # unusable - are deliberately not counted here. They are not delivery attempts, and a
        # deployment with webhooks switched off would otherwise show a steady stream of
        # "deliveries" that never left the process.
        process_metrics.count_webhook("error")
        return False
