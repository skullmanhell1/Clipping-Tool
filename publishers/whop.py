"""Whop file upload/attachment through the server-side @whop/sdk bridge."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

from config import BASE_DIR, settings
from publishers.base import (
    BasePublisher,
    PublisherStatus,
    PublishResult,
    PublishState,
)


class WhopPublisher(BasePublisher):
    name = "whop"
    min_interval_seconds = 2

    def status(self, account_id=""):
        configured = bool(settings.whop_api_key)
        bridge = BASE_DIR / "publisher_bridge" / "whop.mjs"
        # I7: the *interpreter* is checked as well as the script. Node is an optional ~200 MB of
        # image installed only for this publisher, so an image built without it has the bridge
        # file - it is committed source - and nothing to run it with. Checking only the script
        # reported the publisher ready and then failed at publish time with a `FileNotFoundError`
        # from `subprocess`, which is the least actionable place to learn that Node is missing.
        runtime = shutil.which(settings.whop_node_binary) is not None
        available = configured and bridge.exists() and runtime
        if not configured:
            detail = "Set WHOP_API_KEY and install publisher_bridge dependencies"
        elif not bridge.exists():
            detail = "publisher_bridge/whop.mjs is missing"
        elif not runtime:
            detail = (
                f"Node ({settings.whop_node_binary}) is not installed - rebuild with "
                "--build-arg INSTALL_WHOP_BRIDGE=true, or set WHOP_NODE_BINARY"
            )
        else:
            detail = "Ready via @whop/sdk"
        return PublisherStatus(
            self.name,
            configured,
            available,
            True,
            "ready" if available else "not_configured",
            detail,
            account_id or (settings.whop_company_id or ""),
            # PB4: an API key, not a token - there is nothing to expire or refresh.
            token_kind="none",  # noqa: S106 - a credential *kind*, not a credential
        )

    def publish(self, request):
        st = self.status(request.account_id)
        if not st.available:
            return PublishResult(False, PublishState.FAILED, self.name, error=st.message)
        payload = {
            "video_path": str(request.video_path.resolve()),
            "filename": request.video_path.name,
            "title": request.title,
            "caption": request.caption,
            "target_type": request.target_type,
            "target_id": request.target_id,
            "visibility": "private" if request.mode == "review" else "public",
        }
        try:
            p = subprocess.run(
                [settings.whop_node_binary, str(BASE_DIR / "publisher_bridge" / "whop.mjs")],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=True,
                env={**os.environ, "WHOP_API_KEY": settings.whop_api_key or ""},
                timeout=300,
            )
            try:
                data = json.loads(p.stdout)
            except json.JSONDecodeError as exc:
                # The bridge is expected to print exactly one JSON object. Anything else - a Node
                # deprecation warning on stdout, a stack trace, an empty stream - produced
                # "Expecting value: line 1 column 1", which names neither the bridge nor what it
                # actually said. Both streams go into the error so the cause is in the record.
                return PublishResult(
                    False,
                    PublishState.FAILED,
                    self.name,
                    error=(
                        f"the Whop bridge did not return JSON ({exc}). "
                        f"stdout={p.stdout[:400]!r} stderr={p.stderr[:400]!r}"
                    ),
                )

            # The bridge reports failure **in its payload and exits 0** (see
            # `publisher_bridge/whop.mjs`'s `fail()`), so `check=True` cannot catch it and
            # `data["success"]` is the only signal there is. It was never read: a Whop upload that
            # never happened - no API key inside the bridge, an SDK throw, an unreadable file, a
            # network failure mid-upload - was stored as a *successful* attempt in
            # `review_required` with an empty external_id, the real error buried in
            # `result_json.raw.error`. Because the result was not FAILED, the retry policy was
            # never consulted either, so even a transient bridge failure was terminal and labelled
            # fine. The operator was then invited to "approve" a file that does not exist on Whop.
            if not data.get("success", False):
                detail = str(data.get("error") or "the Whop bridge reported failure")
                return PublishResult(
                    False,
                    PublishState.FAILED,
                    self.name,
                    error=detail,
                    raw=data,
                )

            file_id = str(data.get("file_id") or "")
            if not file_id:
                # A success with nothing identifying the upload cannot be verified, resumed or
                # attached later, so it is not a success this code is willing to claim.
                return PublishResult(
                    False,
                    PublishState.FAILED,
                    self.name,
                    error="the Whop bridge reported success but returned no file id",
                    raw=data,
                )

            state = PublishState.PUBLISHED if data.get("attached") else PublishState.REVIEW_REQUIRED
            msg = (
                "Uploaded and attached"
                if data.get("attached")
                else "Uploaded; attach in Whop or configure a supported target"
            )
            return PublishResult(
                True,
                state,
                self.name,
                data.get("url", ""),
                file_id,
                message=msg,
                raw=data,
            )
        except subprocess.TimeoutExpired as exc:
            return PublishResult(
                False,
                PublishState.FAILED,
                self.name,
                error=f"the Whop bridge timed out after {exc.timeout:g}s",
            )
        except subprocess.CalledProcessError as exc:
            # `str(exc)` is only "Command '[...]' returned non-zero exit status 1." - the Node
            # stack trace lives in `exc.stderr` and was discarded. The most common cause is
            # `ERR_MODULE_NOT_FOUND: Cannot find package '@whop/sdk'`, i.e. nobody ran
            # `npm install` in publisher_bridge/, and a bare exit status says nothing about that.
            stderr = (exc.stderr or "").strip()
            detail = f": {stderr[-500:]}" if stderr else ""
            return PublishResult(
                False,
                PublishState.FAILED,
                self.name,
                error=f"the Whop bridge exited {exc.returncode}{detail}",
            )
        except Exception as exc:
            return PublishResult(False, PublishState.FAILED, self.name, error=str(exc))
