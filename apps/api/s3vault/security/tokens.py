"""Opaque token generation and storage-hash helpers.

Session and CSRF tokens are random 256-bit values. Only a SHA-256 hash of a
token is persisted, so a database disclosure does not yield usable sessions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta

TOKEN_BYTES = 32
_ACCESS_KEY_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_SECRET_KEY_ALPHABET = base64.b64encode(secrets.token_bytes(1)).decode()[:0] or None  # noqa: F841

_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


def new_token(nbytes: int = TOKEN_BYTES) -> str:
    """Return a URL-safe opaque token."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """Return the storage form of a token (hex SHA-256)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token_hash(token: str, stored_hash: str) -> bool:
    """Constant-time check of a presented token against its stored hash."""
    return hmac.compare_digest(hash_token(token), stored_hash)


def new_access_key_id() -> str:
    """Return an AWS-S3-shaped access key id (20 chars, uppercase base32-ish)."""
    raw = secrets.token_bytes(20)
    bits = int.from_bytes(raw, "big")
    chars = []
    for _ in range(20):
        chars.append(_ACCESS_KEY_ALPHABET[bits & 0x1F])
        bits >>= 5
    return "".join(reversed(chars))


def new_secret_access_key() -> str:
    """Return a 40-character base64-style secret access key."""
    alphabet = (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    )
    raw = secrets.token_bytes(40)
    out = []
    for i in range(40):
        out.append(alphabet[raw[i % len(raw)] % len(alphabet)])
    return "".join(out)


def new_crc32() -> int:
    """Return a CRC32 checksum for upload-part validation."""
    import zlib

    return zlib.crc32(secrets.token_bytes(4)) & 0xFFFFFFFF


def utcnow() -> datetime:
    return datetime.now(UTC)


def expires_in(seconds: int) -> datetime:
    return utcnow() + timedelta(seconds=seconds)


def is_valid_bucket_name(name: str) -> bool:
    """Validate an S3 bucket name against the DNS-compatible subset S3Vault issues."""
    if not isinstance(name, str) or not name:
        return False
    if len(name) < 3 or len(name) > 63:
        return False
    if ".." in name or ".-" in name or "-." in name:
        return False
    return bool(_BUCKET_RE.fullmatch(name))


def normalise_object_key(key: str) -> str:
    """Validate and normalise an object key.

    Rejects path traversal, control characters, and empty keys. Object keys are
    never interpreted as filesystem paths anywhere in S3Vault.

    Percent-encoded separators are also rejected: a key such as
    ``..%2f..%2fetc%2fpasswd`` is a harmless literal to S3, but if any future
    component decodes it before use it becomes traversal. Rejecting the
    ambiguous form removes that trap at the boundary.
    """
    if not isinstance(key, str) or not key:
        raise ValueError("object key must be a non-empty string")
    if len(key.encode("utf-8")) > 1024:
        raise ValueError("object key exceeds 1024 bytes")
    if key.startswith("/"):
        raise ValueError("object key must not start with '/'")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in key):
        raise ValueError("object key contains control characters")

    lowered = key.lower()
    if "%2f" in lowered or "%5c" in lowered:
        raise ValueError("object key must not contain percent-encoded path separators")

    parts = key.split("/")
    for part in parts:
        if part in {"", ".", ".."}:
            raise ValueError(f"object key contains an unsafe path segment: {part!r}")
    return key


def key_matches_prefix(key: str, prefix: str | None) -> bool:
    """Prefix containment used by credential scope enforcement."""
    if prefix is None or prefix == "":
        return True
    return key == prefix or key.startswith(prefix)