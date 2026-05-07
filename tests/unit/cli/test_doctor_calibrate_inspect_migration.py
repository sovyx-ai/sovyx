"""Unit tests for ``sovyx doctor voice --calibrate --inspect-migration``.

Read-only operator inspection mode for schema bumps. Walks the
migration registry to bring the on-disk profile dict to the runtime's
current ``CALIBRATION_PROFILE_SCHEMA_VERSION``, emits the result to
stdout (pretty-printed), and exits 0 / 1 / 2 based on outcome.

Coverage:

* Mutex enforcement (--inspect-migration without --calibrate /
  combined with --show / --rollback / --evaluate-rules).
* Happy path (schema_version=1 passthrough; output is identity-shape JSON).
* File missing → EXIT_DOCTOR_GENERIC_FAILURE.
* Malformed JSON → EXIT_DOCTOR_GENERIC_FAILURE.
* Schema-version-newer-than-runtime → EXIT_DOCTOR_GENERIC_FAILURE
  (downgrade not supported).

History: introduced in v0.31.1 closing the v0.31.0 audit's F3
(orphan public API → wired CLI flag).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from sovyx.cli.main import app
from sovyx.voice.calibration import save_calibration_profile
from tests.unit.cli.test_doctor_calibrate import _r10_profile

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    """Defensive ANSI strip — conftest already sets NO_COLOR=1 +
    COLUMNS=240, but Rich's bold/dim/italic survive NO_COLOR on
    TTY-detected Linux runners. Strip is idempotent on plain ASCII."""
    return _ANSI_RE.sub("", text)


runner = CliRunner()


# ====================================================================
# Mutex enforcement
# ====================================================================


class TestInspectMigrationMutex:
    """--inspect-migration requires --calibrate; conflicts with peers."""

    def test_inspect_migration_without_calibrate_rejected(self) -> None:
        result = runner.invoke(app, ["doctor", "voice", "--inspect-migration"])
        assert result.exit_code != 0
        clean = _strip_ansi(result.output)
        assert "require --calibrate" in clean

    def test_inspect_migration_with_show_rejected(self) -> None:
        result = runner.invoke(
            app,
            ["doctor", "voice", "--calibrate", "--inspect-migration", "--show"],
        )
        assert result.exit_code != 0
        clean = _strip_ansi(result.output)
        assert "mutually exclusive" in clean

    def test_inspect_migration_with_rollback_rejected(self) -> None:
        result = runner.invoke(
            app,
            ["doctor", "voice", "--calibrate", "--inspect-migration", "--rollback"],
        )
        assert result.exit_code != 0
        clean = _strip_ansi(result.output)
        assert "mutually exclusive" in clean

    def test_inspect_migration_with_evaluate_rules_rejected(self) -> None:
        result = runner.invoke(
            app,
            [
                "doctor",
                "voice",
                "--calibrate",
                "--inspect-migration",
                "--evaluate-rules",
            ],
        )
        assert result.exit_code != 0
        clean = _strip_ansi(result.output)
        assert "mutually exclusive" in clean


# ====================================================================
# Behavioural tests
# ====================================================================


class TestInspectMigrationHappyPath:
    """``--calibrate --inspect-migration`` walks the migration chain."""

    def test_inspect_migration_v1_passthrough(self, tmp_path: Path) -> None:
        """schema_version=1 is the runtime current — identity migration."""
        fake_home = tmp_path / "home"
        sovyx_data = fake_home / ".sovyx"
        sovyx_data.mkdir(parents=True)

        with patch("sovyx.cli.commands.doctor.Path.home", return_value=fake_home):
            save_calibration_profile(_r10_profile(), data_dir=sovyx_data)
            result = runner.invoke(
                app,
                ["doctor", "voice", "--calibrate", "--inspect-migration"],
            )

        assert result.exit_code == 0
        # The migrated dict lands on stdout via sys.stdout.write so
        # operators can pipe into jq / diff. Parse it back to confirm
        # schema_version is the runtime current (=1) and the profile
        # round-tripped intact.
        # The header line ("Voice calibration migration inspection") is
        # printed to console BEFORE the JSON; split on the first '{' and
        # parse the rest.
        json_start = result.output.find("{")
        assert json_start >= 0, f"no JSON in output: {result.output!r}"
        parsed = json.loads(result.output[json_start:])
        assert parsed["schema_version"] == 1
        assert parsed["mind_id"] == "default"
        assert parsed["profile_id"] == "11111111-2222-3333-4444-555555555555"

    def test_inspect_migration_with_explicit_mind_id(self, tmp_path: Path) -> None:
        """``--mind-id <id>`` reads the right per-mind profile."""
        from dataclasses import replace

        fake_home = tmp_path / "home"
        sovyx_data = fake_home / ".sovyx"
        sovyx_data.mkdir(parents=True)

        custom = replace(_r10_profile(), mind_id="meu-mind")
        with patch("sovyx.cli.commands.doctor.Path.home", return_value=fake_home):
            save_calibration_profile(custom, data_dir=sovyx_data)
            result = runner.invoke(
                app,
                [
                    "doctor",
                    "voice",
                    "--calibrate",
                    "--inspect-migration",
                    "--mind-id",
                    "meu-mind",
                ],
            )

        assert result.exit_code == 0
        json_start = result.output.find("{")
        parsed = json.loads(result.output[json_start:])
        assert parsed["mind_id"] == "meu-mind"


# ====================================================================
# Failure modes
# ====================================================================


class TestInspectMigrationFailureModes:
    """File-missing / malformed / unsupported-version → exit non-zero."""

    def test_inspect_migration_no_profile_returns_failure(self, tmp_path: Path) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        # No save → no calibration.json on disk.
        with patch("sovyx.cli.commands.doctor.Path.home", return_value=fake_home):
            result = runner.invoke(
                app,
                ["doctor", "voice", "--calibrate", "--inspect-migration"],
            )

        assert result.exit_code != 0
        clean = _strip_ansi(result.output)
        assert "Inspection failed" in clean
        assert "calibration profile not found" in clean

    def test_inspect_migration_malformed_json_returns_failure(self, tmp_path: Path) -> None:
        fake_home = tmp_path / "home"
        sovyx_data = fake_home / ".sovyx" / "default"
        sovyx_data.mkdir(parents=True)
        # Write garbage to the canonical path.
        (sovyx_data / "calibration.json").write_text("{not json", encoding="utf-8")

        with patch("sovyx.cli.commands.doctor.Path.home", return_value=fake_home):
            result = runner.invoke(
                app,
                ["doctor", "voice", "--calibrate", "--inspect-migration"],
            )

        assert result.exit_code != 0
        clean = _strip_ansi(result.output)
        assert "Inspection failed" in clean
        assert "not valid JSON" in clean

    def test_inspect_migration_future_schema_returns_failure(self, tmp_path: Path) -> None:
        """schema_version=99 → MigrationError ('downgrade not supported')."""
        fake_home = tmp_path / "home"
        sovyx_data = fake_home / ".sovyx" / "default"
        sovyx_data.mkdir(parents=True)
        # Write a profile that claims a future schema version. The
        # other fields don't matter — the migration walker rejects on
        # version comparison BEFORE attempting any field access.
        future_dict = {
            "schema_version": 99,
            "profile_id": "future",
            "mind_id": "default",
            "fingerprint": {},
            "measurements": {},
            "decisions": [],
            "provenance": [],
            "generated_by_engine_version": "0.99.0",
            "generated_by_rule_set_version": 1,
            "generated_at_utc": "2099-01-01T00:00:00Z",
            "signature": None,
        }
        (sovyx_data / "calibration.json").write_text(
            json.dumps(future_dict),
            encoding="utf-8",
        )

        with patch("sovyx.cli.commands.doctor.Path.home", return_value=fake_home):
            result = runner.invoke(
                app,
                ["doctor", "voice", "--calibrate", "--inspect-migration"],
            )

        assert result.exit_code != 0
        clean = _strip_ansi(result.output)
        assert "Inspection failed" in clean
        # The migration walker's error mentions "downgrade not supported".
        assert "downgrade not supported" in clean


# ====================================================================
# --help text reflects the operator-facing contract
# ====================================================================


class TestInspectMigrationHelp:
    """``--help`` lists --inspect-migration with its operator-relevant
    documentation. CI Linux runners trigger Rich TTY rendering which
    can split flag substrings with ANSI bold codes (rc.16 lesson) —
    strip ANSI before substring assertions."""

    def test_inspect_migration_appears_in_help(self) -> None:
        result = runner.invoke(app, ["doctor", "voice", "--help"])
        assert result.exit_code == 0
        clean = _strip_ansi(result.output)
        assert "--inspect-migration" in clean
        # Operator-facing explanation must mention the key contract:
        # read-only + schema-bump preview.
        assert "schema-migration chain" in clean or "schema migration" in clean
