"""SQLite-backed users and sessions (U12).

Follows the storage conventions already in the repo rather than inventing new ones: a
context-managed connection per operation with ``check_same_thread=False`` and a module-level
``RLock`` (as :mod:`worker.job_persistence` does), WAL journalling, and schema created
ad-hoc with ``CREATE TABLE IF NOT EXISTS`` plus a ``PRAGMA table_info`` guard for later
columns (as :mod:`publishers.history` does). There is no migration framework to hook into.

**Session tokens are stored hashed.** The table holds ``sha256(token)``, never the token
itself, so a copy of ``users.db`` does not hand over a set of live sessions - the same
reason password hashes are not reversible. The token exists only in the cookie. sha256 with
no salt or stretching is right here and would be wrong for a password: a 256-bit random
token has no guessable structure to attack, so the only property needed is that the digest
is one-way, and a slow KDF would be run on every authenticated request for nothing.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from auth.passwords import hash_password, needs_rehash, verify_password
from config import settings

#: Length of a session token, in random bytes before base64. 32 bytes = 256 bits.
TOKEN_BYTES = 32

#: Usernames are compared case-insensitively and stored lowercased. Two accounts differing
#: only in case is a phishing surface inside your own tool, and every user who typed their
#: name with a capital would appear to have no account.
MAX_USERNAME_LENGTH = 64


@dataclass(frozen=True)
class User:
    """A user account. Never carries the password hash outside the store."""

    id: str
    username: str
    is_admin: bool = False
    created_at: float = 0.0
    disabled: bool = False

    def to_dict(self) -> dict:
        """The public view. There is no private view - the hash is not on this object."""
        return {
            "id": self.id,
            "username": self.username,
            "is_admin": self.is_admin,
            "created_at": self.created_at,
            "disabled": self.disabled,
        }


@dataclass(frozen=True)
class Session:
    """A live session. ``token`` is populated only when the session is created."""

    id: str
    user_id: str
    created_at: float
    expires_at: float
    token: str = ""


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalise_username(username: str) -> str:
    """Trim and lowercase a username, or raise ``ValueError`` if it is unusable."""
    cleaned = (username or "").strip().lower()
    if not cleaned:
        raise ValueError("A username is required.")
    if len(cleaned) > MAX_USERNAME_LENGTH:
        raise ValueError(f"Username is too long (limit {MAX_USERNAME_LENGTH} characters).")
    return cleaned


class AuthStore:
    """Users and sessions in one SQLite database."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or settings.users_db)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    disabled INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL
                )
                """
            )
            # Looked up on every authenticated request, and swept by expiry.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")

    # ------------------------------------------------------------------ users
    def create_user(self, username: str, password: str, *, is_admin: bool = False) -> User:
        """Create a user. Raises ``ValueError`` for a bad name, duplicate, or weak password."""
        name = normalise_username(username)
        # Hashed before the INSERT so a rejected password costs no database work and, more
        # importantly, so a weak password can never be written and then cleaned up.
        digest = hash_password(password)
        user = User(
            id=uuid.uuid4().hex[:16],
            username=name,
            is_admin=bool(is_admin),
            created_at=time.time(),
        )
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO users (id, username, password_hash, is_admin, disabled, "
                    "created_at) VALUES (?, ?, ?, ?, 0, ?)",
                    (user.id, user.username, digest, int(user.is_admin), user.created_at),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"A user named {name!r} already exists.") from exc
        return user

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> User:
        return User(
            id=row["id"],
            username=row["username"],
            is_admin=bool(row["is_admin"]),
            created_at=float(row["created_at"]),
            disabled=bool(row["disabled"]),
        )

    def get_user(self, user_id: str) -> User | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._row_to_user(row) if row else None

    def get_user_by_name(self, username: str) -> User | None:
        try:
            name = normalise_username(username)
        except ValueError:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()
        return self._row_to_user(row) if row else None

    def list_users(self) -> list[User]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
        return [self._row_to_user(row) for row in rows]

    def count_users(self) -> int:
        with self._lock, self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])

    def set_password(self, user_id: str, password: str) -> bool:
        digest = hash_password(password)
        with self._lock, self._connect() as conn:
            cur = conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (digest, user_id))
            return cur.rowcount > 0

    def set_disabled(self, user_id: str, disabled: bool) -> bool:
        """Disable or re-enable an account, ending its sessions when disabling.

        Leaving the sessions alive would mean a disabled account keeps working until its
        cookie expires, which is not what anyone means by disabling an account.
        """
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE users SET disabled = ? WHERE id = ?", (int(disabled), user_id)
            )
            if disabled:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            return cur.rowcount > 0

    def authenticate(self, username: str, password: str) -> User | None:
        """Return the user when the password is right and the account is usable.

        A missing user, a wrong password and a disabled account are all one ``None`` to the
        caller. The login endpoint must not distinguish them: "no such user" confirms which
        usernames exist, which is the first half of a credential-stuffing run.

        A password is still verified against a *dummy* hash when the user does not exist, so
        the request costs the same either way. Without it, a missing user returns in
        microseconds and a real one in tens of milliseconds - a timing oracle for valid
        usernames that no amount of identical wording hides.
        """
        user = self.get_user_by_name(username)
        if user is None:
            verify_password(password or "", _DUMMY_HASH)
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT password_hash FROM users WHERE id = ?", (user.id,)
            ).fetchone()
        stored = row["password_hash"] if row else _DUMMY_HASH
        if not verify_password(password, stored):
            return None
        if user.disabled:
            return None
        # Upgrade the stored hash opportunistically: this is the only moment the plaintext
        # is in hand, so a cost increase can only ever take effect here.
        if needs_rehash(stored):
            try:
                self.set_password(user.id, password)
            except ValueError:
                # The password is valid but no longer meets the current policy (the floor
                # was raised after the account was made). Not a reason to refuse a correct
                # login - the account simply keeps its old hash.
                pass
        return user

    # --------------------------------------------------------------- sessions
    def create_session(self, user_id: str, *, ttl_seconds: float | None = None) -> Session:
        """Issue a session for ``user_id``. The token is returned once and never stored."""
        ttl = float(
            ttl_seconds if ttl_seconds is not None else settings.auth_session_ttl_hours * 3600
        )
        token = secrets.token_urlsafe(TOKEN_BYTES)
        now = time.time()
        session = Session(
            id=uuid.uuid4().hex[:16],
            user_id=user_id,
            created_at=now,
            expires_at=now + ttl,
            token=token,
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (id, token_hash, user_id, created_at, expires_at, "
                "last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session.id,
                    _token_digest(token),
                    user_id,
                    now,
                    session.expires_at,
                    now,
                ),
            )
        return session

    def resolve_session(self, token: str) -> tuple[User, Session] | None:
        """The user and session for ``token``, or ``None``.

        Expiry is enforced **here**, on read, not only by the sweeper: a session must stop
        working the moment it expires whether or not anything has swept lately. An expired
        row is deleted as it is found, so the sweeper is an optimisation rather than the
        mechanism.

        A session belonging to a disabled or deleted user resolves to ``None`` for the same
        reason - the check has to be against current state, not against what was true when
        the cookie was issued.
        """
        if not token:
            return None
        digest = _token_digest(token)
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE token_hash = ?", (digest,)).fetchone()
            if row is None:
                return None
            if float(row["expires_at"]) <= now:
                conn.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
                return None
            user_row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (row["user_id"],)
            ).fetchone()
            if user_row is None or bool(user_row["disabled"]):
                return None
            conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (now, row["id"]))
            session = Session(
                id=row["id"],
                user_id=row["user_id"],
                created_at=float(row["created_at"]),
                expires_at=float(row["expires_at"]),
            )
            return self._row_to_user(user_row), session

    def delete_session(self, token: str) -> bool:
        """End one session (logout). Idempotent."""
        if not token:
            return False
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_digest(token),))
            return cur.rowcount > 0

    def delete_sessions_for_user(self, user_id: str) -> int:
        with self._lock, self._connect() as conn:
            return int(conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,)).rowcount)

    def purge_expired_sessions(self) -> int:
        with self._lock, self._connect() as conn:
            return int(
                conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),)).rowcount
            )


#: A real hash of an unguessable value, used to spend the same CPU on a login for a user
#: that does not exist as on one that does. Built at import so the cost is not paid here.
_DUMMY_HASH = hash_password("x" * 24)

_store: AuthStore | None = None
_store_lock = threading.Lock()


def get_auth_store() -> AuthStore:
    """The process-wide store, created on first use."""
    global _store
    with _store_lock:
        if _store is None:
            _store = AuthStore()
        return _store


def reset_auth_store() -> None:
    """Drop the cached store so the next call re-reads ``settings.users_db``.

    Exists for tests, which point ``users_db`` at a temporary file *after* this module has
    been imported. Without it the first test to touch auth would fix the path for the whole
    session.
    """
    global _store
    with _store_lock:
        _store = None
