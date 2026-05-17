"""Unit tests for the reactive on-404 arm (Mission C5 §T2.2).

The reactive arm is structurally hard to exercise via TestClient because
the rescan is fire-and-forget via ``asyncio.create_task``. These tests
poke the wrapper directly at the method level via mocks.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from starlette.exceptions import HTTPException as StarletteHTTPException

from sovyx.dashboard._integrity import BundleVerdict, scan_bundle_integrity
from sovyx.dashboard.server import _IntegrityAwareStaticFiles
from sovyx.engine.config import DashboardTuningConfig


def _make_tuning(
    *,
    enabled: bool = True,
    debounce_sec: float = 60.0,
) -> DashboardTuningConfig:
    return DashboardTuningConfig(
        integrity_reactive_enabled=enabled,
        integrity_reactive_debounce_sec=debounce_sec,
    )


def _make_arm(directory: str, static_dir: Path, **kwargs: object) -> _IntegrityAwareStaticFiles:
    tuning = _make_tuning(**kwargs)  # type: ignore[arg-type]
    return _IntegrityAwareStaticFiles(
        directory=directory,
        static_dir=static_dir,
        tuning=tuning,
    )


class TestReactiveArmConstruction:
    def test_accepts_none_tuning(self, tmp_path: Path) -> None:
        assets = tmp_path / "assets"
        assets.mkdir()
        arm = _IntegrityAwareStaticFiles(
            directory=str(assets),
            static_dir=tmp_path,
            tuning=None,
        )
        assert arm._enabled is True
        assert arm._debounce_sec == 60.0

    def test_reads_tuning_values(self, tmp_path: Path) -> None:
        assets = tmp_path / "assets"
        assets.mkdir()
        arm = _make_arm(str(assets), tmp_path, enabled=False, debounce_sec=120.0)
        assert arm._enabled is False
        assert arm._debounce_sec == 120.0


class TestReactiveRescan:
    @pytest.mark.asyncio()
    async def test_healthy_clears_axis(self, tmp_path: Path) -> None:
        """When a rescan finds the bundle FULLY_PRESENT, the dashboard
        axis is cleared from the composite store."""
        # Build a healthy fixture.
        static = tmp_path / "static"
        static.mkdir()
        (static / "index.html").write_text(
            '<html><head><script src="/assets/x.js"></script></head></html>',
            encoding="utf-8",
        )
        (static / "assets").mkdir()
        (static / "assets" / "x.js").write_text("// ok", encoding="utf-8")
        baseline = scan_bundle_integrity(static)
        assert baseline.verdict is BundleVerdict.FULLY_PRESENT

        arm = _make_arm(str(static / "assets"), static)
        with patch("sovyx.dashboard.server._clear_dashboard_axis") as clear_mock:
            await arm._reactive_rescan()
            clear_mock.assert_called_once()

    @pytest.mark.asyncio()
    async def test_partial_records_to_store(self, tmp_path: Path) -> None:
        """When a rescan finds the bundle PARTIAL, the producer wire
        emits a ``bundle_partial`` record."""
        static = tmp_path / "static"
        static.mkdir()
        (static / "index.html").write_text(
            "<html><head>"
            '<script src="/assets/a.js"></script>'
            '<script src="/assets/missing.js"></script>'
            "</head></html>",
            encoding="utf-8",
        )
        (static / "assets").mkdir()
        (static / "assets" / "a.js").write_text("// ok", encoding="utf-8")
        # Note: missing.js is NOT created.
        baseline = scan_bundle_integrity(static)
        assert baseline.verdict is BundleVerdict.PARTIAL

        arm = _make_arm(str(static / "assets"), static)
        with patch("sovyx.dashboard.server._record_dashboard_bundle_incomplete") as record_mock:
            await arm._reactive_rescan()
            record_mock.assert_called_once()
            kwargs = record_mock.call_args.kwargs
            assert kwargs["severity"] == "error"

    @pytest.mark.asyncio()
    async def test_scan_failure_swallowed(self, tmp_path: Path) -> None:
        """A scanner exception during reactive rescan MUST NOT propagate
        (the 404 already returned synchronously to the client)."""
        arm = _make_arm(str(tmp_path), tmp_path)
        with patch(
            "sovyx.dashboard.server.scan_bundle_integrity",
            side_effect=RuntimeError("disk gone"),
        ):
            # No raise.
            await arm._reactive_rescan()


class TestDebounce:
    @pytest.mark.asyncio()
    async def test_disabled_arm_skips_rescan(self, tmp_path: Path) -> None:
        assets = tmp_path / "assets"
        assets.mkdir()
        (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
        arm = _make_arm(str(assets), tmp_path, enabled=False)

        scope = {"method": "GET", "type": "http", "path": "/missing.js"}
        with patch.object(
            _IntegrityAwareStaticFiles.__bases__[0],
            "get_response",
            new=AsyncMock(side_effect=StarletteHTTPException(status_code=404)),
        ):
            with pytest.raises(StarletteHTTPException):
                await arm.get_response("missing.js", scope)
            # The arm.last_scan_at should NOT have moved (disabled).
            assert arm._last_scan_at == 0.0

    @pytest.mark.asyncio()
    async def test_debounce_prevents_storm(self, tmp_path: Path) -> None:
        """Two rapid 404s in the same debounce window → exactly ONE rescan."""
        assets = tmp_path / "assets"
        assets.mkdir()
        (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
        arm = _make_arm(str(assets), tmp_path, debounce_sec=60.0)

        scope = {"method": "GET", "type": "http", "path": "/missing.js"}
        # Track how many times _reactive_rescan would be invoked.
        with (
            patch.object(
                _IntegrityAwareStaticFiles.__bases__[0],
                "get_response",
                new=AsyncMock(side_effect=StarletteHTTPException(status_code=404)),
            ),
            patch.object(arm, "_reactive_rescan", new=AsyncMock()) as rescan_mock,
        ):
            for _ in range(5):
                with pytest.raises(StarletteHTTPException):
                    await arm.get_response("missing.js", scope)
            # Wait briefly for any spawned tasks to land.
            await asyncio.sleep(0.05)
            assert rescan_mock.call_count <= 1, (
                f"debounce broken; rescan fired {rescan_mock.call_count} times"
            )
