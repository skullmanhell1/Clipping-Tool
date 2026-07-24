"""Persistent campaign router, scheduler, and throttled publishing worker."""
from __future__ import annotations
import threading, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from config import settings
from publishers import build_publishers
from publishers.base import PublishRequest, PublishState
from publishers.history import HistoryStore, get_history

class PublishManager:
    def __init__(self, publishers=None, history: Optional[HistoryStore]=None,
                 poll_seconds: Optional[float]=None, autostart=True):
        self.publishers=publishers or build_publishers(); self.history=history or get_history()
        self.poll_seconds=poll_seconds or settings.publish_poll_seconds
        self._last:dict[str,float]={}; self._stop=threading.Event(); self._thread=None
        if autostart:self.start()
    def start(self):
        if self._thread and self._thread.is_alive():return
        self._stop.clear(); self._thread=threading.Thread(target=self._loop,daemon=True,name="publish-scheduler"); self._thread.start()
    def stop(self):
        self._stop.set()
        if self._thread:self._thread.join(timeout=self.poll_seconds+1)
    def statuses(self): return {name:p.status().to_dict() for name,p in self.publishers.items()}
    def submit(self,*,job_id:str,clip:Any,video_path:str|Path,platforms:list[str],
               campaign_id:str="",mode:str="auto",schedule_at:Optional[float]=None,
               route_overrides:Optional[dict[str,dict[str,str]]]=None):
        routes={}; campaign=self.history.campaign(campaign_id) if campaign_id else None
        if campaign: routes.update(campaign.routes)
        routes.update(route_overrides or {})
        selected=platforms or list(routes)
        due=schedule_at or time.time(); ids=[]
        for platform in selected:
            if platform not in self.publishers: continue
            route=routes.get(platform,{})
            req={"video_path":str(video_path),"title":clip.title,"description":clip.description,
                 "hashtags":clip.hashtags,"hook_text":clip.hook_text,"cta":clip.cta,
                 "mentions":clip.mentions,"account_id":route.get("account_id",""),
                 "campaign_id":campaign_id,"target_type":route.get("target_type",""),
                 "target_id":route.get("target_id",""),"mode":mode}
            state=PublishState.SCHEDULED.value if due>time.time()+1 else PublishState.QUEUED.value
            ids.append(self.history.create_attempt(job_id=job_id,clip_id=clip.id,platform=platform,
              request=req,scheduled_at=due,state=state,account_id=req["account_id"],campaign_id=campaign_id,
              target_type=req["target_type"],target_id=req["target_id"],mode=mode))
        return ids
    def run_due_once(self):
        processed=[]; now=time.time()
        for item in self.history.due_attempts(now):
            pub=self.publishers.get(item["platform"])
            if not pub: self.history.update_attempt(item["id"],state="failed",error="Unknown platform",completed_at=now); continue
            interval=max(pub.min_interval_seconds,settings.publish_default_interval_seconds)
            if now-self._last.get(pub.name,0)<interval: continue
            self._execute(item,pub); self._last[pub.name]=time.time(); processed.append(item["id"])
        return processed
    def _execute(self,item,pub):
        self.history.update_attempt(item["id"],state=PublishState.UPLOADING.value,started_at=time.time())
        data=item["request_json"]
        req=PublishRequest(video_path=Path(data["video_path"]),title=data.get("title",""),
          description=data.get("description",""),hashtags=data.get("hashtags",[]),hook_text=data.get("hook_text",""),
          cta=data.get("cta",""),mentions=data.get("mentions",[]),account_id=data.get("account_id",""),
          campaign_id=data.get("campaign_id",""),target_type=data.get("target_type",""),target_id=data.get("target_id",""),mode=data.get("mode","auto"))
        if not req.video_path.exists():
            self.history.update_attempt(item["id"],state="failed",error="Clip file no longer exists",completed_at=time.time()); return
        result=pub.publish(req)
        self.history.update_attempt(item["id"],state=result.state.value,url=result.url,external_id=result.external_id,
          error=result.error,message=result.message,result_json=result.to_dict(),completed_at=time.time())
        self._maybe_delete_local(req.video_path, result)
    def _maybe_delete_local(self, video_path: Path, result):
        """Delete the local clip after a successful publish, if the toggle is on.

        Only clip files under ``clips_dir`` are ever removed — never a source
        video. Best-effort; failures are ignored.
        """
        try:
            from runtime_config import get_runtime_config
            if not get_runtime_config().delete_local_after_publish:
                return
            if not (result.success and result.state in (
                PublishState.PUBLISHED, PublishState.PRIVATE, PublishState.DRAFT)):
                return
            clips_root = Path(settings.clips_dir).resolve()
            path = Path(video_path).resolve()
            if clips_root in path.parents and path.is_file():
                path.unlink(missing_ok=True)
                # Remove the sidecar too, if present.
                path.with_suffix(".json").unlink(missing_ok=True)
        except Exception:
            pass
    def _loop(self):
        while not self._stop.wait(self.poll_seconds):
            try:self.run_due_once()
            except Exception: pass

_manager=None; _lock=threading.Lock()
def get_publish_manager():
    global _manager
    with _lock:
        if _manager is None:_manager=PublishManager()
        return _manager
