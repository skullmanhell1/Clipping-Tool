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
            token_kind="none",
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
            data = json.loads(p.stdout)
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
                data.get("file_id", ""),
                message=msg,
                raw=data,
            )
        except Exception as exc:
            return PublishResult(False, PublishState.FAILED, self.name, error=str(exc))
