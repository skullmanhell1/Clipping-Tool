"""Instagram Graph API resumable Reels uploader."""

from __future__ import annotations

import time

import httpx

from config import settings
from publishers.base import (
    BasePublisher,
    PublisherStatus,
    PublishResult,
    PublishState,
)


class InstagramPublisher(BasePublisher):
    name = "instagram"
    min_interval_seconds = 18

    def __init__(self, client=None, sleep=time.sleep):
        self.client = client or httpx.Client(timeout=300)
        self.sleep = sleep

    def status(self, account_id=""):
        ok = bool(settings.instagram_access_token and (account_id or settings.instagram_account_id))
        approved = settings.instagram_content_publish_approved
        msg = (
            (
                "Content publishing approved"
                if approved
                else "Professional account/app approval required; review mode only"
            )
            if ok
            else "Set INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_ACCOUNT_ID"
        )
        return PublisherStatus(
            self.name,
            ok,
            ok,
            approved,
            "ready" if ok else "not_configured",
            msg,
            account_id or (settings.instagram_account_id or ""),
            not approved,
            # PB4: static long-lived token; renewal is a manual step outside this tool.
            # S106: a credential *kind*, not a credential.
            token_kind="static",  # noqa: S106
        )

    def publish(self, request):
        st = self.status(request.account_id)
        if not st.configured:
            return PublishResult(False, PublishState.FAILED, self.name, error=st.message)
        if not st.direct_publish:
            return PublishResult(True, PublishState.REVIEW_REQUIRED, self.name, message=st.message)
        try:
            ver = settings.instagram_api_version
            ig = st.account_id
            token = settings.instagram_access_token
            create = self.client.post(
                f"https://graph.facebook.com/{ver}/{ig}/media",
                json={
                    "media_type": "REELS",
                    "upload_type": "resumable",
                    "caption": request.caption,
                    "access_token": token,
                },
            )
            create.raise_for_status()
            cid = create.json()["id"]
            size = request.video_path.stat().st_size
            with request.video_path.open("rb") as f:
                up = self.client.post(
                    f"https://rupload.facebook.com/ig-api-upload/{ver}/{cid}",
                    content=f,
                    headers={
                        "Authorization": f"OAuth {token}",
                        "offset": "0",
                        "file_size": str(size),
                    },
                )
            up.raise_for_status()
            if request.mode == "review":
                return PublishResult(
                    True,
                    PublishState.DRAFT,
                    self.name,
                    external_id=cid,
                    message="Reel container uploaded; publish within 24 hours",
                )
            for _ in range(5):
                status = self.client.get(
                    f"https://graph.facebook.com/{ver}/{cid}",
                    params={"fields": "status_code", "access_token": token},
                )
                status.raise_for_status()
                code = status.json().get("status_code")
                if code in ("FINISHED", "PUBLISHED"):
                    break
                if code in ("ERROR", "EXPIRED"):
                    raise RuntimeError(f"Instagram container {code.lower()}")
                self.sleep(1)
            pub = self.client.post(
                f"https://graph.facebook.com/{ver}/{ig}/media_publish",
                json={"creation_id": cid, "access_token": token},
            )
            pub.raise_for_status()
            media_id = pub.json()["id"]
            return PublishResult(
                True,
                PublishState.PUBLISHED,
                self.name,
                external_id=media_id,
                message="Reel published",
                raw=pub.json(),
            )
        except Exception as exc:
            return PublishResult(False, PublishState.FAILED, self.name, error=str(exc))
