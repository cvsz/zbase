"""Unit tests for password hashing, tokens and envelope encryption."""

from __future__ import annotations

import base64
import os

import pytest

from s3vault.errors import AppError
from s3vault.security.envelope import DecryptionError, load_kek, open_sealed, seal
from s3vault.security.passwords import (
    ALGO_SCRYPT,
    PasswordHashError,
    constant_time_equals,
    hash_password,
    needs_rehash,
    verify_and_maybe_rehash,
    verify_password,
)
from s3vault.security.rbac import (
    Capability,
    can_use_credential_permission,
    capabilities_for_role,
    has_capability,
)
from s3vault.security.tokens import (
    hash_token,
    is_valid_bucket_name,
    key_matches_prefix,
    new_access_key_id,
    new_secret_access_key,
    new_token,
    normalise_object_key,
    verify_token_hash,
)

KEK = base64.b64encode(b"\x07" * 32).decode()
OTHER_KEK = base64.b64encode(b"\x08" * 32).decode()


class TestPasswordHashing:
    def test_hash_and_verify_roundtrip(self):
        encoded = hash_password("correct horse battery staple")
        assert verify_password("correct horse battery staple", encoded) is True

    def test_wrong_password_fails(self):
        encoded = hash_password("correct horse battery staple")
        assert verify_password("wrong password entirely", encoded) is False

    def test_hashes_are_salted_and_unique(self):
        a = hash_password("same-password")
        b = hash_password("same-password")
        assert a != b, "identical passwords must not produce identical hashes"
        assert verify_password("same-password", a)
        assert verify_password("same-password", b)

    def test_plaintext_never_appears_in_hash(self):
        encoded = hash_password("hunter2-unlikely-to-appear")
        assert "hunter2" not in encoded

    @pytest.mark.parametrize(
        "bad",
        ["", "not-a-hash", "scrypt$1$2$3", "$argon2id$broken", "unknown$1$2$3$a$b"],
    )
    def test_malformed_hashes_raise_not_match(self, bad):
        with pytest.raises(PasswordHashError):
            verify_password("anything", bad)

    def test_empty_password_rejected_by_hash(self):
        with pytest.raises(PasswordHashError):
            hash_password("")

    def test_scrypt_parameters_are_explicit_in_encoding(self):
        os.environ["S3VAULT_DISABLE_ARGON2"] = "1"
        try:
            encoded = hash_password("scrypt-check")
            assert encoded.startswith(f"{ALGO_SCRYPT}$")
            parts = encoded.split("$")
            assert len(parts) == 6
            assert needs_rehash(encoded) is False
        finally:
            os.environ.pop("S3VAULT_DISABLE_ARGON2", None)

    def test_rehash_is_triggered_for_foreign_parameters(self):
        """A hash made with weaker parameters must verify, then be upgraded."""
        import hashlib

        salt = b"\x01" * 16
        weak_n = 2**10
        digest = hashlib.scrypt(
            b"weak-params", salt=salt, n=weak_n, r=8, p=1, dklen=32, maxmem=132 * 1024 * 1024
        )
        downgraded = "$".join(
            ["scrypt", str(weak_n), "8", "1", salt.hex(), digest.hex()]
        )

        assert needs_rehash(downgraded) is True
        ok, new_encoded = verify_and_maybe_rehash("weak-params", downgraded)
        assert ok is True
        assert new_encoded is not None
        assert needs_rehash(new_encoded) is False
        assert verify_password("weak-params", new_encoded) is True
        assert verify_password("other", new_encoded) is False

    def test_verify_and_maybe_rehash_skips_when_current(self):
        encoded = hash_password("already-current")
        ok, replacement = verify_and_maybe_rehash("already-current", encoded)
        assert ok is True
        assert replacement is None

    def test_constant_time_equals(self):
        assert constant_time_equals("abc", "abc") is True
        assert constant_time_equals("abc", "abd") is False
        assert constant_time_equals("abc", "abcd") is False


class TestTokens:
    def test_tokens_are_unique_and_long(self):
        tokens = {new_token() for _ in range(500)}
        assert len(tokens) == 500
        assert all(len(t) >= 43 for t in tokens)

    def test_token_hash_roundtrip(self):
        token = new_token()
        stored = hash_token(token)
        assert stored != token
        assert verify_token_hash(token, stored) is True
        assert verify_token_hash(new_token(), stored) is False

    def test_access_key_id_shape_and_uniqueness(self):
        ids = {new_access_key_id() for _ in range(300)}
        assert len(ids) == 300
        for value in ids:
            assert len(value) == 20
            assert value.isalnum()
            assert value.upper() == value

    def test_secret_access_key_shape_and_uniqueness(self):
        secrets_ = {new_secret_access_key() for _ in range(300)}
        assert len(secrets_) == 300
        for value in secrets_:
            assert len(value) == 40


class TestBucketAndKeyValidation:
    @pytest.mark.parametrize("name", ["documents", "my-bucket", "a.b.c", "bucket123"])
    def test_valid_bucket_names(self, name):
        assert is_valid_bucket_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "ab",  # too short
            "UPPER",  # invalid characters
            "-leading",
            "trailing-",
            "double..dot",
            "dot.-dash",
            "under_score",
            "a" * 64,  # too long
            "",
        ],
    )
    def test_invalid_bucket_names(self, name):
        assert is_valid_bucket_name(name) is False

    def test_valid_object_keys(self):
        assert normalise_object_key("a/b/c.txt") == "a/b/c.txt"
        assert normalise_object_key("single.bin") == "single.bin"

    @pytest.mark.parametrize(
        "key",
        [
            "../etc/passwd",
            "a/../b",
            "a//b",
            "/absolute",
            "",
            "ctrl\x00char",
            "new\nline",
        ],
    )
    def test_unsafe_object_keys_rejected(self, key):
        with pytest.raises(ValueError):
            normalise_object_key(key)

    def test_oversized_key_rejected(self):
        with pytest.raises(ValueError):
            normalise_object_key("x" * 2000)

    def test_prefix_matching(self):
        assert key_matches_prefix("docs/a.txt", "docs/") is True
        assert key_matches_prefix("docs/a.txt", "docs") is True
        assert key_matches_prefix("other/a.txt", "docs/") is False
        assert key_matches_prefix("anything", None) is True


class TestEnvelopeEncryption:
    def test_seal_and_open_roundtrip(self):
        secret = new_secret_access_key()
        envelope = seal(secret, credential_id="cid-1", access_key_id="AKID1", kek=base64.b64decode(KEK))
        recovered = open_sealed(
            envelope,
            credential_id="cid-1",
            access_key_id="AKID1",
            kek=base64.b64decode(KEK),
        )
        assert recovered == secret

    def test_ciphertext_does_not_contain_plaintext(self):
        secret = "SUPERSECRETSECRETKEY"
        envelope = seal(secret, credential_id="c", access_key_id="a", kek=base64.b64decode(KEK))
        assert secret.encode() not in envelope.blob

    def test_wrong_kek_fails_closed(self):
        envelope = seal("secret", credential_id="c", access_key_id="a", kek=base64.b64decode(KEK))
        with pytest.raises(DecryptionError):
            open_sealed(
                envelope,
                credential_id="c",
                access_key_id="a",
                kek=base64.b64decode(OTHER_KEK),
            )

    def test_row_transplant_is_detected_via_aad(self):
        """Moving a ciphertext into another credential row must fail."""
        envelope = seal("secret", credential_id="cid-A", access_key_id="AKID-A", kek=base64.b64decode(KEK))
        with pytest.raises(DecryptionError):
            open_sealed(
                envelope,
                credential_id="cid-B",
                access_key_id="AKID-A",
                kek=base64.b64decode(KEK),
            )

    def test_tampered_ciphertext_is_detected(self):
        envelope = seal("secret", credential_id="c", access_key_id="a", kek=base64.b64decode(KEK))
        blob = bytearray(envelope.blob)
        blob[-1] ^= 0xFF
        with pytest.raises(DecryptionError):
            open_sealed(bytes(blob), credential_id="c", access_key_id="a", kek=base64.b64decode(KEK))

    def test_truncated_blob_rejected(self):
        envelope = seal("secret", credential_id="c", access_key_id="a", kek=base64.b64decode(KEK))
        with pytest.raises(DecryptionError):
            open_sealed(envelope.blob[:10], credential_id="c", access_key_id="a", kek=base64.b64decode(KEK))

    def test_unknown_magic_rejected(self):
        with pytest.raises(DecryptionError, match="unknown format"):
            open_sealed(b"XXXX" + b"\x00" * 100, credential_id="c", access_key_id="a", kek=base64.b64decode(KEK))

    def test_load_kek_rejects_wrong_length(self, monkeypatch):
        monkeypatch.setenv("KEK_BASE64", base64.b64encode(b"short").decode())
        monkeypatch.delenv("KEK_FILE", raising=False)
        with pytest.raises(DecryptionError):
            load_kek()

    def test_load_kek_requires_configuration(self, monkeypatch):
        monkeypatch.delenv("KEK_BASE64", raising=False)
        monkeypatch.delenv("KEK_FILE", raising=False)
        with pytest.raises(DecryptionError, match="no key-encryption key"):
            load_kek()

    def test_decryption_error_is_an_app_error(self):
        assert issubclass(DecryptionError, AppError)


class TestRBAC:
    def test_admin_has_every_capability(self):
        assert capabilities_for_role("admin") == frozenset(Capability)

    def test_auditor_cannot_write(self):
        assert has_capability("auditor", Capability.OBJECT_READ) is True
        assert has_capability("auditor", Capability.OBJECT_WRITE) is False
        assert has_capability("auditor", Capability.OBJECT_DELETE) is False

    def test_only_admin_has_engine_admin(self):
        assert has_capability("admin", Capability.ENGINE_ADMIN) is True
        for role in ("owner", "operator", "auditor"):
            assert has_capability(role, Capability.ENGINE_ADMIN) is False

    def test_only_admin_can_write_settings(self):
        assert has_capability("admin", Capability.SETTINGS_WRITE) is True
        for role in ("owner", "operator", "auditor"):
            assert has_capability(role, Capability.SETTINGS_WRITE) is False

    def test_readwrite_implies_read_and_write(self):
        assert can_use_credential_permission("owner", frozenset({"readwrite"}), "read") is True
        assert can_use_credential_permission("owner", frozenset({"readwrite"}), "write") is True

    def test_read_does_not_imply_write(self):
        assert can_use_credential_permission("owner", frozenset({"read"}), "write") is False

    def test_unknown_role_rejected(self):
        with pytest.raises(ValueError):
            capabilities_for_role("superuser")