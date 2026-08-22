"""YouTube Data API v3 resumable uploader (OAuth refresh-token flow)."""

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

#: How long before expiry a cached token is treated as already expired (PB4).
#:
#: An upload is not instantaneous - a resumable PUT of a 1080p clip takes tens of seconds - so a
#: token with five seconds left will expire *during* the request it was fetched for. Refreshing
#: early costs one cheap token exchange; not doing so costs a failed upload of a whole file.
TOKEN_EXPIRY_MARGIN_S = 120.0

#: Fallback lifetime when Google does not return ``expires_in``. Google's access tokens are
#: documented as one hour; this is deliberately shorter so an unexpected response shape leads to
#: refreshing too often rather than using a dead token.
DEFAULT_TOKEN_TTL_S = 1800.0


class YouTubePublisher(BasePublisher):
    name = "youtube"
    min_interval_seconds = 15

    def __init__(self, client=None, history=None, clock=time.time):
        self.client = client or httpx.Client(timeout=300)
        self._history = history
        self._clock = clock

    @property
    def history(self):
        """The token store, resolved lazily so constructing a publisher opens no database."""
        if self._history is None:
            from publishers.history import get_history

            self._history = get_history()
        return self._history

    def status(self, account_id=""):
        ok = all(
            [
                settings.youtube_client_id,
                settings.youtube_client_secret,
                settings.youtube_refresh_token,
            ]
        )
        account = account_id or (settings.youtube_channel_id or "")
        expires_at = None
        if ok:
            cached = self._cached_token(account)
            expires_at = cached.get("expires_at") if cached else None
        return PublisherStatus(
            self.name,
            ok,
            ok,
            True,
            "ready" if ok else "not_configured",
            "OAuth ready; vertical videos publish as Shorts"
            if ok
            else "Set YouTube OAuth client ID, secret, and refresh token",
            account,
            token_expires_at=expires_at,
            # The one publisher that can renew itself: it was given a refresh token.
            token_kind="refreshable" if ok else "none",
        )

    # ------------------------------------------------------------------ PB4 --
    def _cached_token(self, account_id: str) -> dict | None:
        try:
            return self.history.get_token(self.name, account_id)
        except Exception:
            # A token cache that cannot be read must not stop a publish: the exchange below
            # still works, it is just no longer cached.
            return None

    def _exchange(self) -> tuple[str, float]:
        """Trade the refresh token for an access token; return ``(token, expires_at)``."""
        response = self.client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": settings.youtube_client_id,
                "client_secret": settings.youtube_client_secret,
                "refresh_token": settings.youtube_refresh_token,
                "grant_type": "refresh_token",
            },
        )
        response.raise_for_status()
        payload = response.json()
        token = payload["access_token"]
        try:
            ttl = float(payload.get("expires_in") or DEFAULT_TOKEN_TTL_S)
        except (TypeError, ValueError):
            ttl = DEFAULT_TOKEN_TTL_S
        return token, self._clock() + ttl

    def _token(self, account_id: str = "", *, force: bool = False) -> str:
        """A valid access token, reusing the cached one until it is close to expiring.

        This used to exchange the refresh token on *every* publish. That works, and it means one
        extra network round trip per upload plus a token request per clip against a quota that is
        shared with the uploads themselves - and it discards the expiry Google returns, so
        nothing in the product could ever say when the credential would stop working.
        """
        account = account_id or (settings.youtube_channel_id or "")
        if not force:
            cached = self._cached_token(account)
            if cached and cached.get("access_token"):
                expires_at = cached.get("expires_at")
                if expires_at is None or float(expires_at) - TOKEN_EXPIRY_MARGIN_S > self._clock():
                    return str(cached["access_token"])

        token, expires_at = self._exchange()
        try:
            self.history.save_token(self.name, token, account_id=account, expires_at=expires_at)
        except Exception:
            pass  # Caching is an optimisation; the token in hand is still good.
        return token

    def refresh_credentials(self, account_id: str = "") -> bool:
        """Force a token exchange (PB4). ``False`` when not configured or the exchange fails."""
        if not self.status(account_id).configured:
            return False
        try:
            self._token(account_id, force=True)
            return True
        except Exception:
            return False

    def invalidate_credentials(self, account_id: str = "") -> None:
        try:
            self.history.clear_token(self.name, account_id or (settings.youtube_channel_id or ""))
        except Exception:
            pass

    # -------------------------------------------------------------- publish --
    #: Longest upload YouTube still serves as a Short, in seconds.
    #:
    #: The URL used to be hard-coded to `/shorts/<id>` for **every** upload, while
    #: `preflight.PLATFORM_LIMITS["youtube"]` deliberately permits up to 900 s and 16:9 landscape.
    #: A five-minute landscape upload therefore got a Shorts link that does not resolve to what was
    #: posted, which is a wrong answer stored on the attempt record and shown to the operator.
    SHORTS_MAX_SECONDS = 180.0

    def _watch_url(self, video_id: str, request) -> str:
        """The URL that actually resolves to this upload.

        A clip **known** to be longer than a Short gets the `watch?v=` form; everything else keeps
        the `/shorts/` link. Only the known-long case was wrong — `preflight` deliberately permits
        YouTube clips up to 900 s and 16:9 landscape, and those were being handed a Shorts URL that
        does not resolve to what was posted.

        Defaulting the *unknown* case to Shorts rather than to `watch?v=` is deliberate, even though
        `watch?v=` resolves for both. This tool produces short-form clips; Shorts is the honest
        description of the overwhelming majority, and a URL that names what the thing actually is
        beats one that is merely never wrong. The narrow fix also leaves the common case unchanged,
        so this cannot regress the links already stored on past attempts.
        """
        try:
            duration = float((request.metadata or {}).get("duration") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        if duration > self.SHORTS_MAX_SECONDS:
            return f"https://www.youtube.com/watch?v={video_id}"
        return f"https://youtube.com/shorts/{video_id}"

    def publish(self, request):
        st = self.status(request.account_id)
        if not st.configured:
            return PublishResult(False, PublishState.FAILED, self.name, error=st.message)
        # Whether the file bytes have started going to Google. See side_effect_possible.
        uploading = False
        try:
            token = self._token(request.account_id)
            privacy = "private" if request.mode == "review" else "public"
            meta = {
                "snippet": {
                    "title": request.title[:100],
                    "description": request.caption[:5000],
                    "tags": [h.lstrip("#") for h in request.hashtags],
                },
                "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
            }
            init = self.client.post(
                "https://www.googleapis.com/upload/youtube/v3/videos",
                params={"uploadType": "resumable", "part": "snippet,status"},
                json=meta,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Upload-Content-Type": "video/mp4",
                    "X-Upload-Content-Length": str(request.video_path.stat().st_size),
                },
            )
            if init.status_code == 401:
                # The cached token was rejected - it may have been revoked, or expired earlier
                # than its stated lifetime. One forced refresh and one retry, because a second
                # 401 on a freshly minted token is a credential problem, not a stale cache, and
                # looping on it would spend the retry budget learning nothing.
                self.invalidate_credentials(request.account_id)
                token = self._token(request.account_id, force=True)
                init = self.client.post(
                    "https://www.googleapis.com/upload/youtube/v3/videos",
                    params={"uploadType": "resumable", "part": "snippet,status"},
                    json=meta,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-Upload-Content-Type": "video/mp4",
                        "X-Upload-Content-Length": str(request.video_path.stat().st_size),
                    },
                )
            init.raise_for_status()
            location = init.headers["location"]
            uploading = True
            with request.video_path.open("rb") as f:
                upload = self.client.put(location, content=f, headers={"Content-Type": "video/mp4"})
            upload.raise_for_status()
            data = upload.json()
            vid = str(data.get("id") or "")
            if not vid:
                # A 200 with no video id is not a verifiable upload, and claiming success both
                # fabricates a URL (`https://youtube.com/shorts/` with nothing after it) and lets
                # the manager delete the operator's local copy of the clip.
                return PublishResult(
                    False,
                    PublishState.FAILED,
                    self.name,
                    error="YouTube accepted the upload but returned no video id",
                    raw=data,
                    side_effect_possible=True,
                )
            state = PublishState.PRIVATE if privacy == "private" else PublishState.PUBLISHED
            return PublishResult(
                True,
                state,
                self.name,
                self._watch_url(vid, request),
                vid,
                message=f"Uploaded as {privacy}",
                raw=data,
            )
        except Exception as exc:
            return PublishResult(
                False,
                PublishState.FAILED,
                self.name,
                error=str(exc),
                status_code=status_of(exc),
                # Once the PUT has begun, Google may have received the whole file. A retry
                # re-initiates a *new* resumable session and uploads from byte 0, so a timeout on
                # a completed upload produces a second video. The resumable protocol can resume
                # (query the session with `Content-Range: bytes */size`), but the session URL is a
                # local variable here and nothing persists it - so resuming is not available and
                # a human has to look. See PublishResult.side_effect_possible.
                side_effect_possible=uploading,
            )
