"""Tests for ``_render_voice_failover_history_surface``.

Mission anchor: ``docs-internal/missions/MISSION-c3-failover-ladder-iteration-2026-05-16.md``
§T2.11.

Pin the CLI surface: empty-state renders the documented hint;
populated state renders one row per ladder run + per-candidate
detail; JSON mode suppresses the surface entirely.
"""

from __future__ import annotations

import pytest

from sovyx.cli.commands.doctor import _render_voice_failover_history_surface
from sovyx.voice.health._failover_history import (
    FailoverCandidateRecord,
    FailoverLadderRunRecord,
    get_default_failover_history,
    reset_default_failover_history,
)


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    reset_default_failover_history()


class TestEmptyStateRender:
    def test_renders_empty_state_message(self, capsys: pytest.CaptureFixture[str]) -> None:
        _render_voice_failover_history_surface(output_json=False)
        captured = capsys.readouterr()
        assert "Voice — failover history" in captured.out
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

        _render_voice_failover_history_surface(output_json=False)
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

        _render_voice_failover_history_surface(output_json=False)
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
        _render_voice_failover_history_surface(output_json=False, limit=3)
        captured = capsys.readouterr()
        # Newest first — id-9, 8, 7.
        assert "id-00000009" in captured.out
        assert "id-00000008" in captured.out
        assert "id-00000007" in captured.out
        # id-6 NOT rendered (limit=3).
        assert "id-00000006" not in captured.out
