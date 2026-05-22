"""Falsifiability tests — Mission C Gate 8 boundary round-trip coverage.

Mission anchor:
``docs-internal/MISSION-C-REMEDIATION-PLAN-2026-05-21.md`` §3 Phase C.6
sub-sequence step 1 + the original
``docs-internal/missions/MISSION-c2-voice-status-response-contract-2026-05-16.md``
§T4.1 STRICT scope contract.

These tests EXIST to be the operational proof that:

1. **Voice scope (default) is STRICT.** Synthetic ``voice.py`` with an
   uncovered ``Model.model_validate(...)`` exits 1. Behaviour is
   identical to the pre-Mission-C-C.6-§1 baseline.
2. **Voice scope vacuous pass.** No ``.model_validate`` sites in
   ``voice.py`` → exit 0 with the "vacuous pass" summary line that
   ``scripts/verify_gates.sh`` greps for.
3. **All scope LENIENT.** Same uncovered site but ``--scope all``
   returns 0 (warning emitted to stdout) so the harness does not
   redden on the C.4-wired routes that don't yet have paired tests.
4. **All scope + ``--strict`` STILL fails.** STRICT mode is reachable
   from the all-routes scope so a future v0.5x.0 flip is a simple
   ``--strict`` toggle.
5. **``SOVYX_GATES_C2_SCOPE=all`` env override matches the
   ``--scope all`` flag** behaviour (precedence: CLI > env > default).
6. **CLI ``--scope voice`` overrides ``SOVYX_GATES_C2_SCOPE=all``.**
7. **All scope skips ``__init__.py`` and ``_*`` private helpers** —
   only real route modules are scanned.
8. **Invalid scope value exits 2** (operator-actionable error).
9. **Voice-scope summary-line contract preserved** so
   ``scripts/verify_gates.sh`` Gate 8 grep continues to match.

If any test here regresses, Gate 8 is broken — DO NOT silence it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCANNER = _REPO_ROOT / "scripts" / "dev" / "check_boundary_round_trip_coverage.py"


def _write_voice_routes_uncovered(scan_root: Path) -> None:
    scan_root.mkdir(parents=True, exist_ok=True)
    (scan_root / "voice.py").write_text(
        """\
from pydantic import BaseModel


class SyntheticUncoveredResponse(BaseModel):
    ok: bool


def handler() -> SyntheticUncoveredResponse:
    payload = {"ok": True}
    return SyntheticUncoveredResponse.model_validate(payload)
""",
        encoding="utf-8",
    )


def _write_voice_routes_covered(scan_root: Path, test_dir: Path) -> None:
    scan_root.mkdir(parents=True, exist_ok=True)
    test_dir.mkdir(parents=True, exist_ok=True)
    (scan_root / "voice.py").write_text(
        """\
from pydantic import BaseModel


class SyntheticCoveredResponse(BaseModel):
    ok: bool


def handler() -> SyntheticCoveredResponse:
    payload = {"ok": True}
    return SyntheticCoveredResponse.model_validate(payload)
""",
        encoding="utf-8",
    )
    # Paired test exercises Model.model_validate(...) directly.
    (test_dir / "test_synthetic_covered.py").write_text(
        """\
def test_pairs():
    # Mention SyntheticCoveredResponse and exercise model_validate.
    payload = {"ok": True}
    SyntheticCoveredResponse.model_validate(payload)
""",
        encoding="utf-8",
    )


def _write_voice_routes_empty(scan_root: Path) -> None:
    scan_root.mkdir(parents=True, exist_ok=True)
    (scan_root / "voice.py").write_text(
        """\
def handler() -> dict:
    return {"ok": True}
""",
        encoding="utf-8",
    )


def _write_multi_routes_with_uncovered(scan_root: Path) -> None:
    scan_root.mkdir(parents=True, exist_ok=True)
    (scan_root / "voice.py").write_text(
        """\
from pydantic import BaseModel


class VoiceShape(BaseModel):
    ok: bool


def voice_handler():
    return VoiceShape.model_validate({"ok": True})
""",
        encoding="utf-8",
    )
    (scan_root / "other_route.py").write_text(
        """\
from pydantic import BaseModel


class OtherShape(BaseModel):
    ok: bool


def other_handler():
    return OtherShape.model_validate({"ok": True})
""",
        encoding="utf-8",
    )
    # __init__.py + private helper must be skipped.
    (scan_root / "__init__.py").write_text("", encoding="utf-8")
    (scan_root / "_private_helper.py").write_text(
        """\
from pydantic import BaseModel


class HelperShape(BaseModel):
    ok: bool


def helper_call():
    # This MUST be skipped by all-scope (private helper).
    return HelperShape.model_validate({"ok": True})
""",
        encoding="utf-8",
    )


def _run_scanner(
    *args: object,
    scan_root: Path,
    test_dir: Path,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Invoke the scanner; return (returncode, stdout, stderr)."""
    cmd: list[str] = [
        sys.executable,
        str(_SCANNER),
        "--scan-root",
        str(scan_root),
        "--test-dir",
        str(test_dir),
    ]
    for arg in args:
        cmd.append(str(arg))
    env = None
    if extra_env is not None:
        import os

        env = {**os.environ, **extra_env}
    proc = subprocess.run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _scanner_json(
    *args: object,
    scan_root: Path,
    test_dir: Path,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    rc, stdout, _stderr = _run_scanner(
        "--json",
        *args,
        scan_root=scan_root,
        test_dir=test_dir,
        extra_env=extra_env,
    )
    return rc, json.loads(stdout)


class TestGate8VoiceScopeStrictDefault:
    """Default behaviour is STRICT on voice.py — preserves Mission C2 §T4.1."""

    def test_uncovered_voice_site_exits_1(self, tmp_path: Path) -> None:
        scan_root = tmp_path / "routes"
        test_dir = tmp_path / "tests"
        _write_voice_routes_uncovered(scan_root)
        test_dir.mkdir()
        rc, _stdout, stderr = _run_scanner(
            scan_root=scan_root,
            test_dir=test_dir,
        )
        assert rc == 1
        assert "FAILED" in stderr
        assert "SyntheticUncoveredResponse" in stderr

    def test_covered_voice_site_exits_0_with_grep_summary(self, tmp_path: Path) -> None:
        scan_root = tmp_path / "routes"
        test_dir = tmp_path / "tests"
        _write_voice_routes_covered(scan_root, test_dir)
        rc, stdout, _stderr = _run_scanner(
            scan_root=scan_root,
            test_dir=test_dir,
        )
        assert rc == 0
        # verify_gates.sh greps for "boundary round-trip coverage:.*all paired"
        assert "boundary round-trip coverage:" in stdout
        assert "all paired with tests" in stdout

    def test_voice_vacuous_pass_when_no_model_validate(self, tmp_path: Path) -> None:
        scan_root = tmp_path / "routes"
        test_dir = tmp_path / "tests"
        _write_voice_routes_empty(scan_root)
        test_dir.mkdir()
        rc, stdout, _stderr = _run_scanner(
            scan_root=scan_root,
            test_dir=test_dir,
        )
        assert rc == 0
        # verify_gates.sh greps for "vacuous pass"
        assert "vacuous pass" in stdout

    def test_voice_scope_json_strict_is_true(self, tmp_path: Path) -> None:
        scan_root = tmp_path / "routes"
        test_dir = tmp_path / "tests"
        _write_voice_routes_covered(scan_root, test_dir)
        rc, payload = _scanner_json(
            scan_root=scan_root,
            test_dir=test_dir,
        )
        assert rc == 0
        assert payload["scope"] == "voice"
        assert payload["strict"] is True
        assert payload["passed"] is True


class TestGate8AllScopeLenientByDefault:
    """All-routes scope is LENIENT by default — warns but does not fail."""

    def test_uncovered_in_all_scope_exits_0(self, tmp_path: Path) -> None:
        scan_root = tmp_path / "routes"
        test_dir = tmp_path / "tests"
        _write_multi_routes_with_uncovered(scan_root)
        test_dir.mkdir()
        rc, stdout, _stderr = _run_scanner(
            "--scope",
            "all",
            scan_root=scan_root,
            test_dir=test_dir,
        )
        assert rc == 0
        assert "all-routes scope" in stdout
        assert "LENIENT" in stdout
        # Both real route models reported uncovered; __init__.py + _private
        # helper MUST NOT contribute.
        assert "VoiceShape" in stdout
        assert "OtherShape" in stdout
        assert "HelperShape" not in stdout

    def test_uncovered_in_all_scope_json_reports_both_models(self, tmp_path: Path) -> None:
        scan_root = tmp_path / "routes"
        test_dir = tmp_path / "tests"
        _write_multi_routes_with_uncovered(scan_root)
        test_dir.mkdir()
        rc, payload = _scanner_json(
            "--scope",
            "all",
            scan_root=scan_root,
            test_dir=test_dir,
        )
        assert rc == 0
        assert payload["scope"] == "all"
        assert payload["strict"] is False  # LENIENT default
        assert payload["passed"] is False
        models = {entry["model"] for entry in payload["uncovered_models"]}  # type: ignore[index]
        assert models == {"VoiceShape", "OtherShape"}
        assert payload["files_scanned"] == 2  # voice.py + other_route.py

    def test_all_scope_summary_does_not_match_voice_grep_contract(self, tmp_path: Path) -> None:
        """verify_gates.sh greps the voice-scope summary line ('all
        paired with tests'). All-scope LENIENT MUST NOT emit that exact
        phrase so the harness does not false-positive on a LENIENT warn.
        """
        scan_root = tmp_path / "routes"
        test_dir = tmp_path / "tests"
        _write_multi_routes_with_uncovered(scan_root)
        test_dir.mkdir()
        _rc, stdout, _stderr = _run_scanner(
            "--scope",
            "all",
            scan_root=scan_root,
            test_dir=test_dir,
        )
        assert "all paired with tests" not in stdout


class TestGate8AllScopeStrictFlip:
    """A future STRICT flip is one ``--strict`` flag away."""

    def test_uncovered_in_all_scope_with_strict_exits_1(self, tmp_path: Path) -> None:
        scan_root = tmp_path / "routes"
        test_dir = tmp_path / "tests"
        _write_multi_routes_with_uncovered(scan_root)
        test_dir.mkdir()
        rc, stdout, _stderr = _run_scanner(
            "--scope",
            "all",
            "--strict",
            scan_root=scan_root,
            test_dir=test_dir,
        )
        assert rc == 1
        assert "all-routes scope" in stdout
        assert "STRICT" in stdout

    def test_uncovered_in_all_scope_with_env_strict_exits_1(self, tmp_path: Path) -> None:
        scan_root = tmp_path / "routes"
        test_dir = tmp_path / "tests"
        _write_multi_routes_with_uncovered(scan_root)
        test_dir.mkdir()
        rc, _stdout, _stderr = _run_scanner(
            "--scope",
            "all",
            scan_root=scan_root,
            test_dir=test_dir,
            extra_env={"SOVYX_C_GATE_STRICT": "1"},
        )
        assert rc == 1


class TestGate8ScopePrecedence:
    """CLI flag > env var > default."""

    def test_env_scope_all_matches_cli_scope_all(self, tmp_path: Path) -> None:
        scan_root = tmp_path / "routes"
        test_dir = tmp_path / "tests"
        _write_multi_routes_with_uncovered(scan_root)
        test_dir.mkdir()
        rc, payload = _scanner_json(
            scan_root=scan_root,
            test_dir=test_dir,
            extra_env={"SOVYX_GATES_C2_SCOPE": "all"},
        )
        assert rc == 0
        assert payload["scope"] == "all"
        assert payload["files_scanned"] == 2

    def test_cli_voice_overrides_env_all(self, tmp_path: Path) -> None:
        scan_root = tmp_path / "routes"
        test_dir = tmp_path / "tests"
        _write_voice_routes_uncovered(scan_root)
        test_dir.mkdir()
        rc, payload = _scanner_json(
            "--scope",
            "voice",
            scan_root=scan_root,
            test_dir=test_dir,
            extra_env={"SOVYX_GATES_C2_SCOPE": "all"},
        )
        assert rc == 1  # voice scope STRICT on uncovered
        assert payload["scope"] == "voice"

    def test_default_scope_is_voice_when_env_unset(self, tmp_path: Path) -> None:
        scan_root = tmp_path / "routes"
        test_dir = tmp_path / "tests"
        _write_voice_routes_covered(scan_root, test_dir)
        # Explicitly clear SOVYX_GATES_C2_SCOPE (subprocess inherits the
        # parent env otherwise) by passing extra_env that removes any
        # ambient value. We achieve this by always passing the env dict
        # without that key — see _run_scanner. The default voice scope
        # is the no-override path.
        rc, payload = _scanner_json(
            scan_root=scan_root,
            test_dir=test_dir,
            extra_env={"SOVYX_GATES_C2_SCOPE": ""},
        )
        # Empty string is not a valid scope; the resolver MUST fall back.
        # In current implementation, an empty-string env value is treated
        # as falsy by the `or` chain, so it falls through to default.
        assert rc == 0
        assert payload["scope"] == "voice"


class TestGate8InvalidScope:
    def test_invalid_scope_exits_2(self, tmp_path: Path) -> None:
        scan_root = tmp_path / "routes"
        test_dir = tmp_path / "tests"
        scan_root.mkdir()
        test_dir.mkdir()
        rc, _stdout, stderr = _run_scanner(
            scan_root=scan_root,
            test_dir=test_dir,
            extra_env={"SOVYX_GATES_C2_SCOPE": "nonsense"},
        )
        assert rc == 2
        assert "unknown scope" in stderr


class TestGate8AllScopePrivateHelperSkip:
    """All scope MUST skip __init__.py and ``_*.py`` private helpers."""

    def test_private_helper_models_not_counted(self, tmp_path: Path) -> None:
        scan_root = tmp_path / "routes"
        test_dir = tmp_path / "tests"
        _write_multi_routes_with_uncovered(scan_root)
        test_dir.mkdir()
        _rc, payload = _scanner_json(
            "--scope",
            "all",
            scan_root=scan_root,
            test_dir=test_dir,
        )
        # files_scanned excludes __init__.py + _private_helper.py
        assert payload["files_scanned"] == 2
        # HelperShape (from _private_helper.py) MUST NOT appear in any
        # model list.
        assert "HelperShape" not in payload["unique_models"]  # type: ignore[operator]
        assert all(
            entry["model"] != "HelperShape"  # type: ignore[index]
            for entry in payload["uncovered_models"]  # type: ignore[union-attr]
        )
