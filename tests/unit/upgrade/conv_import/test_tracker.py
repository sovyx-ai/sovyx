"""Tests for ImportProgressTracker — in-memory job registry."""

from __future__ import annotations

import asyncio

import pytest

from sovyx.upgrade.conv_import import ImportProgressTracker, ImportState


class TestImportProgressTracker:
    """Lifecycle + concurrency properties."""

    @pytest.mark.asyncio()
    async def test_start_returns_unique_job_id(self) -> None:
        tracker = ImportProgressTracker()
        id1 = await tracker.start("chatgpt")
        id2 = await tracker.start("chatgpt")
        assert id1 != id2

    @pytest.mark.asyncio()
    async def test_initial_state_pending(self) -> None:
        tracker = ImportProgressTracker()
        job_id = await tracker.start("chatgpt")
        snap = await tracker.get(job_id)
        assert snap is not None
        assert snap.state == ImportState.PENDING
        assert snap.platform == "chatgpt"
        assert snap.conversations_total == 0
        assert snap.warnings == []
        assert snap.error is None

    @pytest.mark.asyncio()
    async def test_update_applies_deltas(self) -> None:
        tracker = ImportProgressTracker()
        job_id = await tracker.start("chatgpt")
        await tracker.update(job_id, conversations_total=10)
        await tracker.update(job_id, conversations_processed_delta=3)
        await tracker.update(job_id, conversations_processed_delta=2)
        snap = await tracker.get(job_id)
        assert snap is not None
        assert snap.conversations_total == 10  # noqa: PLR2004
        assert snap.conversations_processed == 5  # noqa: PLR2004

    @pytest.mark.asyncio()
    async def test_warnings_accumulate(self) -> None:
        tracker = ImportProgressTracker()
        job_id = await tracker.start("chatgpt")
        await tracker.update(job_id, warning="first")
        await tracker.update(job_id, warning="second")
        snap = await tracker.get(job_id)
        assert snap is not None
        assert snap.warnings == ["first", "second"]

    @pytest.mark.asyncio()
    async def test_finish_success_transitions_to_completed(self) -> None:
        tracker = ImportProgressTracker()
        job_id = await tracker.start("chatgpt")
        await tracker.finish(job_id)
        snap = await tracker.get(job_id)
        assert snap is not None
        assert snap.state == ImportState.COMPLETED
        assert snap.error is None
        assert snap.finished_at is not None

    @pytest.mark.asyncio()
    async def test_finish_with_error_transitions_to_failed(self) -> None:
        tracker = ImportProgressTracker()
        job_id = await tracker.start("chatgpt")
        await tracker.finish(job_id, error="parse blew up")
        snap = await tracker.get(job_id)
        assert snap is not None
        assert snap.state == ImportState.FAILED
        assert snap.error == "parse blew up"

    @pytest.mark.asyncio()
    async def test_get_unknown_id_returns_none(self) -> None:
        tracker = ImportProgressTracker()
        assert await tracker.get("does-not-exist") is None

    @pytest.mark.asyncio()
    async def test_update_unknown_id_is_noop(self) -> None:
        tracker = ImportProgressTracker()
        # Must not raise.
        await tracker.update("does-not-exist", conversations_processed_delta=5)

    @pytest.mark.asyncio()
    async def test_snapshot_is_independent(self) -> None:
        """Mutating the returned snapshot must not affect the stored job."""
        tracker = ImportProgressTracker()
        job_id = await tracker.start("chatgpt")
        await tracker.update(job_id, warning="original")
        snap1 = await tracker.get(job_id)
        assert snap1 is not None
        snap1.warnings.append("tampered")
        snap2 = await tracker.get(job_id)
        assert snap2 is not None
        assert snap2.warnings == ["original"]

    @pytest.mark.asyncio()
    async def test_concurrent_updates_are_serialised(self) -> None:
        """Parallel update() calls don't lose deltas."""
        tracker = ImportProgressTracker()
        job_id = await tracker.start("chatgpt")

        async def bump() -> None:
            for _ in range(50):
                await tracker.update(job_id, conversations_processed_delta=1)

        await asyncio.gather(bump(), bump(), bump())
        snap = await tracker.get(job_id)
        assert snap is not None
        assert snap.conversations_processed == 150  # noqa: PLR2004
