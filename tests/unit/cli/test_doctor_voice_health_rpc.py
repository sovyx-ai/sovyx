"""DOCTOR-3 regression — daemon-first voice-health surfaces in ``sovyx doctor voice``.

Mission anchor:
``docs-internal/missions/MISSION-AUDIO-ENGINE-CROSS-PLATFORM-AUDIT-2026-07-02-FINDINGS.md``
§DOCTOR-3.

Pre-fix the quarantine / failover-history / degraded-banner sections
rendered the CLI process's OWN in-memory singletons — always empty in
a non-daemon process — so a live daemon with a quarantined mic still
printed "No endpoints in quarantine" (AP #70/#71 class), and the
failover empty-state falsely claimed "on this daemon process".

Post-fix the surfaces prefer the daemon's ``voice.health.snapshot``
RPC (mirroring the ``doctor resources`` pattern) and disclose the
local fallback explicitly. Producer and fallback share ONE serializer
(:func:`collect_voice_health_snapshot`, AP #40/#53), pinned here by a
store-populate → serialize → render round-trip.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

from sovyx.cli.commands import doctor as doctor_mod
from sovyx.engine._degraded_store import (
    DegradedEntry,
    get_default_degraded_store,
    make_action_chip,
    now_monotonic,
    reset_default_degraded_store,
)
from sovyx.engine._rpc_handlers import collect_voice_health_snapshot
from sovyx.voice.health._failover_history import reset_default_failover_history
from sovyx.voice.health._quarantine import (
    get_default_quarantine,
    reset_default_quarantine,
)


@pytest.fixture(autouse=True)
def _reset_stores() -> Generator[None, None, None]:
    # All three are process-wide singletons — earlier test modules in
    # the same worker may have populated any of them.
    reset_default_quarantine()
    reset_default_degraded_store()
    reset_default_failover_history()
    yield
    reset_default_quarantine()
    reset_default_degraded_store()
    reset_default_failover_history()


def _populate_quarantine() -> None:
    get_default_quarantine().add(
        endpoint_guid="{guid-razer-01}",
        device_friendly_name="Razer BlackShark",
        host_api="Windows WASAPI",
        reason="probe_pinned",
        derived_reason="driver_silent",
    )


def _populate_degraded() -> None:
    now = now_monotonic()
    get_default_degraded_store().record(
        DegradedEntry(
            axis="stt",
            reason="stt_language_coerced",
            severity="warn",
            title_token="degraded.stt.languageCoerced.title",
            body_token="degraded.stt.languageCoerced.body",
            action_chips=(
                make_action_chip(
                    "degraded.stt.languageCoerced.switchToEnglish",
                    "navigate",
                    "/settings/voice",
                ),
            ),
            first_observed_monotonic=now,
            last_observed_monotonic=now,
        ),
    )


class TestSharedSerializer:
    """The one-symbol producer both the RPC handler and CLI fallback use."""

    def test_empty_process_serializes_empty_axes(self) -> None:
        payload = collect_voice_health_snapshot()
        assert payload == {
            "quarantine": [],
            "failover_history": [],
            "degraded": [],
        }

    def test_quarantine_entries_round_trip_with_producer_side_ttl(self) -> None:
        _populate_quarantine()
        payload = collect_voice_health_snapshot()
        assert len(payload["quarantine"]) == 1
        row = payload["quarantine"][0]
        assert row["endpoint_guid"] == "{guid-razer-01}"
        assert row["device_friendly_name"] == "Razer BlackShark"
        assert row["derived_reason"] == "driver_silent"
        # Producer-side TTL: monotonic deadlines don't cross process
        # boundaries, so recheck_in_s is computed at serialize time.
        assert row["recheck_in_s"] > 0.0

    def test_payload_is_json_safe(self) -> None:
        import json

        _populate_quarantine()
        _populate_degraded()
        json.dumps(collect_voice_health_snapshot())  # must not raise

    def test_rpc_handler_registered_and_serves_snapshot(self) -> None:
        """DOCTOR-3 producer anchor — the daemon serves the method.

        Sibling of the AP #53 parity suite: the CLI call literal
        ``voice.health.snapshot`` must be producible.
        """
        import asyncio
        from pathlib import Path

        from sovyx.engine._rpc_handlers import register_cli_handlers
        from sovyx.engine.rpc_server import DaemonRPCServer

        rpc = DaemonRPCServer(Path("unused-doctor3-test.sock"))
        register_cli_handlers(rpc, MagicMock())
        assert "voice.health.snapshot" in rpc._methods  # noqa: SLF001
        _populate_quarantine()
        result = asyncio.run(rpc._methods["voice.health.snapshot"]())  # noqa: SLF001
        assert result["quarantine"][0]["endpoint_guid"] == "{guid-razer-01}"


class TestFetchVoiceHealthPayload:
    def test_prefers_daemon_when_reachable(self) -> None:
        daemon_payload = {
            "quarantine": [],
            "failover_history": [],
            "degraded": [],
        }
        mock_client = MagicMock()
        mock_client.is_daemon_running.return_value = True

        async def _call(method: str) -> dict[str, object]:
            assert method == "voice.health.snapshot"
            return daemon_payload

        mock_client.call = _call
        with patch.object(doctor_mod, "DaemonClient", return_value=mock_client):
            payload, source = doctor_mod._fetch_voice_health_payload()
        assert source == "daemon"
        assert payload == daemon_payload

    def test_falls_back_to_local_when_daemon_down(self) -> None:
        _populate_quarantine()
        mock_client = MagicMock()
        mock_client.is_daemon_running.return_value = False
        with patch.object(doctor_mod, "DaemonClient", return_value=mock_client):
            payload, source = doctor_mod._fetch_voice_health_payload()
        assert source == "local"
        assert payload["quarantine"][0]["endpoint_guid"] == "{guid-razer-01}"

    def test_falls_back_to_local_when_rpc_raises(self) -> None:
        mock_client = MagicMock()
        mock_client.is_daemon_running.return_value = True

        async def _call(method: str) -> dict[str, object]:
            raise ConnectionResetError("daemon went away mid-call")

        mock_client.call = _call
        with patch.object(doctor_mod, "DaemonClient", return_value=mock_client):
            _, source = doctor_mod._fetch_voice_health_payload()
        assert source == "local"


class TestQuarantineSurfaceRender:
    def test_daemon_entries_render_in_table(self, capsys: pytest.CaptureFixture[str]) -> None:
        """DOCTOR-3 acceptance: entries living in the DAEMON-side stores
        (simulated by populating this process's stores and shipping them
        through the real serializer as a 'daemon' payload) render in the
        CLI section."""
        _populate_quarantine()
        payload = collect_voice_health_snapshot()
        doctor_mod._render_voice_quarantine_surface(
            output_json=False,
            reason_filter=None,
            payload=payload,
            source="daemon",
        )
        captured = capsys.readouterr()
        assert "Razer BlackShark" in captured.out
        assert "driver_silent" in captured.out
        assert "{guid-razer-01}" in captured.out
        assert "No endpoints in quarantine" not in captured.out
        assert "Daemon not reachable" not in captured.out

    def test_local_empty_state_shows_disclosure(self, capsys: pytest.CaptureFixture[str]) -> None:
        doctor_mod._render_voice_quarantine_surface(
            output_json=False,
            reason_filter=None,
            payload=collect_voice_health_snapshot(),
            source="local",
        )
        captured = capsys.readouterr()
        assert "Daemon not reachable" in captured.out
        assert "showing this CLI process only" in captured.out
        assert "No endpoints in quarantine" in captured.out

    def test_reason_filter_applies_to_daemon_payload(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _populate_quarantine()
        payload = collect_voice_health_snapshot()
        doctor_mod._render_voice_quarantine_surface(
            output_json=False,
            reason_filter="vad_frontend_dead",
            payload=payload,
            source="daemon",
        )
        captured = capsys.readouterr()
        assert "No quarantined endpoints match reason" in captured.out


class TestDegradedBannerSurfaceRender:
    def test_daemon_entries_render_with_composite_severity(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _populate_degraded()
        payload = collect_voice_health_snapshot()
        doctor_mod._render_voice_degraded_banner_surface(
            output_json=False,
            payload=payload,
            source="daemon",
        )
        captured = capsys.readouterr()
        assert "stt" in captured.out
        assert "stt_language_coerced" in captured.out
        assert "WARN" in captured.out
        assert "/settings/voice" in captured.out  # action chip target
        assert "No degraded axes" not in captured.out

    def test_local_empty_state_shows_disclosure(self, capsys: pytest.CaptureFixture[str]) -> None:
        doctor_mod._render_voice_degraded_banner_surface(
            output_json=False,
            payload=collect_voice_health_snapshot(),
            source="local",
        )
        captured = capsys.readouterr()
        assert "Daemon not reachable" in captured.out
        assert "No degraded axes" in captured.out

    def test_malformed_rows_are_dropped_not_raised(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        doctor_mod._render_voice_degraded_banner_surface(
            output_json=False,
            payload={"degraded": ["not-a-dict", 42]},
            source="daemon",
        )
        captured = capsys.readouterr()
        assert "No degraded axes" in captured.out
