"""Tests for APIKeyService (V05-10).

Covers create, validate, revoke, list, scopes (bitmask), SHA-256 storage,
svx_ prefix, rate limits, expiration, and edge cases.
"""

from __future__ import annotations

import hashlib
import time
from uuid import UUID, uuid4

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.cloud.apikeys import (
    KEY_PREFIX_LIVE,
    KEY_PREFIX_TEST,
    KEY_SUFFIX_LENGTH,
    APIKeyInfo,
    APIKeyRecord,
    APIKeyService,
    APIKeyStore,
    APIKeyValidation,
    Scope,
    _hash_key,
    _record_to_info,
)

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def store() -> APIKeyStore:
    """Fresh in-memory key store."""
    return APIKeyStore()


@pytest.fixture()
def service(store: APIKeyStore) -> APIKeyService:
    """APIKeyService wired to an in-memory store."""
    return APIKeyService(store)


@pytest.fixture()
def user_id() -> UUID:
    """Deterministic user UUID for tests."""
    return uuid4()


# ── Scope tests ───────────────────────────────────────────────────────────


class TestScope:
    """Scope bitmask tests."""

    def test_individual_values(self) -> None:
        assert Scope.READ == 1
        assert Scope.WRITE == 2
        assert Scope.ADMIN == 4

    def test_combination(self) -> None:
        combined = Scope.READ | Scope.WRITE
        assert Scope.READ in combined
        assert Scope.WRITE in combined
        assert Scope.ADMIN not in combined

    def test_from_strings(self) -> None:
        result = Scope.from_strings(["read", "write"])
        assert result == Scope.READ | Scope.WRITE

    def test_from_strings_case_insensitive(self) -> None:
        result = Scope.from_strings(["READ", "Write", "admin"])
        assert result == Scope.READ | Scope.WRITE | Scope.ADMIN

    def test_from_strings_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid scope"):
            Scope.from_strings(["read", "delete"])

    def test_from_strings_empty(self) -> None:
        result = Scope.from_strings([])
        assert result == Scope(0)

    def test_to_strings(self) -> None:
        combined = Scope.READ | Scope.ADMIN
        names = combined.to_strings()
        assert sorted(names) == ["admin", "read"]

    def test_to_strings_single(self) -> None:
        assert Scope.WRITE.to_strings() == ["write"]

    def test_all_scopes(self) -> None:
        all_scopes = Scope.READ | Scope.WRITE | Scope.ADMIN
        assert all_scopes == 7


# ── hash_key tests ────────────────────────────────────────────────────────


class TestHashKey:
    """SHA-256 hashing."""

    def test_deterministic(self) -> None:
        assert _hash_key("svx_live_abc") == _hash_key("svx_live_abc")

    def test_different_keys_different_hashes(self) -> None:
        assert _hash_key("svx_live_abc") != _hash_key("svx_live_xyz")

    def test_sha256_format(self) -> None:
        h = _hash_key("test")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_matches_hashlib(self) -> None:
        raw = "svx_live_test123"
        expected = hashlib.sha256(raw.encode()).hexdigest()
        assert _hash_key(raw) == expected


# ── APIKeyRecord tests ────────────────────────────────────────────────────


class TestAPIKeyRecord:
    """Record property tests."""

    def test_is_revoked_false(self) -> None:
        record = self._make_record()
        assert record.is_revoked is False

    def test_is_revoked_true(self) -> None:
        record = self._make_record(revoked_at=int(time.time()))
        assert record.is_revoked is True

    def test_is_expired_no_expiry(self) -> None:
        record = self._make_record(expires_at=None)
        assert record.is_expired is False

    def test_is_expired_future(self) -> None:
        record = self._make_record(expires_at=int(time.time()) + 3600)
        assert record.is_expired is False

    def test_is_expired_past(self) -> None:
        record = self._make_record(expires_at=int(time.time()) - 1)
        assert record.is_expired is True

    @staticmethod
    def _make_record(
        *,
        revoked_at: int | None = None,
        expires_at: int | None = None,
    ) -> APIKeyRecord:
        return APIKeyRecord(
            id=uuid4(),
            user_id=uuid4(),
            key_hash="abc123",
            key_prefix=KEY_PREFIX_LIVE,
            key_suffix="test",
            name="test",
            scopes=Scope.READ,
            rate_limit=60,
            created_at=int(time.time()),
            expires_at=expires_at,
            revoked_at=revoked_at,
        )


# ── APIKeyInfo tests ──────────────────────────────────────────────────────


class TestAPIKeyInfo:
    """Info view tests."""

    def test_is_active_normal(self) -> None:
        info = APIKeyInfo(
            id=uuid4(),
            name="test",
            key_prefix=KEY_PREFIX_LIVE,
            key_suffix="abcd",
            scopes=Scope.READ,
            rate_limit=60,
            created_at=int(time.time()),
        )
        assert info.is_active is True

    def test_is_active_revoked(self) -> None:
        info = APIKeyInfo(
            id=uuid4(),
            name="test",
            key_prefix=KEY_PREFIX_LIVE,
            key_suffix="abcd",
            scopes=Scope.READ,
            rate_limit=60,
            created_at=int(time.time()),
            revoked_at=int(time.time()),
        )
        assert info.is_active is False

    def test_is_active_expired(self) -> None:
        info = APIKeyInfo(
            id=uuid4(),
            name="test",
            key_prefix=KEY_PREFIX_LIVE,
            key_suffix="abcd",
            scopes=Scope.READ,
            rate_limit=60,
            created_at=int(time.time()),
            expires_at=int(time.time()) - 1,
        )
        assert info.is_active is False


# ── APIKeyStore tests ─────────────────────────────────────────────────────


class TestAPIKeyStore:
    """In-memory store operations."""

    @pytest.mark.asyncio()
    async def test_insert_and_get_by_hash(self, store: APIKeyStore) -> None:
        record = self._make_record()
        await store.insert(record)
        result = await store.get_by_hash(record.key_hash)
        assert result is not None
        assert result.id == record.id

    @pytest.mark.asyncio()
    async def test_get_by_hash_not_found(self, store: APIKeyStore) -> None:
        result = await store.get_by_hash("nonexistent")
        assert result is None

    @pytest.mark.asyncio()
    async def test_get_by_id(self, store: APIKeyStore) -> None:
        record = self._make_record()
        await store.insert(record)
        result = await store.get_by_id(record.id)
        assert result is not None
        assert result.key_hash == record.key_hash

    @pytest.mark.asyncio()
    async def test_get_by_id_not_found(self, store: APIKeyStore) -> None:
        result = await store.get_by_id(uuid4())
        assert result is None

    @pytest.mark.asyncio()
    async def test_list_by_user(self, store: APIKeyStore) -> None:
        uid = uuid4()
        r1 = self._make_record(user_id=uid, key_hash="h1")
        r2 = self._make_record(user_id=uid, key_hash="h2")
        r3 = self._make_record(key_hash="h3")  # different user
        await store.insert(r1)
        await store.insert(r2)
        await store.insert(r3)
        results = await store.list_by_user(uid)
        assert len(results) == 2

    @pytest.mark.asyncio()
    async def test_list_by_user_empty(self, store: APIKeyStore) -> None:
        results = await store.list_by_user(uuid4())
        assert results == []

    @pytest.mark.asyncio()
    async def test_update(self, store: APIKeyStore) -> None:
        record = self._make_record()
        await store.insert(record)
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
            revoked_at=int(time.time()),
        )
        await store.update(revoked)
        result = await store.get_by_id(record.id)
        assert result is not None
        assert result.is_revoked

    @pytest.mark.asyncio()
    async def test_touch(self, store: APIKeyStore) -> None:
        record = self._make_record()
        await store.insert(record)
        assert record.last_used_at is None
        await store.touch(record.id)
        updated = await store.get_by_id(record.id)
        assert updated is not None
        assert updated.last_used_at is not None

    @pytest.mark.asyncio()
    async def test_touch_nonexistent(self, store: APIKeyStore) -> None:
        # Should not raise
        await store.touch(uuid4())

    @staticmethod
    def _make_record(
        *,
        user_id: UUID | None = None,
        key_hash: str = "default_hash",
    ) -> APIKeyRecord:
        return APIKeyRecord(
            id=uuid4(),
            user_id=user_id or uuid4(),
            key_hash=key_hash,
            key_prefix=KEY_PREFIX_LIVE,
            key_suffix="test",
            name="test",
            scopes=Scope.READ,
            rate_limit=60,
            created_at=int(time.time()),
        )


# ── APIKeyService.create tests ───────────────────────────────────────────


class TestCreate:
    """Key creation tests."""

    @pytest.mark.asyncio()
    async def test_create_returns_raw_key_and_info(
        self, service: APIKeyService, user_id: UUID
    ) -> None:
        raw_key, info = await service.create(user_id, "My Key")
        assert raw_key.startswith(KEY_PREFIX_LIVE)
        assert isinstance(info, APIKeyInfo)
        assert info.name == "My Key"

    @pytest.mark.asyncio()
    async def test_create_live_environment(
        self, service: APIKeyService, user_id: UUID
    ) -> None:
        raw_key, _ = await service.create(user_id, environment="live")
        assert raw_key.startswith(KEY_PREFIX_LIVE)

    @pytest.mark.asyncio()
    async def test_create_test_environment(
        self, service: APIKeyService, user_id: UUID
    ) -> None:
        raw_key, info = await service.create(user_id, environment="test")
        assert raw_key.startswith(KEY_PREFIX_TEST)
        assert info.key_prefix == KEY_PREFIX_TEST

    @pytest.mark.asyncio()
    async def test_create_invalid_environment(
        self, service: APIKeyService, user_id: UUID
    ) -> None:
        with pytest.raises(ValueError, match="Invalid environment"):
            await service.create(user_id, environment="staging")

    @pytest.mark.asyncio()
    async def test_create_default_scope_is_read(
        self, service: APIKeyService, user_id: UUID
    ) -> None:
        _, info = await service.create(user_id)
        assert info.scopes == Scope.READ

    @pytest.mark.asyncio()
    async def test_create_custom_scopes(
        self, service: APIKeyService, user_id: UUID
    ) -> None:
        _, info = await service.create(
            user_id, scopes=Scope.READ | Scope.WRITE
        )
        assert Scope.READ in info.scopes
        assert Scope.WRITE in info.scopes

    @pytest.mark.asyncio()
    async def test_create_custom_rate_limit(
        self, service: APIKeyService, user_id: UUID
    ) -> None:
        _, info = await service.create(user_id, rate_limit=120)
        assert info.rate_limit == 120

    @pytest.mark.asyncio()
    async def test_create_with_expiry(
        self, service: APIKeyService, user_id: UUID
    ) -> None:
        exp = int(time.time()) + 86400
        _, info = await service.create(user_id, expires_at=exp)
        assert info.expires_at == exp

    @pytest.mark.asyncio()
    async def test_create_stores_hash_not_key(
        self, service: APIKeyService, store: APIKeyStore, user_id: UUID
    ) -> None:
        raw_key, info = await service.create(user_id)
        record = await store.get_by_id(info.id)
        assert record is not None
        # Hash matches
        assert record.key_hash == _hash_key(raw_key)
        # Raw key is not stored anywhere in the record
        assert raw_key not in str(record)

    @pytest.mark.asyncio()
    async def test_create_suffix_is_last_chars(
        self, service: APIKeyService, user_id: UUID
    ) -> None:
        raw_key, info = await service.create(user_id)
        prefix = info.key_prefix
        random_part = raw_key[len(prefix):]
        assert info.key_suffix == random_part[-KEY_SUFFIX_LENGTH:]

    @pytest.mark.asyncio()
    async def test_create_unique_keys(
        self, service: APIKeyService, user_id: UUID
    ) -> None:
        key1, _ = await service.create(user_id, "Key 1")
        key2, _ = await service.create(user_id, "Key 2")
        assert key1 != key2

    @pytest.mark.asyncio()
    async def test_create_key_length(
        self, service: APIKeyService, user_id: UUID
    ) -> None:
        raw_key, _ = await service.create(user_id)
        # svx_live_ (9) + base64url(32 bytes) = 9 + 43 = 52 chars
        assert len(raw_key) > len(KEY_PREFIX_LIVE) + 20  # reasonable minimum


# ── APIKeyService.validate tests ──────────────────────────────────────────


class TestValidate:
    """Key validation tests."""

    @pytest.mark.asyncio()
    async def test_validate_valid_key(
        self, service: APIKeyService, user_id: UUID
    ) -> None:
        raw_key, info = await service.create(user_id, scopes=Scope.READ | Scope.WRITE)
        result = await service.validate(raw_key)
        assert result is not None
        assert isinstance(result, APIKeyValidation)
        assert result.user_id == user_id
        assert result.scopes == Scope.READ | Scope.WRITE

    @pytest.mark.asyncio()
    async def test_validate_unknown_key(self, service: APIKeyService) -> None:
        result = await service.validate("svx_live_nonexistent")
        assert result is None

    @pytest.mark.asyncio()
    async def test_validate_revoked_key(
        self, service: APIKeyService, user_id: UUID
    ) -> None:
        raw_key, info = await service.create(user_id)
        await service.revoke(info.id)
        result = await service.validate(raw_key)
        assert result is None

    @pytest.mark.asyncio()
    async def test_validate_expired_key(
        self, service: APIKeyService, user_id: UUID
    ) -> None:
        raw_key, _ = await service.create(
            user_id, expires_at=int(time.time()) - 1
        )
        result = await service.validate(raw_key)
        assert result is None

    @pytest.mark.asyncio()
    async def test_validate_returns_rate_limit(
        self, service: APIKeyService, user_id: UUID
    ) -> None:
        raw_key, _ = await service.create(user_id, rate_limit=300)
        result = await service.validate(raw_key)
        assert result is not None
        assert result.rate_limit == 300

    @pytest.mark.asyncio()
    async def test_validate_updates_last_used(
        self, service: APIKeyService, store: APIKeyStore, user_id: UUID
    ) -> None:
        raw_key, info = await service.create(user_id)
        # Before validation, last_used_at is None
        record_before = await store.get_by_id(info.id)
        assert record_before is not None
        assert record_before.last_used_at is None
        # Validate triggers touch via create_task
        await service.validate(raw_key)
        # Give the background task a chance to run
        import asyncio

        await asyncio.sleep(0.01)
        record_after = await store.get_by_id(info.id)
        assert record_after is not None
        assert record_after.last_used_at is not None


# ── APIKeyService.revoke tests ────────────────────────────────────────────


class TestRevoke:
    """Key revocation tests."""

    @pytest.mark.asyncio()
    async def test_revoke_success(
        self, service: APIKeyService, user_id: UUID
    ) -> None:
        _, info = await service.create(user_id)
        assert await service.revoke(info.id) is True

    @pytest.mark.asyncio()
    async def test_revoke_not_found(self, service: APIKeyService) -> None:
        assert await service.revoke(uuid4()) is False

    @pytest.mark.asyncio()
    async def test_revoke_already_revoked(
        self, service: APIKeyService, user_id: UUID
    ) -> None:
        _, info = await service.create(user_id)
        assert await service.revoke(info.id) is True
        assert await service.revoke(info.id) is False

    @pytest.mark.asyncio()
    async def test_revoke_sets_timestamp(
        self, service: APIKeyService, store: APIKeyStore, user_id: UUID
    ) -> None:
        _, info = await service.create(user_id)
        before = int(time.time())
        await service.revoke(info.id)
        record = await store.get_by_id(info.id)
        assert record is not None
        assert record.revoked_at is not None
        assert record.revoked_at >= before


# ── APIKeyService.list tests ─────────────────────────────────────────────


class TestListKeys:
    """Key listing tests."""

    @pytest.mark.asyncio()
    async def test_list_empty(
        self, service: APIKeyService, user_id: UUID
    ) -> None:
        result = await service.list_keys(user_id)
        assert result == []

    @pytest.mark.asyncio()
    async def test_list_multiple(
        self, service: APIKeyService, user_id: UUID
    ) -> None:
        await service.create(user_id, "Key 1")
        await service.create(user_id, "Key 2")
        await service.create(user_id, "Key 3")
        result = await service.list_keys(user_id)
        assert len(result) == 3
        names = {k.name for k in result}
        assert names == {"Key 1", "Key 2", "Key 3"}

    @pytest.mark.asyncio()
    async def test_list_includes_revoked(
        self, service: APIKeyService, user_id: UUID
    ) -> None:
        _, info1 = await service.create(user_id, "Active")
        _, info2 = await service.create(user_id, "Revoked")
        await service.revoke(info2.id)
        result = await service.list_keys(user_id)
        assert len(result) == 2

    @pytest.mark.asyncio()
    async def test_list_no_hashes_exposed(
        self, service: APIKeyService, user_id: UUID
    ) -> None:
        await service.create(user_id)
        keys = await service.list_keys(user_id)
        for k in keys:
            assert isinstance(k, APIKeyInfo)
            assert not hasattr(k, "key_hash")

    @pytest.mark.asyncio()
    async def test_list_isolation(self, service: APIKeyService) -> None:
        """Keys from different users don't mix."""
        user_a = uuid4()
        user_b = uuid4()
        await service.create(user_a, "A's key")
        await service.create(user_b, "B's key")
        assert len(await service.list_keys(user_a)) == 1
        assert len(await service.list_keys(user_b)) == 1


# ── APIKeyService.get_key tests ──────────────────────────────────────────


class TestGetKey:
    """Single key retrieval."""

    @pytest.mark.asyncio()
    async def test_get_key_found(
        self, service: APIKeyService, user_id: UUID
    ) -> None:
        _, info = await service.create(user_id, "Test Key")
        result = await service.get_key(info.id)
        assert result is not None
        assert result.name == "Test Key"

    @pytest.mark.asyncio()
    async def test_get_key_not_found(self, service: APIKeyService) -> None:
        result = await service.get_key(uuid4())
        assert result is None


# ── record_to_info tests ─────────────────────────────────────────────────


class TestRecordToInfo:
    """Conversion from record to public info."""

    def test_strips_hash_and_user_id(self) -> None:
        record = APIKeyRecord(
            id=uuid4(),
            user_id=uuid4(),
            key_hash="secret_hash",
            key_prefix=KEY_PREFIX_LIVE,
            key_suffix="abcd",
            name="test",
            scopes=Scope.READ,
            rate_limit=60,
            created_at=int(time.time()),
        )
        info = _record_to_info(record)
        assert info.id == record.id
        assert info.name == record.name
        assert info.key_prefix == record.key_prefix
        assert info.key_suffix == record.key_suffix
        assert info.scopes == record.scopes
        assert not hasattr(info, "key_hash")
        assert not hasattr(info, "user_id")


# ── Property-based tests ─────────────────────────────────────────────────


class TestProperties:
    """Hypothesis property-based tests."""

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(name=st.text(min_size=1, max_size=100))
    @pytest.mark.asyncio()
    async def test_create_roundtrip(self, name: str) -> None:
        """Any valid name should create a key that validates."""
        store = APIKeyStore()
        service = APIKeyService(store)
        uid = uuid4()
        raw_key, info = await service.create(uid, name)
        assert info.name == name
        result = await service.validate(raw_key)
        assert result is not None
        assert result.user_id == uid

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        scopes=st.lists(
            st.sampled_from(["read", "write", "admin"]),
            min_size=1,
            max_size=3,
            unique=True,
        )
    )
    @pytest.mark.asyncio()
    async def test_scope_roundtrip(self, scopes: list[str]) -> None:
        """Scope from_strings → to_strings preserves all scope names."""
        combined = Scope.from_strings(scopes)
        result_names = combined.to_strings()
        assert set(result_names) == {s.lower() for s in scopes}
