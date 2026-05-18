"""Unit tests for the server-level integrity wiring helpers (Mission C5 §T2.1).

Distinct from the integration suite at
``tests/integration/dashboard/test_dashboard_bundle_integrity_e2e.py``
which exercises the full ``create_app() → TestClient → /api/engine/degraded``
flow. This file pins the **module-level helper contracts**:

* ``_clear_dashboard_axis()`` — idempotent + best-effort against a missing
  store import.
* ``_record_dashboard_bundle_incomplete(report, severity, tuning)`` —
  axis-name + reason-token + action-chip URL contract; severity routing;
  metadata shape; legacy-event dual-emission.

Mirrors the C4 helper-test convention at
``tests/unit/engine/test_degraded_store.py`` for the store side.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from sovyx.dashboard._integrity import (
    BundleIntegrityReport,
    BundleVerdict,
)
from sovyx.dashboard.server import (
    _clear_dashboard_axis,
    _record_dashboard_bundle_incomplete,
)
from sovyx.engine._degraded_store import (
    DegradedEntry,
    get_default_degraded_store,
    reset_default_degraded_store,
)


@pytest.fixture(autouse=True)
def _reset_store() -> None:
    """Isolate every test from the process-wide composite store."""
    reset_default_degraded_store()
    yield
    reset_default_degraded_store()


def _make_partial_report(tmp: Path, *, missing_count: int = 1) -> BundleIntegrityReport:
    """Build a synthetic PARTIAL report for the producer-wire tests."""
    return BundleIntegrityReport(
        verdict=BundleVerdict.PARTIAL,
        static_dir=tmp / "static",
        index_html_path=tmp / "static" / "index.html",
        referenced_assets=tuple(f"assets/chunk-{i}.js" for i in range(missing_count + 1)),
        missing_assets=tuple(f"assets/chunk-{i}.js" for i in range(missing_count)),
        orphan_assets=(),
        scan_duration_ms=4.5,
        scanned_at_monotonic=time.monotonic(),
    )


def _make_missing_report(
    tmp: Path,
    *,
    verdict: BundleVerdict = BundleVerdict.INDEX_HTML_MISSING,
) -> BundleIntegrityReport:
    return BundleIntegrityReport(
        verdict=verdict,
        static_dir=tmp / "static",
        index_html_path=tmp / "static" / "index.html",
        referenced_assets=(),
        missing_assets=(),
        orphan_assets=(),
        scan_duration_ms=0.05,
        scanned_at_monotonic=time.monotonic(),
    )


class TestClearDashboardAxis:
    """``_clear_dashboard_axis`` is idempotent + safe across store states."""

    def test_clears_existing_entry(self, tmp_path: Path) -> None:
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
        _clear_dashboard_axis()
        assert "dashboard" not in store.distinct_axes()

    def test_idempotent_when_no_entry(self) -> None:
        """Calling clear on an empty store is a no-op (no exception)."""
        _clear_dashboard_axis()
        _clear_dashboard_axis()  # second call also no-op
        assert get_default_degraded_store().distinct_axes() == []

    def test_only_clears_dashboard_axis_not_others(self, tmp_path: Path) -> None:
        store = get_default_degraded_store()
        store.record(
            DegradedEntry(
                axis="voice",
                reason="failover_ladder_exhausted",
                severity="error",
                title_token="t",
                body_token="b",
                action_chips=(),
                metadata={},
                first_observed_monotonic=0.0,
                last_observed_monotonic=0.0,
                occurrence_count=1,
            ),
        )
        store.record(
            DegradedEntry(
                axis="dashboard",
                reason="bundle_partial",
                severity="error",
                title_token="t",
                body_token="b",
                action_chips=(),
                metadata={},
                first_observed_monotonic=0.0,
                last_observed_monotonic=0.0,
                occurrence_count=1,
            ),
        )
        _clear_dashboard_axis()
        axes = store.distinct_axes()
        assert "voice" in axes
        assert "dashboard" not in axes

    def test_swallows_import_failure(self) -> None:
        """If the store import raises, the helper logs debug + returns
        cleanly (observability-only surface)."""
        with patch(
            "sovyx.engine._degraded_store.get_default_degraded_store",
            side_effect=RuntimeError("simulated import failure"),
        ):
            # No raise — best-effort contract.
            _clear_dashboard_axis()


class TestRecordDashboardBundleIncomplete:
    """``_record_dashboard_bundle_incomplete`` axis/reason/chip contract."""

    def test_partial_records_with_error_severity(self, tmp_path: Path) -> None:
        report = _make_partial_report(tmp_path, missing_count=3)
        _record_dashboard_bundle_incomplete(report, severity="error", tuning=None)

        snapshot = get_default_degraded_store().snapshot()
        entries = [e for e in snapshot if e.axis == "dashboard"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry.reason == "bundle_partial"
        assert entry.severity == "error"
        assert entry.title_token == "degraded.dashboard.bundle_partial.title"
        assert entry.body_token == "degraded.dashboard.bundle_partial.partial.body"

    def test_missing_records_with_critical_severity(self, tmp_path: Path) -> None:
        report = _make_missing_report(tmp_path, verdict=BundleVerdict.STATIC_DIR_MISSING)
        _record_dashboard_bundle_incomplete(report, severity="critical", tuning=None)

        entries = [e for e in get_default_degraded_store().snapshot() if e.axis == "dashboard"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry.reason == "bundle_missing"
        assert entry.severity == "critical"
        assert entry.body_token == "degraded.dashboard.bundle_missing.static_dir_missing.body"

    def test_body_token_discriminates_verdict_subcases(self, tmp_path: Path) -> None:
        """Each ``bundle_missing`` verdict yields a verdict-discriminated
        body token. Tests INDEX_HTML_MISSING + LEGACY_INDEX_HTML_NO_ASSETS."""
        for verdict, expected_suffix in (
            (BundleVerdict.INDEX_HTML_MISSING, "index_html_missing"),
            (BundleVerdict.LEGACY_INDEX_HTML_NO_ASSETS, "legacy_index_html_no_assets"),
        ):
            reset_default_degraded_store()
            report = _make_missing_report(tmp_path, verdict=verdict)
            _record_dashboard_bundle_incomplete(report, severity="critical", tuning=None)
            entries = [e for e in get_default_degraded_store().snapshot() if e.axis == "dashboard"]
            assert len(entries) == 1
            assert (
                entries[0].body_token
                == f"degraded.dashboard.bundle_missing.{expected_suffix}.body"
            )

    def test_action_chips_carry_default_urls(self, tmp_path: Path) -> None:
        """When ``tuning is None`` the producer falls back to the canonical
        sovyx.dev URLs (Mission C5 §T2.5 default values)."""
        report = _make_partial_report(tmp_path)
        _record_dashboard_bundle_incomplete(report, severity="error", tuning=None)

        entry = next(e for e in get_default_degraded_store().snapshot() if e.axis == "dashboard")
        chip_labels = {chip.label_token for chip in entry.action_chips}
        chip_targets = {chip.target for chip in entry.action_chips}
        assert chip_labels == {
            "degraded.dashboard.reinstall",
            "degraded.dashboard.runDoctor",
        }
        assert chip_targets == {
            "https://sovyx.dev/docs/install/troubleshooting#reinstall",
            "https://sovyx.dev/docs/cli/doctor#dashboard",
        }

    def test_action_chips_honor_tuning_url_overrides(self, tmp_path: Path) -> None:
        """When ``tuning`` provides override URLs (e.g. self-hosted docs),
        the chips MUST use them. Validates the
        ``SOVYX_TUNING__DASHBOARD__INTEGRITY_ACTION_CHIP_*_URL`` knob path."""
        from sovyx.engine.config import DashboardTuningConfig

        tuning = DashboardTuningConfig(
            integrity_action_chip_reinstall_url="https://acme.corp/sovyx/reinstall",
            integrity_action_chip_doctor_url="https://acme.corp/sovyx/doctor",
        )
        report = _make_partial_report(tmp_path)
        _record_dashboard_bundle_incomplete(report, severity="error", tuning=tuning)

        entry = next(e for e in get_default_degraded_store().snapshot() if e.axis == "dashboard")
        chip_targets = {chip.target for chip in entry.action_chips}
        assert chip_targets == {
            "https://acme.corp/sovyx/reinstall",
            "https://acme.corp/sovyx/doctor",
        }

    def test_metadata_carries_missing_sample_bounded_to_five(self, tmp_path: Path) -> None:
        """``missing_sample`` is bounded to 5 entries even when the
        missing-asset list is longer — anti-pattern #15 bounded
        cardinality in the operator-visible payload."""
        report = _make_partial_report(tmp_path, missing_count=12)
        _record_dashboard_bundle_incomplete(report, severity="error", tuning=None)

        entry = next(e for e in get_default_degraded_store().snapshot() if e.axis == "dashboard")
        assert entry.metadata["missing_count"] == 12
        assert len(entry.metadata["missing_sample"]) == 5
        assert entry.metadata["verdict"] == "partial"

    def test_dual_emission_warn_event_name(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """ADR-D14 LENIENT: the producer emits the new
        ``dashboard.distribution.bundle_partial`` / ``bundle_missing``
        WARN event alongside the composite-store record. The legacy
        ``dashboard_static_missing`` is emitted from ``create_app()``
        only (not from this helper).

        Sovyx uses structlog which doesn't propagate to stdlib logging
        by default — so we assert via captured stdout / stderr rather
        than ``caplog``.
        """
        report = _make_partial_report(tmp_path)
        _record_dashboard_bundle_incomplete(report, severity="error", tuning=None)
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "dashboard.distribution.bundle_partial" in combined, (
            f"expected bundle_partial WARN in captured output; got {combined!r}"
        )


class TestRecordIsBestEffort:
    """Store-write failures MUST NOT propagate (observability-only)."""

    def test_store_failure_swallowed(self, tmp_path: Path) -> None:
        report = _make_partial_report(tmp_path)
        with patch(
            "sovyx.engine._degraded_store.get_default_degraded_store",
            side_effect=RuntimeError("simulated store failure"),
        ):
            # No raise even when the store layer fails — the WARN log
            # still fires; the operator's banner just won't surface.
            _record_dashboard_bundle_incomplete(report, severity="error", tuning=None)
