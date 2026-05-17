"""End-to-end integration tests for Mission C5 Phase 1.B runtime wiring.

Mission anchor: ``docs-internal/missions/MISSION-c5-dashboard-distribution-integrity-2026-05-17.md``
§T2.6 + §F2.

Exercises the boot-scan classifier + composite-store wire + reactive
on-404 arm via TestClient against synthetic static-dir fixtures.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard import server as dashboard_server
from sovyx.dashboard._integrity import scan_bundle_integrity
from sovyx.engine._degraded_store import (
    get_default_degraded_store,
    reset_default_degraded_store,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
HEAD_STATIC_DIR = REPO_ROOT / "src" / "sovyx" / "dashboard" / "static"
_TOKEN = "test-c5-token"


@pytest.fixture(autouse=True)
def _reset_store() -> None:
    """Isolate each test from the process-wide composite store."""
    reset_default_degraded_store()
    yield
    reset_default_degraded_store()


def _copy_head(tmp: Path) -> Path:
    fixture = tmp / "static"
    shutil.copytree(HEAD_STATIC_DIR, fixture)
    return fixture


def _create_app_against(static_dir: Path) -> TestClient:
    """Materialize an app whose STATIC_DIR is overridden to ``static_dir``.

    We monkey-patch the module-level ``STATIC_DIR`` via ``setattr`` so
    ``create_app`` reads the fixture path instead of the installed
    head bundle. This mirrors how production wires the path (read at
    boot, not per-request).
    """
    original = dashboard_server.STATIC_DIR
    dashboard_server.STATIC_DIR = static_dir
    try:
        app = dashboard_server.create_app(token=_TOKEN)
    finally:
        dashboard_server.STATIC_DIR = original
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


class TestBootScanWiresStore:
    def test_fully_present_clears_axis(self, tmp_path: Path) -> None:
        # Seed the store with a stale entry so the clear-on-success
        # behavior is observable.
        from sovyx.engine._degraded_store import DegradedEntry

        store = get_default_degraded_store()
        store.record(
            DegradedEntry(
                axis="dashboard",
                reason="bundle_partial",
                severity="error",
                title_token="stale",
                body_token="stale",
                action_chips=(),
                metadata={},
                first_observed_monotonic=0.0,
                last_observed_monotonic=0.0,
                occurrence_count=1,
            ),
        )
        assert "dashboard" in store.distinct_axes()

        fixture = _copy_head(tmp_path)
        _ = _create_app_against(fixture)

        # Healthy boot must clear the stale entry.
        assert "dashboard" not in store.distinct_axes()

    def test_partial_records_to_store_as_error(self, tmp_path: Path) -> None:
        fixture = _copy_head(tmp_path)
        baseline = scan_bundle_integrity(fixture)
        target = baseline.referenced_assets[0]
        (fixture / target).unlink()

        _ = _create_app_against(fixture)

        store = get_default_degraded_store()
        snapshot = store.snapshot()
        dashboard_entries = [e for e in snapshot if e.axis == "dashboard"]
        assert len(dashboard_entries) == 1
        entry = dashboard_entries[0]
        assert entry.reason == "bundle_partial"
        assert entry.severity == "error"
        assert entry.metadata["verdict"] == "partial"
        assert entry.metadata["missing_count"] >= 1

    def test_index_html_missing_records_critical(self, tmp_path: Path) -> None:
        fixture = tmp_path / "static"
        fixture.mkdir()
        (fixture / "assets").mkdir()
        _ = _create_app_against(fixture)
        snapshot = get_default_degraded_store().snapshot()
        dashboard_entries = [e for e in snapshot if e.axis == "dashboard"]
        assert len(dashboard_entries) == 1
        entry = dashboard_entries[0]
        assert entry.reason == "bundle_missing"
        assert entry.severity == "critical"
        assert entry.metadata["verdict"] == "index_html_missing"

    def test_static_dir_missing_records_critical(self, tmp_path: Path) -> None:
        target = tmp_path / "nonexistent"
        _ = _create_app_against(target)
        snapshot = get_default_degraded_store().snapshot()
        dashboard_entries = [e for e in snapshot if e.axis == "dashboard"]
        assert len(dashboard_entries) == 1
        entry = dashboard_entries[0]
        assert entry.reason == "bundle_missing"
        assert entry.severity == "critical"
        assert entry.metadata["verdict"] == "static_dir_missing"

    def test_app_state_cache_populated(self, tmp_path: Path) -> None:
        fixture = _copy_head(tmp_path)
        # Need the app instance, not just the client. Use a manual
        # override + create_app pair.
        original = dashboard_server.STATIC_DIR
        dashboard_server.STATIC_DIR = fixture
        try:
            app = dashboard_server.create_app(token=_TOKEN)
        finally:
            dashboard_server.STATIC_DIR = original
        assert hasattr(app.state, "dashboard_integrity_report")
        report = app.state.dashboard_integrity_report
        assert report.verdict.value == "fully_present"


class TestEngineDegradedEndpointSurface:
    def test_partial_surfaces_in_engine_degraded(self, tmp_path: Path) -> None:
        """F3 falsifiability — the operator-log L459 replay shape MUST
        surface through ``GET /api/engine/degraded`` as ``axis="dashboard"``.
        """
        fixture = _copy_head(tmp_path)
        baseline = scan_bundle_integrity(fixture)
        target = baseline.referenced_assets[0]
        (fixture / target).unlink()

        client = _create_app_against(fixture)
        resp = client.get("/api/engine/degraded")
        assert resp.status_code == 200
        payload = resp.json()
        axes = {entry["axis"] for entry in payload["axes"]}
        assert "dashboard" in axes
        dashboard_entry = next(entry for entry in payload["axes"] if entry["axis"] == "dashboard")
        assert dashboard_entry["reason"] == "bundle_partial"
        assert dashboard_entry["severity"] == "error"

    def test_healthy_omits_dashboard_axis(self, tmp_path: Path) -> None:
        fixture = _copy_head(tmp_path)
        client = _create_app_against(fixture)
        resp = client.get("/api/engine/degraded")
        assert resp.status_code == 200
        payload = resp.json()
        axes = {entry["axis"] for entry in payload["axes"]}
        assert "dashboard" not in axes
