"""Tests for :mod:`sovyx.voice.health._half_heal_recovery`.

Paranoid-QA R2 HIGH #3 regression coverage. The write-ahead log
protects against mid-apply process deaths: if a cascade died mid-
amixer_set, the next boot must detect the pending WAL and restore
the pre-apply mixer state before probing.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice.health._half_heal_recovery import (
    HalfHealWal,
    clear_wal,
    default_wal_path,
    load_wal,
    recover_if_present,
    write_wal,
)
from sovyx.voice.health.contract import MixerApplySnapshot

# ── default_wal_path ────────────────────────────────────────────────


class TestDefaultWalPath:
    def test_path_under_voice_health_subdirectory(self, tmp_path: Path) -> None:
        """The WAL lives under ``data_dir/voice_health/`` so a future
        multi-concern voice-health WAL set can share the directory
        without polluting the data_dir top level."""
        path = default_wal_path(tmp_path)
        assert path.parent.name == "voice_health"
        assert path.parent.parent == tmp_path
        assert path.suffix == ".json"


# ── write_wal ───────────────────────────────────────────────────────


class TestWriteWal:
    def test_writes_schema_versioned_json(self, tmp_path: Path) -> None:
        path = tmp_path / "wal.json"
        ok = write_wal(
            card_index=0,
            reverted_controls=(("Capture", 40), ("Internal Mic Boost", 0)),
            path=path,
        )
        assert ok is True
        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == 1
        assert payload["card_index"] == 0
        assert payload["reverted_controls"] == [
            ["Capture", 40],
            ["Internal Mic Boost", 0],
        ]

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "deeper" / "wal.json"
        ok = write_wal(
            card_index=1,
            reverted_controls=(("Capture", 60),),
            path=path,
        )
        assert ok is True
        assert path.exists()

    def test_atomic_replace_of_existing(self, tmp_path: Path) -> None:
        path = tmp_path / "wal.json"
        path.write_text("old garbage", encoding="utf-8")
        ok = write_wal(
            card_index=2,
            reverted_controls=(("Capture", 10),),
            path=path,
        )
        assert ok is True
        # Valid JSON, not the stale content.
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["card_index"] == 2

    def test_empty_controls_still_writes(self, tmp_path: Path) -> None:
        """Edge case: a preset that requires no mutations still
        writes an empty WAL. The orchestrator skips the WAL write
        when ``pre_apply_controls`` is empty, but the helper itself
        must handle this without raising."""
        path = tmp_path / "wal.json"
        ok = write_wal(card_index=0, reverted_controls=(), path=path)
        assert ok is True
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["reverted_controls"] == []


# ── load_wal ─────────────────────────────────────────────────────────


class TestLoadWal:
    def test_absent_file_returns_none(self, tmp_path: Path) -> None:
        assert load_wal(tmp_path / "nonexistent.json") is None

    def test_roundtrip_write_load(self, tmp_path: Path) -> None:
        path = tmp_path / "wal.json"
        write_wal(
            card_index=3,
            reverted_controls=(("Capture", 40), ("Mic Boost", 2)),
            path=path,
        )
        wal = load_wal(path)
        assert wal is not None
        assert wal.card_index == 3
        assert wal.reverted_controls == (("Capture", 40), ("Mic Boost", 2))

    def test_malformed_json_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "wal.json"
        path.write_text("this isn't json", encoding="utf-8")
        assert load_wal(path) is None

    def test_schema_version_mismatch_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "wal.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 99,
                    "card_index": 0,
                    "reverted_controls": [],
                }
            ),
            encoding="utf-8",
        )
        assert load_wal(path) is None

    def test_missing_fields_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "wal.json"
        path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        assert load_wal(path) is None

    def test_non_object_top_level_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "wal.json"
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        assert load_wal(path) is None

    def test_invalid_control_entry_skipped(self, tmp_path: Path) -> None:
        """A control entry that isn't a 2-tuple is silently skipped;
        well-formed entries survive."""
        path = tmp_path / "wal.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "card_index": 0,
                    "reverted_controls": [
                        ["Capture", 40],
                        "not a list",  # skipped
                        [1, 2, 3],  # wrong arity, skipped
                        ["Mic Boost", 0],
                    ],
                }
            ),
            encoding="utf-8",
        )
        wal = load_wal(path)
        assert wal is not None
        assert wal.reverted_controls == (("Capture", 40), ("Mic Boost", 0))


# ── clear_wal ────────────────────────────────────────────────────────


class TestClearWal:
    def test_deletes_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "wal.json"
        path.write_text("{}", encoding="utf-8")
        clear_wal(path)
        assert not path.exists()

    def test_idempotent_on_missing_file(self, tmp_path: Path) -> None:
        """Second call after file is already gone must NOT raise."""
        path = tmp_path / "nonexistent.json"
        clear_wal(path)  # file doesn't exist
        clear_wal(path)  # second call, still doesn't raise


# ── HalfHealWal dataclass ────────────────────────────────────────────


class TestHalfHealWal:
    def test_promote_to_apply_snapshot(self) -> None:
        wal = HalfHealWal(card_index=0, reverted_controls=(("Capture", 40),))
        snap = wal.to_apply_snapshot()
        assert isinstance(snap, MixerApplySnapshot)
        assert snap.card_index == 0
        assert snap.reverted_controls == (("Capture", 40),)
        assert snap.applied_controls == ()


# ── recover_if_present ───────────────────────────────────────────────


class TestRecoverIfPresent:
    @pytest.mark.asyncio()
    async def test_no_wal_returns_false_no_side_effects(self, tmp_path: Path) -> None:
        path = tmp_path / "wal.json"
        restore_fn = AsyncMock()
        tuning = VoiceTuningConfig()
        assert await recover_if_present(path=path, restore_fn=restore_fn, tuning=tuning) is False
        restore_fn.assert_not_awaited()

    @pytest.mark.asyncio()
    async def test_wal_present_triggers_restore_and_deletes_wal(self, tmp_path: Path) -> None:
        path = tmp_path / "wal.json"
        write_wal(
            card_index=0,
            reverted_controls=(("Capture", 40),),
            path=path,
        )
        restore_fn = AsyncMock()
        tuning = VoiceTuningConfig()
        assert await recover_if_present(path=path, restore_fn=restore_fn, tuning=tuning) is True
        restore_fn.assert_awaited_once()
        # WAL deleted after replay.
        assert not path.exists()

    @pytest.mark.asyncio()
    async def test_restore_passes_promoted_snapshot(self, tmp_path: Path) -> None:
        path = tmp_path / "wal.json"
        write_wal(
            card_index=7,
            reverted_controls=(("Cap", 50), ("Boost", 1)),
            path=path,
        )
        restore_fn = AsyncMock()
        tuning = VoiceTuningConfig()
        await recover_if_present(path=path, restore_fn=restore_fn, tuning=tuning)
        args, _ = restore_fn.await_args
        snapshot = args[0]
        assert isinstance(snapshot, MixerApplySnapshot)
        assert snapshot.card_index == 7
        assert snapshot.reverted_controls == (("Cap", 50), ("Boost", 1))

    @pytest.mark.asyncio()
    async def test_restore_failure_still_deletes_wal(self, tmp_path: Path) -> None:
        """If the replay itself raises, the WAL is still cleared so
        we don't loop forever on a broken restore. Future cascades
        will probe the mixer state and make fresh decisions."""
        path = tmp_path / "wal.json"
        write_wal(
            card_index=0,
            reverted_controls=(("Capture", 40),),
            path=path,
        )
        restore_fn = AsyncMock(side_effect=RuntimeError("synthetic failure"))
        tuning = VoiceTuningConfig()
        # recover_if_present swallows the exception.
        returned = await recover_if_present(path=path, restore_fn=restore_fn, tuning=tuning)
        assert returned is True  # WAL was present + replay attempted
        assert not path.exists()  # WAL deleted even though restore failed

    @pytest.mark.asyncio()
    async def test_malformed_wal_returns_false_does_not_call_restore(self, tmp_path: Path) -> None:
        path = tmp_path / "wal.json"
        path.write_text("not json at all", encoding="utf-8")
        restore_fn = AsyncMock()
        tuning = VoiceTuningConfig()
        # load_wal returns None → recover_if_present returns False.
        assert await recover_if_present(path=path, restore_fn=restore_fn, tuning=tuning) is False
        restore_fn.assert_not_awaited()

    @pytest.mark.asyncio()
    async def test_timeout_cancels_long_restore_and_clears_wal(
        self, tmp_path: Path
    ) -> None:
        """Paranoid-QA R3 HIGH #1 regression.

        A crafted WAL can starve the boot cascade by making
        ``restore_fn`` take arbitrarily long (each amixer subprocess
        uses ``linux_mixer_subprocess_timeout_s`` × N entries). The
        wrapper must cancel the replay when the wall-clock cap
        trips, then clear the WAL so the next boot doesn't loop.
        """
        import asyncio as aio

        path = tmp_path / "wal.json"
        write_wal(
            card_index=0,
            reverted_controls=(("Capture", 40),),
            path=path,
        )

        async def slow_restore(
            _snap: MixerApplySnapshot,  # noqa: ARG001
            *,
            tuning: VoiceTuningConfig,  # noqa: ARG001
        ) -> None:
            await aio.sleep(10.0)  # will be cancelled by timeout

        tuning = VoiceTuningConfig()
        returned = await recover_if_present(
            path=path,
            restore_fn=slow_restore,
            tuning=tuning,
            timeout_s=0.05,
        )
        assert returned is True
        # Timeout cancelled the restore; WAL cleared so next boot
        # doesn't retry forever.
        assert not path.exists()


# ── Paranoid-QA R3 HIGH #2/#3 — size + content validation ──────────


class TestLoadWalSizeCap:
    """Size-cap + entry-count + control-name validation guard against
    an attacker with ``data_dir`` write access staging a malicious
    WAL (DoS, amixer selector smuggling, log poisoning).
    """

    def test_oversized_wal_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "wal.json"
        huge_payload = json.dumps(
            {
                "schema_version": 1,
                "card_index": 0,
                "reverted_controls": [["Capture", 40]],
                "filler": "A" * (128 * 1024),
            }
        )
        path.write_text(huge_payload, encoding="utf-8")
        assert load_wal(path) is None
        assert path.exists()

    def test_too_many_entries_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "wal.json"
        entries = [["X", 0] for _ in range(200)]
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "card_index": 0,
                    "reverted_controls": entries,
                }
            ),
            encoding="utf-8",
        )
        assert load_wal(path) is None

    def test_control_name_too_long_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "wal.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "card_index": 0,
                    "reverted_controls": [["A" * 200, 0]],
                }
            ),
            encoding="utf-8",
        )
        assert load_wal(path) is None

    @pytest.mark.parametrize(
        "bad_name",
        [
            "Master',index=0,iface=CARD",
            "Capture\x00hidden",
            "Capture\nSOMETHING",
            'Capture"embedded',
            "Capture,other",
            "Capture=value",
        ],
    )
    def test_forbidden_chars_in_control_name_refused(
        self, tmp_path: Path, bad_name: str
    ) -> None:
        path = tmp_path / "wal.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "card_index": 0,
                    "reverted_controls": [[bad_name, 0]],
                }
            ),
            encoding="utf-8",
        )
        assert load_wal(path) is None, (
            f"WAL with control name {bad_name!r} should have been refused"
        )

    def test_empty_control_name_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "wal.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "card_index": 0,
                    "reverted_controls": [["", 0]],
                }
            ),
            encoding="utf-8",
        )
        assert load_wal(path) is None

    def test_legitimate_names_accepted(self, tmp_path: Path) -> None:
        """Sanity: the R3 hardening does not reject realistic
        control names (kernel-sourced amixer names)."""
        path = tmp_path / "wal.json"
        realistic = [
            ["Capture", 40],
            ["Internal Mic Boost", 0],
            ["Front Mic Boost", 2],
            ["Digital Capture Volume", 60],
            ["Mic", 50],
        ]
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "card_index": 0,
                    "reverted_controls": realistic,
                }
            ),
            encoding="utf-8",
        )
        wal = load_wal(path)
        assert wal is not None
        assert len(wal.reverted_controls) == 5
