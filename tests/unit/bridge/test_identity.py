"""Tests for sovyx.bridge.identity — PersonResolver."""

from __future__ import annotations

import pytest

from sovyx.bridge.identity import PersonResolver
from sovyx.engine.types import ChannelType, PersonId
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.system import get_system_migrations


@pytest.fixture
async def pool(tmp_path: object) -> DatabasePool:
    """Create a test database pool with system schema."""
    from pathlib import Path

    db_path = Path(str(tmp_path)) / "system.db"
    p = DatabasePool(db_path)
    await p.initialize()
    from sovyx.persistence.migrations import MigrationRunner

    mgr = MigrationRunner(p)
    await mgr.initialize()
    await mgr.run_migrations(get_system_migrations())
    return p


@pytest.fixture
def resolver(pool: DatabasePool) -> PersonResolver:
    return PersonResolver(pool)


class TestResolve:
    """Person resolution."""

    async def test_new_user_creates_person(self, resolver: PersonResolver) -> None:
        pid = await resolver.resolve(ChannelType.TELEGRAM, "123", "Guipe")
        assert isinstance(pid, str)
        assert len(pid) > 0

    async def test_existing_user_returns_same_id(self, resolver: PersonResolver) -> None:
        pid1 = await resolver.resolve(ChannelType.TELEGRAM, "123", "Guipe")
        pid2 = await resolver.resolve(ChannelType.TELEGRAM, "123", "Guipe")
        assert pid1 == pid2

    async def test_different_users_different_ids(self, resolver: PersonResolver) -> None:
        pid1 = await resolver.resolve(ChannelType.TELEGRAM, "111", "Alice")
        pid2 = await resolver.resolve(ChannelType.TELEGRAM, "222", "Bob")
        assert pid1 != pid2

    async def test_no_display_name_uses_user_id(self, resolver: PersonResolver) -> None:
        pid = await resolver.resolve(ChannelType.TELEGRAM, "999")
        person = await resolver.get_person(pid)
        assert person is not None
        assert person["name"] == "999"

    async def test_display_name_stored(self, resolver: PersonResolver) -> None:
        pid = await resolver.resolve(ChannelType.TELEGRAM, "123", "Guipe")
        person = await resolver.get_person(pid)
        assert person is not None
        assert person["display_name"] == "Guipe"


class TestGetPerson:
    """Person lookup."""

    async def test_existing_person(self, resolver: PersonResolver) -> None:
        pid = await resolver.resolve(ChannelType.TELEGRAM, "123", "Guipe")
        person = await resolver.get_person(pid)
        assert person is not None
        assert person["id"] == pid

    async def test_nonexistent_person(self, resolver: PersonResolver) -> None:
        person = await resolver.get_person(PersonId("nonexistent"))
        assert person is None


class TestConcurrentResolve:
    """Race condition safety tests for PersonResolver."""

    @pytest.mark.asyncio
    async def test_concurrent_same_user_no_crash(self, resolver: PersonResolver) -> None:
        """Two concurrent resolves for same user must not crash."""
        import asyncio

        results = await asyncio.gather(
            resolver.resolve(ChannelType.TELEGRAM, "race-user", "User"),
            resolver.resolve(ChannelType.TELEGRAM, "race-user", "User"),
        )
        # Both should return a valid PersonId (may or may not be the same)
        assert all(r for r in results)
        # Both should resolve to the same person (eventually consistent)
        final_id = await resolver.resolve(ChannelType.TELEGRAM, "race-user", "User")
        assert final_id == results[0] or final_id == results[1]

    @pytest.mark.asyncio
    async def test_no_orphan_persons(self, resolver: PersonResolver) -> None:
        """Race condition must not create orphan persons (no mapping)."""
        import asyncio

        await asyncio.gather(
            resolver.resolve(ChannelType.TELEGRAM, "orphan-test", "User"),
            resolver.resolve(ChannelType.TELEGRAM, "orphan-test", "User"),
        )
        # Count persons vs mappings — must be equal (no orphans)
        async with resolver._pool.read() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM persons")
            persons = (await cursor.fetchone())[0]
            cursor = await conn.execute("SELECT COUNT(*) FROM channel_mappings")
            mappings = (await cursor.fetchone())[0]
        assert persons == mappings, f"Orphan persons: {persons} persons vs {mappings} mappings"

    @pytest.mark.asyncio
    async def test_idempotent_resolve(self, resolver: PersonResolver) -> None:
        """Multiple resolves for same user always return same ID."""
        id1 = await resolver.resolve(ChannelType.TELEGRAM, "idem-user", "User")
        id2 = await resolver.resolve(ChannelType.TELEGRAM, "idem-user", "User")
        id3 = await resolver.resolve(ChannelType.TELEGRAM, "idem-user", "User")
        assert id1 == id2 == id3
