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
