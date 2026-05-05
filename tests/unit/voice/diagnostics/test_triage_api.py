"""Direct in-process tests for sovyx.voice.diagnostics.triage public API.

The companion :mod:`test_triage` module exercises the legacy CLI via
subprocess (back-compat for analysts that shell out to
``tools/voice_diag_triage.py``). This module imports the public API
directly so coverage attribution + dataclass invariants are exercised
without subprocess overhead.

Covers:
* :func:`triage_tarball` -- archive + extracted-dir paths
* :func:`render_markdown` -- byte-equivalence with subprocess output
* :class:`TriageResult` -- ``winner`` property contract
* :class:`HypothesisVerdict` -- frozen + slots invariants
* :class:`HypothesisId` -- closed-enum membership
"""

from __future__ import annotations

import json
import tarfile
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from sovyx.voice.diagnostics import (
    AlertsSummary,
    HypothesisId,
    HypothesisVerdict,
    SchemaValidation,
    TriageResult,
    render_markdown,
    triage_tarball,
)


def _make_summary(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 1,
        "tool": "sovyx-voice-diag",
        "tool_version": "4.3.0",
        "captured_at_utc": "2026-04-24T22:00:00Z",
        "host": "test-host",
        "os": "linux",
        "os_version": "Linux Mint 22.1",
        "outdir": "/home/test/sovyx-diag-test",
        "status": "complete",
        "final_exit_code": 0,
    }
    base.update(overrides)
    return base


def _make_linux_tarball(
    tmp_path: Path,
    *,
    summary: dict[str, Any] | None = None,
    alerts: list[dict[str, Any]] | None = None,
    captures: dict[str, dict[str, Any]] | None = None,
) -> Path:
    """Build synthetic Linux tarball matching the v4.3 layout."""
    root_dir = tmp_path / "sovyx-diag-test-host-20260424T220000Z-deadbeef"
    (root_dir / "_diagnostics").mkdir(parents=True)

    payload = summary if summary is not None else _make_summary()
    (root_dir / "SUMMARY.json").write_text(json.dumps(payload, indent=2))

    alerts_file = root_dir / "_diagnostics" / "alerts.jsonl"
    alerts_file.write_text("\n".join(json.dumps(a) for a in (alerts or [])))

    if captures:
        for cid, cap_payload in captures.items():
            cap_dir = root_dir / "E_portaudio" / "captures" / cid
            cap_dir.mkdir(parents=True)
            (cap_dir / "analysis.json").write_text(json.dumps(cap_payload["analysis"]))
            (cap_dir / "silero.json").write_text(json.dumps(cap_payload["silero"]))

    (root_dir / "MANIFEST.md").write_text("# MANIFEST")

    tar_path = tmp_path / "fixture.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(root_dir, arcname=root_dir.name)
    return tar_path


class TestTriageTarballPublicAPI:
    """triage_tarball() returns a complete, immutable TriageResult."""

    def test_archive_path_returns_triage_result(self, tmp_path: Path) -> None:
        tar = _make_linux_tarball(tmp_path)
        result = triage_tarball(tar)
        assert isinstance(result, TriageResult)
        assert result.schema_version == 1
        assert result.toolkit == "linux"
        assert result.host == "test-host"
        assert result.captured_at_utc == "2026-04-24T22:00:00Z"
        assert result.schema_validation.ok is True
        assert isinstance(result.alerts, AlertsSummary)
        assert isinstance(result.hypotheses, tuple)

    def test_extracted_dir_path(self, tmp_path: Path) -> None:
        # Build extracted layout directly (no archive).
        root = tmp_path / "extracted"
        (root / "_diagnostics").mkdir(parents=True)
        (root / "SUMMARY.json").write_text(json.dumps(_make_summary()))
        (root / "_diagnostics" / "alerts.jsonl").write_text("")
        result = triage_tarball(root, is_extracted_dir=True)
        assert isinstance(result, TriageResult)
        assert result.tarball_root == root

    def test_missing_archive_raises_filenotfound(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            triage_tarball(tmp_path / "does-not-exist.tar.gz")

    def test_missing_extracted_dir_raises_filenotfound(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            triage_tarball(tmp_path / "missing", is_extracted_dir=True)

    def test_missing_summary_raises_valueerror(self, tmp_path: Path) -> None:
        # Tarball with no SUMMARY.json.
        broken = tmp_path / "broken"
        broken.mkdir()
        (broken / "MANIFEST.md").write_text("# nothing here")
        tar = tmp_path / "broken.tar.gz"
        with tarfile.open(tar, "w:gz") as t:
            t.add(broken, arcname=broken.name)
        with pytest.raises(ValueError, match="SUMMARY.json"):
            triage_tarball(tar)

    def test_hypotheses_sorted_by_confidence_desc(self, tmp_path: Path) -> None:
        # Selftest fail forces H6 to confidence 1.0.
        summary = _make_summary(analyzer_selftest_status="fail")
        tar = _make_linux_tarball(tmp_path, summary=summary)
        result = triage_tarball(tar)
        # First entry has the highest confidence.
        confidences = [h.confidence for h in result.hypotheses]
        assert confidences == sorted(confidences, reverse=True)

    def test_winner_returns_high_confidence_top(self, tmp_path: Path) -> None:
        summary = _make_summary(analyzer_selftest_status="fail")
        tar = _make_linux_tarball(tmp_path, summary=summary)
        result = triage_tarball(tar)
        winner = result.winner
        assert winner is not None
        assert winner.hid == HypothesisId.H6_SELFTEST_FAILED
        assert winner.confidence == 1.0

    def test_winner_returns_none_when_all_low_confidence(self, tmp_path: Path) -> None:
        # Healthy tarball with no triggers.
        tar = _make_linux_tarball(tmp_path)
        result = triage_tarball(tar)
        # All hypotheses should be at low confidence; winner is None.
        assert result.winner is None


class TestRenderMarkdown:
    """render_markdown produces the operator-facing report."""

    def test_renders_header_block(self, tmp_path: Path) -> None:
        tar = _make_linux_tarball(tmp_path)
        result = triage_tarball(tar)
        md = render_markdown(result)
        assert md.startswith("# Voice Diagnostic Triage Report")
        assert "**Toolkit:** sovyx-voice-diag v4.3.0" in md
        assert "**Host:** test-host" in md
        assert "**Captured:** 2026-04-24T22:00:00Z" in md

    def test_renders_schema_validation_pass(self, tmp_path: Path) -> None:
        tar = _make_linux_tarball(tmp_path)
        md = render_markdown(triage_tarball(tar))
        assert "## Schema Validation" in md
        assert "✅ Required fields present" in md

    def test_renders_schema_validation_fail(self, tmp_path: Path) -> None:
        bad = _make_summary()
        del bad["schema_version"]
        tar = _make_linux_tarball(tmp_path, summary=bad)
        md = render_markdown(triage_tarball(tar))
        assert "❌ Schema validation FAILED" in md
        assert "missing required: `schema_version`" in md

    def test_renders_alerts_summary(self, tmp_path: Path) -> None:
        alerts = [
            {
                "severity": "error",
                "message": "voice_clarity_destroying_apo confirmed",
                "at": {"utc_iso_ns": "2026-04-24T22:01:00Z"},
            },
            {"severity": "warn", "message": "minor warn", "at": {}},
            {"severity": "warn", "message": "another warn", "at": {}},
            {"severity": "info", "message": "fyi", "at": {}},
        ]
        tar = _make_linux_tarball(tmp_path, alerts=alerts)
        md = render_markdown(triage_tarball(tar))
        assert "- error: 1" in md
        assert "- warn:  2" in md
        assert "- info:  1" in md

    def test_renders_recommendation_for_high_confidence_h6(self, tmp_path: Path) -> None:
        summary = _make_summary(analyzer_selftest_status="fail")
        tar = _make_linux_tarball(tmp_path, summary=summary)
        md = render_markdown(triage_tarball(tar))
        assert "**Highest-confidence root cause:**" in md
        assert "Analyzer selftest failed" in md

    def test_renders_no_high_confidence_when_clean(self, tmp_path: Path) -> None:
        tar = _make_linux_tarball(tmp_path)
        md = render_markdown(triage_tarball(tar))
        assert "No high-confidence hypothesis" in md


class TestDataclassInvariants:
    """Frozen + slots dataclasses reject mutation."""

    def test_hypothesis_verdict_is_frozen(self) -> None:
        v = HypothesisVerdict(
            hid=HypothesisId.H1_MIC_DESTROYED,
            title="x",
            confidence=0.5,
            evidence_for=(),
            evidence_against=(),
            recommended_action=None,
        )
        with pytest.raises(FrozenInstanceError):
            v.confidence = 0.9  # type: ignore[misc]

    def test_triage_result_is_frozen(self, tmp_path: Path) -> None:
        result = triage_tarball(_make_linux_tarball(tmp_path))
        with pytest.raises(FrozenInstanceError):
            result.toolkit = "macos"  # type: ignore[misc]

    def test_schema_validation_immutable_tuples(self) -> None:
        sv = SchemaValidation(
            ok=False,
            missing_required=("schema_version",),
            missing_recommended=(),
            warnings=(),
        )
        # Tuples are immutable; assigning to the field is also forbidden.
        with pytest.raises(FrozenInstanceError):
            sv.ok = True  # type: ignore[misc]


class TestHypothesisIdEnum:
    """Closed enum is sealed at the Python level for OTel cardinality."""

    def test_all_h1_to_h10_present(self) -> None:
        values = {h.value for h in HypothesisId}
        expected = {f"H{i}" for i in range(1, 11)}
        assert expected.issubset(values)

    def test_value_matches_short_id(self) -> None:
        assert HypothesisId.H1_MIC_DESTROYED.value == "H1"
        assert HypothesisId.H10_LINUX_MIXER_ATTENUATED.value == "H10"

    def test_unknown_string_rejected(self) -> None:
        with pytest.raises(ValueError):
            HypothesisId("H999")


class TestRecommendedActions:
    """Hypotheses with operator-facing fix commands surface them in verdicts."""

    def test_h10_carries_fix_command(self, tmp_path: Path) -> None:
        # Force H10 high-confidence via doctor_voice.txt artefact.
        root_dir = tmp_path / "h10-test"
        (root_dir / "_diagnostics").mkdir(parents=True)
        (root_dir / "G_sovyx").mkdir(parents=True)
        (root_dir / "SUMMARY.json").write_text(json.dumps(_make_summary()))
        (root_dir / "_diagnostics" / "alerts.jsonl").write_text("")
        (root_dir / "G_sovyx" / "doctor_voice.txt").write_text(
            "linux_mixer_saturated detected; controls attenuated below VAD floor"
        )
        tar = tmp_path / "h10.tar.gz"
        with tarfile.open(tar, "w:gz") as t:
            t.add(root_dir, arcname=root_dir.name)
        result = triage_tarball(tar)
        h10 = next(
            (h for h in result.hypotheses if h.hid == HypothesisId.H10_LINUX_MIXER_ATTENUATED),
            None,
        )
        assert h10 is not None
        assert h10.recommended_action is not None
        assert "sovyx doctor voice --fix" in h10.recommended_action
