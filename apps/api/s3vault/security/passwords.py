"""Password hashing with a pluggable memory-hard KDF.

The stored hash carries an algorithm identifier so credentials can be
transparently upgraded on the next successful login.

Default: **scrypt** from :mod:`hashlib` (stdlib, memory-hard, backed by the
platform's OpenSSL). Argon2id is used automatically when ``argon2-cffi`` is
importable and is not disabled.

Why scrypt by default: this deployment targets CPython 3.14, for which no
prebuilt Argon2 wheel is published, and no Rust toolchain is present in the
runtime image. Shipping Argon2id by default would require either building from
source at image-build time or pinning an older interpreter, which would leave
the tested interpreter different from the shipped one. See
docs/SECURITY.md#password-hashing for the full rationale and the upgrade path.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass

ALGO_SCRYPT = "scrypt"
ALGO_ARGON2ID = "argon2id"

#: Costs chosen so a single hash costs ~50-100ms on a small VPS core.
SCRYPT_N = 2**15  # 32768
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_SALT_BYTES = 16

ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST_KIB = 64 * 1024  # 64 MiB
ARGON2_PARALLELISM = 4


class PasswordHashError(ValueError):
    """Raised when a stored hash is malformed or of an unknown algorithm."""


@dataclass(frozen=True, slots=True)
class HashResult:
    encoded: str
    algorithm: str
    needs_rehash: bool = False


def argon2_available() -> bool:
    if os.environ.get("S3VAULT_DISABLE_ARGON2", "").strip() in {"1", "true", "yes"}:
        return False
    try:
        import argon2  # noqa: F401
    except Exception:  # noqa: BLE001 - absence is the signal
        return False
    return True


def _hash_scrypt(password: str, *, salt: bytes | None = None) -> tuple[str, bytes]:
    salt = salt or os.urandom(SCRYPT_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=132 * 1024 * 1024,
    )
    return f"{ALGO_SCRYPT}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}", salt


def _hash_argon2id(password: str) -> str:
    from argon2 import PasswordHasher

    hasher = PasswordHasher(
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST_KIB,
        parallelism=ARGON2_PARALLELISM,
    )
    return hasher.hash(password)


def hash_password(password: str) -> str:
    """Hash ``password`` with the strongest available algorithm.

    Returns a self-describing encoded hash suitable for storage.
    """
    if not isinstance(password, str) or password == "":
        raise PasswordHashError("password must be a non-empty string")
    if argon2_available():
        return _hash_argon2id(password)
    encoded, _ = _hash_scrypt(password)
    return encoded


def _verify_scrypt(encoded: str, password: str) -> bool:
    try:
        algo, n_raw, r_raw, p_raw, salt_hex, digest_hex = encoded.split("$")
    except ValueError as exc:
        raise PasswordHashError("malformed scrypt hash") from exc
    if algo != ALGO_SCRYPT:
        raise PasswordHashError(f"unexpected algorithm marker: {algo}")
    try:
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError as exc:
        raise PasswordHashError("malformed scrypt parameters") from exc
    actual = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=len(expected),
        maxmem=132 * 1024 * 1024,
    )
    return hmac.compare_digest(actual, expected)


def _verify_argon2id(encoded: str, password: str) -> bool:
    try:
        from argon2 import PasswordHasher
        from argon2.exceptions import Argon2Error, VerificationError, VerifyMismatchError
    except ImportError as exc:
        # A hash exists but the verifier is not installed. This is an
        # operational fault, never a successful match.
        raise PasswordHashError(
            "password was hashed with argon2id but argon2-cffi is not installed"
        ) from exc
    hasher = PasswordHasher()
    try:
        return bool(hasher.verify(encoded, password))
    except (VerifyMismatchError, VerificationError, Argon2Error):
        return False


def verify_password(password: str, encoded: str) -> bool:
    """Verify ``password`` against ``encoded`` in constant time.

    Raises:
        PasswordHashError: if the stored hash is unknown or malformed. Callers
            must treat this as "cannot verify", never as a match.
    """
    if not isinstance(password, str) or not isinstance(encoded, str) or not encoded:
        raise PasswordHashError("password and hash must be non-empty strings")
    if encoded.startswith("$argon2"):
        return _verify_argon2id(encoded, password)
    if encoded.startswith(f"{ALGO_SCRYPT}$"):
        return _verify_scrypt(encoded, password)
    raise PasswordHashError("unrecognised password hash format")


def needs_rehash(encoded: str) -> bool:
    """True when ``encoded`` should be upgraded to the current parameters."""
    if not encoded:
        return True
    if argon2_available():
        return not encoded.startswith("$argon2")
    if encoded.startswith(f"{ALGO_SCRYPT}$"):
        try:
            parts = encoded.split("$")
            return (
                int(parts[1]) != SCRYPT_N
                or int(parts[2]) != SCRYPT_R
                or int(parts[3]) != SCRYPT_P
            )
        except (ValueError, IndexError):
            return True
    return True


def verify_and_maybe_rehash(password: str, encoded: str) -> tuple[bool, str | None]:
    """Verify and, on success, return a replacement hash when parameters changed.

    Returns ``(ok, new_encoded_or_None)``.
    """
    ok = verify_password(password, encoded)
    if ok and needs_rehash(encoded):
        return True, hash_password(password)
    return ok, None


def constant_time_equals(left: str, right: str) -> bool:
    """Constant-time string comparison for tokens and identifiers."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))