"""API key service — create, validate, revoke, and list API keys.

Generates keys with ``svx_`` prefix and stores only SHA-256 hashes in the
database.  The raw key is returned exactly once on creation and never stored
or logged.

References:
    - SPE-033 §3.2: APIKeyService specification
    - Format: ``svx_{env}_{random}`` (Stripe/OpenAI pattern)
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from enum import IntFlag
from typing import TYPE_CHECKING
from uuid import uuid4

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────

KEY_PREFIX_LIVE = "svx_live_"
KEY_PREFIX_TEST = "svx_test_"
KEY_RANDOM_BYTES = 32  # 256-bit entropy via token_urlsafe
DEFAULT_RATE_LIMIT = 60  # requests per minute
KEY_SUFFIX_LENGTH = 4


class Scope(IntFlag):
    """API key permission scopes (bitmask)."""

    READ = 1
    WRITE = 2
    ADMIN = 4

    @classmethod
    def from_strings(cls, names: Sequence[str]) -> Scope:
        """Convert a list of scope name strings to a combined Scope bitmask.

        Args:
            names: Scope names (case-insensitive).  E.g. ``["read", "write"]``.

        Returns:
            Combined ``Scope`` value.

        Raises:
            ValueError: If any name is not a valid scope.
        """
        result = cls(0)
        for name in names:
            upper = name.upper()
            if upper not in cls.__members__:
                msg = f"Invalid scope: {name!r}. Valid: {sorted(cls.__members__)}"
                raise ValueError(msg)
            result |= cls[upper]
        return result

    def to_strings(self) -> list[str]:
        """Return list of individual scope name strings."""
        return [s.name.lower() for s in Scope if s in self and s.name is not None]


@dataclass(frozen=True, slots=True)
class APIKeyRecord:
    """Persisted API key metadata (hash only — never the raw key)."""

    id: UUID
    user_id: UUID
    key_hash: str
    key_prefix: str
    key_suffix: str
    name: str
    scopes: Scope
    rate_limit: int
    created_at: int  # unix timestamp
    expires_at: int | None = None  # unix timestamp, None = never
    revoked_at: int | None = None
    last_used_at: int | None = None

    @property
    def is_revoked(self) -> bool:
        """Whether this key has been revoked."""
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        """Whether this key has expired (if an expiry was set)."""
        if self.expires_at is None:
            return False
        return int(time.time()) > self.expires_at


@dataclass(frozen=True, slots=True)
class APIKeyInfo:
    """Public view of an API key (for listing — no hash)."""

    id: UUID
    name: str
    key_prefix: str
    key_suffix: str
    scopes: Scope
    rate_limit: int
    created_at: int
    expires_at: int | None = None
    revoked_at: int | None = None
    last_used_at: int | None = None

    @property
    def is_active(self) -> bool:
        """Whether this key is active (not revoked and not expired)."""
        if self.revoked_at is not None:
            return False
        return not (self.expires_at is not None and int(time.time()) > self.expires_at)


@dataclass(frozen=True, slots=True)
class APIKeyValidation:
    """Result of a successful key validation."""

    key_id: UUID
    user_id: UUID
    scopes: Scope
    rate_limit: int


@dataclass
class APIKeyStore:
    """In-memory key store (abstract interface for persistence).

    Subclass or replace with a real database-backed implementation.
    """

    _keys: dict[str, APIKeyRecord] = field(default_factory=dict)  # hash → record
    _by_id: dict[UUID, APIKeyRecord] = field(default_factory=dict)
    _by_user: dict[UUID, list[UUID]] = field(default_factory=dict)

    async def insert(self, record: APIKeyRecord) -> None:
        """Store a new API key record."""
        self._keys[record.key_hash] = record
        self._by_id[record.id] = record
        self._by_user.setdefault(record.user_id, []).append(record.id)

    async def get_by_hash(self, key_hash: str) -> APIKeyRecord | None:
        """Look up a key by its SHA-256 hash."""
        return self._keys.get(key_hash)

    async def get_by_id(self, key_id: UUID) -> APIKeyRecord | None:
        """Look up a key by its UUID."""
        return self._by_id.get(key_id)

    async def list_by_user(self, user_id: UUID) -> list[APIKeyRecord]:
        """List all keys belonging to a user."""
        key_ids = self._by_user.get(user_id, [])
        return [self._by_id[kid] for kid in key_ids if kid in self._by_id]

    async def update(self, record: APIKeyRecord) -> None:
        """Replace a key record (for revocation, touch, etc.)."""
        self._keys[record.key_hash] = record
        self._by_id[record.id] = record

    async def touch(self, key_id: UUID) -> None:
        """Update last_used_at timestamp for a key."""
        record = self._by_id.get(key_id)
        if record is None:
            return
        # Frozen dataclass — must replace
        updated = APIKeyRecord(
            id=record.id,
            user_id=record.user_id,
            key_hash=record.key_hash,
            key_prefix=record.key_prefix,
            key_suffix=record.key_suffix,
            name=record.name,
            scopes=record.scopes,
            rate_limit=record.rate_limit,
            created_at=record.created_at,
            expires_at=record.expires_at,
            revoked_at=record.revoked_at,
            last_used_at=int(time.time()),
        )
        await self.update(updated)


def _hash_key(raw_key: str) -> str:
    """Compute SHA-256 hex digest of a raw key string."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


class APIKeyService:
    """Manage Sovyx API keys — create, revoke, list, validate.

    Keys follow the format ``svx_{env}_{random}`` where env is ``live``
    or ``test``, and the random part is 32 bytes of URL-safe base64.

    Only the SHA-256 hash of the key is persisted.  The raw key is returned
    to the caller exactly once on creation.

    Example::

        store = APIKeyStore()
        service = APIKeyService(store)
        raw_key, info = await service.create(user_id, "My Key", [Scope.READ])
        # raw_key = "svx_live_..." — show to user ONCE
        validation = await service.validate(raw_key)

    """

    def __init__(self, store: APIKeyStore) -> None:
        self._store = store

    async def create(
        self,
        user_id: UUID,
        name: str = "Default",
        scopes: Scope | None = None,
        *,
        environment: str = "live",
        rate_limit: int = DEFAULT_RATE_LIMIT,
        expires_at: int | None = None,
    ) -> tuple[str, APIKeyInfo]:
        """Create a new API key.

        Args:
            user_id: Owner's account UUID.
            name: Human-readable key name.
            scopes: Permission bitmask.  Defaults to READ.
            environment: ``"live"`` or ``"test"``.
            rate_limit: Max requests per minute for this key.
            expires_at: Optional unix timestamp for key expiry.

        Returns:
            Tuple of ``(raw_key, info)``.  The raw key is shown once.

        Raises:
            ValueError: If environment is invalid.
        """
        if environment not in ("live", "test"):
            msg = f"Invalid environment: {environment!r}. Must be 'live' or 'test'"
            raise ValueError(msg)

        if scopes is None:
            scopes = Scope.READ

        prefix = KEY_PREFIX_LIVE if environment == "live" else KEY_PREFIX_TEST
        random_part = secrets.token_urlsafe(KEY_RANDOM_BYTES)
        raw_key = f"{prefix}{random_part}"

        key_hash = _hash_key(raw_key)
        key_id = uuid4()
        now = int(time.time())

        record = APIKeyRecord(
            id=key_id,
            user_id=user_id,
            key_hash=key_hash,
            key_prefix=prefix,
            key_suffix=random_part[-KEY_SUFFIX_LENGTH:],
            name=name,
            scopes=scopes,
            rate_limit=rate_limit,
            created_at=now,
            expires_at=expires_at,
        )

        await self._store.insert(record)

        logger.info(
            "API key created",
            key_id=str(key_id),
            user_id=str(user_id),
            name=name,
            environment=environment,
            scopes=scopes.to_strings(),
        )

        info = _record_to_info(record)
        return raw_key, info

    async def validate(self, raw_key: str) -> APIKeyValidation | None:
        """Validate a raw API key string.

        Performs O(1) hash-index lookup.  Returns None if the key is
        unknown, revoked, or expired.  Updates ``last_used_at`` in the
        background.

        Args:
            raw_key: The full key string (``svx_live_...`` or ``svx_test_...``).

        Returns:
            ``APIKeyValidation`` on success, ``None`` on failure.
        """
        key_hash = _hash_key(raw_key)
        record = await self._store.get_by_hash(key_hash)

        if record is None:
            return None

        if record.is_revoked:
            logger.debug("Rejected revoked key", key_id=str(record.id))
            return None

        if record.is_expired:
            logger.debug("Rejected expired key", key_id=str(record.id))
            return None

        # Update last_used_at (fire-and-forget)
        asyncio.create_task(self._store.touch(record.id))

        return APIKeyValidation(
            key_id=record.id,
            user_id=record.user_id,
            scopes=record.scopes,
            rate_limit=record.rate_limit,
        )

    async def revoke(self, key_id: UUID) -> bool:
        """Revoke an API key by its UUID.

        Args:
            key_id: The key's unique identifier.

        Returns:
            True if the key was found and revoked, False if not found
            or already revoked.
        """
        record = await self._store.get_by_id(key_id)
        if record is None:
            return False

        if record.is_revoked:
            return False

        revoked = APIKeyRecord(
            id=record.id,
            user_id=record.user_id,
            key_hash=record.key_hash,
            key_prefix=record.key_prefix,
            key_suffix=record.key_suffix,
            name=record.name,
            scopes=record.scopes,
            rate_limit=record.rate_limit,
            created_at=record.created_at,
            expires_at=record.expires_at,
            revoked_at=int(time.time()),
            last_used_at=record.last_used_at,
        )

        await self._store.update(revoked)
        logger.info("API key revoked", key_id=str(key_id))
        return True

    async def list_keys(self, user_id: UUID) -> list[APIKeyInfo]:
        """List all API keys for a user.

        Args:
            user_id: Owner's account UUID.

        Returns:
            List of ``APIKeyInfo`` (no hashes exposed).
        """
        records = await self._store.list_by_user(user_id)
        return [_record_to_info(r) for r in records]

    async def get_key(self, key_id: UUID) -> APIKeyInfo | None:
        """Get a single key's info by its UUID.

        Args:
            key_id: The key's unique identifier.

        Returns:
            ``APIKeyInfo`` or None if not found.
        """
        record = await self._store.get_by_id(key_id)
        if record is None:
            return None
        return _record_to_info(record)


def _record_to_info(record: APIKeyRecord) -> APIKeyInfo:
    """Convert an internal record to a public info object."""
    return APIKeyInfo(
        id=record.id,
        name=record.name,
        key_prefix=record.key_prefix,
        key_suffix=record.key_suffix,
        scopes=record.scopes,
        rate_limit=record.rate_limit,
        created_at=record.created_at,
        expires_at=record.expires_at,
        revoked_at=record.revoked_at,
        last_used_at=record.last_used_at,
    )

