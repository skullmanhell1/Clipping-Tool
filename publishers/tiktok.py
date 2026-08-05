"""TikTok Content Posting API uploader (draft/private until audit approval)."""

from __future__ import annotations

import httpx

from config import settings
from publishers.base import (
    BasePublisher,
    PublisherStatus,
    PublishResult,
    PublishState,
)


class TikTokPublisher(BasePublisher):
    name = "tiktok"
    min_interval_seconds = 10

    def __init__(self, client=None):
        self.client = client or httpx.Client(timeout=300)

    def status(self, account_id=""):
        ok = bool(settings.tiktok_access_token)
        approved = settings.tiktok_direct_post_approved
        msg = (
            (
                "Direct Post approved"
                if approved
                else "Draft/private upload only until TikTok audit approval"
            )
            if ok
            else "Set TIKTOK_ACCESS_TOKEN"
        )
        return PublisherStatus(
            self.name,
            ok,
            ok,
            approved,
            "ready" if ok else "not_configured",
            msg,
            account_id or (settings.tiktok_open_id or ""),
            not approved,
            # PB4: a long-lived token the operator pasted in. Nothing here can renew it, and
            # its expiry is not visible to us - which is different from "it does not expire".
            token_kind="static",  # noqa: S106 - a credential *kind*, not a credential
        )

    def publish(self, request):
        st = self.status(request.account_id)
        if not st.configured:
            return PublishResult(False, PublishState.FAILED, self.name, error=st.message)
        try:
            direct = st.direct_publish and request.mode == "auto"
            endpoint = (
                "https://open.tiktokapis.com/v2/post/publish/video/init/"
                if direct
                else "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
            )
            size = request.video_path.stat().st_size
            body = {
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": size,
                    "chunk_size": size,
                    "total_chunk_count": 1,
                }
            }
            if direct:
                body["post_info"] = {
                    "title": request.caption[:2200],
                    "privacy_level": "PUBLIC_TO_EVERYONE",
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                }
            r = self.client.post(
                endpoint,
                json=body,
                headers={
                    "Authorization": f"Bearer {settings.tiktok_access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
            )
            r.raise_for_status()
            data = r.json()
            err = data.get("error", {})
            if err.get("code") not in (None, "ok"):
                raise RuntimeError(err.get("message") or err.get("code"))
            target = data["data"]["upload_url"]
            pub_id = data["data"]["publish_id"]
            with request.video_path.open("rb") as f:
                up = self.client.put(
                    target,
                    content=f,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Length": str(size),
                        "Content-Range": f"bytes 0-{size-1}/{size}",
                    },
                )
            up.raise_for_status()
            state = PublishState.PUBLISHED if direct else PublishState.DRAFT
            msg = (
                "Direct post submitted for moderation"
                if direct
                else "Draft uploaded; finish in TikTok inbox"
            )
            return PublishResult(True, state, self.name, external_id=pub_id, message=msg, raw=data)
        except Exception as exc:
            return PublishResult(False, PublishState.FAILED, self.name, error=str(exc))
