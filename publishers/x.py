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
)


class XPublisher(BasePublisher):
    name="x"; min_interval_seconds=5
    def __init__(self,client=None,sleep=time.sleep): self.client=client or httpx.Client(timeout=300); self.sleep=sleep
    def status(self,account_id=""):
        ok=bool(settings.x_access_token); approved=settings.x_direct_post_approved
        msg=("Media upload and posting approved" if approved else "X user-context approval/token required; review only") if ok else "Set X_ACCESS_TOKEN (OAuth user context)"
        return PublisherStatus(self.name,ok,ok,approved,"ready" if ok else "not_configured",msg,
          account_id or (settings.x_account_id or ""),not approved,
          # PB4: static OAuth user-context token; renewal is a manual step.
          token_kind="static")  # noqa: S106 - a credential *kind*, not a credential
    def _h(self): return {"Authorization":f"Bearer {settings.x_access_token}"}
    def publish(self,request):
        st=self.status(request.account_id)
        if not st.configured:return PublishResult(False,PublishState.FAILED,self.name,error=st.message)
        if not st.direct_publish or request.mode=="review":
            return PublishResult(True,PublishState.REVIEW_REQUIRED,self.name,message="X has no API draft; approve review before posting")
        try:
            size=request.video_path.stat().st_size
            init=self.client.post("https://api.x.com/2/media/upload/initialize",json={"total_bytes":size,"media_type":"video/mp4","media_category":"tweet_video"},headers=self._h())
            init.raise_for_status(); media_id=str(init.json().get("id") or init.json().get("media_id_string"))
            with request.video_path.open("rb") as f:
                i=0
                while chunk:=f.read(4*1024*1024):
                    r=self.client.post("https://api.x.com/2/media/upload/append",data={"id":media_id,"segment_index":i},files={"media":("chunk",chunk,"application/octet-stream")},headers=self._h()); r.raise_for_status(); i+=1
            fin=self.client.post("https://api.x.com/2/media/upload/finalize",json={"id":media_id},headers=self._h()); fin.raise_for_status()
            post=self.client.post("https://api.x.com/2/tweets",json={"text":f"{request.title}\n\n{request.caption}"[:280],"media":{"media_ids":[media_id]}},headers=self._h())
            post.raise_for_status(); tid=str(post.json().get("data",{}).get("id",""))
            return PublishResult(True,PublishState.PUBLISHED,self.name,f"https://x.com/i/web/status/{tid}",tid,message="Posted",raw=post.json())
        except Exception as exc:return PublishResult(False,PublishState.FAILED,self.name,error=str(exc))
