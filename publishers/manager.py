"""Persistent campaign router, scheduler, and throttled publishing worker."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from config import settings
from publishers import build_publishers, preflight, retry, tailoring
from publishers.base import PublishRequest, PublishState
from publishers.history import HistoryStore, get_history


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
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="publish-scheduler")
        self._thread.start()

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
            if status.token_kind != "refreshable" or not status.configured:
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
        if not retry.should_retry(retry_count, error):
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
