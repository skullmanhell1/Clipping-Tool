"""SQLite-backed clip, campaign, and publish-attempt history."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from config import settings
from worker.job_persistence import _try_wal, describe_store_failure


@dataclass
class Campaign:
    id: str
    name: str
    routes: dict[str, dict[str, str]] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HistoryStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or settings.history_db)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    @contextmanager
    def _connect(self):
        """Yield a connection, committing on success and **closing** either way.

        ``with sqlite3.connect(...)`` manages the *transaction*, not the connection: it
        commits or rolls back but never closes. Every call site here uses ``with``, so
        connections were left for the garbage collector and descriptors accumulated in
        the meantime. The inner ``with conn`` keeps the commit/rollback behaviour the
        call sites depend on, so none of them change.
        """
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init(self) -> None:
        """Create the schema, translating a filesystem failure into a message that names it.

        This is the frame the reported crash came from, twice. SQLite's ``attempt to write a
        readonly database`` names neither the file nor the directory, and the directory is the usual
        culprit -- so the same sentence has now stood for two different causes (a WAL sidecar it
        could not create, and a mount the container's UID cannot write). Re-raised as the same type
        so existing callers and tests are unaffected; only the wording changes.
        """
        try:
            self._create_schema()
        except sqlite3.OperationalError as exc:
            raise sqlite3.OperationalError(
                describe_store_failure(self.path, "publish history", exc)
            ) from exc

    def _create_schema(self) -> None:
        with self._connect() as db:
            _try_wal(db, "publish history")
            db.executescript("""
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
              message TEXT, request_json TEXT, result_json TEXT,
              -- PB5: how many automatic retries this attempt has already consumed.
              retry_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_attempt_due
              ON publish_attempts(state, scheduled_at);
            CREATE TABLE IF NOT EXISTS campaigns (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, routes_json TEXT NOT NULL,
              created_at REAL NOT NULL
            );
            -- PB4: cached OAuth access tokens with their expiry.
            --
            -- Separate from `settings`, which holds the long-lived *refresh* credential an
            -- operator configures. This table holds the short-lived access token derived from
            -- it, which the process must not lose on restart and must not re-mint per upload.
            CREATE TABLE IF NOT EXISTS oauth_tokens (
              platform TEXT NOT NULL, account_id TEXT NOT NULL DEFAULT '',
              access_token TEXT NOT NULL, expires_at REAL, refreshed_at REAL NOT NULL,
              PRIMARY KEY (platform, account_id)
            );
            """)
            # Migration for databases created before PB5 added the column. `ALTER TABLE ... ADD
            # COLUMN` has no `IF NOT EXISTS` in SQLite, so existing columns are checked first;
            # an unconditional ALTER would raise on every start after the first.
            existing = {
                row["name"] for row in db.execute("PRAGMA table_info(publish_attempts)").fetchall()
            }
            if "retry_count" not in existing:
                db.execute(
                    "ALTER TABLE publish_attempts ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"
                )

    def record_clip(self, job_id: str, clip: Any, path: str | Path, campaign_id: str = "") -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO clips
              (id,job_id,clip_id,filename,path,title,description,hashtags,campaign_id,score,created_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(job_id,clip_id) DO UPDATE SET
              title=excluded.title,description=excluded.description,
              hashtags=excluded.hashtags,campaign_id=excluded.campaign_id""",
                (
                    f"{job_id}:{clip.id}",
                    job_id,
                    clip.id,
                    clip.filename,
                    str(path),
                    clip.title,
                    clip.description,
                    json.dumps(clip.hashtags),
                    campaign_id,
                    clip.score,
                    time.time(),
                ),
            )

    def sync_clip(self, job_id: str, clip: Any) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE clips SET title=?,description=?,hashtags=? WHERE job_id=? AND clip_id=?",
                (clip.title, clip.description, json.dumps(clip.hashtags), job_id, clip.id),
            )

    def create_attempt(
        self,
        *,
        job_id: str,
        clip_id: str,
        platform: str,
        request: dict[str, Any],
        scheduled_at: float,
        state: str,
        account_id: str = "",
        campaign_id: str = "",
        target_type: str = "",
        target_id: str = "",
        mode: str = "auto",
    ) -> str:
        attempt_id = uuid.uuid4().hex[:16]
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO publish_attempts
              (id,job_id,clip_id,platform,account_id,campaign_id,target_type,target_id,
               mode,state,scheduled_at,created_at,request_json)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    attempt_id,
                    job_id,
                    clip_id,
                    platform,
                    account_id,
                    campaign_id,
                    target_type,
                    target_id,
                    mode,
                    state,
                    scheduled_at,
                    time.time(),
                    json.dumps(request),
                ),
            )
        return attempt_id

    def update_attempt(self, attempt_id: str, **fields: Any) -> None:
        # ``request_json`` is writable so an approved attempt can be re-queued with an
        # amended request (specifically mode="auto"); without that, re-running a
        # review_required attempt would replay mode="review" and park it right back in
        # review forever.
        # ``retry_count`` is writable so the scheduler can record automatic retries (PB5).
        allowed = {
            "state",
            "started_at",
            "completed_at",
            "url",
            "external_id",
            "error",
            "message",
            "result_json",
            "scheduled_at",
            "request_json",
            "retry_count",
        }
        data = {
            k: (
                json.dumps(v)
                if k in ("result_json", "request_json") and not isinstance(v, str)
                else v
            )
            for k, v in fields.items()
            if k in allowed
        }
        if not data:
            return
        with self._lock, self._connect() as db:
            db.execute(
                f"UPDATE publish_attempts SET {','.join(f'{k}=?' for k in data)} WHERE id=?",  # noqa: S608 - column names come from the `allowed` set above; values are parameterised
                (*data.values(), attempt_id),
            )

    def due_attempts(self, now: float | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM publish_attempts
              WHERE state IN ('queued','scheduled') AND scheduled_at<=?
              ORDER BY scheduled_at,created_at""",
                (now or time.time(),),
            ).fetchall()
        return [self._row(r) for r in rows]

    def history(self, limit: int = 200, platform: str = "") -> dict[str, Any]:
        with self._connect() as db:
            clips = db.execute(
                "SELECT * FROM clips ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            if platform:
                attempts = db.execute(
                    "SELECT * FROM publish_attempts WHERE platform=? ORDER BY created_at DESC LIMIT ?",
                    (platform, limit),
                ).fetchall()
            else:
                attempts = db.execute(
                    "SELECT * FROM publish_attempts ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return {
            "clips": [self._row(r) for r in clips],
            "publish_attempts": [self._row(r) for r in attempts],
        }

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM publish_attempts WHERE id=?", (attempt_id,)).fetchone()
        return self._row(row) if row else None

    # ---------------------------------------------------------------- tokens --
    def get_token(self, platform: str, account_id: str = "") -> dict[str, Any] | None:
        """The cached access token for ``platform``/``account_id``, if any (PB4)."""
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM oauth_tokens WHERE platform=? AND account_id=?",
                (platform, account_id or ""),
            ).fetchone()
        return dict(row) if row else None

    def save_token(
        self,
        platform: str,
        access_token: str,
        *,
        account_id: str = "",
        expires_at: float | None = None,
    ) -> None:
        """Store (or replace) the cached access token for a platform (PB4)."""
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO oauth_tokens"
                "(platform,account_id,access_token,expires_at,refreshed_at) VALUES(?,?,?,?,?)",
                (platform, account_id or "", access_token, expires_at, time.time()),
            )

    def clear_token(self, platform: str, account_id: str = "") -> None:
        """Forget a cached token, so the next publish mints a fresh one (PB4)."""
        with self._lock, self._connect() as db:
            db.execute(
                "DELETE FROM oauth_tokens WHERE platform=? AND account_id=?",
                (platform, account_id or ""),
            )

    def scheduled_between(self, start: float, end: float) -> list[dict[str, Any]]:
        """Attempts scheduled within ``[start, end]``, for the calendar view (PB7).

        Includes every state rather than only pending ones: a calendar that hid what had already
        happened would show an operator an empty week they had in fact filled, and "what did I
        post on Tuesday" is the same question as "what am I posting on Thursday".
        """
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM publish_attempts WHERE scheduled_at BETWEEN ? AND ? "
                "ORDER BY scheduled_at, created_at",
                (start, end),
            ).fetchall()
        return [self._row(r) for r in rows]

    def save_campaign(
        self, name: str, routes: dict[str, dict[str, str]], campaign_id: str = ""
    ) -> Campaign:
        item = Campaign(campaign_id or uuid.uuid4().hex[:12], name, routes)
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO campaigns VALUES(?,?,?,?)",
                (item.id, item.name, json.dumps(item.routes), item.created_at),
            )
        return item

    def campaigns(self) -> list[Campaign]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM campaigns ORDER BY created_at DESC").fetchall()
        return [
            Campaign(r["id"], r["name"], json.loads(r["routes_json"]), r["created_at"])
            for r in rows
        ]

    def campaign(self, campaign_id: str) -> Campaign | None:
        with self._connect() as db:
            r = db.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        return (
            Campaign(r["id"], r["name"], json.loads(r["routes_json"]), r["created_at"])
            if r
            else None
        )

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in ("hashtags", "request_json", "result_json"):
            if key in data and data[key]:
                try:
                    data[key] = json.loads(data[key])
                except (TypeError, json.JSONDecodeError):
                    pass
        return data


_store: HistoryStore | None = None
_lock = threading.Lock()


def get_history() -> HistoryStore:
    global _store
    with _lock:
        if _store is None:
            _store = HistoryStore()
        return _store
