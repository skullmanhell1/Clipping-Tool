"""Persistent campaign router, scheduler, and throttled publishing worker."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from config import settings
from publishers import build_publishers, preflight, retry, tailoring
from publishers.base import PublishRequest, PublishState
from publishers.history import HistoryStore, get_history

logger = logging.getLogger(__name__)

#: How long an attempt may sit in ``uploading`` before it is treated as abandoned, in seconds.
#:
#: Comfortably longer than any single upload this tool performs — the publishers use a 300 s HTTP
#: timeout — so a live upload is never reclassified, and short enough that an operator is not left
#: staring at a zombie row for a day.
STALE_UPLOAD_SECONDS = 3600.0


class PublishManager:
    def __init__(
        self,
        publishers=None,
        history: HistoryStore | None = None,
        poll_seconds: float | None = None,
        autostart=True,
    ):
        self.publishers = publishers or build_publishers()
        self.history = history or get_history()
        self.poll_seconds = poll_seconds or settings.publish_poll_seconds
        self._last: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread = None
        if autostart:
            self.start()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        # Reclaim abandoned uploads before the scheduler starts, so a restart resolves them rather
        # than leaving them invisible. Done here rather than in `__init__` so a test constructing a
        # manager with `autostart=False` does not have its fixtures rewritten underneath it.
        self.reclaim_stale_uploads()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="publish-scheduler")
        self._thread.start()

    def reclaim_stale_uploads(self, older_than_seconds: float = STALE_UPLOAD_SECONDS) -> int:
        """Move abandoned ``uploading`` attempts to ``unknown``. Returns how many.

        An attempt reaches ``uploading`` immediately before the platform call and is written back
        immediately after. If the process dies in between — a restart, a deploy, an OOM kill, and
        the scheduler runs in the API process — the row stays ``uploading`` for ever: the scheduler
        only selects queued/scheduled, and every human endpoint refuses ``uploading``. The post may
        or may not exist, there is no record either way, and nothing the operator can click will
        move it.

        They become ``unknown`` rather than ``failed``, and the distinction is the point: a failed
        attempt is safe to retry automatically, and this one is **not** — the post may be live.
        ``unknown`` is resumable by a person, who can check the platform and then approve or cancel.

        Never raises: this runs on the start-up path and a bookkeeping problem must not stop the
        publisher from working.
        """
        try:
            stale = self.history.stale_uploading(time.time() - float(older_than_seconds))
        except Exception:
            logger.exception("could not scan for abandoned uploads")
            return 0
        for item in stale:
            try:
                self.history.update_attempt(
                    item["id"],
                    state=PublishState.UNKNOWN.value,
                    error=(
                        "The process stopped while this upload was in flight, so whether it was "
                        "posted is unknown. Check the platform, then approve to post or cancel to "
                        "discard. Not retried automatically, because retrying could post twice."
                    ),
                    completed_at=time.time(),
                )
            except Exception:
                logger.exception("could not reclaim abandoned upload %s", item.get("id"))
        if stale:
            logger.warning(
                "reclaimed %d publish attempt(s) abandoned mid-upload; they need a human decision",
                len(stale),
            )
        return len(stale)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.poll_seconds + 1)

    def statuses(self):
        return {name: p.status().to_dict() for name, p in self.publishers.items()}

    def submit(
        self,
        *,
        job_id: str,
        clip: Any,
        video_path: str | Path,
        platforms: list[str],
        campaign_id: str = "",
        mode: str = "auto",
        schedule_at: float | None = None,
        route_overrides: dict[str, dict[str, str]] | None = None,
    ):
        routes = {}
        campaign = self.history.campaign(campaign_id) if campaign_id else None
        if campaign:
            routes.update(campaign.routes)
        routes.update(route_overrides or {})
        selected = platforms or list(routes)
        due = schedule_at or time.time()
        ids = []
        for platform in selected:
            if platform not in self.publishers:
                continue
            # S2: one live attempt per (job, clip, platform).
            #
            # `publish_attempts` has no uniqueness constraint - unlike `clips`, which carries
            # `UNIQUE(job_id, clip_id)` - and nothing here looked for an existing row. So two
            # clicks of Publish, or an auto-publish racing a manual one, created two attempts and
            # therefore two posts. Returning the existing id makes a repeat submission idempotent
            # rather than additive, which is what a caller pressing a button twice means.
            #
            # Only *live* attempts block: a previously failed or cancelled attempt should be
            # re-submittable, which is how someone recovers after fixing their credentials.
            existing = self.history.live_attempt_id(job_id, clip.id, platform)
            if existing is not None:
                ids.append(existing)
                logger.info(
                    "publish already queued for clip %s on %s (attempt %s); not duplicating",
                    clip.id,
                    platform,
                    existing,
                )
                continue
            route = routes.get(platform, {})
            req = {
                "video_path": str(video_path),
                "title": clip.title,
                "description": clip.description,
                "hashtags": clip.hashtags,
                "hook_text": clip.hook_text,
                "cta": clip.cta,
                "mentions": clip.mentions,
                "account_id": route.get("account_id", ""),
                "campaign_id": campaign_id,
                "target_type": route.get("target_type", ""),
                "target_id": route.get("target_id", ""),
                "mode": mode,
            }
            state = (
                PublishState.SCHEDULED.value if due > time.time() + 1 else PublishState.QUEUED.value
            )
            ids.append(
                self.history.create_attempt(
                    job_id=job_id,
                    clip_id=clip.id,
                    platform=platform,
                    request=req,
                    scheduled_at=due,
                    state=state,
                    account_id=req["account_id"],
                    campaign_id=campaign_id,
                    target_type=req["target_type"],
                    target_id=req["target_id"],
                    mode=mode,
                )
            )
        return ids

    def run_due_once(self):
        processed = []
        now = time.time()
        for item in self.history.due_attempts(now):
            pub = self.publishers.get(item["platform"])
            if not pub:
                self.history.update_attempt(
                    item["id"], state="failed", error="Unknown platform", completed_at=now
                )
                continue
            # The publisher's own limit governs; the setting is only a floor (default 0).
            interval = max(pub.min_interval_seconds, settings.publish_min_interval_floor_seconds)
            if now - self._last.get(pub.name, 0) < interval:
                continue
            self._execute(item, pub)
            self._last[pub.name] = time.time()
            processed.append(item["id"])
        return processed

    def _execute(self, item, pub):
        self.history.update_attempt(
            item["id"], state=PublishState.UPLOADING.value, started_at=time.time()
        )
        # PB6: fit the copy to *this* platform. The stored request keeps the full text, so a retry
        # or a re-route re-tailors from the original rather than shortening what was already cut.
        data = tailoring.tailor_request(item["request_json"] or {}, pub.name)
        req = PublishRequest(
            video_path=Path(data["video_path"]),
            title=data.get("title", ""),
            description=data.get("description", ""),
            hashtags=data.get("hashtags", []),
            hook_text=data.get("hook_text", ""),
            cta=data.get("cta", ""),
            mentions=data.get("mentions", []),
            account_id=data.get("account_id", ""),
            campaign_id=data.get("campaign_id", ""),
            target_type=data.get("target_type", ""),
            target_id=data.get("target_id", ""),
            mode=data.get("mode", "auto"),
        )
        if not req.video_path.exists():
            self.history.update_attempt(
                item["id"],
                state="failed",
                error="Clip file no longer exists",
                completed_at=time.time(),
            )
            return
        # O10: validate against the platform's constraints before spending an upload
        # attempt. The only check here used to be existence, so a clip the platform would
        # refuse was discovered by uploading it - the user then read a rejection from
        # TikTok's API instead of a sentence saying their clip was too long. Warnings do not
        # block: a 4:5 clip going somewhere that prefers 9:16 is the user's call, not ours.
        report = preflight.validate_clip(req.video_path, pub.name)
        if not report.ok:
            self.history.update_attempt(
                item["id"],
                state="failed",
                error=f"Clip rejected before upload - {report.summary()}",
                result_json=report.to_dict(),
                completed_at=time.time(),
            )
            return
        # PB4: renew an access token that is about to expire *before* spending the upload.
        # Only publishers that can actually refresh do anything here; the rest report
        # `token_kind="static"` and are left alone, since retrying a dead static token cannot help.
        self._ensure_credentials(pub, req.account_id)

        result = pub.publish(req)
        if not result.success and result.state == PublishState.FAILED:
            if self._schedule_retry(item, pub, result):
                return
        self.history.update_attempt(
            item["id"],
            state=result.state.value,
            url=result.url,
            external_id=result.external_id,
            error=result.error,
            message=result.message,
            result_json=result.to_dict(),
            completed_at=time.time(),
        )
        self._maybe_delete_local(req.video_path, result)

    def _ensure_credentials(self, pub, account_id: str) -> None:
        """Refresh a token that is within the expiry margin (PB4). Never raises.

        A refresh failure is deliberately *not* fatal here: the publish is attempted anyway. If
        the credential really is dead the platform says so, and that error is far more useful to
        whoever reads it than one this layer invented from an expiry timestamp.
        """
        try:
            status = pub.status(account_id)
            if status.token_kind != "refreshable" or not status.configured:  # noqa: S105 - comparing against a credential kind, not a secret
                return
            expires_at = status.token_expires_at
            if expires_at is None:
                return
            if float(expires_at) - settings.publish_token_refresh_margin_seconds > time.time():
                return
            pub.refresh_credentials(account_id)
        except Exception:
            pass

    def _schedule_retry(self, item, pub, result) -> bool:
        """Re-queue a transiently-failed attempt with backoff. True when a retry was scheduled.

        Only ``failed`` results reach here - a ``review_required`` attempt is waiting on a person
        and is never retried automatically, which is the same line ``/approve`` and ``/retry``
        draw in the API.
        """
        retry_count = int(item.get("retry_count") or 0)
        error = result.error or "publish failed"

        # S2: never auto-retry a failure that may already have posted.
        #
        # Every platform here is a multi-request flow - X initialize/append/finalize/tweet,
        # Instagram create-container/upload/publish, YouTube initiate/PUT, TikTok init/PUT - and a
        # retry re-runs it **from step one**. So a read timeout on the last call of an upload that
        # the platform actually accepted produces a *second post*, and no idempotency key exists
        # anywhere to prevent it. The publishers now say when they have reached the irreversible
        # step, and this is where that is honoured.
        #
        # The asymmetry is deliberate: a duplicate post cannot be undone by this tool, while a post
        # this refuses to retry can be re-published by one click on `/approve`. So the uncertain
        # case goes to a person, with the uncertainty named.
        if getattr(result, "side_effect_possible", False):
            self.history.update_attempt(
                item["id"],
                state=PublishState.REVIEW_REQUIRED.value,
                error=error,
                message=(
                    "The upload failed after the post may already have been created. Not retried "
                    "automatically, because retrying would re-run the whole upload and could post "
                    "twice. Check the platform, then approve to post or cancel to discard."
                ),
                result_json=result.to_dict(),
                completed_at=time.time(),
            )
            return True

        if not retry.should_retry(retry_count, error, result.status_code):
            if retry_count:
                # Say how hard we tried: "failed after 4 attempts over two hours" and "failed
                # immediately" call for different responses, and the platform error alone cannot
                # tell them apart.
                self.history.update_attempt(
                    item["id"],
                    state=PublishState.FAILED.value,
                    error=retry.exhausted_message(retry_count, error),
                    message=result.message,
                    result_json=result.to_dict(),
                    completed_at=time.time(),
                )
                return True
            return False

        delay = retry.backoff_seconds(retry_count + 1)
        when = time.time() + delay
        self.history.update_attempt(
            item["id"],
            state=PublishState.SCHEDULED.value,
            scheduled_at=when,
            retry_count=retry_count + 1,
            error=error,
            message=f"Transient failure; retry {retry_count + 1} of "
            f"{retry.max_attempts() - 1} in {delay:.0f}s",
            result_json=result.to_dict(),
            # Cleared so the record describes the run in flight rather than the last one.
            started_at=None,
            completed_at=None,
        )
        return True

    def _maybe_delete_local(self, video_path: Path, result):
        """Delete the local clip after a successful publish, if the toggle is on.

        Only clip files under ``clips_dir`` are ever removed — never a source
        video. Best-effort; failures are ignored.
        """
        try:
            from runtime_config import get_runtime_config

            if not get_runtime_config().delete_local_after_publish:
                return
            if not (
                result.success
                and result.state
                in (PublishState.PUBLISHED, PublishState.PRIVATE, PublishState.DRAFT)
            ):
                return
            clips_root = Path(settings.clips_dir).resolve()
            path = Path(video_path).resolve()
            if clips_root in path.parents and path.is_file():
                path.unlink(missing_ok=True)
                # Remove the sidecar too, if present.
                path.with_suffix(".json").unlink(missing_ok=True)
        except Exception:
            pass

    def _loop(self):
        while not self._stop.wait(self.poll_seconds):
            try:
                self.run_due_once()
            except Exception:
                pass


_manager = None
_lock = threading.Lock()


def get_publish_manager():
    global _manager
    with _lock:
        if _manager is None:
            _manager = PublishManager()
        return _manager
