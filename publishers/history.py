"""SQLite-backed clip, campaign, and publish-attempt history."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from config import settings


@dataclass
class Campaign:
    id: str
    name: str
    routes: dict[str, dict[str, str]] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]: return asdict(self)


class HistoryStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or settings.history_db)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS clips (
              id TEXT PRIMARY KEY, job_id TEXT NOT NULL, clip_id TEXT NOT NULL,
              filename TEXT NOT NULL, path TEXT NOT NULL, title TEXT,
              description TEXT, hashtags TEXT, campaign_id TEXT,
              score REAL DEFAULT 0, created_at REAL NOT NULL,
              UNIQUE(job_id, clip_id)
            );
            CREATE TABLE IF NOT EXISTS publish_attempts (
              id TEXT PRIMARY KEY, job_id TEXT NOT NULL, clip_id TEXT NOT NULL,
              platform TEXT NOT NULL, account_id TEXT, campaign_id TEXT,
              target_type TEXT, target_id TEXT, mode TEXT, state TEXT NOT NULL,
              scheduled_at REAL, created_at REAL NOT NULL, started_at REAL,
              completed_at REAL, url TEXT, external_id TEXT, error TEXT,
              message TEXT, request_json TEXT, result_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_attempt_due
              ON publish_attempts(state, scheduled_at);
            CREATE TABLE IF NOT EXISTS campaigns (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, routes_json TEXT NOT NULL,
              created_at REAL NOT NULL
            );
            """)

    def record_clip(self, job_id: str, clip: Any, path: str | Path,
                    campaign_id: str = "") -> None:
        with self._lock, self._connect() as db:
            db.execute("""INSERT INTO clips
              (id,job_id,clip_id,filename,path,title,description,hashtags,campaign_id,score,created_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(job_id,clip_id) DO UPDATE SET
              title=excluded.title,description=excluded.description,
              hashtags=excluded.hashtags,campaign_id=excluded.campaign_id""",
              (f"{job_id}:{clip.id}", job_id, clip.id, clip.filename, str(path),
               clip.title, clip.description, json.dumps(clip.hashtags), campaign_id,
               clip.score, time.time()))

    def sync_clip(self, job_id: str, clip: Any) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE clips SET title=?,description=?,hashtags=? WHERE job_id=? AND clip_id=?",
                       (clip.title, clip.description, json.dumps(clip.hashtags), job_id, clip.id))

    def create_attempt(self, *, job_id: str, clip_id: str, platform: str,
                       request: dict[str, Any], scheduled_at: float,
                       state: str, account_id: str = "", campaign_id: str = "",
                       target_type: str = "", target_id: str = "", mode: str = "auto") -> str:
        attempt_id = uuid.uuid4().hex[:16]
        with self._lock, self._connect() as db:
            db.execute("""INSERT INTO publish_attempts
              (id,job_id,clip_id,platform,account_id,campaign_id,target_type,target_id,
               mode,state,scheduled_at,created_at,request_json)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (attempt_id, job_id, clip_id, platform, account_id, campaign_id,
               target_type, target_id, mode, state, scheduled_at, time.time(),
               json.dumps(request)))
        return attempt_id

    def update_attempt(self, attempt_id: str, **fields: Any) -> None:
        # ``request_json`` is writable so an approved attempt can be re-queued with an
        # amended request (specifically mode="auto"); without that, re-running a
        # review_required attempt would replay mode="review" and park it right back in
        # review forever.
        allowed = {"state","started_at","completed_at","url","external_id","error",
                   "message","result_json","scheduled_at","request_json"}
        data = {k: (json.dumps(v) if k in ("result_json","request_json") and not isinstance(v, str) else v)
                for k,v in fields.items() if k in allowed}
        if not data: return
        with self._lock, self._connect() as db:
            db.execute(f"UPDATE publish_attempts SET {','.join(f'{k}=?' for k in data)} WHERE id=?",
                       (*data.values(), attempt_id))

    def due_attempts(self, now: Optional[float] = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("""SELECT * FROM publish_attempts
              WHERE state IN ('queued','scheduled') AND scheduled_at<=?
              ORDER BY scheduled_at,created_at""", (now or time.time(),)).fetchall()
        return [self._row(r) for r in rows]

    def history(self, limit: int = 200, platform: str = "") -> dict[str, Any]:
        with self._connect() as db:
            clips = db.execute("SELECT * FROM clips ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            if platform:
                attempts = db.execute("SELECT * FROM publish_attempts WHERE platform=? ORDER BY created_at DESC LIMIT ?", (platform,limit)).fetchall()
            else:
                attempts = db.execute("SELECT * FROM publish_attempts ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return {"clips": [self._row(r) for r in clips],
                "publish_attempts": [self._row(r) for r in attempts]}

    def get_attempt(self, attempt_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM publish_attempts WHERE id=?", (attempt_id,)).fetchone()
        return self._row(row) if row else None

    def save_campaign(self, name: str, routes: dict[str, dict[str, str]], campaign_id: str = "") -> Campaign:
        item = Campaign(campaign_id or uuid.uuid4().hex[:12], name, routes)
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO campaigns VALUES(?,?,?,?)",
                       (item.id,item.name,json.dumps(item.routes),item.created_at))
        return item

    def campaigns(self) -> list[Campaign]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM campaigns ORDER BY created_at DESC").fetchall()
        return [Campaign(r["id"],r["name"],json.loads(r["routes_json"]),r["created_at"]) for r in rows]

    def campaign(self, campaign_id: str) -> Optional[Campaign]:
        with self._connect() as db:
            r = db.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        return Campaign(r["id"],r["name"],json.loads(r["routes_json"]),r["created_at"]) if r else None

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in ("hashtags","request_json","result_json"):
            if key in data and data[key]:
                try: data[key] = json.loads(data[key])
                except (TypeError, json.JSONDecodeError): pass
        return data


_store: Optional[HistoryStore] = None
_lock = threading.Lock()
def get_history() -> HistoryStore:
    global _store
    with _lock:
        if _store is None: _store = HistoryStore()
        return _store
