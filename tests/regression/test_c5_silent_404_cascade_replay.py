"""Forensic-replay regression for Mission C5 §F3.

Replays the v0.43.1 operator log L458→L467 silent-404-cascade signature
as a synthetic partial bundle, then asserts the composite endpoint
surfaces the dashboard axis with ``reason="bundle_partial"`` and
``severity="error"``.

This test MUST FAIL on pre-mission HEAD (because the dashboard axis
producer wire does not exist) and PASS post-Phase-1.B. It is the
mechanical proof that C5's runtime detection closes the operator's
forensic gap.

Forensic anchor:
- ``docs-internal/FORENSIC-AUDIT-LOG-2026-05-14-v0.43.1.md`` §C5
- Operator log L458 (auth 401) → L459 (chunk 404
  ``dashboard-BLNxX04a.js``) → L467 (cascade continues)
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard import server as dashboard_server
from sovyx.dashboard._integrity import scan_bundle_integrity
from sovyx.engine._degraded_store import reset_default_degraded_store

REPO_ROOT = Path(__file__).resolve().parents[2]
HEAD_STATIC_DIR = REPO_ROOT / "src" / "sovyx" / "dashboard" / "static"
_TOKEN = "test-c5-regression"


@pytest.fixture(autouse=True)
def _reset_store() -> None:
    reset_default_degraded_store()
    yield
    reset_default_degraded_store()


def _build_partial_bundle(tmp: Path, *, drop_count: int) -> Path:
    """Build a fixture with ``drop_count`` referenced chunks missing.

    Mirrors the operator's v0.43.1 install state: index.html present,
    SOME ``/assets/<chunk>.js`` chunks absent.
    """
    fixture = tmp / "static"
    shutil.copytree(HEAD_STATIC_DIR, fixture)
    baseline = scan_bundle_integrity(fixture)
    assert len(baseline.referenced_assets) >= drop_count
    for ref in baseline.referenced_assets[:drop_count]:
        (fixture / ref).unlink()
    return fixture


def test_l458_l459_l467_composite_surface(tmp_path: Path) -> None:
    """F3 falsifiability — operator's L459 chunk-404 replay surfaces.

    Pre-mission: this test FAILS because ``/api/engine/degraded``
    contains no ``axis="dashboard"`` entry (no producer wire exists).
    Post-mission: PASSES.
    """
    # Drop 3 referenced chunks to mirror the operator's cascade signature
    # (L459, L461, L463 each show a distinct missing chunk in the log).
    fixture = _build_partial_bundle(tmp_path, drop_count=3)

    original = dashboard_server.STATIC_DIR
    dashboard_server.STATIC_DIR = fixture
    try:
        app = dashboard_server.create_app(token=_TOKEN)
    finally:
        dashboard_server.STATIC_DIR = original
    client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

    resp = client.get("/api/engine/degraded")
    assert resp.status_code == 200, (
        f"composite endpoint must remain 200 even when dashboard axis "
        f"degraded; got {resp.status_code}"
    )
    payload = resp.json()
    axes = {entry["axis"] for entry in payload["axes"]}
    assert "dashboard" in axes, (
        f"dashboard axis missing from composite payload — operator's "
        f"v0.43.1 silent 404 cascade still has no operator-actionable "
        f"surface. Payload: {payload}"
    )

    dashboard_entry = next(entry for entry in payload["axes"] if entry["axis"] == "dashboard")
    assert dashboard_entry["reason"] == "bundle_partial", (
        f"expected reason='bundle_partial' (partial bundle), got {dashboard_entry['reason']!r}"
    )
    assert dashboard_entry["severity"] == "error", (
        f"expected severity='error' for partial bundle, got {dashboard_entry['severity']!r}"
    )

    # The missing-sample MUST include the dropped chunks so the
    # operator-facing surface points at what to look for.
    metadata = dashboard_entry["metadata"]
    assert metadata["verdict"] == "partial"
    assert metadata["missing_count"] >= 3
    assert isinstance(metadata["missing_sample"], list)
    assert len(metadata["missing_sample"]) >= 1

    # Action chips MUST surface the canonical operator-action — reinstall
    # or run sovyx doctor — so the banner is operator-actionable.
    chip_labels = {chip["label_token"] for chip in dashboard_entry["action_chips"]}
    assert "degraded.dashboard.reinstall" in chip_labels
    assert "degraded.dashboard.runDoctor" in chip_labels


def test_severity_escalates_when_dashboard_compounds_with_other_axes(
    tmp_path: Path,
) -> None:
    """Mission C5 §1.4 — when the dashboard axis degrades alongside a
    voice/llm/stt axis, composite severity escalates (1=warn → 2=error →
    3+=critical) per C4 ADR-D6. This proves anti-pattern #42's
    "cognitive frame collapsing N WARNs into ONE banner" remains intact
    with the 4th axis added.
    """
    fixture = _build_partial_bundle(tmp_path, drop_count=1)

    # Seed a sibling axis (voice) into the store before boot so the
    # composite payload sees both.
    from sovyx.engine._degraded_store import DegradedEntry, get_default_degraded_store

    get_default_degraded_store().record(
        DegradedEntry(
            axis="voice",
            reason="failover_ladder_exhausted",
            severity="error",
            title_token="degraded.voice.failoverExhausted.title",
            body_token="degraded.voice.failoverExhausted.body",
            action_chips=(),
            metadata={},
            first_observed_monotonic=1.0,
            last_observed_monotonic=1.0,
            occurrence_count=1,
        ),
    )

    original = dashboard_server.STATIC_DIR
    dashboard_server.STATIC_DIR = fixture
    try:
        app = dashboard_server.create_app(token=_TOKEN)
    finally:
        dashboard_server.STATIC_DIR = original
    client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

    resp = client.get("/api/engine/degraded")
    payload = resp.json()
    axes = {entry["axis"] for entry in payload["axes"]}
    assert {"voice", "dashboard"}.issubset(axes)
    # 2 distinct axes → composite_severity = error per ADR-D6
    assert payload["composite_axis_count"] >= 2
    assert payload["composite_severity"] == "error"
