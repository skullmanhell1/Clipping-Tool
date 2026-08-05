"""Password hashing (U12), on the standard library.

``hashlib.scrypt`` is memory-hard, is one of the three KDFs OWASP names for password
storage, and needs no dependency — which matters here, because none of the usual libraries
is declared in any requirements file and `filterwarnings = ["error"]` makes adding one a
CI risk of its own.

The stored form is self-describing::

    scrypt$16384$8$1$<salt-hex>$<key-hex>

Parameters are recorded **per hash** rather than read from settings at verification time.
Otherwise raising the cost would silently invalidate every existing password: the verifier
would derive a key with new parameters and compare it against one derived with the old
ones, which fails for correct passwords. Recording them means old hashes keep verifying and
:func:`needs_rehash` says which ones to upgrade on next successful login.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

#: CPU/memory cost. 2**15 * 128 * 8 ≈ 32 MB per hash. Chosen as the largest value that
#: stays comfortably under the default 32 MB `maxmem` OpenSSL applies, so no caller has to
#: pass one; going higher raises `ValueError: memory limit exceeded` rather than being slow,
#: which is a confusing way to discover a limit.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32

#: Passwords longer than this are rejected rather than hashed. scrypt's cost is independent
#: of input length, so this is not a DoS guard - it is a guard against a client sending a
#: megabyte where a password belongs.
MAX_PASSWORD_LENGTH = 1024

#: Below this, a password is refused at the point of being set. Deliberately a floor rather
#: than a composition rule: length is the property that matters and "must contain a symbol"
#: pushes people towards `Password1!`.
MIN_PASSWORD_LENGTH = 10

_PREFIX = "scrypt"


class PasswordError(ValueError):
    """A password could not be hashed or a stored hash could not be read."""


def hash_password(password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R,
                  p: int = SCRYPT_P) -> str:
    """Return a self-describing scrypt hash of ``password``."""
    if not isinstance(password, str) or not password:
        raise PasswordError("A password is required.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordError(
            f"Password is too long (limit {MAX_PASSWORD_LENGTH} characters)."
        )
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    salt = secrets.token_bytes(SALT_BYTES)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=KEY_BYTES)
    return f"{_PREFIX}${n}${r}${p}${salt.hex()}${key.hex()}"


def _parse(stored: str) -> tuple[int, int, int, bytes, bytes]:
    try:
        scheme, n, r, p, salt_hex, key_hex = stored.split("$")
    except (AttributeError, ValueError) as exc:
        raise PasswordError("Stored password hash is not in a recognised format.") from exc
    if scheme != _PREFIX:
        raise PasswordError(f"Unsupported password hash scheme: {scheme!r}")
    try:
        return int(n), int(r), int(p), bytes.fromhex(salt_hex), bytes.fromhex(key_hex)
    except ValueError as exc:
        raise PasswordError("Stored password hash is corrupt.") from exc


def verify_password(password: str, stored: str) -> bool:
    """Whether ``password`` matches ``stored``.

    Returns ``False`` for a wrong password and for an unusable stored hash alike, and never
    raises for either. A corrupt row must not be distinguishable from a wrong password from
    the outside - the difference is an operator's problem, and telling a caller which one it
    is tells them a valid username exists.

    The comparison is :func:`hmac.compare_digest`, not ``==``. Both operands are already
    hashes, so the timing signal is small, but it is not zero and the fix is one call.
    """
    if not isinstance(password, str) or not password:
        return False
    if len(password) > MAX_PASSWORD_LENGTH:
        return False
    try:
        n, r, p, salt, expected = _parse(stored)
        candidate = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
        )
    except (PasswordError, ValueError):
        return False
    return hmac.compare_digest(candidate, expected)


def needs_rehash(stored: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R,
                 p: int = SCRYPT_P) -> bool:
    """Whether ``stored`` was made with weaker parameters than the current ones.

    Called after a *successful* login, which is the only moment the plaintext is available
    to re-derive with. An unreadable hash is reported as needing a rehash: it cannot be
    verified against anyway, so the next successful login should replace it.
    """
    try:
        have_n, have_r, have_p, _salt, key = _parse(stored)
    except PasswordError:
        return True
    return (have_n, have_r, have_p, len(key)) != (n, r, p, KEY_BYTES)
