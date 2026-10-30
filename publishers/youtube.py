"""YouTube Data API v3 resumable uploader (OAuth refresh-token flow)."""

from __future__ import annotations

import time

import httpx

from config import settings
from publishers.base import (
    BasePublisher,
    PublisherError,
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

# OAuth token endpoint; the URL itself is not a secret.
YOUTUBE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105
YOUTUBE_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def _google_error(response, action: str) -> PublisherError:
    """Turn a Google error response into an actionable, secret-free message."""
    try:
        payload = response.json()
    except Exception:  # pragma: no cover - defensive for a proxy returning HTML
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        nested_errors = error.get("errors")
        nested_reason = ""
        if isinstance(nested_errors, list) and nested_errors:
            first_error = nested_errors[0]
            if isinstance(first_error, dict):
                nested_reason = str(first_error.get("reason") or "").strip()
        reason = str(nested_reason or error.get("status") or error.get("code") or "").strip()
        message = str(error.get("message") or "").strip()
        details = ": ".join(part for part in (reason, message) if part)
    else:
        details = str(error or "").strip()
    if response.status_code == 400 and str(error).lower() == "invalid_grant":
        details = "refresh token was rejected; re-authorize the app with offline access"
    if response.status_code == 403 and not details:
        details = "YouTube Data API access was denied"
    suffix = f": {details}" if details else ""
    return PublisherError(f"YouTube {action} failed ({response.status_code}){suffix}")


class YouTubePublisher(BasePublisher):
    name = "youtube"
    min_interval_seconds = 15

    def __init__(self, client=None, history=None, clock=time.time):
        self.client = client or httpx.Client(timeout=300)
        self._history = history
        self._clock = clock
        self._last_scopes: tuple[str, ...] = ()

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
            YOUTUBE_TOKEN_URL,
            data={
                "client_id": settings.youtube_client_id,
                "client_secret": settings.youtube_client_secret,
                "refresh_token": settings.youtube_refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if response.status_code >= 400:
            raise _google_error(response, "OAuth token exchange")
        payload = response.json()
        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise PublisherError("YouTube OAuth token exchange returned no access token")
        raw_scope = str(payload.get("scope") or "").strip()
        self._last_scopes = tuple(scope for scope in raw_scope.split() if scope)
        if self._last_scopes and YOUTUBE_UPLOAD_SCOPE not in self._last_scopes:
            raise PublisherError(
                "YouTube OAuth token is missing the youtube.upload scope; "
                "re-authorize the app with offline access and upload permission"
            )
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

    def verify_credentials(self, account_id: str = "") -> dict:
        """Verify the refresh token can mint an upload-capable access token.

        This is intentionally a credential preflight, not a fake upload. Google only returns the
        granted scope set during the OAuth exchange, so this proves the refresh token is live and
        checks ``youtube.upload`` when Google includes the scope list. The first real upload still
        remains the authoritative check that the API is enabled for the Cloud project.
        """
        if not self.status(account_id).configured:
            return {
                "verified": False,
                "message": "Set YouTube OAuth client ID, secret, and refresh token first",
            }
        try:
            token = self._token(account_id, force=True)
            cached = self._cached_token(account_id or (settings.youtube_channel_id or "")) or {}
            return {
                "verified": True,
                "message": "OAuth refresh succeeded; YouTube upload permission is ready",
                "account_id": account_id or (settings.youtube_channel_id or ""),
                "expires_at": cached.get("expires_at"),
                "scopes": list(self._last_scopes),
                "access_token_present": bool(token),
            }
        except Exception as exc:
            return {"verified": False, "message": str(exc)}

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

        A clip known to be longer than a Short or known to be landscape gets the
        `watch?v=` form. A queued ``youtube_shorts`` product request is kept as
        a Shorts URL because preflight has already enforced its geometry.

        Defaulting the *unknown* case to Shorts rather than to `watch?v=` is deliberate, even though
        `watch?v=` resolves for both. This tool produces short-form clips; Shorts is the honest
        description of the overwhelming majority, and a URL that names what the thing actually is
        beats one that is merely never wrong. The narrow fix also leaves the common case unchanged,
        so this cannot regress the links already stored on past attempts.
        """
        try:
            metadata = request.metadata or {}
            duration = float(metadata.get("duration") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        try:
            width = float((request.metadata or {}).get("width") or 0.0)
            height = float((request.metadata or {}).get("height") or 0.0)
        except (TypeError, ValueError):
            width, height = 0.0, 0.0
        known_landscape = width > height > 0.0
        if duration > self.SHORTS_MAX_SECONDS or known_landscape:
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
                YOUTUBE_UPLOAD_URL,
                params={"uploadType": "resumable", "part": "snippet,status"},
                json=meta,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=UTF-8",
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
                    YOUTUBE_UPLOAD_URL,
                    params={"uploadType": "resumable", "part": "snippet,status"},
                    json=meta,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json; charset=UTF-8",
                        "X-Upload-Content-Type": "video/mp4",
                        "X-Upload-Content-Length": str(request.video_path.stat().st_size),
                    },
                )
            if init.status_code >= 400:
                raise _google_error(init, "resumable upload initialization")
            location = str(init.headers.get("location") or "").strip()
            if not location:
                raise PublisherError("YouTube did not return a resumable upload session URL")
            uploading = True
            with request.video_path.open("rb") as f:
                upload = self.client.put(
                    location,
                    content=f,
                    headers={
                        # Google requires the bearer credential on the upload request too; the
                        # session URL alone is not an authorization substitute.
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "video/mp4",
                        "Content-Length": str(request.video_path.stat().st_size),
                    },
                )
            if upload.status_code >= 400:
                raise _google_error(upload, "video upload")
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
