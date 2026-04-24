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
    REASON_PRESET_DB_NOT_SUPPORTED,
    REASON_PRESET_ROLE_MISSING,
    _compute_target_raw,
    _translate_preset_value,
    apply_mixer_preset,
    apply_mixer_reset,
    restore_mixer_snapshot,
)
from sovyx.voice.health.bypass._strategy import BypassApplyError
from sovyx.voice.health.contract import (
    MixerApplySnapshot,
    MixerControlRole,
    MixerControlSnapshot,
    MixerPresetControl,
    MixerPresetSpec,
    MixerPresetValueDb,
    MixerPresetValueFraction,
    MixerPresetValueRaw,
)

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


# ── apply_mixer_preset (F1.D) ─────────────────────────────────────────


def _snap(
    name: str,
    *,
    current_raw: int,
    max_raw: int = 80,
    min_raw: int = 0,
) -> MixerControlSnapshot:
    return MixerControlSnapshot(
        name=name,
        min_raw=min_raw,
        max_raw=max_raw,
        current_raw=current_raw,
        current_db=None,
        max_db=None,
        is_boost_control=False,
        saturation_risk=False,
    )


class TestTranslatePresetValue:
    def test_raw_clamps_low(self) -> None:
        ctl = _snap("Capture", current_raw=10, max_raw=80, min_raw=0)
        assert _translate_preset_value(MixerPresetValueRaw(raw=-5), ctl) == 0

    def test_raw_clamps_high(self) -> None:
        ctl = _snap("Capture", current_raw=10, max_raw=80, min_raw=0)
        assert _translate_preset_value(MixerPresetValueRaw(raw=999), ctl) == 80

    def test_raw_passthrough(self) -> None:
        ctl = _snap("Capture", current_raw=10, max_raw=80, min_raw=0)
        assert _translate_preset_value(MixerPresetValueRaw(raw=40), ctl) == 40

    def test_fraction_zero(self) -> None:
        ctl = _snap("Capture", current_raw=40, max_raw=80, min_raw=0)
        assert _translate_preset_value(MixerPresetValueFraction(fraction=0.0), ctl) == 0

    def test_fraction_one(self) -> None:
        ctl = _snap("Capture", current_raw=40, max_raw=80, min_raw=0)
        assert _translate_preset_value(MixerPresetValueFraction(fraction=1.0), ctl) == 80

    def test_fraction_half(self) -> None:
        ctl = _snap("Capture", current_raw=40, max_raw=80, min_raw=0)
        assert _translate_preset_value(MixerPresetValueFraction(fraction=0.5), ctl) == 40

    def test_fraction_with_negative_min_floor(self) -> None:
        ctl = _snap("Gain", current_raw=0, max_raw=20, min_raw=-20)
        # span=40; 0.25 → -20 + 10 = -10
        assert _translate_preset_value(MixerPresetValueFraction(fraction=0.25), ctl) == -10

    def test_db_raises(self) -> None:
        ctl = _snap("Capture", current_raw=40, max_raw=80, min_raw=0)
        with pytest.raises(BypassApplyError) as exc:
            _translate_preset_value(MixerPresetValueDb(db=-12.0), ctl)
        assert exc.value.reason == REASON_PRESET_DB_NOT_SUPPORTED


def _preset_capture_to_full() -> MixerPresetSpec:
    return MixerPresetSpec(
        controls=(
            MixerPresetControl(
                role=MixerControlRole.CAPTURE_MASTER,
                value=MixerPresetValueFraction(fraction=1.0),
            ),
            MixerPresetControl(
                role=MixerControlRole.INTERNAL_MIC_BOOST,
                value=MixerPresetValueRaw(raw=0),
            ),
        ),
    )


class TestApplyMixerPresetGates:
    @pytest.mark.asyncio()
    async def test_not_linux_raises(self) -> None:
        with patch.object(mod, "sys") as sys_mock:
            sys_mock.platform = "win32"
            with pytest.raises(BypassApplyError) as exc:
                await apply_mixer_preset(
                    1,
                    _preset_capture_to_full(),
                    {},
                    tuning=VoiceTuningConfig(),
                )
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
                await apply_mixer_preset(
                    1,
                    _preset_capture_to_full(),
                    {},
                    tuning=VoiceTuningConfig(),
                )
            assert exc.value.reason == REASON_AMIXER_UNAVAILABLE

    @pytest.mark.asyncio()
    async def test_role_missing_raises(self, _linux_env: None) -> None:
        role_mapping = {
            # Missing CAPTURE_MASTER + INTERNAL_MIC_BOOST — preset references both.
            MixerControlRole.UNKNOWN: (_snap("x", current_raw=10),),
        }
        with pytest.raises(BypassApplyError) as exc:
            await apply_mixer_preset(
                1,
                _preset_capture_to_full(),
                role_mapping,
                tuning=VoiceTuningConfig(),
            )
        assert exc.value.reason == REASON_PRESET_ROLE_MISSING

    @pytest.mark.asyncio()
    async def test_db_value_raises(self, _linux_env: None) -> None:
        preset = MixerPresetSpec(
            controls=(
                MixerPresetControl(
                    role=MixerControlRole.CAPTURE_MASTER,
                    value=MixerPresetValueDb(db=-20.0),
                ),
            ),
        )
        role_mapping = {
            MixerControlRole.CAPTURE_MASTER: (_snap("Capture", current_raw=40),),
        }
        with pytest.raises(BypassApplyError) as exc:
            await apply_mixer_preset(
                1,
                preset,
                role_mapping,
                tuning=VoiceTuningConfig(),
            )
        assert exc.value.reason == REASON_PRESET_DB_NOT_SUPPORTED


class TestApplyMixerPresetHappyPath:
    @pytest.mark.asyncio()
    async def test_applies_fraction_and_raw(self, _linux_env: None) -> None:
        calls: list[tuple[int, str, int]] = []

        def fake_set(card: int, name: str, target: int, _timeout: float) -> None:
            calls.append((card, name, target))

        role_mapping = {
            MixerControlRole.CAPTURE_MASTER: (_snap("Capture", current_raw=40, max_raw=80),),
            MixerControlRole.INTERNAL_MIC_BOOST: (
                _snap("Internal Mic Boost", current_raw=3, max_raw=3),
            ),
        }
        with patch.object(mod, "_amixer_set", side_effect=fake_set):
            snap = await apply_mixer_preset(
                1,
                _preset_capture_to_full(),
                role_mapping,
                tuning=VoiceTuningConfig(),
            )
        assert calls == [
            (1, "Capture", 80),  # fraction 1.0 → 80
            (1, "Internal Mic Boost", 0),  # raw 0
        ]
        assert snap.card_index == 1
        assert snap.reverted_controls == (("Capture", 40), ("Internal Mic Boost", 3))
        assert snap.applied_controls == (("Capture", 80), ("Internal Mic Boost", 0))

    @pytest.mark.asyncio()
    async def test_no_op_when_already_at_target(self, _linux_env: None) -> None:
        calls: list[tuple[int, str, int]] = []

        def fake_set(card: int, name: str, target: int, _timeout: float) -> None:
            calls.append((card, name, target))

        # CAPTURE_MASTER current=80, max=80, fraction=1.0 → target=80 → skip.
        role_mapping = {
            MixerControlRole.CAPTURE_MASTER: (_snap("Capture", current_raw=80, max_raw=80),),
            MixerControlRole.INTERNAL_MIC_BOOST: (
                _snap("Internal Mic Boost", current_raw=0, max_raw=3),
            ),
        }
        with patch.object(mod, "_amixer_set", side_effect=fake_set):
            snap = await apply_mixer_preset(
                1,
                _preset_capture_to_full(),
                role_mapping,
                tuning=VoiceTuningConfig(),
            )
        # Both controls already at target → no amixer calls, empty snapshot.
        assert calls == []
        assert snap.reverted_controls == ()
        assert snap.applied_controls == ()

    @pytest.mark.asyncio()
    async def test_multiple_controls_same_role_all_applied(self, _linux_env: None) -> None:
        """Desktop-HDA case: Front + Rear Mic Boost both → PREAMP_BOOST."""
        calls: list[tuple[str, int]] = []

        def fake_set(_card: int, name: str, target: int, _timeout: float) -> None:
            calls.append((name, target))

        role_mapping = {
            MixerControlRole.PREAMP_BOOST: (
                _snap("Front Mic Boost", current_raw=3, max_raw=3),
                _snap("Rear Mic Boost", current_raw=3, max_raw=3),
            ),
        }
        preset = MixerPresetSpec(
            controls=(
                MixerPresetControl(
                    role=MixerControlRole.PREAMP_BOOST,
                    value=MixerPresetValueRaw(raw=0),
                ),
            ),
        )
        with patch.object(mod, "_amixer_set", side_effect=fake_set):
            snap = await apply_mixer_preset(
                1,
                preset,
                role_mapping,
                tuning=VoiceTuningConfig(),
            )
        assert calls == [("Front Mic Boost", 0), ("Rear Mic Boost", 0)]
        assert snap.reverted_controls == (
            ("Front Mic Boost", 3),
            ("Rear Mic Boost", 3),
        )


class TestApplyMixerPresetRollback:
    @pytest.mark.asyncio()
    async def test_rollback_on_mid_sequence_failure(self, _linux_env: None) -> None:
        """If the second write fails, the first is rolled back before re-raise."""
        calls: list[tuple[str, int]] = []

        def flaky_set(_card: int, name: str, target: int, _timeout: float) -> None:
            calls.append((name, target))
            if name == "Internal Mic Boost":
                msg = "synthetic amixer failure"
                raise BypassApplyError(msg, reason=REASON_AMIXER_SET_FAILED)

        role_mapping = {
            MixerControlRole.CAPTURE_MASTER: (_snap("Capture", current_raw=40, max_raw=80),),
            MixerControlRole.INTERNAL_MIC_BOOST: (
                _snap("Internal Mic Boost", current_raw=3, max_raw=3),
            ),
        }
        with (
            patch.object(mod, "_amixer_set", side_effect=flaky_set),
            pytest.raises(BypassApplyError) as exc,
        ):
            await apply_mixer_preset(
                1,
                _preset_capture_to_full(),
                role_mapping,
                tuning=VoiceTuningConfig(),
            )
        assert exc.value.reason == REASON_AMIXER_SET_FAILED
        # Sequence: forward-Capture-to-80, forward-InternalMicBoost-fails,
        # rollback-Capture-to-40.
        assert calls == [
            ("Capture", 80),
            ("Internal Mic Boost", 0),
            ("Capture", 40),
        ]

    @pytest.mark.asyncio()
    async def test_rollback_on_cancellation(self, _linux_env: None) -> None:
        """CancelledError during apply triggers LIFO rollback and re-propagates."""
        calls: list[tuple[str, int]] = []

        def cancelling_set(_card: int, name: str, target: int, _timeout: float) -> None:
            calls.append((name, target))
            if name == "Internal Mic Boost":
                raise asyncio.CancelledError

        role_mapping = {
            MixerControlRole.CAPTURE_MASTER: (_snap("Capture", current_raw=40, max_raw=80),),
            MixerControlRole.INTERNAL_MIC_BOOST: (
                _snap("Internal Mic Boost", current_raw=3, max_raw=3),
            ),
        }
        with (
            patch.object(mod, "_amixer_set", side_effect=cancelling_set),
            pytest.raises(asyncio.CancelledError),
        ):
            await apply_mixer_preset(
                1,
                _preset_capture_to_full(),
                role_mapping,
                tuning=VoiceTuningConfig(),
            )
        assert calls == [
            ("Capture", 80),
            ("Internal Mic Boost", 0),
            ("Capture", 40),  # rollback
        ]


class TestApplyMixerPresetAutoMute:
    @pytest.mark.asyncio()
    async def test_auto_mute_disabled_sets_enum_label(self, _linux_env: None) -> None:
        enum_calls: list[tuple[str, str]] = []

        def fake_set(_card: int, _name: str, _target: int, _timeout: float) -> None:
            return

        def fake_get_enum(
            _card: int,
            _name: str,
            _timeout: float,
        ) -> str | None:
            return "Enabled"  # pre-apply state

        def fake_set_enum(
            _card: int,
            name: str,
            label: str,
            _timeout: float,
        ) -> None:
            enum_calls.append((name, label))

        preset = MixerPresetSpec(
            controls=(
                MixerPresetControl(
                    role=MixerControlRole.CAPTURE_MASTER,
                    value=MixerPresetValueFraction(fraction=1.0),
                ),
            ),
            auto_mute_mode="disabled",
        )
        role_mapping = {
            MixerControlRole.CAPTURE_MASTER: (_snap("Capture", current_raw=40, max_raw=80),),
            MixerControlRole.AUTO_MUTE: (_snap("Auto-Mute Mode", current_raw=0),),
        }
        with (
            patch.object(mod, "_amixer_set", side_effect=fake_set),
            patch.object(mod, "_amixer_get_enum", side_effect=fake_get_enum),
            patch.object(mod, "_amixer_set_enum", side_effect=fake_set_enum),
        ):
            await apply_mixer_preset(
                1,
                preset,
                role_mapping,
                tuning=VoiceTuningConfig(),
            )
        assert enum_calls == [("Auto-Mute Mode", "Disabled")]

    @pytest.mark.asyncio()
    async def test_auto_mute_leave_is_no_op(self, _linux_env: None) -> None:
        enum_calls: list[tuple[str, str]] = []

        def fake_set_enum(
            _card: int,
            name: str,
            label: str,
            _timeout: float,
        ) -> None:
            enum_calls.append((name, label))

        preset = MixerPresetSpec(
            controls=(
                MixerPresetControl(
                    role=MixerControlRole.CAPTURE_MASTER,
                    value=MixerPresetValueFraction(fraction=1.0),
                ),
            ),
            # auto_mute_mode default="leave" — enum write must NOT fire.
        )
        role_mapping = {
            MixerControlRole.CAPTURE_MASTER: (_snap("Capture", current_raw=40, max_raw=80),),
        }
        with (
            patch.object(mod, "_amixer_set"),
            patch.object(mod, "_amixer_set_enum", side_effect=fake_set_enum),
        ):
            await apply_mixer_preset(
                1,
                preset,
                role_mapping,
                tuning=VoiceTuningConfig(),
            )
        assert enum_calls == []

    @pytest.mark.asyncio()
    async def test_auto_mute_already_at_target_skips(self, _linux_env: None) -> None:
        enum_writes: list[tuple[str, str]] = []

        def fake_get_enum(
            _card: int,
            _name: str,
            _timeout: float,
        ) -> str | None:
            return "Disabled"  # already matches target

        def fake_set_enum(
            _card: int,
            name: str,
            label: str,
            _timeout: float,
        ) -> None:
            enum_writes.append((name, label))

        preset = MixerPresetSpec(
            controls=(
                MixerPresetControl(
                    role=MixerControlRole.CAPTURE_MASTER,
                    value=MixerPresetValueRaw(raw=80),
                ),
            ),
            auto_mute_mode="disabled",
        )
        role_mapping = {
            MixerControlRole.CAPTURE_MASTER: (_snap("Capture", current_raw=40, max_raw=80),),
            MixerControlRole.AUTO_MUTE: (_snap("Auto-Mute Mode", current_raw=0),),
        }
        with (
            patch.object(mod, "_amixer_set"),
            patch.object(mod, "_amixer_get_enum", side_effect=fake_get_enum),
            patch.object(mod, "_amixer_set_enum", side_effect=fake_set_enum),
        ):
            await apply_mixer_preset(
                1,
                preset,
                role_mapping,
                tuning=VoiceTuningConfig(),
            )
        assert enum_writes == []

    @pytest.mark.asyncio()
    async def test_auto_mute_absent_control_skipped(self, _linux_env: None) -> None:
        """Card without Auto-Mute control — _amixer_get_enum returns None."""
        enum_writes: list[tuple[str, str]] = []

        def fake_get_enum(
            _card: int,
            _name: str,
            _timeout: float,
        ) -> str | None:
            return None

        def fake_set_enum(
            _card: int,
            name: str,
            label: str,
            _timeout: float,
        ) -> None:
            enum_writes.append((name, label))

        preset = MixerPresetSpec(
            controls=(
                MixerPresetControl(
                    role=MixerControlRole.CAPTURE_MASTER,
                    value=MixerPresetValueRaw(raw=80),
                ),
            ),
            auto_mute_mode="disabled",
        )
        role_mapping = {
            MixerControlRole.CAPTURE_MASTER: (_snap("Capture", current_raw=40, max_raw=80),),
            # No AUTO_MUTE entry — falls back to canonical name, get returns None.
        }
        with (
            patch.object(mod, "_amixer_set"),
            patch.object(mod, "_amixer_get_enum", side_effect=fake_get_enum),
            patch.object(mod, "_amixer_set_enum", side_effect=fake_set_enum),
        ):
            await apply_mixer_preset(
                1,
                preset,
                role_mapping,
                tuning=VoiceTuningConfig(),
            )
        assert enum_writes == []


class TestApplyMixerPresetRuntimePm:
    @pytest.mark.asyncio()
    async def test_runtime_pm_on_logs_but_does_not_write(
        self, _linux_env: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        preset = MixerPresetSpec(
            controls=(
                MixerPresetControl(
                    role=MixerControlRole.CAPTURE_MASTER,
                    value=MixerPresetValueFraction(fraction=1.0),
                ),
            ),
            runtime_pm_target="on",
        )
        role_mapping = {
            MixerControlRole.CAPTURE_MASTER: (_snap("Capture", current_raw=40, max_raw=80),),
        }
        with patch.object(mod, "_amixer_set"):
            await apply_mixer_preset(
                1,
                preset,
                role_mapping,
                tuning=VoiceTuningConfig(),
            )
        # Log emitted — F1.G systemd handles the actual /sys write.
        assert any("linux_mixer_runtime_pm_deferred" in rec.message for rec in caplog.records)
