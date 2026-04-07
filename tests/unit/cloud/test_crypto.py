"""Tests for BackupCrypto — Argon2id + AES-256-GCM (V05-06).

Covers: roundtrip, wrong password, tampered ciphertext, different salt,
edge cases, and property-based tests via Hypothesis.
"""

from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.cloud.crypto import (
    ARGON2_HASH_LEN,
    ARGON2_SALT_LEN,
    NONCE_LEN,
    OVERHEAD,
    BackupCrypto,
    DerivedKey,
)


class TestDeriveKey:
    """Tests for BackupCrypto.derive_key."""

    def test_produces_32_byte_key(self) -> None:
        result = BackupCrypto.derive_key("test-passphrase")
        assert len(result.key) == ARGON2_HASH_LEN
        assert isinstance(result.key, bytes)

    def test_produces_16_byte_salt(self) -> None:
        result = BackupCrypto.derive_key("test-passphrase")
        assert len(result.salt) == ARGON2_SALT_LEN

    def test_returns_derived_key_dataclass(self) -> None:
        result = BackupCrypto.derive_key("test")
        assert isinstance(result, DerivedKey)

    def test_same_password_same_salt_same_key(self) -> None:
        salt = os.urandom(ARGON2_SALT_LEN)
        k1 = BackupCrypto.derive_key("hunter2", salt=salt)
        k2 = BackupCrypto.derive_key("hunter2", salt=salt)
        assert k1.key == k2.key

    def test_different_salt_different_key(self) -> None:
        k1 = BackupCrypto.derive_key("same-pass", salt=b"\x00" * ARGON2_SALT_LEN)
        k2 = BackupCrypto.derive_key("same-pass", salt=b"\x01" * ARGON2_SALT_LEN)
        assert k1.key != k2.key

    def test_different_password_different_key(self) -> None:
        salt = os.urandom(ARGON2_SALT_LEN)
        k1 = BackupCrypto.derive_key("password-a", salt=salt)
        k2 = BackupCrypto.derive_key("password-b", salt=salt)
        assert k1.key != k2.key

    def test_empty_password_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            BackupCrypto.derive_key("")

    def test_wrong_salt_length_raises(self) -> None:
        with pytest.raises(ValueError, match="16 bytes"):
            BackupCrypto.derive_key("test", salt=b"\x00" * 8)

    def test_custom_salt_used(self) -> None:
        salt = b"\xab" * ARGON2_SALT_LEN
        result = BackupCrypto.derive_key("test", salt=salt)
        assert result.salt == salt

    def test_generated_salt_is_random(self) -> None:
        k1 = BackupCrypto.derive_key("test")
        k2 = BackupCrypto.derive_key("test")
        # Salts should differ (random), therefore keys differ
        assert k1.salt != k2.salt
        assert k1.key != k2.key

    def test_derived_key_is_frozen(self) -> None:
        result = BackupCrypto.derive_key("test")
        with pytest.raises(AttributeError):
            result.key = b"tampered"  # type: ignore[misc]


class TestEncryptDecrypt:
    """Tests for BackupCrypto.encrypt / decrypt roundtrip."""

    def test_roundtrip(self) -> None:
        plaintext = b"sensitive backup data"
        encrypted = BackupCrypto.encrypt(plaintext, "my-password")
        decrypted = BackupCrypto.decrypt(encrypted, "my-password")
        assert decrypted == plaintext

    def test_roundtrip_empty_data(self) -> None:
        encrypted = BackupCrypto.encrypt(b"", "password")
        decrypted = BackupCrypto.decrypt(encrypted, "password")
        assert decrypted == b""

    def test_roundtrip_large_data(self) -> None:
        plaintext = os.urandom(1024 * 1024)  # 1 MiB
        encrypted = BackupCrypto.encrypt(plaintext, "strong-pass")
        decrypted = BackupCrypto.decrypt(encrypted, "strong-pass")
        assert decrypted == plaintext

    def test_wrong_password_raises_invalid_tag(self) -> None:
        encrypted = BackupCrypto.encrypt(b"secret", "correct")
        with pytest.raises(InvalidTag):
            BackupCrypto.decrypt(encrypted, "wrong")

    def test_tampered_ciphertext_raises_invalid_tag(self) -> None:
        encrypted = bytearray(BackupCrypto.encrypt(b"data", "pass"))
        # Flip a byte in the ciphertext area (after salt + nonce)
        idx = ARGON2_SALT_LEN + NONCE_LEN + 1
        encrypted[idx] ^= 0xFF
        with pytest.raises(InvalidTag):
            BackupCrypto.decrypt(bytes(encrypted), "pass")

    def test_tampered_salt_raises_invalid_tag(self) -> None:
        encrypted = bytearray(BackupCrypto.encrypt(b"data", "pass"))
        # Flip a byte in the salt — derives different key
        encrypted[0] ^= 0xFF
        with pytest.raises(InvalidTag):
            BackupCrypto.decrypt(bytes(encrypted), "pass")

    def test_tampered_nonce_raises_invalid_tag(self) -> None:
        encrypted = bytearray(BackupCrypto.encrypt(b"data", "pass"))
        # Flip a byte in the nonce area
        encrypted[ARGON2_SALT_LEN] ^= 0xFF
        with pytest.raises(InvalidTag):
            BackupCrypto.decrypt(bytes(encrypted), "pass")

    def test_ciphertext_too_short_raises(self) -> None:
        with pytest.raises(ValueError, match="too short"):
            BackupCrypto.decrypt(b"\x00" * (OVERHEAD - 1), "pass")

    def test_encrypted_output_has_overhead(self) -> None:
        plaintext = b"hello"
        encrypted = BackupCrypto.encrypt(plaintext, "pass")
        # output = salt(16) + nonce(12) + ciphertext(5) + tag(16) = 49
        assert len(encrypted) == len(plaintext) + OVERHEAD

    def test_each_encryption_is_unique(self) -> None:
        """Random salt + random nonce means no two encryptions are the same."""
        plaintext = b"same-data"
        e1 = BackupCrypto.encrypt(plaintext, "same-pass")
        e2 = BackupCrypto.encrypt(plaintext, "same-pass")
        assert e1 != e2

    def test_empty_password_encrypt_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            BackupCrypto.encrypt(b"data", "")

    def test_empty_password_decrypt_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            BackupCrypto.decrypt(b"\x00" * OVERHEAD, "")

    def test_unicode_password(self) -> None:
        plaintext = b"dados secretos"
        encrypted = BackupCrypto.encrypt(plaintext, "sëñhà-com-àçéñtôs-🔮")
        decrypted = BackupCrypto.decrypt(encrypted, "sëñhà-com-àçéñtôs-🔮")
        assert decrypted == plaintext

    def test_long_password(self) -> None:
        password = "a" * 10_000
        plaintext = b"long-password-test"
        encrypted = BackupCrypto.encrypt(plaintext, password)
        decrypted = BackupCrypto.decrypt(encrypted, password)
        assert decrypted == plaintext


class TestVerifyPassword:
    """Tests for BackupCrypto.verify_password."""

    def test_correct_password_returns_true(self) -> None:
        encrypted = BackupCrypto.encrypt(b"data", "right")
        assert BackupCrypto.verify_password(encrypted, "right") is True

    def test_wrong_password_returns_false(self) -> None:
        encrypted = BackupCrypto.encrypt(b"data", "right")
        assert BackupCrypto.verify_password(encrypted, "wrong") is False

    def test_tampered_data_returns_false(self) -> None:
        encrypted = bytearray(BackupCrypto.encrypt(b"data", "pass"))
        encrypted[-1] ^= 0xFF
        assert BackupCrypto.verify_password(bytes(encrypted), "pass") is False

    def test_too_short_data_returns_false(self) -> None:
        assert BackupCrypto.verify_password(b"short", "pass") is False


class TestCryptoShredding:
    """Tests verifying crypto-shredding property (GDPR compliance)."""

    def test_different_salt_cannot_decrypt(self) -> None:
        """Deleting the salt makes decryption impossible (crypto-shredding)."""
        plaintext = b"GDPR-sensitive data"
        encrypted = BackupCrypto.encrypt(plaintext, "password")

        # Replace salt with random bytes (simulates deletion + garbage)
        mangled = os.urandom(ARGON2_SALT_LEN) + encrypted[ARGON2_SALT_LEN:]
        with pytest.raises(InvalidTag):
            BackupCrypto.decrypt(mangled, "password")


class TestPropertyBased:
    """Property-based tests using Hypothesis."""

    @settings(
        deadline=None,
        max_examples=10,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        data=st.binary(min_size=0, max_size=4096),
        password=st.text(min_size=1, max_size=200),
    )
    def test_roundtrip_any_data_any_password(self, data: bytes, password: str) -> None:
        encrypted = BackupCrypto.encrypt(data, password)
        decrypted = BackupCrypto.decrypt(encrypted, password)
        assert decrypted == data

    @settings(
        deadline=None,
        max_examples=10,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        data=st.binary(min_size=1, max_size=256),
        correct=st.text(min_size=1, max_size=50),
        wrong=st.text(min_size=1, max_size=50),
    )
    def test_wrong_password_never_decrypts(self, data: bytes, correct: str, wrong: str) -> None:
        """A wrong password should never produce the original plaintext."""
        if correct == wrong:
            return  # Skip when passwords happen to match
        encrypted = BackupCrypto.encrypt(data, correct)
        assert BackupCrypto.verify_password(encrypted, wrong) is False

    @settings(
        deadline=None,
        max_examples=10,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(data=st.binary(min_size=0, max_size=1024))
    def test_encrypted_size_is_predictable(self, data: bytes) -> None:
        encrypted = BackupCrypto.encrypt(data, "test-password")
        assert len(encrypted) == len(data) + OVERHEAD
