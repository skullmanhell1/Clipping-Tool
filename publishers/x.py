"""X API v2 chunked video upload and post creation."""

from __future__ import annotations

import time

import httpx

from config import settings
from publishers.base import (
    BasePublisher,
    PublisherStatus,
    PublishResult,
    PublishState,
    status_of,
)


class XPublisher(BasePublisher):
    name = "x"
    min_interval_seconds = 5
    #: How long to wait for X to transcode the upload before handing it to a human.
    #:
    #: An attribute so a test can shorten it. The wait itself did not exist: the injected `sleep`
    #: was assigned and never called.
    processing_timeout_seconds = 60.0

    def __init__(self, client=None, sleep=time.sleep):
        self.client = client or httpx.Client(timeout=300)
        self.sleep = sleep

    def status(self, account_id=""):
        ok = bool(settings.x_access_token)
        approved = settings.x_direct_post_approved
        msg = (
            (
                "Media upload and posting approved"
                if approved
                else "X user-context approval/token required; review only"
            )
            if ok
            else "Set X_ACCESS_TOKEN (OAuth user context)"
        )
        return PublisherStatus(
            self.name,
            ok,
            ok,
            approved,
            "ready" if ok else "not_configured",
            msg,
            account_id or (settings.x_account_id or ""),
            not approved,
            # PB4: static OAuth user-context token; renewal is a manual step.
            token_kind="static",  # noqa: S106 - a credential *kind*, not a credential
        )

    def _h(self):
        return {"Authorization": f"Bearer {settings.x_access_token}"}

    def publish(self, request):
        st = self.status(request.account_id)
        if not st.configured:
            return PublishResult(False, PublishState.FAILED, self.name, error=st.message)
        if not st.direct_publish or request.mode == "review":
            return PublishResult(
                True,
                PublishState.REVIEW_REQUIRED,
                self.name,
                message="X has no API draft; approve review before posting",
            )
        # Whether the tweet request has gone out. See PublishResult.side_effect_possible.
        posted = False
        try:
            size = request.video_path.stat().st_size
            init = self.client.post(
                "https://api.x.com/2/media/upload/initialize",
                json={
                    "total_bytes": size,
                    "media_type": "video/mp4",
                    "media_category": "tweet_video",
                },
                headers=self._h(),
            )
            init.raise_for_status()
            init_body = init.json()
            # Validated rather than stringified. `str(a or b)` yields the literal string "None"
            # when both keys are absent, and that "None" was then APPENDed and FINALIZEd against a
            # media id that does not exist - four requests and the whole file uploaded before X
            # says anything useful.
            media_id = str(init_body.get("id") or init_body.get("media_id_string") or "")
            if not media_id:
                return PublishResult(
                    False,
                    PublishState.FAILED,
                    self.name,
                    error="X accepted the upload initialisation but returned no media id",
                    raw=init_body,
                )
            with request.video_path.open("rb") as f:
                i = 0
                while chunk := f.read(4 * 1024 * 1024):
                    r = self.client.post(
                        "https://api.x.com/2/media/upload/append",
                        data={"id": media_id, "segment_index": i},
                        files={"media": ("chunk", chunk, "application/octet-stream")},
                        headers=self._h(),
                    )
                    r.raise_for_status()
                    i += 1
            fin = self.client.post(
                "https://api.x.com/2/media/upload/finalize",
                json={"id": media_id},
                headers=self._h(),
            )
            fin.raise_for_status()

            # Wait for X to finish transcoding before attaching the media to a tweet.
            #
            # This step was missing entirely, and the injected `sleep` on this class was **never
            # called** - direct evidence that the wait was designed and not written. X's FINALIZE
            # returns `processing_info` with a `state` and a `check_after_secs`, and posting before
            # the state is `succeeded` produces a routine "media not ready" 4xx *after* the whole
            # file has been uploaded in 4 MB chunks. That error matches no transient pattern, so it
            # was classified permanent and the upload was thrown away.
            info = (fin.json() or {}).get("processing_info") or {}
            waited = 0.0
            while str(info.get("state") or "succeeded") in ("pending", "in_progress"):
                if waited >= self.processing_timeout_seconds:
                    # The bytes are on X's servers and the media id is valid for ~24 hours, so the
                    # honest answer names the id rather than discarding it.
                    return PublishResult(
                        True,
                        PublishState.REVIEW_REQUIRED,
                        self.name,
                        external_id=media_id,
                        message=(
                            "Uploaded to X, still transcoding after "
                            f"{self.processing_timeout_seconds:g}s. Approve to post - the media id "
                            "stays valid, so this costs no re-upload."
                        ),
                    )
                delay = max(1.0, float(info.get("check_after_secs") or 1.0))
                self.sleep(delay)
                waited += delay
                probe = self.client.get(
                    "https://api.x.com/2/media/upload",
                    params={"media_id": media_id, "command": "STATUS"},
                    headers=self._h(),
                )
                probe.raise_for_status()
                info = (probe.json() or {}).get("processing_info") or {}
                if str(info.get("state") or "") == "failed":
                    raise RuntimeError(
                        f"X could not process the video: {info.get('error') or 'unknown error'}"
                    )

            posted = True
            post = self.client.post(
                "https://api.x.com/2/tweets",
                json={
                    "text": f"{request.title}\n\n{request.caption}"[:280],
                    "media": {"media_ids": [media_id]},
                },
                headers=self._h(),
            )
            post.raise_for_status()
            body = post.json()
            tid = str((body.get("data") or {}).get("id") or "")
            if not tid:
                # No id means no verifiable tweet, and claiming success would both fabricate a URL
                # (`https://x.com/i/web/status/` with nothing after it) and let the manager delete
                # the operator's local copy of the clip.
                return PublishResult(
                    False,
                    PublishState.FAILED,
                    self.name,
                    error="X accepted the tweet but returned no tweet id",
                    raw=body,
                    side_effect_possible=True,
                )
            return PublishResult(
                True,
                PublishState.PUBLISHED,
                self.name,
                f"https://x.com/i/web/status/{tid}",
                tid,
                message="Posted",
                raw=body,
            )
        except Exception as exc:
            return PublishResult(
                False,
                PublishState.FAILED,
                self.name,
                error=str(exc),
                status_code=status_of(exc),
                # A failure once the tweet request has gone out may already have posted, and a
                # retry re-runs initialize/append/finalize/tweet from the top - i.e. a second
                # tweet. See PublishResult.side_effect_possible.
                side_effect_possible=posted,
            )
