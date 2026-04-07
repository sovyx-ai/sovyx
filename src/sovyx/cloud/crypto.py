"""Backup cryptography — Argon2id key derivation + AES-256-GCM encrypt/decrypt.

Provides zero-knowledge encryption for cloud backups. The encryption key is
derived from a user passphrase using Argon2id (RFC 9106) and never leaves
the device. Deleting the salt renders all encrypted data unrecoverable
(crypto-shredding for GDPR compliance).

Wire format: salt(16) + nonce(12) + ciphertext + tag(16)

References:
    - IMPL-001 §1.2: Argon2id parameters calibrated for Pi 5
    - IMPL-001 §1.1: AES-GCM decision over XChaCha20
    - SPE-033 §2.1: Crypto-shredding for GDPR
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Argon2id parameters — RFC 9106 SECOND RECOMMENDED, Pi 5 calibrated (IMPL-001 §1.2)
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 65536  # 64 MiB
ARGON2_PARALLELISM = 4
ARGON2_HASH_LEN = 32  # 256-bit key for AES-256
ARGON2_SALT_LEN = 16  # RFC 9106 recommended
ARGON2_TYPE = Type.ID  # Argon2id — side-channel resistant

# AES-256-GCM parameters
NONCE_LEN = 12  # 96-bit nonce — NIST SP 800-38D recommended
TAG_LEN = 16  # GCM authentication tag (appended by AESGCM)
OVERHEAD = ARGON2_SALT_LEN + NONCE_LEN + TAG_LEN  # 44 bytes minimum


@dataclass(frozen=True, slots=True)
class DerivedKey:
    """Result of Argon2id key derivation."""

    key: bytes  # 32-byte AES-256 key
    salt: bytes  # 16-byte random salt


class BackupCrypto:
    """Zero-knowledge backup encryption using Argon2id + AES-256-GCM.

    Usage::

        crypto = BackupCrypto()
        encrypted = crypto.encrypt(data, password="user-passphrase")
        decrypted = crypto.decrypt(encrypted, password="user-passphrase")

    The salt is embedded in the ciphertext header, so the caller only needs
    to store the encrypted blob and remember the passphrase. Losing the
    passphrase means permanent data loss (by design — zero-knowledge).
    """

    @staticmethod
    def derive_key(password: str, salt: bytes | None = None) -> DerivedKey:
        """Derive a 256-bit key from a password using Argon2id.

        Args:
            password: User passphrase (must be non-empty).
            salt: Optional 16-byte salt. Generated randomly if ``None``.

        Returns:
            DerivedKey containing the 32-byte key and the salt used.

        Raises:
            ValueError: If *password* is empty or *salt* has wrong length.

        Performance:
            ~300 ms on Raspberry Pi 5 (ARM64 Cortex-A76).
            ~50 ms on modern x86-64.
        """
        if not password:
            msg = "Password must not be empty"
            raise ValueError(msg)

        if salt is None:
            salt = os.urandom(ARGON2_SALT_LEN)

        if len(salt) != ARGON2_SALT_LEN:
            msg = f"Salt must be {ARGON2_SALT_LEN} bytes, got {len(salt)}"
            raise ValueError(msg)

        key = hash_secret_raw(
            secret=password.encode("utf-8"),
            salt=salt,
            time_cost=ARGON2_TIME_COST,
            memory_cost=ARGON2_MEMORY_COST,
            parallelism=ARGON2_PARALLELISM,
            hash_len=ARGON2_HASH_LEN,
            type=ARGON2_TYPE,
        )

        return DerivedKey(key=key, salt=salt)

    @staticmethod
    def encrypt(data: bytes, password: str) -> bytes:
        """Encrypt data with a password using AES-256-GCM.

        The output includes the salt and nonce so it is fully self-contained.

        Wire format::

            [salt:16][nonce:12][ciphertext + GCM tag:N+16]

        Args:
            data: Plaintext bytes to encrypt.
            password: User passphrase for key derivation.

        Returns:
            Encrypted blob (salt + nonce + ciphertext + tag).

        Raises:
            ValueError: If *password* is empty.
        """
        derived = BackupCrypto.derive_key(password)
        nonce = os.urandom(NONCE_LEN)
        cipher = AESGCM(derived.key)
        ciphertext = cipher.encrypt(nonce, data, None)

        return derived.salt + nonce + ciphertext

    @staticmethod
    def decrypt(data: bytes, password: str) -> bytes:
        """Decrypt data that was encrypted with :meth:`encrypt`.

        Args:
            data: Encrypted blob (salt + nonce + ciphertext + tag).
            password: The same passphrase used for encryption.

        Returns:
            Original plaintext bytes.

        Raises:
            ValueError: If *data* is too short or *password* is empty.
            cryptography.exceptions.InvalidTag:
                If the password is wrong or the ciphertext was tampered with.
        """
        if len(data) < OVERHEAD:
            msg = f"Ciphertext too short: {len(data)} bytes (minimum {OVERHEAD})"
            raise ValueError(msg)

        salt = data[:ARGON2_SALT_LEN]
        nonce = data[ARGON2_SALT_LEN : ARGON2_SALT_LEN + NONCE_LEN]
        ciphertext = data[ARGON2_SALT_LEN + NONCE_LEN :]

        derived = BackupCrypto.derive_key(password, salt=salt)
        cipher = AESGCM(derived.key)

        return cipher.decrypt(nonce, ciphertext, None)

    @staticmethod
    def verify_password(data: bytes, password: str) -> bool:
        """Check if a password can decrypt the given data.

        A convenience wrapper that catches ``InvalidTag`` and returns a bool.

        Args:
            data: Encrypted blob.
            password: Passphrase to test.

        Returns:
            ``True`` if the password is correct, ``False`` otherwise.
        """
        try:
            BackupCrypto.decrypt(data, password)
        except Exception:  # noqa: BLE001
            return False
        return True
