"""Tests for ``sovyx voice`` CLI commands (Phase 7 / T7.39).

Covers ``sovyx voice forget`` and ``sovyx voice history`` — the
operator-side complement to the dashboard's POST /api/voice/forget
endpoint. Both surfaces hit the same ConsentLedger file so changes
made via either path are visible through both.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from sovyx.cli.commands.voice import voice_app
from sovyx.voice._consent_ledger import ConsentAction, ConsentLedger

runner = CliRunner()


@pytest.fixture()
def seeded_ledger(tmp_path: Path) -> Path:
    """Seed a ConsentLedger with records for two users + return data_dir."""
    ledger_path = tmp_path / "voice" / "consent.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger = ConsentLedger(path=ledger_path)
    for action in (
        ConsentAction.WAKE,
        ConsentAction.LISTEN,
        ConsentAction.TRANSCRIBE,
    ):
        ledger.append(user_id="u-target", action=action, context={})
    ledger.append(user_id="u-bystander", action=ConsentAction.WAKE, context={})
    return tmp_path


def _patch_data_dir(tmp_path: Path):  # noqa: ANN202 — context-manager helper
    """Patch _resolve_ledger_path so the CLI hits our tmp ledger.

    The CLI resolves data_dir via EngineConfig() which on a dev
    machine reads ``~/.sovyx``. Tests must isolate to tmp_path so
    they don't touch real operator state.
    """
    return patch(
        "sovyx.cli.commands.voice._resolve_ledger_path",
        return_value=tmp_path / "voice" / "consent.jsonl",
    )


class TestForget:
    def test_forget_with_yes_flag_purges_records(self, seeded_ledger: Path) -> None:
        with _patch_data_dir(seeded_ledger):
            result = runner.invoke(
                voice_app,
                ["forget", "--user-id", "u-target", "--yes"],
            )
        assert result.exit_code == 0
        assert "purged" in result.stdout
        assert "3" in result.stdout

        # Confirm side effect: ledger has only the DELETE tombstone for u-target.
        ledger = ConsentLedger(path=seeded_ledger / "voice" / "consent.jsonl")
        history = ledger.history(user_id="u-target")
        assert len(history) == 1
        assert history[0].action == ConsentAction.DELETE

    def test_forget_does_not_touch_other_users(self, seeded_ledger: Path) -> None:
        with _patch_data_dir(seeded_ledger):
            runner.invoke(
                voice_app,
                ["forget", "--user-id", "u-target", "--yes"],
            )
        ledger = ConsentLedger(path=seeded_ledger / "voice" / "consent.jsonl")
        bystander = ledger.history(user_id="u-bystander")
        assert len(bystander) == 1
        assert bystander[0].action == ConsentAction.WAKE

    def test_forget_empty_user_id_rejected(self, tmp_path: Path) -> None:
        with _patch_data_dir(tmp_path):
            result = runner.invoke(voice_app, ["forget", "--user-id", "", "--yes"])
        assert result.exit_code == 2  # noqa: PLR2004
        assert "non-empty" in result.stdout

    def test_forget_idempotent(self, seeded_ledger: Path) -> None:
        # First call purges 3 records.
        with _patch_data_dir(seeded_ledger):
            first = runner.invoke(
                voice_app,
                ["forget", "--user-id", "u-target", "--yes"],
            )
        assert first.exit_code == 0
        # Second call is safe — no new records to purge but tombstone
        # remains. The CLI doesn't crash.
        with _patch_data_dir(seeded_ledger):
            second = runner.invoke(
                voice_app,
                ["forget", "--user-id", "u-target", "--yes"],
            )
        assert second.exit_code == 0


class TestHistory:
    def test_history_lists_records_as_jsonl(self, seeded_ledger: Path) -> None:
        with _patch_data_dir(seeded_ledger):
            result = runner.invoke(
                voice_app,
                ["history", "--user-id", "u-target"],
            )
        assert result.exit_code == 0
        # Output is JSONL — each line a complete JSON object.
        lines = [ln for ln in result.stdout.splitlines() if ln.strip().startswith("{")]
        assert len(lines) == 3  # noqa: PLR2004 — 3 seeded actions
        actions = []
        for line in lines:
            obj = json.loads(line)
            actions.append(obj["action"])
        assert "wake" in actions
        assert "listen" in actions
        assert "transcribe" in actions

    def test_history_empty_user_id_rejected(self, tmp_path: Path) -> None:
        with _patch_data_dir(tmp_path):
            result = runner.invoke(voice_app, ["history", "--user-id", ""])
        assert result.exit_code == 2  # noqa: PLR2004

    def test_history_unknown_user_says_no_records(self, seeded_ledger: Path) -> None:
        with _patch_data_dir(seeded_ledger):
            result = runner.invoke(
                voice_app,
                ["history", "--user-id", "u-never-existed"],
            )
        assert result.exit_code == 0
        assert "no records" in result.stdout
