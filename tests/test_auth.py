"""U12: multi-user authentication and per-user ownership.

The abuse cases are the tests. Every section below is written from "how would I get at
someone else's clips" rather than from "does login work", because login working is the easy
half and is not where the risk is.

The load-bearing test in this file is
:func:`test_every_job_scoped_route_refuses_a_stranger`, which enumerates the application's
own routes rather than listing them by hand. Authorization spread over forty handlers holds
until somebody adds the forty-first, and a per-endpoint test suite cannot fail for an
endpoint nobody wrote a test for.
"""

from __future__ import annotations

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from api.main import app
from api.security import (
    EXEMPT_EXACT,
    extract_token,
    is_exempt,
    login_rate_limiter,
    may_access_job,
)
from auth import get_auth_store, reset_auth_store
from auth.passwords import (
    MIN_PASSWORD_LENGTH,
    PasswordError,
    hash_password,
    needs_rehash,
    verify_password,
)
from auth.store import AuthStore, normalise_username
from config import settings as app_settings
from worker.jobs import get_manager
from worker.models import ClipResult, Job, JobStatus, ProcessingOptions

GOOD_PASSWORD = "correct-horse-battery-staple"
OTHER_PASSWORD = "a-different-long-password"


# --------------------------------------------------------------------------- #
# Password hashing
# --------------------------------------------------------------------------- #
def test_a_hash_does_not_contain_the_password():
    stored = hash_password(GOOD_PASSWORD)
    assert GOOD_PASSWORD not in stored
    assert stored.startswith("scrypt$")


def test_the_right_password_verifies():
    assert verify_password(GOOD_PASSWORD, hash_password(GOOD_PASSWORD)) is True


def test_a_wrong_password_does_not():
    assert verify_password(OTHER_PASSWORD, hash_password(GOOD_PASSWORD)) is False


def test_the_same_password_hashes_differently_every_time():
    """A per-hash salt, so identical passwords are not identical rows."""
    assert hash_password(GOOD_PASSWORD) != hash_password(GOOD_PASSWORD)


def test_parameters_are_recorded_in_the_hash():
    """So raising the cost later does not invalidate every existing password."""
    stored = hash_password(GOOD_PASSWORD, n=2**12)
    assert stored.split("$")[1] == str(2**12)
    # Still verifiable even though the current default is higher. This is the property that
    # makes raising the cost safe: without per-hash parameters, every existing password would
    # stop verifying the moment the default moved.
    assert verify_password(GOOD_PASSWORD, stored) is True
    assert needs_rehash(stored) is True


def test_a_current_hash_does_not_need_rehashing():
    assert needs_rehash(hash_password(GOOD_PASSWORD)) is False


@pytest.mark.parametrize(
    "corrupt",
    ["", "nonsense", "scrypt$x$8$1$aa$bb", "bcrypt$1$2$3$aa$bb", "scrypt$16384$8$1$zz$yy"],
)
def test_a_corrupt_hash_never_verifies_and_never_raises(corrupt):
    """An unreadable row must not be distinguishable from a wrong password."""
    assert verify_password(GOOD_PASSWORD, corrupt) is False
    assert needs_rehash(corrupt) is True


def test_a_short_password_is_refused_when_set():
    with pytest.raises(PasswordError):
        hash_password("x" * (MIN_PASSWORD_LENGTH - 1))


def test_an_absurdly_long_password_is_refused_rather_than_hashed():
    with pytest.raises(PasswordError):
        hash_password("x" * 5000)


def test_an_absurdly_long_password_does_not_verify():
    # Guards the other direction: the length cap must not be a way to bypass the comparison.
    assert verify_password("x" * 5000, hash_password(GOOD_PASSWORD)) is False


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path):
    return AuthStore(tmp_path / "users.db")


def test_usernames_are_case_insensitive(store):
    """Two accounts differing only in case is a phishing surface inside your own tool."""
    store.create_user("Alice", GOOD_PASSWORD)
    assert store.get_user_by_name("alice") is not None
    assert store.get_user_by_name("ALICE") is not None
    with pytest.raises(ValueError):
        store.create_user("ALICE", GOOD_PASSWORD)


def test_a_blank_username_is_refused():
    with pytest.raises(ValueError):
        normalise_username("   ")


def test_authenticate_accepts_the_right_password(store):
    user = store.create_user("bob", GOOD_PASSWORD)
    assert store.authenticate("bob", GOOD_PASSWORD).id == user.id


@pytest.mark.parametrize(
    "username,password",
    [("bob", OTHER_PASSWORD), ("nobody", GOOD_PASSWORD), ("bob", "")],
)
def test_authenticate_refuses_everything_else(store, username, password):
    store.create_user("bob", GOOD_PASSWORD)
    assert store.authenticate(username, password) is None


def test_a_disabled_account_cannot_authenticate(store):
    user = store.create_user("bob", GOOD_PASSWORD)
    store.set_disabled(user.id, True)
    assert store.authenticate("bob", GOOD_PASSWORD) is None


def test_disabling_an_account_ends_its_sessions(store):
    """Otherwise a disabled account keeps working until its cookie expires."""
    user = store.create_user("bob", GOOD_PASSWORD)
    session = store.create_session(user.id)
    assert store.resolve_session(session.token) is not None
    store.set_disabled(user.id, True)
    assert store.resolve_session(session.token) is None


# Disabling relies on two independent layers, and the test above passes if *either* works -
# which means neither is actually pinned by it. They are both kept deliberately: proactive
# revocation is what makes disabling immediate, and the check on read is what covers a user
# disabled by any route that is not `set_disabled` (an operator editing the row by hand, most
# obviously). So each layer gets its own test that the other cannot satisfy.

def test_disabling_deletes_the_session_rows(store):
    """Layer one, asserted at the database rather than through resolve_session."""
    user = store.create_user("bob", GOOD_PASSWORD)
    store.create_session(user.id)
    store.create_session(user.id)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
    store.set_disabled(user.id, True)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_a_session_stops_resolving_when_the_user_row_is_disabled_directly(store):
    """Layer two, with `set_disabled` deliberately bypassed.

    An operator flipping the column by hand, or any future code path that does, must not
    leave a working session behind. Written against the database precisely so it cannot be
    satisfied by the revocation in `set_disabled`.
    """
    user = store.create_user("bob", GOOD_PASSWORD)
    session = store.create_session(user.id)
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE users SET disabled = 1 WHERE id = ?", (user.id,))
    # The session row is still there...
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    # ...and must not resolve anyway.
    assert store.resolve_session(session.token) is None


def test_a_session_resolves_to_its_user(store):
    user = store.create_user("bob", GOOD_PASSWORD)
    session = store.create_session(user.id)
    resolved = store.resolve_session(session.token)
    assert resolved is not None
    assert resolved[0].id == user.id


def test_an_expired_session_does_not_resolve(store):
    """Enforced on read, so shortening the TTL takes effect without waiting for a sweep."""
    user = store.create_user("bob", GOOD_PASSWORD)
    session = store.create_session(user.id, ttl_seconds=-1)
    assert store.resolve_session(session.token) is None


def test_an_expired_session_row_is_deleted_when_it_is_read(store):
    """So the sweeper is an optimisation rather than the mechanism."""
    user = store.create_user("bob", GOOD_PASSWORD)
    session = store.create_session(user.id, ttl_seconds=-1)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    store.resolve_session(session.token)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_the_sweeper_removes_expired_sessions(store):
    user = store.create_user("bob", GOOD_PASSWORD)
    store.create_session(user.id, ttl_seconds=-1)
    live = store.create_session(user.id)
    assert store.purge_expired_sessions() == 1
    assert store.resolve_session(live.token) is not None


def test_logout_makes_the_token_stop_working(store):
    """The row is deleted, not merely un-cookied: a captured token must die too."""
    user = store.create_user("bob", GOOD_PASSWORD)
    session = store.create_session(user.id)
    assert store.delete_session(session.token) is True
    assert store.resolve_session(session.token) is None


def test_an_unknown_token_resolves_to_nothing(store):
    assert store.resolve_session("not-a-real-token") is None
    assert store.resolve_session("") is None


def test_the_session_token_is_not_stored_anywhere_in_the_database(store):
    """A copy of users.db must not hand over a set of live sessions."""
    user = store.create_user("bob", GOOD_PASSWORD)
    session = store.create_session(user.id)
    raw = store.path.read_bytes()
    assert session.token.encode() not in raw
    # And the digest that *is* stored does not resolve as if it were the token.
    with sqlite3.connect(store.path) as conn:
        stored_hash = conn.execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert store.resolve_session(stored_hash) is None


def test_a_user_object_never_carries_the_password_hash(store):
    user = store.create_user("bob", GOOD_PASSWORD)
    assert "password" not in user.to_dict()
    assert not hasattr(user, "password_hash")


def test_changing_a_password_invalidates_the_old_one(store):
    user = store.create_user("bob", GOOD_PASSWORD)
    store.set_password(user.id, OTHER_PASSWORD)
    assert store.authenticate("bob", GOOD_PASSWORD) is None
    assert store.authenticate("bob", OTHER_PASSWORD) is not None


def test_a_weak_password_is_refused_on_change(store):
    user = store.create_user("bob", GOOD_PASSWORD)
    with pytest.raises(PasswordError):
        store.set_password(user.id, "short")
    # And the old password still works, so a rejected change is not a lockout.
    assert store.authenticate("bob", GOOD_PASSWORD) is not None


def test_a_weak_stored_hash_is_upgraded_on_successful_login(store):
    """The only moment the plaintext is in hand, so the only moment a cost rise can apply."""
    user = store.create_user("bob", GOOD_PASSWORD)
    store_weak = hash_password(GOOD_PASSWORD, n=2**12)
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?", (store_weak, user.id)
        )
    assert store.authenticate("bob", GOOD_PASSWORD) is not None
    with sqlite3.connect(store.path) as conn:
        after = conn.execute(
            "SELECT password_hash FROM users WHERE id = ?", (user.id,)
        ).fetchone()[0]
    assert needs_rehash(after) is False


def test_the_schema_is_created_on_a_fresh_file(tmp_path):
    path = tmp_path / "nested" / "users.db"
    AuthStore(path)
    assert path.is_file()


def test_opening_an_existing_database_twice_is_safe(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` plus the index guards must be idempotent."""
    path = tmp_path / "users.db"
    first = AuthStore(path)
    user = first.create_user("bob", GOOD_PASSWORD)
    second = AuthStore(path)
    assert second.get_user(user.id) is not None


# --------------------------------------------------------------------------- #
# Exempt paths
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", sorted(EXEMPT_EXACT))
def test_exempt_paths_are_exempt(path):
    assert is_exempt(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/api/jobs",
        "/api/info",
        "/api/jobs/abc",
        "/clips/abc/clip.mp4",
        "/docs",
        "/openapi.json",
        "/api/users",
        "/api/storage",
    ],
)
def test_these_paths_are_not_exempt(path):
    """Pinned individually: an accidental prefix in EXEMPT_PREFIXES would open a hole
    silently, and `/api/info` in particular enumerates configured providers."""
    assert is_exempt(path) is False


def test_the_assets_prefix_is_exempt_but_not_a_lookalike():
    assert is_exempt("/assets/index-abc123.js") is True
    assert is_exempt("/assets-private/secret") is False


# --------------------------------------------------------------------------- #
# Token extraction
# --------------------------------------------------------------------------- #
def _headers(**kwargs):
    from starlette.datastructures import Headers

    return Headers(kwargs)


def test_a_cookie_is_read(monkeypatch):
    monkeypatch.setattr(app_settings, "auth_session_cookie", "clipper_session")
    assert extract_token(_headers(cookie="clipper_session=abc123")) == "abc123"


def test_the_right_cookie_is_read_among_several(monkeypatch):
    monkeypatch.setattr(app_settings, "auth_session_cookie", "clipper_session")
    header = "other=1; clipper_session=abc123; another=2"
    assert extract_token(_headers(cookie=header)) == "abc123"


def test_a_bearer_header_is_read():
    """For scripts and the CLI, which have no cookie jar."""
    assert extract_token(_headers(authorization="Bearer tok123")) == "tok123"
    assert extract_token(_headers(authorization="bearer tok123")) == "tok123"


def test_a_non_bearer_authorization_header_is_ignored():
    assert extract_token(_headers(authorization="Basic dXNlcjpwYXNz")) == ""


def test_no_credentials_is_an_empty_token():
    assert extract_token(_headers()) == ""


# --------------------------------------------------------------------------- #
# may_access_job
# --------------------------------------------------------------------------- #
class _FakeUser:
    def __init__(self, uid, is_admin=False):
        self.id = uid
        self.is_admin = is_admin


def test_the_owner_may_access_their_job():
    job = Job(input_type="file", source="s.mp4", options=ProcessingOptions(), owner="u1")
    assert may_access_job(job, _FakeUser("u1")) is True


def test_a_stranger_may_not():
    job = Job(input_type="file", source="s.mp4", options=ProcessingOptions(), owner="u1")
    assert may_access_job(job, _FakeUser("u2")) is False


def test_an_admin_may_access_anything():
    job = Job(input_type="file", source="s.mp4", options=ProcessingOptions(), owner="u1")
    assert may_access_job(job, _FakeUser("admin", is_admin=True)) is True


def test_an_unowned_job_is_admin_only(monkeypatch):
    """Pre-U12 jobs have no owner. Treating unowned as public would hand a stranger's whole
    library to the first account created after enabling auth."""
    monkeypatch.setattr(app_settings, "auth_enabled", True)
    job = Job(input_type="file", source="s.mp4", options=ProcessingOptions(), owner="")
    assert may_access_job(job, _FakeUser("u1")) is False
    assert may_access_job(job, _FakeUser("admin", is_admin=True)) is True


def test_a_missing_job_is_never_accessible():
    assert may_access_job(None, _FakeUser("u1")) is False
    assert may_access_job(None, _FakeUser("a", is_admin=True)) is False


# --------------------------------------------------------------------------- #
# HTTP: the middleware
# --------------------------------------------------------------------------- #
@pytest.fixture
def auth_on(tmp_path, monkeypatch):
    """Turn authentication on with an isolated user database."""
    monkeypatch.setattr(app_settings, "auth_enabled", True)
    monkeypatch.setattr(app_settings, "users_db", tmp_path / "users.db")
    monkeypatch.setattr(app_settings, "auth_session_cookie", "clipper_session")
    reset_auth_store()
    login_rate_limiter.reset()
    yield get_auth_store()
    # Dropped again so no later test inherits either the store or the flag.
    reset_auth_store()
    login_rate_limiter.reset()


def _job_for(owner: str) -> tuple[Job, ClipResult]:
    clip = ClipResult(
        id="clipA", filename="clipA.mp4", start=0.0, end=5.0, duration=5.0, title="t"
    )
    job = Job(
        input_type="file", source="s.mp4", options=ProcessingOptions(), owner=owner
    )
    job.clips = [clip]
    job.status = JobStatus.COMPLETED
    get_manager().store.add(job)
    clip_dir = app_settings.clips_dir / job.id
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / clip.filename).write_bytes(b"FAKEVIDEO")
    return job, clip


def _signed_in(store, username, password=GOOD_PASSWORD, is_admin=False):
    """A client with a live session for a new user."""
    user = store.create_user(username, password, is_admin=is_admin)
    client = TestClient(app)
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return user, client


def test_with_auth_off_nothing_changes():
    """The default path: no session, no 401, exactly the pre-U12 behaviour."""
    client = TestClient(app)
    assert client.get("/api/jobs").status_code == 200
    assert client.get("/api/info").status_code == 200


def test_with_auth_on_the_api_refuses_anonymous_callers(auth_on):
    client = TestClient(app)
    for path in ("/api/jobs", "/api/info", "/api/storage", "/api/publishers"):
        assert client.get(path).status_code == 401, path


def test_healthz_stays_open(auth_on):
    """A liveness probe has no credentials and must not need any."""
    assert TestClient(app).get("/healthz").status_code == 200


def test_the_auth_config_endpoint_stays_open(auth_on):
    """The SPA cannot otherwise tell 'auth is off' from 'auth is on and I am signed out'."""
    resp = TestClient(app).get("/api/auth/config")
    assert resp.status_code == 200
    assert resp.json() == {"auth_enabled": True}


def test_auth_config_reports_off_when_it_is_off():
    assert TestClient(app).get("/api/auth/config").json() == {"auth_enabled": False}


def test_a_signed_in_caller_is_served(auth_on):
    _user, client = _signed_in(auth_on, "alice")
    assert client.get("/api/jobs").status_code == 200


def test_a_bearer_token_works_without_a_cookie(auth_on):
    user = auth_on.create_user("alice", GOOD_PASSWORD)
    session = auth_on.create_session(user.id)
    client = TestClient(app)
    resp = client.get("/api/jobs", headers={"Authorization": f"Bearer {session.token}"})
    assert resp.status_code == 200


def test_a_garbage_token_is_refused(auth_on):
    client = TestClient(app)
    resp = client.get("/api/jobs", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_logging_out_clears_the_cookie(auth_on):
    _user, client = _signed_in(auth_on, "alice")
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/jobs").status_code == 401


def test_logging_out_revokes_the_token_itself_not_just_the_cookie(auth_on):
    """The distinction the previous test cannot make.

    After a logout the client has no cookie, so it would be refused whether or not the
    session was actually destroyed - which means that test passes even if logout only clears
    the cookie. A token someone captured does not live in the client's cookie jar, so it is
    replayed here explicitly: the session row has to be gone.
    """
    user = auth_on.create_user("alice", GOOD_PASSWORD)
    session = auth_on.create_session(user.id)
    client = TestClient(app)
    client.cookies.set("clipper_session", session.token)
    assert client.get("/api/jobs").status_code == 200

    assert client.post("/api/auth/logout").status_code == 200

    # Replayed as a bearer token, so the cleared cookie cannot be what refuses it.
    replay = TestClient(app)
    assert replay.get(
        "/api/jobs", headers={"Authorization": f"Bearer {session.token}"}
    ).status_code == 401
    assert auth_on.resolve_session(session.token) is None


def test_the_session_cookie_is_httponly_and_lax(auth_on):
    """httponly so an XSS bug cannot read the token; lax is the CSRF defence."""
    auth_on.create_user("alice", GOOD_PASSWORD)
    client = TestClient(app)
    resp = client.post(
        "/api/auth/login", json={"username": "alice", "password": GOOD_PASSWORD}
    )
    header = resp.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header


def test_the_cookie_is_not_secure_by_default(auth_on):
    """A Secure cookie is silently dropped over http, so a localhost install could not
    sign in. Opt in with AUTH_COOKIE_SECURE."""
    auth_on.create_user("alice", GOOD_PASSWORD)
    resp = TestClient(app).post(
        "/api/auth/login", json={"username": "alice", "password": GOOD_PASSWORD}
    )
    assert "secure" not in resp.headers["set-cookie"].lower()


def test_the_cookie_is_secure_when_configured(auth_on, monkeypatch):
    monkeypatch.setattr(app_settings, "auth_cookie_secure", True)
    auth_on.create_user("alice", GOOD_PASSWORD)
    resp = TestClient(app).post(
        "/api/auth/login", json={"username": "alice", "password": GOOD_PASSWORD}
    )
    assert "secure" in resp.headers["set-cookie"].lower()


def test_login_is_unavailable_when_auth_is_off():
    """Issuing a session nothing checks would show a signed-in state that means nothing."""
    resp = TestClient(app).post(
        "/api/auth/login", json={"username": "alice", "password": GOOD_PASSWORD}
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Login: enumeration and brute force
# --------------------------------------------------------------------------- #
def test_a_wrong_password_and_a_missing_user_are_indistinguishable(auth_on):
    """Different wording would confirm which usernames exist."""
    auth_on.create_user("alice", GOOD_PASSWORD)
    client = TestClient(app)
    wrong = client.post(
        "/api/auth/login", json={"username": "alice", "password": OTHER_PASSWORD}
    )
    missing = client.post(
        "/api/auth/login", json={"username": "nobody", "password": OTHER_PASSWORD}
    )
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json()["detail"] == missing.json()["detail"]


def test_a_disabled_account_is_also_indistinguishable(auth_on):
    user = auth_on.create_user("alice", GOOD_PASSWORD)
    auth_on.set_disabled(user.id, True)
    resp = TestClient(app).post(
        "/api/auth/login", json={"username": "alice", "password": GOOD_PASSWORD}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Incorrect username or password."


def test_repeated_failures_are_rate_limited(auth_on, monkeypatch):
    monkeypatch.setattr(app_settings, "auth_login_max_attempts", 3)
    auth_on.create_user("alice", GOOD_PASSWORD)
    client = TestClient(app)
    for _ in range(3):
        assert client.post(
            "/api/auth/login", json={"username": "alice", "password": OTHER_PASSWORD}
        ).status_code == 401
    blocked = client.post(
        "/api/auth/login", json={"username": "alice", "password": OTHER_PASSWORD}
    )
    assert blocked.status_code == 429


def test_the_rate_limit_blocks_the_right_password_too(auth_on, monkeypatch):
    """Otherwise the limit is trivially bypassed by a correct guess at the end of a run."""
    monkeypatch.setattr(app_settings, "auth_login_max_attempts", 2)
    auth_on.create_user("alice", GOOD_PASSWORD)
    client = TestClient(app)
    for _ in range(2):
        client.post("/api/auth/login", json={"username": "alice", "password": "wrong-pass"})
    resp = client.post(
        "/api/auth/login", json={"username": "alice", "password": GOOD_PASSWORD}
    )
    assert resp.status_code == 429


def test_a_successful_login_clears_the_failure_count(auth_on, monkeypatch):
    monkeypatch.setattr(app_settings, "auth_login_max_attempts", 3)
    auth_on.create_user("alice", GOOD_PASSWORD)
    client = TestClient(app)
    client.post("/api/auth/login", json={"username": "alice", "password": "wrong-pass"})
    client.post("/api/auth/login", json={"username": "alice", "password": GOOD_PASSWORD})
    for _ in range(3):
        assert client.post(
            "/api/auth/login", json={"username": "alice", "password": "wrong-pass"}
        ).status_code == 401


def test_the_rate_limit_window_expires(auth_on, monkeypatch):
    monkeypatch.setattr(app_settings, "auth_login_max_attempts", 1)
    monkeypatch.setattr(app_settings, "auth_login_window_seconds", 1)
    auth_on.create_user("alice", GOOD_PASSWORD)
    client = TestClient(app)
    client.post("/api/auth/login", json={"username": "alice", "password": "wrong-pass"})
    assert client.post(
        "/api/auth/login", json={"username": "alice", "password": "wrong-pass"}
    ).status_code == 429
    time.sleep(1.1)
    assert client.post(
        "/api/auth/login", json={"username": "alice", "password": "wrong-pass"}
    ).status_code == 401


def test_the_rate_limit_can_be_disabled(auth_on, monkeypatch):
    monkeypatch.setattr(app_settings, "auth_login_max_attempts", 0)
    auth_on.create_user("alice", GOOD_PASSWORD)
    client = TestClient(app)
    for _ in range(5):
        assert client.post(
            "/api/auth/login", json={"username": "alice", "password": "wrong-pass"}
        ).status_code == 401


# --------------------------------------------------------------------------- #
# Ownership: the abuse cases
# --------------------------------------------------------------------------- #
def test_a_job_list_shows_only_your_own(auth_on):
    alice, alice_client = _signed_in(auth_on, "alice")
    _bob, bob_client = _signed_in(auth_on, "bob")
    job, _clip = _job_for(alice.id)

    mine = alice_client.get("/api/jobs").json()["jobs"]
    theirs = bob_client.get("/api/jobs").json()["jobs"]
    assert job.id in [j["id"] for j in mine]
    assert job.id not in [j["id"] for j in theirs]


def test_an_admin_sees_every_job(auth_on):
    alice, _ = _signed_in(auth_on, "alice")
    job, _clip = _job_for(alice.id)
    _admin, admin_client = _signed_in(auth_on, "root", is_admin=True)
    assert job.id in [j["id"] for j in admin_client.get("/api/jobs").json()["jobs"]]


def test_fetching_someone_elses_job_is_a_404_not_a_403(auth_on):
    """A 403 confirms a guessed id is real, which makes job ids enumerable."""
    alice, _ = _signed_in(auth_on, "alice")
    _bob, bob_client = _signed_in(auth_on, "bob")
    job, _clip = _job_for(alice.id)
    resp = bob_client.get(f"/api/jobs/{job.id}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Job not found"
    # And identical to a job that genuinely does not exist.
    assert bob_client.get("/api/jobs/deadbeefdead").json()["detail"] == "Job not found"


def test_the_owner_can_still_fetch_their_job(auth_on):
    alice, alice_client = _signed_in(auth_on, "alice")
    job, _clip = _job_for(alice.id)
    assert alice_client.get(f"/api/jobs/{job.id}").status_code == 200


def test_clip_media_is_not_readable_by_another_user(auth_on):
    """The `/clips` StaticFiles mount is not a route, so route dependencies would miss it.
    This is the one that matters: the media *is* the product."""
    alice, alice_client = _signed_in(auth_on, "alice")
    _bob, bob_client = _signed_in(auth_on, "bob")
    job, clip = _job_for(alice.id)
    url = f"/clips/{job.id}/{clip.filename}"
    assert alice_client.get(url).status_code == 200
    assert bob_client.get(url).status_code == 404


def test_clip_media_is_not_readable_anonymously(auth_on):
    alice, _ = _signed_in(auth_on, "alice")
    job, clip = _job_for(alice.id)
    assert TestClient(app).get(f"/clips/{job.id}/{clip.filename}").status_code == 401


def test_clip_media_is_public_when_auth_is_off():
    """The default single-tenant behaviour: the mount serves as it always did."""
    job, clip = _job_for("")
    assert TestClient(app).get(f"/clips/{job.id}/{clip.filename}").status_code == 200


def test_media_with_no_job_record_is_refused(auth_on):
    """Files outlive their record - the job store is pruned to MAX_PERSISTED_JOBS while the
    media stays on disk - so falling through would leak the oldest users' clips."""
    _alice, alice_client = _signed_in(auth_on, "alice")
    orphan = app_settings.clips_dir / "orphanjob00"
    orphan.mkdir(parents=True, exist_ok=True)
    (orphan / "clip.mp4").write_bytes(b"FAKE")
    assert alice_client.get("/clips/orphanjob00/clip.mp4").status_code == 404


def test_downloading_someone_elses_clip_is_refused(auth_on):
    alice, _ = _signed_in(auth_on, "alice")
    _bob, bob_client = _signed_in(auth_on, "bob")
    job, clip = _job_for(alice.id)
    assert bob_client.get(
        f"/api/clips/{job.id}/{clip.filename}/download"
    ).status_code == 404
    assert bob_client.get(f"/api/clips/{job.id}/{clip.filename}/video").status_code == 404


def test_reviewing_someone_elses_clip_is_refused(auth_on):
    alice, _ = _signed_in(auth_on, "alice")
    _bob, bob_client = _signed_in(auth_on, "bob")
    job, clip = _job_for(alice.id)
    resp = bob_client.post(
        f"/api/jobs/{job.id}/clips/{clip.id}/review",
        json={"review_state": "rejected", "review_note": ""},
    )
    assert resp.status_code == 404


def test_editing_someone_elses_clip_is_refused(auth_on):
    alice, _ = _signed_in(auth_on, "alice")
    _bob, bob_client = _signed_in(auth_on, "bob")
    job, clip = _job_for(alice.id)
    resp = bob_client.patch(
        f"/api/jobs/{job.id}/clips/{clip.id}", json={"title": "hijacked"}
    )
    assert resp.status_code == 404
    # And the title is untouched.
    assert get_manager().store.get_clip(job.id, clip.id).title == "t"


def test_rerendering_someone_elses_clip_is_refused(auth_on):
    alice, _ = _signed_in(auth_on, "alice")
    _bob, bob_client = _signed_in(auth_on, "bob")
    job, clip = _job_for(alice.id)
    resp = bob_client.post(
        f"/api/jobs/{job.id}/clips/{clip.id}/rerender", json={"settings": {}, "cuts": []}
    )
    assert resp.status_code == 404


def test_cancelling_someone_elses_job_is_refused(auth_on):
    alice, _ = _signed_in(auth_on, "alice")
    _bob, bob_client = _signed_in(auth_on, "bob")
    job, _clip = _job_for(alice.id)
    assert bob_client.post(f"/api/jobs/{job.id}/cancel").status_code == 404


def test_deleting_someone_elses_source_is_refused(auth_on):
    alice, _ = _signed_in(auth_on, "alice")
    _bob, bob_client = _signed_in(auth_on, "bob")
    job, _clip = _job_for(alice.id)
    resp = bob_client.request(
        "DELETE", f"/api/jobs/{job.id}/source", params={"confirm": "true"}
    )
    assert resp.status_code == 404


def test_a_submitted_job_is_stamped_with_its_owner(auth_on):
    alice, alice_client = _signed_in(auth_on, "alice")
    resp = alice_client.post(
        "/api/jobs/url", json={"url": "https://www.youtube.com/watch?v=abc", "options": {}}
    )
    assert resp.status_code == 200
    assert resp.json()["owner"] == alice.id


def test_ownership_survives_a_restart(tmp_path):
    """Owner is carried in the persisted JSON blob, not a column - so this is what proves it
    is written at all. Without it, ownership would hold until the process restarted and then
    every job would become unowned, i.e. admin-only, i.e. invisible to the person who made it.
    """
    from worker.job_persistence import Job_Persistence

    store = Job_Persistence(tmp_path / "jobs.db")
    job = Job(
        input_type="file", source="s.mp4", options=ProcessingOptions(), owner="user-42"
    )
    store.save(job)
    reloaded = Job_Persistence(tmp_path / "jobs.db").load_all()
    assert [j.owner for j in reloaded if j.id == job.id] == ["user-42"]


def test_an_unowned_job_round_trips_as_unowned(tmp_path):
    from worker.job_persistence import Job_Persistence

    store = Job_Persistence(tmp_path / "jobs.db")
    job = Job(input_type="file", source="s.mp4", options=ProcessingOptions())
    store.save(job)
    reloaded = Job_Persistence(tmp_path / "jobs.db").load_all()
    assert [j.owner for j in reloaded if j.id == job.id] == [""]


def test_a_job_submitted_with_auth_off_has_no_owner():
    client = TestClient(app)
    resp = client.post(
        "/api/jobs/url", json={"url": "https://www.youtube.com/watch?v=xyz", "options": {}}
    )
    assert resp.status_code == 200
    assert resp.json()["owner"] == ""


# --------------------------------------------------------------------------- #
# The tripwire: every job-scoped route, enumerated from the app itself
# --------------------------------------------------------------------------- #
def _assert_no_job_route_serves(client, job, clip) -> int:
    """Assert no job-scoped route answers 2xx to ``client``. Returns how many were checked.

    Routes are read off ``app.routes`` rather than listed, so an endpoint added later is
    covered the day it is written. That is the whole point: a hand-written list of endpoints
    cannot fail for the one nobody remembered.

    The assertion is "not a success" rather than "exactly 404" because a generic empty body
    makes some handlers answer 422 on body validation before authorization is reached. The
    exact-404 behaviour is pinned by the per-endpoint tests above; this exists to catch the
    endpoint that answers **200**.
    """
    checked = 0
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None)
        if "{job_id}" not in path or not methods:
            continue
        url = (
            path.replace("{job_id}", job.id)
            .replace("{clip_id}", clip.id)
            .replace("{filename}", clip.filename)
        )
        if "{" in url:  # an unexpected path parameter: fail loudly rather than skip it
            raise AssertionError(f"unhandled path parameter in {path}")
        for method in sorted(set(methods) - {"HEAD", "OPTIONS"}):
            resp = client.request(method, url, json={})
            assert not (200 <= resp.status_code < 300), (
                f"{method} {path} answered {resp.status_code} to a non-owner"
            )
            checked += 1
    return checked


def test_every_job_scoped_route_refuses_a_stranger(auth_on):
    """No job-scoped endpoint may answer 2xx to someone who does not own the job."""
    alice, _ = _signed_in(auth_on, "alice")
    _bob, bob_client = _signed_in(auth_on, "bob")
    job, clip = _job_for(alice.id)

    checked = _assert_no_job_route_serves(bob_client, job, clip)
    # Guard against a vacuous pass: if the route scan silently matched nothing, the helper
    # would assert nothing at all and this test would be decoration.
    assert checked >= 10, f"only {checked} job-scoped route(s) checked; the scan is wrong"


def test_the_route_scan_would_notice_an_unprotected_endpoint(auth_on):
    """A self-test for the tripwire: it must fail when a job-scoped route is unguarded.

    Without this, a scan that quietly stopped matching routes - a change in how FastAPI
    exposes ``path``, say - would keep passing while protecting nothing.
    """
    alice, _ = _signed_in(auth_on, "alice")
    _bob, bob_client = _signed_in(auth_on, "bob")
    job, clip = _job_for(alice.id)

    def _leaky(job_id: str) -> dict:  # pragma: no cover - exercised over HTTP
        return {"job_id": job_id}

    # Registered and then moved to the *front* of the table. `app.mount("/", ...)` serves the
    # SPA, and a Starlette Mount at "/" matches every path, so anything appended after it is
    # unreachable: a route added the ordinary way would 404 from the static handler and this
    # self-test would "pass" without ever having had something to find.
    leaky_path = "/api/jobs/{job_id}/deliberately-unguarded"
    app.add_api_route(leaky_path, _leaky, methods=["GET"])
    app.router.routes.insert(0, app.router.routes.pop())

    try:
        assert bob_client.get(
            f"/api/jobs/{job.id}/deliberately-unguarded"
        ).status_code == 200, "the leaky route is unreachable, so this proves nothing"
        with pytest.raises(AssertionError, match="answered 200 to a non-owner"):
            _assert_no_job_route_serves(bob_client, job, clip)
    finally:
        app.router.routes = [
            r for r in app.router.routes if getattr(r, "path", "") != leaky_path
        ]
    # And the scan is clean again once the leak is removed.
    assert _assert_no_job_route_serves(bob_client, job, clip) >= 10


# --------------------------------------------------------------------------- #
# User administration
# --------------------------------------------------------------------------- #
def test_only_an_admin_can_list_users(auth_on):
    _alice, alice_client = _signed_in(auth_on, "alice")
    assert alice_client.get("/api/users").status_code == 403
    _admin, admin_client = _signed_in(auth_on, "root", is_admin=True)
    assert admin_client.get("/api/users").status_code == 200


def test_an_admin_can_create_a_user(auth_on):
    _admin, admin_client = _signed_in(auth_on, "root", is_admin=True)
    resp = admin_client.post(
        "/api/users", json={"username": "carol", "password": GOOD_PASSWORD}
    )
    assert resp.status_code == 200
    assert resp.json()["user"]["username"] == "carol"
    assert "password" not in resp.text.lower() or "password_hash" not in resp.text


def test_a_non_admin_cannot_create_a_user(auth_on):
    """There is no self-service registration: an open sign-up on a tool that spends GPU
    minutes per request is an invitation."""
    _alice, alice_client = _signed_in(auth_on, "alice")
    resp = alice_client.post(
        "/api/users", json={"username": "carol", "password": GOOD_PASSWORD}
    )
    assert resp.status_code == 403
    assert auth_on.get_user_by_name("carol") is None


def test_creating_a_duplicate_user_is_a_conflict(auth_on):
    _admin, admin_client = _signed_in(auth_on, "root", is_admin=True)
    admin_client.post("/api/users", json={"username": "carol", "password": GOOD_PASSWORD})
    resp = admin_client.post(
        "/api/users", json={"username": "carol", "password": GOOD_PASSWORD}
    )
    assert resp.status_code == 409


def test_creating_a_user_with_a_weak_password_is_refused(auth_on):
    _admin, admin_client = _signed_in(auth_on, "root", is_admin=True)
    resp = admin_client.post("/api/users", json={"username": "carol", "password": "abc"})
    assert resp.status_code == 422


def test_an_admin_can_disable_an_account_and_its_sessions_stop(auth_on):
    alice, alice_client = _signed_in(auth_on, "alice")
    _admin, admin_client = _signed_in(auth_on, "root", is_admin=True)
    assert alice_client.get("/api/jobs").status_code == 200
    assert admin_client.post(
        f"/api/users/{alice.id}/disabled", json={"disabled": True}
    ).status_code == 200
    assert alice_client.get("/api/jobs").status_code == 401


def test_an_admin_cannot_disable_themselves(auth_on):
    """With one admin that locks the instance out of its own user administration."""
    admin, admin_client = _signed_in(auth_on, "root", is_admin=True)
    resp = admin_client.post(f"/api/users/{admin.id}/disabled", json={"disabled": True})
    assert resp.status_code == 409


def test_user_administration_is_absent_when_auth_is_off():
    assert TestClient(app).get("/api/users").status_code == 404


# --------------------------------------------------------------------------- #
# Changing your own password
# --------------------------------------------------------------------------- #
def test_changing_your_password_requires_the_current_one(auth_on):
    """Otherwise a borrowed session becomes a permanent takeover."""
    _alice, alice_client = _signed_in(auth_on, "alice")
    resp = alice_client.post(
        "/api/auth/password",
        json={"current_password": "wrong-password", "new_password": OTHER_PASSWORD},
    )
    assert resp.status_code == 403
    assert auth_on.authenticate("alice", GOOD_PASSWORD) is not None


def test_changing_your_password_works_and_keeps_you_signed_in(auth_on):
    _alice, alice_client = _signed_in(auth_on, "alice")
    resp = alice_client.post(
        "/api/auth/password",
        json={"current_password": GOOD_PASSWORD, "new_password": OTHER_PASSWORD},
    )
    assert resp.status_code == 200
    assert auth_on.authenticate("alice", OTHER_PASSWORD) is not None
    # The tab that made the change keeps working, on a *new* session.
    assert alice_client.get("/api/jobs").status_code == 200


def test_changing_a_password_ends_other_sessions(auth_on):
    """A password change is what you do when you think a session is compromised."""
    alice = auth_on.create_user("alice", GOOD_PASSWORD)
    stale = auth_on.create_session(alice.id)
    other = TestClient(app)
    other.cookies.set("clipper_session", stale.token)
    assert other.get("/api/jobs").status_code == 200

    main_client = TestClient(app)
    main_client.post("/api/auth/login", json={"username": "alice", "password": GOOD_PASSWORD})
    main_client.post(
        "/api/auth/password",
        json={"current_password": GOOD_PASSWORD, "new_password": OTHER_PASSWORD},
    )
    assert other.get("/api/jobs").status_code == 401


def test_a_weak_new_password_is_refused(auth_on):
    _alice, alice_client = _signed_in(auth_on, "alice")
    resp = alice_client.post(
        "/api/auth/password",
        json={"current_password": GOOD_PASSWORD, "new_password": "short"},
    )
    assert resp.status_code == 422


def test_the_session_endpoint_reports_the_signed_in_user(auth_on):
    alice, alice_client = _signed_in(auth_on, "alice")
    body = alice_client.get("/api/auth/session").json()
    assert body["user"]["username"] == "alice"
    assert body["user"]["id"] == alice.id


def test_the_session_endpoint_reports_nobody_when_auth_is_off():
    assert TestClient(app).get("/api/auth/session").json() == {"user": None}


# --------------------------------------------------------------------------- #
# Bootstrap
# --------------------------------------------------------------------------- #
def test_bootstrap_creates_the_configured_admin(auth_on, monkeypatch):
    monkeypatch.setattr(app_settings, "auth_bootstrap_username", "root")
    monkeypatch.setattr(app_settings, "auth_bootstrap_password", GOOD_PASSWORD)
    user = api_main.bootstrap_admin()
    assert user is not None
    assert user.is_admin is True
    assert auth_on.authenticate("root", GOOD_PASSWORD) is not None


def test_bootstrap_does_nothing_when_a_user_already_exists(auth_on, monkeypatch):
    auth_on.create_user("alice", GOOD_PASSWORD)
    monkeypatch.setattr(app_settings, "auth_bootstrap_username", "root")
    monkeypatch.setattr(app_settings, "auth_bootstrap_password", GOOD_PASSWORD)
    assert api_main.bootstrap_admin() is None
    assert auth_on.get_user_by_name("root") is None


def test_bootstrap_refuses_to_start_with_no_way_in(auth_on, monkeypatch):
    """A server that starts and can only answer 401 is the harder problem to diagnose."""
    monkeypatch.setattr(app_settings, "auth_bootstrap_username", "")
    monkeypatch.setattr(app_settings, "auth_bootstrap_password", "")
    with pytest.raises(RuntimeError, match="AUTH_BOOTSTRAP_USERNAME"):
        api_main.bootstrap_admin()


def test_bootstrap_is_a_no_op_when_auth_is_off():
    assert api_main.bootstrap_admin() is None
