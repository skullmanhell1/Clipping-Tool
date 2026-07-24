"""YouTube Data API v3 resumable uploader (OAuth refresh-token flow)."""
from __future__ import annotations
import httpx
from config import settings
from publishers.base import BasePublisher, PublishRequest, PublishResult, PublishState, PublisherStatus

class YouTubePublisher(BasePublisher):
    name="youtube"; min_interval_seconds=15
    def __init__(self, client=None): self.client=client or httpx.Client(timeout=300)
    def status(self, account_id=""):
        ok=all([settings.youtube_client_id,settings.youtube_client_secret,settings.youtube_refresh_token])
        return PublisherStatus(self.name,ok,ok,True,"ready" if ok else "not_configured",
          "OAuth ready; vertical videos publish as Shorts" if ok else "Set YouTube OAuth client ID, secret, and refresh token",
          account_id or (settings.youtube_channel_id or ""))
    def _token(self):
        r=self.client.post("https://oauth2.googleapis.com/token",data={"client_id":settings.youtube_client_id,
          "client_secret":settings.youtube_client_secret,"refresh_token":settings.youtube_refresh_token,
          "grant_type":"refresh_token"}); r.raise_for_status(); return r.json()["access_token"]
    def publish(self, request):
        st=self.status(request.account_id)
        if not st.configured:return PublishResult(False,PublishState.FAILED,self.name,error=st.message)
        try:
            token=self._token(); privacy="private" if request.mode=="review" else "public"
            meta={"snippet":{"title":request.title[:100],"description":request.caption[:5000],
                  "tags":[h.lstrip("#") for h in request.hashtags]},"status":{"privacyStatus":privacy,"selfDeclaredMadeForKids":False}}
            init=self.client.post("https://www.googleapis.com/upload/youtube/v3/videos",
              params={"uploadType":"resumable","part":"snippet,status"},json=meta,
              headers={"Authorization":f"Bearer {token}","X-Upload-Content-Type":"video/mp4",
                       "X-Upload-Content-Length":str(request.video_path.stat().st_size)})
            init.raise_for_status(); location=init.headers["location"]
            with request.video_path.open("rb") as f:
                upload=self.client.put(location,content=f,headers={"Content-Type":"video/mp4"})
            upload.raise_for_status(); data=upload.json(); vid=data.get("id","")
            state=PublishState.PRIVATE if privacy=="private" else PublishState.PUBLISHED
            return PublishResult(True,state,self.name,f"https://youtube.com/shorts/{vid}",vid,
                                 message=f"Uploaded as {privacy}",raw=data)
        except Exception as exc:return PublishResult(False,PublishState.FAILED,self.name,error=str(exc))
