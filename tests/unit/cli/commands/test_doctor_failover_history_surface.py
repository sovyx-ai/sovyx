"""Tests for ``_render_voice_failover_history_surface``.

Mission anchor: ``docs-internal/missions/MISSION-c3-failover-ladder-iteration-2026-05-16.md``
§T2.11.

Pin the CLI surface: empty-state renders the documented hint;
populated state renders one row per ladder run + per-candidate
detail; JSON mode suppresses the surface entirely.

DOCTOR-3 migration: the renderer is now daemon-first (it self-fetches
via ``_fetch_voice_health_payload`` when no ``payload`` is passed).
These tests pin the LOCAL-source path hermetically by patching the
fetch to serialize this process's ring via the shared
``collect_voice_health_snapshot`` producer — a live daemon on the dev
box must not leak into the assertions.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sovyx.cli.commands import doctor as doctor_mod
from sovyx.cli.commands.doctor import _render_voice_failover_history_surface
from sovyx.engine._rpc_handlers import collect_voice_health_snapshot
from sovyx.voice.health._failover_history import (
    FailoverCandidateRecord,
    FailoverLadderRunRecord,
    get_default_failover_history,
    reset_default_failover_history,
)


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    reset_default_failover_history()


def _render_from_local_ring(*, output_json: bool = False, limit: int = 8) -> None:
    """Render with the fetch pinned to this process's ring (hermetic)."""
    with patch.object(
        doctor_mod,
        "_fetch_voice_health_payload",
        return_value=(collect_voice_health_snapshot(), "local"),
    ):
        _render_voice_failover_history_surface(output_json=output_json, limit=limit)


class TestEmptyStateRender:
    def test_renders_empty_state_message(self, capsys: pytest.CaptureFixture[str]) -> None:
        _render_from_local_ring()
        captured = capsys.readouterr()
        assert "Voice — failover history" in captured.out
        assert "No failover ladder has run yet" in captured.out
        # DOCTOR-3 wording fix: the pre-fix empty-state falsely claimed
        # "on this daemon process" while reading the CLI process's ring.
        assert "on this daemon process" not in captured.out

    def test_local_source_prints_disclosure(self, capsys: pytest.CaptureFixture[str]) -> None:
        """DOCTOR-3 — daemon-unreachable path discloses the local scope."""
        _render_from_local_ring()
        captured = capsys.readouterr()
        assert "Daemon not reachable" in captured.out
        assert "showing this CLI process only" in captured.out

    def test_daemon_source_prints_no_disclosure(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When the payload came from the daemon RPC, no disclosure."""
        _render_voice_failover_history_surface(
            output_json=False,
            payload={"failover_history": []},
            source="daemon",
        )
        captured = capsys.readouterr()
        assert "Daemon not reachable" not in captured.out
        assert "No failover ladder has run yet" in captured.out

    def test_json_mode_suppresses_surface(self, capsys: pytest.CaptureFixture[str]) -> None:
        _render_voice_failover_history_surface(output_json=True)
        captured = capsys.readouterr()
        assert captured.out == ""


class TestPopulatedStateRender:
    def test_renders_succeeded_ladder(self, capsys: pytest.CaptureFixture[str]) -> None:
        ring = get_default_failover_history()
        run = FailoverLadderRunRecord(
            ladder_id="abc123def456",
            started_monotonic=1000.0,
            completed_monotonic=1001.0,
            verdict="succeeded",
            candidates_tried=2,
            succeeded_index=1,
            from_endpoint="razer",
            elapsed_ms=1000,
        )
        run.add_candidate(
            FailoverCandidateRecord(
                index=0,
                target_endpoint="hd-audio-generic",
                verdict="failed",
                error_class="unopenable_this_boot",
                elapsed_ms=500,
            ),
        )
        run.add_candidate(
            FailoverCandidateRecord(
                index=1,
                target_endpoint="pipewire",
                verdict="succeeded",
                elapsed_ms=400,
            ),
        )
        ring.record_ladder(run)

        _render_from_local_ring()
        captured = capsys.readouterr()

        assert "abc123def456" in captured.out
        assert "succeeded" in captured.out
        assert "hd-audio-generic" in captured.out
        assert "pipewire" in captured.out
        assert "unopenable_this_boot" in captured.out

    def test_renders_exhausted_ladder_with_skipped_candidate(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ring = get_default_failover_history()
        run = FailoverLadderRunRecord(
            ladder_id="exhausted001",
            started_monotonic=1000.0,
            verdict="exhausted",
            candidates_tried=2,
            from_endpoint="razer",
        )
        run.add_candidate(
            FailoverCandidateRecord(
                index=0,
                target_endpoint="dead-device",
                verdict="skipped",
                skipped_reason="probe_cache_unopenable",
            ),
        )
        ring.record_ladder(run)

        _render_from_local_ring()
        captured = capsys.readouterr()

        assert "exhausted" in captured.out
        assert "skipped" in captured.out
        assert "probe_cache_unopenable" in captured.out

    def test_limit_caps_rendered_entries(self, capsys: pytest.CaptureFixture[str]) -> None:
        ring = get_default_failover_history()
        for i in range(10):
            ring.record_ladder(
                FailoverLadderRunRecord(
                    ladder_id=f"id-{i:08d}",
                    started_monotonic=float(i),
                    verdict="succeeded",
                ),
            )
        _render_from_local_ring(limit=3)
        captured = capsys.readouterr()
        # Newest first — id-9, 8, 7.
        assert "id-00000009" in captured.out
        assert "id-00000008" in captured.out
        assert "id-00000007" in captured.out
        # id-6 NOT rendered (limit=3).
        assert "id-00000006" not in captured.out
