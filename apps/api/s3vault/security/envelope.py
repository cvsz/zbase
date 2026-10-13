"""Envelope encryption for S3 secret access keys.

Design (see docs/SECURITY.md#credential-secret-storage):

* A 256-bit key-encryption key (KEK) lives **outside** the database, supplied
  as ``KEK_BASE64`` (or ``KEK_FILE``) from the process environment or a
  mounted secret file.
* Each credential gets a fresh random 256-bit data-encryption key (DEK).
* The DEK is wrapped under the KEK with AES-256-GCM using its own nonce; the
  secret is encrypted under the DEK with a **separate** nonce. Nonces are never
  reused for two messages under the same key.
* Both operations use additional authenticated data binding the ciphertext to
  the credential id and access key id, so a row cannot be transplanted into
  another credential row undetected.
* The wrapped DEK is stored alongside the ciphertext.

Layout of the serialised blob::

    b"S3V1" | nonce_dek(12) | wrapped_dek(32+16) | nonce_data(12) | ct+tag

Tampering with any field fails its GCM tag, so an attacker with write access to
the database still cannot recover a usable secret.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..errors import AppError

DEK_BYTES = 32
NONCE_BYTES = 12
KEK_BYTES = 32
GCM_TAG_BYTES = 16

_MAGIC = b"S3V1"
_WRAPPED_DEK_LEN = DEK_BYTES + GCM_TAG_BYTES
_HEADER_LEN = len(_MAGIC)


class DecryptionError(AppError):
    code = "credential_secret_unavailable"
    status = 500


@dataclass(frozen=True, slots=True)
class SealedSecret:
    """Serialisable envelope; store as ``bytea``."""

    blob: bytes

    def hex(self) -> str:
        return self.blob.hex()


def load_kek(kek_base64: str | None = None) -> bytes:
    """Resolve the KEK from an explicit value, ``KEK_BASE64`` or ``KEK_FILE``."""
    raw_b64 = kek_base64 or os.environ.get("KEK_BASE64")
    if not raw_b64:
        path = os.environ.get("KEK_FILE")
        if path:
            try:
                with open(path, encoding="utf-8") as handle:
                    raw_b64 = handle.read().strip()
            except OSError as exc:
                raise DecryptionError("KEK_FILE could not be read") from exc
    if not raw_b64:
        raise DecryptionError("no key-encryption key is configured")
    try:
        key = base64.b64decode(raw_b64, validate=True)
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed app error
        raise DecryptionError("key-encryption key is not valid base64") from exc
    if len(key) != KEK_BYTES:
        raise DecryptionError(
            f"key-encryption key must be {KEK_BYTES} bytes, got {len(key)}"
        )
    return key


def _aad(credential_id: str, access_key_id: str) -> bytes:
    """Bind ciphertext to the credential identity."""
    return f"{credential_id}|{access_key_id}".encode("utf-8")


def seal(plaintext: str, *, credential_id: str, access_key_id: str, kek: bytes) -> SealedSecret:
    """Encrypt ``plaintext`` into an envelope bound to the credential identity."""
    if len(kek) != KEK_BYTES:
        raise DecryptionError("invalid KEK length")

    aad = _aad(credential_id, access_key_id)
    dek = os.urandom(DEK_BYTES)

    # Distinct nonces: one for KEK/DEK wrapping, one for DEK/payload.
    nonce_dek = os.urandom(NONCE_BYTES)
    wrapped_dek = AESGCM(kek).encrypt(nonce_dek, dek, aad)

    nonce_data = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(dek).encrypt(nonce_data, plaintext.encode("utf-8"), aad)

    return SealedSecret(
        blob=_MAGIC + nonce_dek + wrapped_dek + nonce_data + ciphertext
    )


def open_sealed(
    sealed: SealedSecret | bytes,
    *,
    credential_id: str,
    access_key_id: str,
    kek: bytes,
) -> str:
    """Decrypt an envelope produced by :func:`seal`.

    Raises:
        DecryptionError: on any structural problem, wrong KEK, or tag failure.
    """
    blob = sealed.blob if isinstance(sealed, SealedSecret) else bytes(sealed)
    min_len = _HEADER_LEN + NONCE_BYTES + _WRAPPED_DEK_LEN + NONCE_BYTES + GCM_TAG_BYTES

    if len(blob) < min_len:
        raise DecryptionError("encrypted credential secret is truncated")
    if not blob.startswith(_MAGIC):
        raise DecryptionError("encrypted credential secret has an unknown format")

    offset = _HEADER_LEN
    nonce_dek = blob[offset : offset + NONCE_BYTES]
    offset += NONCE_BYTES
    wrapped_dek = blob[offset : offset + _WRAPPED_DEK_LEN]
    offset += _WRAPPED_DEK_LEN
    nonce_data = blob[offset : offset + NONCE_BYTES]
    offset += NONCE_BYTES
    ciphertext = blob[offset:]

    aad = _aad(credential_id, access_key_id)

    try:
        dek = AESGCM(kek).decrypt(nonce_dek, wrapped_dek, aad)
    except InvalidTag as exc:
        raise DecryptionError(
            "credential secret cannot be decrypted with the configured key "
            "(wrong KEK or tampered data)"
        ) from exc

    try:
        plaintext = AESGCM(dek).decrypt(nonce_data, ciphertext, aad)
    except InvalidTag as exc:
        raise DecryptionError("credential secret failed authentication") from exc

    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DecryptionError("credential secret is not valid UTF-8") from exc