"""Unit tests for ``sovyx dashboard doctor`` (Mission C5 §T3.3 / §T3.5)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from sovyx.cli.main import app
from sovyx.dashboard._integrity import scan_bundle_integrity

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[3]
HEAD_STATIC_DIR = REPO_ROOT / "src" / "sovyx" / "dashboard" / "static"


def _copy_head(tmp: Path) -> Path:
    fixture = tmp / "static"
    shutil.copytree(HEAD_STATIC_DIR, fixture)
    return fixture


def _with_static_dir(static_dir: Path, args: list[str]) -> tuple[int, str]:
    """Run ``sovyx dashboard doctor`` with STATIC_DIR monkeypatched."""
    from sovyx.cli.commands import dashboard as dashboard_cmd

    original = dashboard_cmd.STATIC_DIR
    dashboard_cmd.STATIC_DIR = static_dir
    try:
        result = runner.invoke(app, args)
    finally:
        dashboard_cmd.STATIC_DIR = original
    return result.exit_code, result.stdout


class TestDashboardDoctorExitCodes:
    def test_healthy_exits_zero(self) -> None:
        """The committed HEAD bundle MUST yield exit 0 + FULLY_PRESENT."""
        result = runner.invoke(app, ["dashboard", "doctor"])
        assert result.exit_code == 0
        assert "FULLY_PRESENT" in result.stdout

    def test_partial_exits_nonzero(self, tmp_path: Path) -> None:
        fixture = _copy_head(tmp_path)
        baseline = scan_bundle_integrity(fixture)
        (fixture / baseline.referenced_assets[0]).unlink()
        rc, out = _with_static_dir(fixture, ["dashboard", "doctor"])
        assert rc != 0
        assert "PARTIAL" in out

    def test_index_html_missing_exits_nonzero(self, tmp_path: Path) -> None:
        fixture = tmp_path / "static"
        fixture.mkdir()
        (fixture / "assets").mkdir()
        rc, out = _with_static_dir(fixture, ["dashboard", "doctor"])
        assert rc != 0
        assert "INDEX_HTML_MISSING" in out

    def test_static_dir_missing_exits_nonzero(self, tmp_path: Path) -> None:
        fixture = tmp_path / "nonexistent"
        rc, out = _with_static_dir(fixture, ["dashboard", "doctor"])
        assert rc != 0
        assert "STATIC_DIR_MISSING" in out


class TestDashboardDoctorJsonOutput:
    def test_json_output_parseable_on_healthy(self) -> None:
        result = runner.invoke(app, ["dashboard", "doctor", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["verdict"] == "fully_present"
        assert isinstance(payload["referenced_count"], int)
        assert payload["referenced_count"] >= 1
        assert isinstance(payload["scan_duration_ms"], (int, float))

    def test_json_output_lists_missing(self, tmp_path: Path) -> None:
        fixture = _copy_head(tmp_path)
        baseline = scan_bundle_integrity(fixture)
        (fixture / baseline.referenced_assets[0]).unlink()
        rc, out = _with_static_dir(fixture, ["dashboard", "doctor", "--json"])
        assert rc != 0
        payload = json.loads(out)
        assert payload["verdict"] == "partial"
        assert payload["missing_count"] >= 1
        assert isinstance(payload["missing_assets"], list)
        assert len(payload["missing_assets"]) >= 1


class TestDashboardDoctorHumanRendering:
    def test_remediation_hint_shows_pipx(self, tmp_path: Path) -> None:
        fixture = _copy_head(tmp_path)
        baseline = scan_bundle_integrity(fixture)
        (fixture / baseline.referenced_assets[0]).unlink()
        rc, out = _with_static_dir(fixture, ["dashboard", "doctor"])
        assert rc != 0
        assert "pipx" in out.lower() or "npm" in out.lower()

    def test_missing_chunks_listed(self, tmp_path: Path) -> None:
        fixture = _copy_head(tmp_path)
        baseline = scan_bundle_integrity(fixture)
        target = baseline.referenced_assets[0]
        (fixture / target).unlink()
        rc, out = _with_static_dir(fixture, ["dashboard", "doctor"])
        assert rc != 0
        # The exact chunk name should appear in the rendered list.
        chunk_name = target.split("/")[-1]
        assert chunk_name in out
