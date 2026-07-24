"""Whop file upload/attachment through the server-side @whop/sdk bridge."""
from __future__ import annotations
import json, os, subprocess
from pathlib import Path
from config import BASE_DIR, settings
from publishers.base import BasePublisher, PublishRequest, PublishResult, PublishState, PublisherStatus

class WhopPublisher(BasePublisher):
    name="whop"; min_interval_seconds=2
    def status(self, account_id=""):
        configured=bool(settings.whop_api_key)
        bridge=BASE_DIR/"publisher_bridge"/"whop.mjs"
        available=configured and bridge.exists()
        return PublisherStatus(self.name,configured,available,True,
          "ready" if available else "not_configured",
          "Ready via @whop/sdk" if available else "Set WHOP_API_KEY and install publisher_bridge dependencies",
          account_id or (settings.whop_company_id or ""))
    def publish(self, request):
        st=self.status(request.account_id)
        if not st.available:
            return PublishResult(False,PublishState.FAILED,self.name,error=st.message)
        payload={"video_path":str(request.video_path.resolve()),"filename":request.video_path.name,
                 "title":request.title,"caption":request.caption,"target_type":request.target_type,
                 "target_id":request.target_id,"visibility":"private" if request.mode=="review" else "public"}
        try:
            p=subprocess.run([settings.whop_node_binary,str(BASE_DIR/"publisher_bridge"/"whop.mjs")],
              input=json.dumps(payload),text=True,capture_output=True,check=True,
              env={**os.environ,"WHOP_API_KEY":settings.whop_api_key or ""},timeout=300)
            data=json.loads(p.stdout)
            state=PublishState.PUBLISHED if data.get("attached") else PublishState.REVIEW_REQUIRED
            msg="Uploaded and attached" if data.get("attached") else "Uploaded; attach in Whop or configure a supported target"
            return PublishResult(True,state,self.name,data.get("url",""),data.get("file_id",""),message=msg,raw=data)
        except Exception as exc:
            return PublishResult(False,PublishState.FAILED,self.name,error=str(exc))
