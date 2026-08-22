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
    status_of,
)

# Local alias so the call sites below read as the private helper they were written against.
_status_of = status_of


class InstagramPublisher(BasePublisher):
    name = "instagram"
    min_interval_seconds = 18
    #: How many times to poll the container before giving up and returning a resumable DRAFT.
    #:
    #: Was a bare ``range(5)`` with a one-second sleep - a five-second budget for Reel
    #: transcoding, which routinely takes longer. Attributes rather than constants so a test can
    #: shorten them without monkeypatching `time.sleep` for the whole module.
    readiness_attempts = 10
    readiness_interval_seconds = 2.0

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
            token_kind="static",  # noqa: S106 - a credential *kind*, not a credential
        )

    def publish(self, request):
        st = self.status(request.account_id)
        if not st.configured:
            return PublishResult(False, PublishState.FAILED, self.name, error=st.message)
        if not st.direct_publish:
            return PublishResult(True, PublishState.REVIEW_REQUIRED, self.name, message=st.message)
        # Tracks whether the irreversible request has gone out, so the `except` below can say
        # whether a retry would duplicate the Reel. See PublishResult.side_effect_possible.
        published = False
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
            ready = False
            code = ""
            for attempt in range(self.readiness_attempts):
                status = self.client.get(
                    f"https://graph.facebook.com/{ver}/{cid}",
                    params={"fields": "status_code"},
                    # The token goes in a **header**, not the query string. As a query parameter it
                    # was embedded in `httpx.HTTPStatusError`'s message, which is recorded as the
                    # attempt's `error`, written to history.db and served by GET /api/history - so
                    # one 4xx here persisted a long-lived credential into the database and the
                    # dashboard. The upload call below has always used the header form.
                    headers={"Authorization": f"OAuth {token}"},
                )
                status.raise_for_status()
                code = str(status.json().get("status_code") or "")
                if code in ("FINISHED", "PUBLISHED"):
                    ready = True
                    break
                if code in ("ERROR", "EXPIRED"):
                    raise RuntimeError(f"Instagram container {code.lower()}")
                # Not on the last iteration: sleeping after the final poll delays the answer
                # without improving it.
                if attempt < self.readiness_attempts - 1:
                    self.sleep(self.readiness_interval_seconds)

            if not ready:
                # Do **not** publish anyway, which is what this used to do: the loop simply fell
                # through to media_publish after ~5 seconds. Reel transcoding routinely takes
                # longer than that, so the common outcome was a Graph 4xx *after* the whole file
                # had been uploaded - and "not ready" matches nothing in the transient patterns, so
                # it was classified permanent and a perfectly good container was abandoned.
                #
                # The container stays valid for 24 hours, so the honest result is a DRAFT carrying
                # its id: `/approve` can publish it without re-uploading a byte.
                return PublishResult(
                    True,
                    PublishState.DRAFT,
                    self.name,
                    external_id=cid,
                    message=(
                        f"Reel uploaded; Instagram is still processing it "
                        f"(status {code or 'unknown'}). Approve to publish - the container stays "
                        "valid for 24 hours, so this costs no re-upload."
                    ),
                )

            published = True
            pub = self.client.post(
                f"https://graph.facebook.com/{ver}/{ig}/media_publish",
                json={"creation_id": cid, "access_token": token},
            )
            pub.raise_for_status()
            body = pub.json()
            media_id = str(body.get("id") or "")
            if not media_id:
                # A 200 with no id is not evidence of a publish, and claiming success here would
                # also let `_maybe_delete_local` delete the operator's only copy of the clip.
                return PublishResult(
                    False,
                    PublishState.FAILED,
                    self.name,
                    error="Instagram accepted media_publish but returned no media id",
                    raw=body,
                    # The publish call was made, so the Reel may well exist.
                    side_effect_possible=True,
                )
            return PublishResult(
                True,
                PublishState.PUBLISHED,
                self.name,
                external_id=media_id,
                message="Reel published",
                raw=body,
            )
        except Exception as exc:
            return PublishResult(
                False,
                PublishState.FAILED,
                self.name,
                error=str(exc),
                status_code=_status_of(exc),
                # `published` is set immediately before the media_publish call, so a failure with
                # it set means the request went out and the Reel may exist. Retrying would create a
                # second one.
                side_effect_possible=published,
            )
