"""Multi-user authentication and per-user ownership (U12).

A top-level package rather than a module under ``worker/`` because none of this is
processing: it is consumed by ``api/`` and it owns its own SQLite store, which is the same
shape as ``publishers/`` and ``storage_backends/``.

Three deliberate constraints shaped what is here:

* **Everything is off by default.** ``AUTH_ENABLED=false`` is the shipped behaviour and it
  stays the default, so an existing single-tenant install is byte-for-byte unaffected. This
  is the same rule the visual settings follow, for the same reason: a feature that changes
  behaviour by default makes every existing deployment a migration.
* **Standard library only.** No `passlib`, `bcrypt`, `python-jose`, `PyJWT` or
  `itsdangerous` is declared in any requirements file, and `pyproject.toml` turns warnings
  into errors, so a new dependency's first DeprecationWarning fails CI. `hashlib.scrypt`
  is a memory-hard KDF in the standard library and is what the OWASP guidance names
  alongside bcrypt and argon2, so nothing is given up by using it.
* **Sessions are opaque and server-side.** Not JWTs. A JWT cannot be revoked without a
  server-side denylist, at which point it is a session with extra steps and hand-rolled
  signature verification. Logging out, and an operator ending someone's session, both
  have to actually work.
"""

from auth.passwords import (
    PasswordError,
    hash_password,
    needs_rehash,
    verify_password,
)
from auth.store import (
    AuthStore,
    Session,
    User,
    get_auth_store,
    reset_auth_store,
)

__all__ = [
    "AuthStore",
    "PasswordError",
    "Session",
    "User",
    "get_auth_store",
    "hash_password",
    "needs_rehash",
    "reset_auth_store",
    "verify_password",
]
