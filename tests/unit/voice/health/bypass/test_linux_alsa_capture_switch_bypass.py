"""Tests for ``LinuxALSACaptureSwitchBypass`` (Mission §Phase 2 T2.2).

Mission anchor:
``docs-internal/missions/MISSION-voice-linux-silent-mic-remediation-2026-05-04.md``
§Phase 2 T2.2.

Mocking strategy: per CLAUDE.md anti-pattern #11, all patches use
``patch.object(mod, attr, ...)`` against the strategy module's local
imports. ``subprocess.run`` is replaced with a ``_FakeAmixer`` that
returns canned ``scontents`` output for the eligibility/verify probes
and accepts ``sset`` calls without side effects (we assert on call
shape, not on host state).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.voice.health.bypass import _linux_alsa_capture_switch as mod
from sovyx.voice.health.bypass._linux_alsa_capture_switch import (
    LinuxALSACaptureSwitchBypass,
    _is_boost_control,
    _is_capture_pattern,
    _is_faulted,
    _parse_amixer_output,
)
from sovyx.voice.health.bypass._strategy import BypassApplyError
from sovyx.voice.health.contract import BypassContext

# ── Sample amixer scontents fixtures ────────────────────────────────


_SCONTENTS_CAPTURE_OFF = """Simple mixer control 'Capture',0
  Capabilities: cvolume cswitch
  Capture channels: Front Left - Front Right
  Limits: Capture 0 - 80
  Front Left: Capture 0 [0%] [-34.00dB] [off]
  Front Right: Capture 0 [0%] [-34.00dB] [off]
"""


_SCONTENTS_CAPTURE_HEALTHY = """Simple mixer control 'Capture',0
  Capabilities: cvolume cswitch
  Capture channels: Front Left - Front Right
  Limits: Capture 0 - 80
  Front Left: Capture 64 [80%] [-10.00dB] [on]
  Front Right: Capture 64 [80%] [-10.00dB] [on]
"""


_SCONTENTS_INTERNAL_MIC_BOOST_ZERO = """Simple mixer control 'Internal Mic Boost',0
  Capabilities: volume
  Playback channels: Mono
  Limits: 0 - 3
  Mono: 0 [0%] [0.00dB]
"""


_SCONTENTS_BOTH_FAULTED = _SCONTENTS_CAPTURE_OFF + _SCONTENTS_INTERNAL_MIC_BOOST_ZERO


_SCONTENTS_HEADPHONE_PLAYBACK_ONLY = """Simple mixer control 'Headphone',0
  Capabilities: pvolume pswitch
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 87
  Front Left: Playback 87 [100%] [0.00dB] [on]
  Front Right: Playback 87 [100%] [0.00dB] [on]
"""


# ── Subprocess mock harness ─────────────────────────────────────────


class _FakeAmixer:
    """Stand-in for ``subprocess.run`` calling ``amixer``.

    ``scontents`` invocations return canned output (per card_index from
    the constructor). ``sset`` invocations return rc=0 by default;
    track every call shape for later assertions.
    """

    def __init__(
        self,
        *,
        scontents_per_card: dict[int, str] | None = None,
        scontents_rc: int = 0,
        sset_rc: int = 0,
    ) -> None:
        self.scontents_per_card = scontents_per_card or {}
        self.scontents_rc = scontents_rc
        self.sset_rc = sset_rc
        self.calls: list[tuple[str, ...]] = []
        # Allow tests to override scontents output mid-run (verify
        # phase shows different state than apply phase).
        self._scontents_call_count = 0

    def __call__(self, argv, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(tuple(argv))
        if len(argv) >= 4 and argv[1] == "-c" and argv[3] == "scontents":
            card_index = int(argv[2])
            stdout = self.scontents_per_card.get(card_index, "")
            self._scontents_call_count += 1
            return _completed(self.scontents_rc, stdout)
        if len(argv) >= 4 and argv[1] == "-c" and argv[3] == "sset":
            return _completed(self.sset_rc, "")
        return _completed(1, "", stderr=f"unhandled argv: {argv!r}")


def _completed(rc: int, stdout: str, *, stderr: str = "") -> object:
    cp = MagicMock()
    cp.returncode = rc
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


class _Tuning:
    def __init__(self, *, enabled: bool = True, lenient: bool = False) -> None:
        self.linux_alsa_capture_switch_bypass_enabled = enabled
        self.linux_alsa_capture_switch_bypass_lenient = lenient


def _ctx(*, platform: str = "linux") -> BypassContext:
    return BypassContext(
        endpoint_guid="{guid}",
        endpoint_friendly_name="HD-Audio Generic: SN6180 Analog (hw:1,0)",
        host_api_name="ALSA",
        platform_key=platform,
        capture_task=MagicMock(),
        probe_fn=AsyncMock(),
        current_device_index=4,
        current_device_kind="hardware",
    )


# ── Parser unit tests ───────────────────────────────────────────────


class TestParser:
    def test_capture_off_parsed(self) -> None:
        controls = _parse_amixer_output(_SCONTENTS_CAPTURE_OFF)
        assert len(controls) == 1
        ctrl = controls[0]
        assert ctrl.name == "Capture"
        assert ctrl.switch_off is True
        assert ctrl.raw_value == 0
        assert ctrl.min_raw == 0
        assert ctrl.max_raw == 80

    def test_capture_healthy_parsed(self) -> None:
        controls = _parse_amixer_output(_SCONTENTS_CAPTURE_HEALTHY)
        assert len(controls) == 1
        ctrl = controls[0]
        assert ctrl.switch_off is False
        assert ctrl.raw_value == 64

    def test_boost_zero_parsed(self) -> None:
        controls = _parse_amixer_output(_SCONTENTS_INTERNAL_MIC_BOOST_ZERO)
        assert len(controls) == 1
        ctrl = controls[0]
        assert ctrl.name == "Internal Mic Boost"
        assert ctrl.raw_value == 0
        assert ctrl.min_raw == 0
        assert ctrl.max_raw == 3

    def test_both_faulted_yields_two_controls(self) -> None:
        controls = _parse_amixer_output(_SCONTENTS_BOTH_FAULTED)
        assert len(controls) == 2
        names = {c.name for c in controls}
        assert names == {"Capture", "Internal Mic Boost"}

    def test_empty_input_yields_empty(self) -> None:
        assert _parse_amixer_output("") == []


class TestClassifier:
    def test_capture_pattern_recognised(self) -> None:
        assert _is_capture_pattern("Capture")
        assert _is_capture_pattern("Capture Switch")
        assert _is_capture_pattern("Internal Mic Boost")
        assert _is_capture_pattern("Front Mic")
        assert _is_capture_pattern("Rear Mic")

    def test_capture_pattern_rejects_playback(self) -> None:
        assert not _is_capture_pattern("Headphone")
        assert not _is_capture_pattern("Speaker")
        assert not _is_capture_pattern("Master")

    def test_boost_classifier(self) -> None:
        assert _is_boost_control("Internal Mic Boost")
        assert _is_boost_control("Mic Boost")
        assert not _is_boost_control("Capture")
        assert not _is_boost_control("Internal Mic")

    def test_faulted_capture_off(self) -> None:
        controls = _parse_amixer_output(_SCONTENTS_CAPTURE_OFF)
        assert _is_faulted(controls[0]) is True

    def test_faulted_capture_healthy_not(self) -> None:
        controls = _parse_amixer_output(_SCONTENTS_CAPTURE_HEALTHY)
        assert _is_faulted(controls[0]) is False

    def test_faulted_boost_at_min(self) -> None:
        controls = _parse_amixer_output(_SCONTENTS_INTERNAL_MIC_BOOST_ZERO)
        assert _is_faulted(controls[0]) is True

    def test_faulted_playback_control_ignored(self) -> None:
        controls = _parse_amixer_output(_SCONTENTS_HEADPHONE_PLAYBACK_ONLY)
        # 'Headphone' does NOT match any capture pattern → not faulted.
        assert _is_faulted(controls[0]) is False


# ── Eligibility ─────────────────────────────────────────────────────


class TestEligibility:
    @pytest.mark.asyncio()
    async def test_not_linux_ineligible(self) -> None:
        strategy = LinuxALSACaptureSwitchBypass()
        elig = await strategy.probe_eligibility(_ctx(platform="win32"))
        assert elig.applicable is False
        assert elig.reason == "not_linux_platform"

    @pytest.mark.asyncio()
    async def test_disabled_by_tuning_ineligible(self) -> None:
        strategy = LinuxALSACaptureSwitchBypass()
        with patch.object(mod, "VoiceTuningConfig", return_value=_Tuning(enabled=False)):
            elig = await strategy.probe_eligibility(_ctx())
        assert elig.applicable is False
        assert elig.reason == "alsa_capture_switch_disabled_by_tuning"

    @pytest.mark.asyncio()
    async def test_amixer_missing_ineligible(self) -> None:
        strategy = LinuxALSACaptureSwitchBypass()
        with (
            patch.object(mod, "VoiceTuningConfig", return_value=_Tuning()),
            patch.object(mod.shutil, "which", return_value=None),
        ):
            elig = await strategy.probe_eligibility(_ctx())
        assert elig.applicable is False
        assert elig.reason == "amixer_unavailable_on_host"

    @pytest.mark.asyncio()
    async def test_no_input_cards_ineligible(self) -> None:
        strategy = LinuxALSACaptureSwitchBypass()
        with (
            patch.object(mod, "VoiceTuningConfig", return_value=_Tuning()),
            patch.object(mod.shutil, "which", return_value="/usr/bin/amixer"),
            patch.object(mod, "enumerate_input_card_ids", return_value=[]),
        ):
            elig = await strategy.probe_eligibility(_ctx())
        assert elig.applicable is False
        assert elig.reason == "no_alsa_card_with_input_channels"

    @pytest.mark.asyncio()
    async def test_all_controls_ok_ineligible(self) -> None:
        strategy = LinuxALSACaptureSwitchBypass()
        runner = _FakeAmixer(
            scontents_per_card={1: _SCONTENTS_CAPTURE_HEALTHY},
        )
        with (
            patch.object(mod, "VoiceTuningConfig", return_value=_Tuning()),
            patch.object(mod.shutil, "which", return_value="/usr/bin/amixer"),
            patch.object(mod, "enumerate_input_card_ids", return_value=[(1, "Generic")]),
            patch.object(mod.subprocess, "run", side_effect=runner),
        ):
            elig = await strategy.probe_eligibility(_ctx())
        assert elig.applicable is False
        assert elig.reason == "no_capture_switch_off_or_boost_zero"

    @pytest.mark.asyncio()
    async def test_applicable_when_capture_off(self) -> None:
        strategy = LinuxALSACaptureSwitchBypass()
        runner = _FakeAmixer(
            scontents_per_card={1: _SCONTENTS_CAPTURE_OFF},
        )
        with (
            patch.object(mod, "VoiceTuningConfig", return_value=_Tuning()),
            patch.object(mod.shutil, "which", return_value="/usr/bin/amixer"),
            patch.object(mod, "enumerate_input_card_ids", return_value=[(1, "Generic")]),
            patch.object(mod.subprocess, "run", side_effect=runner),
        ):
            elig = await strategy.probe_eligibility(_ctx())
        assert elig.applicable is True
        assert elig.estimated_cost_ms > 0

    @pytest.mark.asyncio()
    async def test_applicable_when_boost_at_min(self) -> None:
        strategy = LinuxALSACaptureSwitchBypass()
        runner = _FakeAmixer(
            scontents_per_card={1: _SCONTENTS_INTERNAL_MIC_BOOST_ZERO},
        )
        with (
            patch.object(mod, "VoiceTuningConfig", return_value=_Tuning()),
            patch.object(mod.shutil, "which", return_value="/usr/bin/amixer"),
            patch.object(mod, "enumerate_input_card_ids", return_value=[(1, "Generic")]),
            patch.object(mod.subprocess, "run", side_effect=runner),
        ):
            elig = await strategy.probe_eligibility(_ctx())
        assert elig.applicable is True

    @pytest.mark.asyncio()
    async def test_multi_card_one_faulted_one_healthy_eligible(self) -> None:
        # Forensic case: card 0 = HDMI (no input), card 1 = SN6180 with
        # Capture [off]. The strategy should be eligible because at
        # least ONE input card has a faulted control. enumerate_input_card_ids
        # already filters HDMI-only cards out (it returns only cards
        # with capture PCM), so we only need card 1 in the list.
        strategy = LinuxALSACaptureSwitchBypass()
        runner = _FakeAmixer(
            scontents_per_card={
                0: _SCONTENTS_CAPTURE_HEALTHY,  # would be ignored; HDMI
                1: _SCONTENTS_CAPTURE_OFF,
            },
        )
        with (
            patch.object(mod, "VoiceTuningConfig", return_value=_Tuning()),
            patch.object(mod.shutil, "which", return_value="/usr/bin/amixer"),
            patch.object(
                mod, "enumerate_input_card_ids", return_value=[(0, "PCH"), (1, "Generic")]
            ),
            patch.object(mod.subprocess, "run", side_effect=runner),
        ):
            elig = await strategy.probe_eligibility(_ctx())
        assert elig.applicable is True


# ── Apply (lenient + strict) ────────────────────────────────────────


class TestApplyLenientMode:
    @pytest.mark.asyncio()
    async def test_lenient_emits_would_repair_per_target_no_mutation(self) -> None:
        strategy = LinuxALSACaptureSwitchBypass()
        runner = _FakeAmixer(
            scontents_per_card={1: _SCONTENTS_BOTH_FAULTED},
        )
        warning_calls: list[tuple[str, dict]] = []
        with (
            patch.object(
                mod,
                "VoiceTuningConfig",
                return_value=_Tuning(enabled=True, lenient=True),
            ),
            patch.object(mod.shutil, "which", return_value="/usr/bin/amixer"),
            patch.object(mod, "enumerate_input_card_ids", return_value=[(1, "Generic")]),
            patch.object(mod.subprocess, "run", side_effect=runner),
            patch.object(
                mod.logger,
                "warning",
                side_effect=lambda evt, **kw: warning_calls.append((evt, kw)),
            ),
        ):
            outcome = await strategy.apply(_ctx())

        assert outcome == "lenient_no_repair"
        # Two would_repair events — one per faulted control.
        would_repairs = [evt for evt, _ in warning_calls if evt == "voice.bypass.would_repair"]
        assert len(would_repairs) == 2
        # No sset call.
        sset_calls = [c for c in runner.calls if "sset" in c]
        assert sset_calls == [], f"lenient must not run sset; got {sset_calls!r}"


class TestApplyStrictMode:
    @pytest.mark.asyncio()
    async def test_strict_engages_capture_switch_and_lifts_boost(self) -> None:
        strategy = LinuxALSACaptureSwitchBypass()

        # Sequence: scontents probes return faulted state for the apply
        # phase; then return healthy state for the verify phase.
        scans = {"call": 0}
        canned_outputs = [
            _SCONTENTS_BOTH_FAULTED,  # apply re-probe
            _SCONTENTS_CAPTURE_HEALTHY
            + _SCONTENTS_INTERNAL_MIC_BOOST_ZERO.replace(
                "0 [0%] [0.00dB]", "2 [67%] [12.00dB]"
            ),  # verify scan after apply
            _SCONTENTS_CAPTURE_HEALTHY
            + _SCONTENTS_INTERNAL_MIC_BOOST_ZERO.replace(
                "0 [0%] [0.00dB]", "2 [67%] [12.00dB]"
            ),  # second verify scan if needed
        ]

        class _SequencingAmixer(_FakeAmixer):
            def __call__(self, argv, **_kwargs):  # type: ignore[no-untyped-def]
                if len(argv) >= 4 and argv[3] == "scontents":
                    self.calls.append(tuple(argv))
                    idx = min(scans["call"], len(canned_outputs) - 1)
                    scans["call"] += 1
                    return _completed(0, canned_outputs[idx])
                return super().__call__(argv, **_kwargs)

        runner = _SequencingAmixer()

        with (
            patch.object(
                mod,
                "VoiceTuningConfig",
                return_value=_Tuning(enabled=True, lenient=False),
            ),
            patch.object(mod.shutil, "which", return_value="/usr/bin/amixer"),
            patch.object(mod, "enumerate_input_card_ids", return_value=[(1, "Generic")]),
            patch.object(mod.subprocess, "run", side_effect=runner),
        ):
            outcome = await strategy.apply(_ctx())

        assert outcome == "capture_switch_engaged_and_boost_lifted"
        # Verify the sset calls hit Capture (cap + 80%) AND Internal Mic Boost (50%).
        sset_argvs = [c for c in runner.calls if "sset" in c]
        capture_args = [a for a in sset_argvs if "Capture" in a]
        boost_args = [a for a in sset_argvs if "Internal Mic Boost" in a]
        assert any("cap" in a for a in capture_args), (
            f"Capture must receive 'cap' switch flip; got {capture_args!r}"
        )
        assert any("80%" in a for a in capture_args), (
            f"Capture must receive 80% volume; got {capture_args!r}"
        )
        assert any("50%" in a for a in boost_args), (
            f"Internal Mic Boost must receive 50%; got {boost_args!r}"
        )

    @pytest.mark.asyncio()
    async def test_strict_no_targets_at_apply_raises(self) -> None:
        # Eligibility passed, but at apply time the host state has
        # changed — re-probe finds zero faulted controls. This is the
        # "race between eligibility and apply" path.
        strategy = LinuxALSACaptureSwitchBypass()
        runner = _FakeAmixer(scontents_per_card={1: _SCONTENTS_CAPTURE_HEALTHY})

        with (
            patch.object(
                mod,
                "VoiceTuningConfig",
                return_value=_Tuning(enabled=True, lenient=False),
            ),
            patch.object(mod.shutil, "which", return_value="/usr/bin/amixer"),
            patch.object(mod, "enumerate_input_card_ids", return_value=[(1, "Generic")]),
            patch.object(mod.subprocess, "run", side_effect=runner),
            pytest.raises(BypassApplyError) as exc_info,
        ):
            await strategy.apply(_ctx())

        assert exc_info.value.reason == "no_off_capture_or_zero_boost_at_apply"

    @pytest.mark.asyncio()
    async def test_strict_verify_failure_raises(self) -> None:
        # apply() runs sset successfully but the post-apply verify scan
        # still shows the control [off] — likely the host kernel module
        # rejected the change (privacy switch in BIOS, etc.).
        strategy = LinuxALSACaptureSwitchBypass()

        scans = {"call": 0}
        canned_outputs = [
            _SCONTENTS_CAPTURE_OFF,  # eligibility/apply re-probe (faulted)
            _SCONTENTS_CAPTURE_OFF,  # verify scan (still faulted!) — this is the failure
        ]

        class _StuckAmixer(_FakeAmixer):
            def __call__(self, argv, **_kwargs):  # type: ignore[no-untyped-def]
                if len(argv) >= 4 and argv[3] == "scontents":
                    self.calls.append(tuple(argv))
                    idx = min(scans["call"], len(canned_outputs) - 1)
                    scans["call"] += 1
                    return _completed(0, canned_outputs[idx])
                return super().__call__(argv, **_kwargs)

        runner = _StuckAmixer()

        with (
            patch.object(
                mod,
                "VoiceTuningConfig",
                return_value=_Tuning(enabled=True, lenient=False),
            ),
            patch.object(mod.shutil, "which", return_value="/usr/bin/amixer"),
            patch.object(mod, "enumerate_input_card_ids", return_value=[(1, "Generic")]),
            patch.object(mod.subprocess, "run", side_effect=runner),
            pytest.raises(BypassApplyError) as exc_info,
        ):
            await strategy.apply(_ctx())

        assert exc_info.value.reason == "verify_after_sset_still_off"


# ── Revert ──────────────────────────────────────────────────────────


class TestRevert:
    @pytest.mark.asyncio()
    async def test_revert_no_apply_is_noop(self) -> None:
        strategy = LinuxALSACaptureSwitchBypass()
        with patch.object(mod.subprocess, "run") as mock_run:
            await strategy.revert(_ctx())
        mock_run.assert_not_called()

    @pytest.mark.asyncio()
    async def test_revert_restores_pre_apply_state(self) -> None:
        strategy = LinuxALSACaptureSwitchBypass()
        from sovyx.voice.health.bypass._linux_alsa_capture_switch import _AppliedTarget

        strategy._applied_targets = [
            _AppliedTarget(
                card_index=1,
                name="Capture",
                was_switch_off=True,
                previous_raw=0,
            ),
        ]
        runner = _FakeAmixer()

        with (
            patch.object(mod.shutil, "which", return_value="/usr/bin/amixer"),
            patch.object(mod.subprocess, "run", side_effect=runner),
        ):
            await strategy.revert(_ctx())

        # Should issue: sset Capture 0  (restore raw)
        # Then:        sset Capture nocap (restore switch off)
        sset_argvs = [c for c in runner.calls if "sset" in c]
        assert any(a[-1] == "0" for a in sset_argvs), (
            f"revert must restore previous_raw=0; got {sset_argvs!r}"
        )
        assert any(a[-1] == "nocap" for a in sset_argvs), (
            f"revert must restore switch nocap; got {sset_argvs!r}"
        )
        # Idempotent.
        assert strategy._applied_targets == []

    @pytest.mark.asyncio()
    async def test_revert_amixer_gone_logs_skip(self) -> None:
        strategy = LinuxALSACaptureSwitchBypass()
        from sovyx.voice.health.bypass._linux_alsa_capture_switch import _AppliedTarget

        strategy._applied_targets = [
            _AppliedTarget(
                card_index=1,
                name="Capture",
                was_switch_off=True,
                previous_raw=0,
            ),
        ]
        with (
            patch.object(mod.shutil, "which", return_value=None),
            patch.object(mod.subprocess, "run") as mock_run,
        ):
            await strategy.revert(_ctx())  # Must not raise.
        mock_run.assert_not_called()
        assert strategy._applied_targets == []


# ── Strategy contract sanity ────────────────────────────────────────


class TestStrategyContract:
    def test_strategy_name_is_stable(self) -> None:
        assert LinuxALSACaptureSwitchBypass.name == "linux.alsa_capture_switch"

    def test_strategy_implements_protocol(self) -> None:
        from sovyx.voice.health.bypass._strategy import PlatformBypassStrategy

        instance = LinuxALSACaptureSwitchBypass()
        assert isinstance(instance, PlatformBypassStrategy)
