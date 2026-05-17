"""Unit tests for `_render_dashboard_integrity_surface` (Mission C5 §T3.4).

Mirrors the C4 test pattern at `test_doctor_voice_degraded_banner_surface.py`
(if it exists in the suite) — verifies the aggregate ``sovyx doctor``
command renders the dashboard integrity surface alongside the voice
surfaces.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from sovyx.cli.commands.doctor import _render_dashboard_integrity_surface
from sovyx.dashboard._integrity import scan_bundle_integrity

REPO_ROOT = Path(__file__).resolve().parents[3]
HEAD_STATIC_DIR = REPO_ROOT / "src" / "sovyx" / "dashboard" / "static"


def _copy_head(tmp: Path) -> Path:
    fixture = tmp / "static"
    shutil.copytree(HEAD_STATIC_DIR, fixture)
    return fixture


class TestDashboardIntegritySurface:
    def test_json_mode_suppresses_render(
        self,
        capsys: __import__("pytest").CaptureFixture[str],
    ) -> None:
        """JSON mode emits nothing via the rich console."""
        _render_dashboard_integrity_surface(output_json=True)
        captured = capsys.readouterr()
        # Rich may emit a trailing newline at most; either way the
        # FULLY_PRESENT / verdict label MUST NOT appear.
        assert "FULLY_PRESENT" not in captured.out
        assert "PARTIAL" not in captured.out

    def test_healthy_renders_section_header(
        self,
        capsys: __import__("pytest").CaptureFixture[str],
    ) -> None:
        _render_dashboard_integrity_surface(output_json=False)
        captured = capsys.readouterr()
        assert "Dashboard — bundle integrity" in captured.out
        assert "FULLY_PRESENT" in captured.out

    def test_partial_renders_missing_sample(
        self,
        tmp_path: Path,
        capsys: __import__("pytest").CaptureFixture[str],
        monkeypatch: __import__("pytest").MonkeyPatch,
    ) -> None:
        """When a chunk is missing, the surface lists the missing chunk."""
        fixture = _copy_head(tmp_path)
        baseline = scan_bundle_integrity(fixture)
        target = baseline.referenced_assets[0]
        (fixture / target).unlink()

        # Monkeypatch the STATIC_DIR resolved inside the surface function.
        from sovyx import dashboard as dashboard_pkg

        monkeypatch.setattr(dashboard_pkg, "STATIC_DIR", fixture)
        _render_dashboard_integrity_surface(output_json=False)
        captured = capsys.readouterr()
        assert "PARTIAL" in captured.out
        # The target chunk name must appear in the rendered output.
        chunk_name = target.split("/")[-1]
        assert chunk_name in captured.out
