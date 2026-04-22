"""Unit tests for :mod:`sovyx.voice.health._linux_mixer_apply`.

Coverage targets:

* Happy path — applies every saturating control + returns a snapshot
  whose ``reverted_controls`` list mirrors pre-apply raw values.
* Atomicity — a mid-sequence subprocess failure rolls back every
  already-applied control and re-raises :class:`BypassApplyError` with
  a stable reason token (``amixer_set_failed``).
* Cancellation — ``asyncio.CancelledError`` triggers the same LIFO
  rollback before propagating.
* Platform / environment gates — non-Linux, missing ``amixer``, and
  empty control list each raise with the documented reason token.
* ``restore_mixer_snapshot`` never raises, walks snapshots in reverse,
  and survives a per-control subprocess failure.
* ``_compute_target_raw`` applies the capture vs. boost fractions and
  clamps out-of-range fractions into ``[min_raw, max_raw]``.
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice.health import _linux_mixer_apply as mod
from sovyx.voice.health._linux_mixer_apply import (
    REASON_AMIXER_SET_FAILED,
    REASON_AMIXER_TIMEOUT,
    REASON_AMIXER_UNAVAILABLE,
    REASON_NO_CONTROLS,
    REASON_NOT_LINUX,
    _compute_target_raw,
    apply_mixer_reset,
    restore_mixer_snapshot,
)
from sovyx.voice.health.bypass._strategy import BypassApplyError
from sovyx.voice.health.contract import MixerApplySnapshot, MixerControlSnapshot

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture()
def _linux_env() -> Iterator[None]:
    """Pin ``sys.platform == 'linux'`` + ``shutil.which`` => amixer-present."""
    with (
        patch.object(mod, "sys") as sys_mock,
        patch.object(mod, "shutil") as shutil_mock,
    ):
        sys_mock.platform = "linux"
        shutil_mock.which.return_value = "/usr/bin/amixer"
        yield


def _ctl(
    name: str,
    *,
    current_raw: int = 31,
    max_raw: int = 31,
    min_raw: int = 0,
    saturation_risk: bool = True,
) -> MixerControlSnapshot:
    return MixerControlSnapshot(
        name=name,
        min_raw=min_raw,
        max_raw=max_raw,
        current_raw=current_raw,
        current_db=36.0,
        max_db=36.0,
        is_boost_control=True,
        saturation_risk=saturation_risk,
    )


class TestComputeTargetRaw:
    def test_capture_control_uses_capture_fraction(self) -> None:
        ctl = _ctl("Capture", current_raw=80, max_raw=100, min_raw=0)
        target = _compute_target_raw(ctl, boost_fraction=0.0, capture_fraction=0.5)
        assert target == 50

    def test_boost_control_uses_boost_fraction(self) -> None:
        ctl = _ctl("Internal Mic Boost", current_raw=3, max_raw=3, min_raw=0)
        target = _compute_target_raw(ctl, boost_fraction=0.0, capture_fraction=0.5)
        assert target == 0

    def test_out_of_range_fraction_is_clamped_low(self) -> None:
        ctl = _ctl("Capture", current_raw=10, max_raw=10, min_raw=0)
        target = _compute_target_raw(ctl, boost_fraction=0.0, capture_fraction=-0.2)
        assert target == 0

    def test_out_of_range_fraction_is_clamped_high(self) -> None:
        ctl = _ctl("Capture", current_raw=10, max_raw=10, min_raw=0)
        target = _compute_target_raw(ctl, boost_fraction=0.0, capture_fraction=1.2)
        assert target == 10

    def test_negative_floor_is_respected(self) -> None:
        ctl = _ctl("Some Gain", current_raw=0, max_raw=20, min_raw=-20)
        target = _compute_target_raw(ctl, boost_fraction=0.25, capture_fraction=0.5)
        # span = 40, +25% = +10 → min + 10 = -10
        assert target == -10


class TestApplyMixerResetGates:
    @pytest.mark.asyncio()
    async def test_not_linux_raises(self) -> None:
        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "win32"
            with pytest.raises(BypassApplyError) as exc:
                await apply_mixer_reset(1, [_ctl("Capture")], tuning=VoiceTuningConfig())
            assert exc.value.reason == REASON_NOT_LINUX

    @pytest.mark.asyncio()
    async def test_amixer_unavailable_raises(self) -> None:
        with (
            patch.object(mod, "sys") as sys_mock,
            patch.object(mod, "shutil") as shutil_mock,
        ):
            sys_mock.platform = "linux"
            shutil_mock.which.return_value = None
            with pytest.raises(BypassApplyError) as exc:
                await apply_mixer_reset(1, [_ctl("Capture")], tuning=VoiceTuningConfig())
            assert exc.value.reason == REASON_AMIXER_UNAVAILABLE

    @pytest.mark.asyncio()
    async def test_empty_controls_raises(self, _linux_env: None) -> None:
        with pytest.raises(BypassApplyError) as exc:
            await apply_mixer_reset(1, [], tuning=VoiceTuningConfig())
        assert exc.value.reason == REASON_NO_CONTROLS


class TestApplyMixerResetHappyPath:
    @pytest.mark.asyncio()
    async def test_applies_and_records_rollback(self, _linux_env: None) -> None:
        calls: list[tuple[int, str, int]] = []

        def fake_set(card: int, name: str, target: int, _timeout: float) -> None:
            calls.append((card, name, target))

        controls = [
            _ctl("Capture", current_raw=80, max_raw=100),
            _ctl("Internal Mic Boost", current_raw=3, max_raw=3),
        ]
        with patch.object(mod, "_amixer_set", side_effect=fake_set):
            snap = await apply_mixer_reset(1, controls, tuning=VoiceTuningConfig())

        assert calls == [(1, "Capture", 50), (1, "Internal Mic Boost", 0)]
        assert snap.card_index == 1
        assert snap.reverted_controls == (
            ("Capture", 80),
            ("Internal Mic Boost", 3),
        )
        assert snap.applied_controls == (
            ("Capture", 50),
            ("Internal Mic Boost", 0),
        )

    @pytest.mark.asyncio()
    async def test_skips_controls_already_at_target(self, _linux_env: None) -> None:
        # Boost at min — target is already 0, no mutation, no rollback entry.
        already_safe = _ctl("Internal Mic Boost", current_raw=0, max_raw=3, min_raw=0)
        with patch.object(mod, "_amixer_set") as amixer:
            snap = await apply_mixer_reset(1, [already_safe], tuning=VoiceTuningConfig())
        amixer.assert_not_called()
        assert snap.reverted_controls == ()
        assert snap.applied_controls == ()


class TestApplyMixerResetRollback:
    @pytest.mark.asyncio()
    async def test_failure_mid_sequence_rolls_back(self, _linux_env: None) -> None:
        calls: list[tuple[str, int]] = []

        def fake_set(_card: int, name: str, target: int, _timeout: float) -> None:
            calls.append((name, target))
            if name == "Internal Mic Boost" and target == 0:
                raise BypassApplyError(
                    "amixer sset returned exit=1 on card 1 control 'Internal Mic Boost'",
                    reason=REASON_AMIXER_SET_FAILED,
                )

        controls = [
            _ctl("Capture", current_raw=80, max_raw=100),
            _ctl("Internal Mic Boost", current_raw=3, max_raw=3),
        ]
        with (
            patch.object(mod, "_amixer_set", side_effect=fake_set),
            pytest.raises(BypassApplyError) as exc,
        ):
            await apply_mixer_reset(1, controls, tuning=VoiceTuningConfig())

        assert exc.value.reason == REASON_AMIXER_SET_FAILED
        # Forward: Capture->50, then failed Boost->0, then rollback Capture->80
        assert calls == [
            ("Capture", 50),
            ("Internal Mic Boost", 0),
            ("Capture", 80),
        ]

    @pytest.mark.asyncio()
    async def test_cancellation_rolls_back(self, _linux_env: None) -> None:
        calls: list[tuple[str, int]] = []

        def fake_set(_card: int, name: str, target: int, _timeout: float) -> None:
            calls.append((name, target))
            if name == "Internal Mic Boost" and target == 0:
                raise asyncio.CancelledError

        controls = [
            _ctl("Capture", current_raw=80, max_raw=100),
            _ctl("Internal Mic Boost", current_raw=3, max_raw=3),
        ]
        with (
            patch.object(mod, "_amixer_set", side_effect=fake_set),
            pytest.raises(asyncio.CancelledError),
        ):
            await apply_mixer_reset(1, controls, tuning=VoiceTuningConfig())

        assert calls == [
            ("Capture", 50),
            ("Internal Mic Boost", 0),
            ("Capture", 80),
        ]

    @pytest.mark.asyncio()
    async def test_rollback_itself_swallows_errors(self, _linux_env: None) -> None:
        # First apply succeeds. Second apply fails (triggering rollback).
        # Rollback attempt on first control also fails — must NOT propagate.
        sequence: list[tuple[str, int]] = []

        def fake_set(_card: int, name: str, target: int, _timeout: float) -> None:
            sequence.append((name, target))
            if name == "Internal Mic Boost" and target == 0:
                raise BypassApplyError("primary failure", reason=REASON_AMIXER_SET_FAILED)
            if name == "Capture" and target == 80:
                # Rollback for Capture from 50 -> 80 also fails.
                raise BypassApplyError("rollback failure", reason=REASON_AMIXER_SET_FAILED)

        controls = [
            _ctl("Capture", current_raw=80, max_raw=100),
            _ctl("Internal Mic Boost", current_raw=3, max_raw=3),
        ]
        with (
            patch.object(mod, "_amixer_set", side_effect=fake_set),
            pytest.raises(BypassApplyError) as exc,
        ):
            await apply_mixer_reset(1, controls, tuning=VoiceTuningConfig())

        # Original error wins; rollback failure is logged only.
        assert "primary failure" in str(exc.value)


class TestAmixerSet:
    def test_timeout_maps_to_reason(self) -> None:
        with (
            patch.object(mod.subprocess, "run") as run_mock,
            pytest.raises(BypassApplyError) as exc,
        ):
            run_mock.side_effect = subprocess.TimeoutExpired(cmd="amixer", timeout=2.0)
            mod._amixer_set(1, "Capture", 50, timeout_s=2.0)
        assert exc.value.reason == REASON_AMIXER_TIMEOUT

    def test_oserror_maps_to_set_failed(self) -> None:
        with (
            patch.object(mod.subprocess, "run") as run_mock,
            pytest.raises(BypassApplyError) as exc,
        ):
            run_mock.side_effect = OSError("bad fd")
            mod._amixer_set(1, "Capture", 50, timeout_s=2.0)
        assert exc.value.reason == REASON_AMIXER_SET_FAILED

    def test_nonzero_exit_maps_to_set_failed(self) -> None:
        class _Completed:
            returncode = 1
            stderr = "unknown control"

        with (
            patch.object(mod.subprocess, "run", return_value=_Completed()),
            pytest.raises(BypassApplyError) as exc,
        ):
            mod._amixer_set(1, "Capture", 50, timeout_s=2.0)
        assert exc.value.reason == REASON_AMIXER_SET_FAILED


class TestRestoreMixerSnapshot:
    @pytest.mark.asyncio()
    async def test_walks_in_reverse(self, _linux_env: None) -> None:
        calls: list[tuple[str, int]] = []

        def fake_set(_card: int, name: str, target: int, _timeout: float) -> None:
            calls.append((name, target))

        snap = MixerApplySnapshot(
            card_index=1,
            reverted_controls=(("Capture", 80), ("Internal Mic Boost", 3)),
            applied_controls=(("Capture", 50), ("Internal Mic Boost", 0)),
        )
        with patch.object(mod, "_amixer_set", side_effect=fake_set):
            await restore_mixer_snapshot(snap, tuning=VoiceTuningConfig())
        assert calls == [("Internal Mic Boost", 3), ("Capture", 80)]

    @pytest.mark.asyncio()
    async def test_swallows_bypass_apply_error(self, _linux_env: None) -> None:
        def fake_set(_card: int, name: str, _target: int, _timeout: float) -> None:
            if name == "Internal Mic Boost":
                raise BypassApplyError("nope", reason=REASON_AMIXER_SET_FAILED)

        snap = MixerApplySnapshot(
            card_index=1,
            reverted_controls=(("Capture", 80), ("Internal Mic Boost", 3)),
            applied_controls=(("Capture", 50), ("Internal Mic Boost", 0)),
        )
        with patch.object(mod, "_amixer_set", side_effect=fake_set):
            # Must not raise.
            await restore_mixer_snapshot(snap, tuning=VoiceTuningConfig())

    @pytest.mark.asyncio()
    async def test_not_linux_is_noop(self) -> None:
        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "win32"
            snap = MixerApplySnapshot(
                card_index=1,
                reverted_controls=(("Capture", 80),),
                applied_controls=(("Capture", 50),),
            )
            await restore_mixer_snapshot(snap, tuning=VoiceTuningConfig())

    @pytest.mark.asyncio()
    async def test_amixer_missing_is_noop(self) -> None:
        with (
            patch.object(mod, "sys") as sys_mock,
            patch.object(mod, "shutil") as shutil_mock,
        ):
            sys_mock.platform = "linux"
            shutil_mock.which.return_value = None
            snap = MixerApplySnapshot(
                card_index=1,
                reverted_controls=(("Capture", 80),),
                applied_controls=(("Capture", 50),),
            )
            await restore_mixer_snapshot(snap, tuning=VoiceTuningConfig())
