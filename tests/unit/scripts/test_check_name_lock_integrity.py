"""Tests for ``scripts/dev/check_name_lock_integrity.py`` (Quality Gate 19).

Mission anchor: ``docs-internal/MISSION-OMEGA-3-DOCS-ARCHITECTURE-2026-06-01.md`` §T0.

Gate 19 (anti-pattern #68 DRAFT) rejects dead ``docs-internal/*`` path links in
``src/sovyx`` docstrings — these ship to PyPI as public dead links (CLAUDE.md
Git section). Past archive moves silently rotted 34 such links. These tests pin
the detector logic (so a refactor can't break detection silently) AND guard the
live repo (end-to-end PASS = no dead link was reintroduced).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[3] / "scripts" / "dev" / "check_name_lock_integrity.py"
)
_spec = importlib.util.spec_from_file_location("check_name_lock_integrity", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)


def test_normalize_strips_trailing_non_path_chars() -> None:
    assert checker._normalize("docs-internal/foo.md.") == "docs-internal/foo.md"
    assert checker._normalize("docs-internal/dir/") == "docs-internal/dir"
    assert checker._normalize("docs-internal/wrapped-") == "docs-internal/wrapped"
    assert checker._normalize("docs-internal/foo.md") == "docs-internal/foo.md"


def test_resolves_existing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "docs-internal").mkdir()
    (tmp_path / "docs-internal" / "real.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    assert checker._resolves("docs-internal/real.md") is True


def test_resolves_missing_file_is_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "docs-internal").mkdir()
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    assert checker._resolves("docs-internal/ghost.md") is False


def test_resolves_prefix_tolerates_wrapped_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missions = tmp_path / "docs-internal" / "missions"
    missions.mkdir(parents=True)
    (missions / "MISSION-c5-dashboard-distribution-integrity-2026-05-17.md").write_text(
        "x", encoding="utf-8"
    )
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    # A reference broken across two comment lines still resolves via prefix match.
    assert checker._resolves("docs-internal/missions/MISSION-c5-dashboard-distribution") is True


def _make_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, src_body: str) -> None:
    src = tmp_path / "src" / "sovyx"
    src.mkdir(parents=True)
    (tmp_path / "docs-internal").mkdir()
    (tmp_path / "docs-internal" / "real.md").write_text("x", encoding="utf-8")
    (src / "mod.py").write_text(src_body, encoding="utf-8")
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    monkeypatch.setattr(checker, "SRC", src)


def test_main_flags_dead_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_tree(tmp_path, monkeypatch, '"""See docs-internal/plans/ghost.md for details."""\n')
    rc = checker.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "violation(s)" in out
    assert "docs-internal/plans/ghost.md" in out


def test_main_passes_clean_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_tree(tmp_path, monkeypatch, '"""See docs-internal/real.md for the rationale."""\n')
    rc = checker.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out


def test_main_ignores_bare_spec_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A spec-ID citation carrying NO docs-internal/ prefix is provenance, not a link.
    _make_tree(tmp_path, monkeypatch, '"""Phase 11 Task 11.8 of IMPL-OBSERVABILITY-001."""\n')
    assert checker.main() == 0


def test_main_skips_signed_kb_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Signed _mixer_kb/profiles/*.yaml are immutable (editing breaks the
    # Ed25519 signature, AP #26) — a dead link there must NOT fail the gate.
    src = tmp_path / "src" / "sovyx"
    profiles = src / "voice" / "health" / "_mixer_kb" / "profiles"
    profiles.mkdir(parents=True)
    (tmp_path / "docs-internal").mkdir()
    (profiles / "dev_board.yaml").write_text(
        "# See docs-internal/diagnostics/ghost.md for the refactor.\nid: x\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    monkeypatch.setattr(checker, "SRC", src)
    assert checker.main() == 0  # signed profile skipped despite dead link

    # A non-signed README in the same dir is still scanned.
    (profiles / "README.md").write_text(
        "See docs-internal/diagnostics/ghost.md\n", encoding="utf-8"
    )
    assert checker.main() == 1


def test_live_repo_has_no_dead_links() -> None:
    """End-to-end guard: the real src/sovyx tree must have ZERO dead docs-internal links."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "name-lock integrity: PASS" in result.stdout
