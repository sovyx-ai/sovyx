"""Tests for ``LinuxWirePlumberDefaultSourceBypass`` (Mission §Phase 2 T2.1).

Mission anchor:
``docs-internal/missions/MISSION-voice-linux-silent-mic-remediation-2026-05-04.md``
§Phase 2 T2.1.

Mocking strategy: per CLAUDE.md anti-pattern #11, all patches use
``patch.object(mod, attr, ...)`` against the strategy module's local
imports — never string paths. ``subprocess.run`` is replaced with a
``_FakeRunner`` whose side effects dispatch on the first argv element
(``pactl``, ``wpctl``) and the verb (``info``, ``get-default-source``,
``list``, ``set-default``, ``set-source-mute``, ``set-source-volume``,
``set-default-source``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.voice.health.bypass import _linux_wireplumber_default_source as mod
from sovyx.voice.health.bypass._linux_wireplumber_default_source import (
    _STUB_SOURCE_NAMES,
    LinuxWirePlumberDefaultSourceBypass,
    _block_is_muted,
    _block_volume_below_threshold,
    _extract_source_block,
    _looks_like_monitor,
)
from sovyx.voice.health.bypass._strategy import BypassApplyError, BypassRevertError
from sovyx.voice.health.contract import BypassContext

# ── Subprocess mock harness ─────────────────────────────────────────


_PACTL_INFO_OK_STDOUT = (
    "Server String: /run/user/1000/pulse/native\n"
    "Library Protocol Version: 35\n"
    "Server Protocol Version: 35\n"
    "Is Local: yes\n"
    "Server Name: PulseAudio (on PipeWire 1.0.5)\n"
)


_PACTL_GET_DEFAULT_MONITOR = "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor\n"
_PACTL_GET_DEFAULT_REAL_MIC = "alsa_input.pci-0000_04_00.6.analog-stereo\n"


_PACTL_LIST_SHORT_SOURCES = (
    "59\talsa_output.pci-0000_00_1f.3.analog-stereo.monitor\tPipeWire\t"
    "s32le 2ch 48000Hz\tSUSPENDED\n"
    "60\talsa_input.pci-0000_04_00.6.analog-stereo\tPipeWire\t"
    "s32le 2ch 48000Hz\tSUSPENDED\n"
)


# Long-form list with the real-mic source muted.
_PACTL_LIST_SOURCES_REAL_MIC_MUTED = """\
Source #59
\tState: SUSPENDED
\tName: alsa_output.pci-0000_00_1f.3.analog-stereo.monitor
\tMute: no
\tVolume: front-left: 65536 / 100% / 0.00 dB,
\t        front-right: 65536 / 100% / 0.00 dB

Source #60
\tState: SUSPENDED
\tName: alsa_input.pci-0000_04_00.6.analog-stereo
\tMute: yes
\tVolume: front-left: 65536 / 100% / 0.00 dB,
\t        front-right: 65536 / 100% / 0.00 dB
"""


# Long-form with all-channels-near-zero on the real mic (the
# evodencias.txt 2026-05-04 vol=0.34 case + further drop to <5%).
_PACTL_LIST_SOURCES_REAL_MIC_LOW_VOLUME = """\
Source #60
\tState: SUSPENDED
\tName: alsa_input.pci-0000_04_00.6.analog-stereo
\tMute: no
\tVolume: front-left: 1310 / 2% / -65.69 dB,
\t        front-right: 1310 / 2% / -65.69 dB
"""


# Healthy default source — non-monitor + unmuted + decent volume.
_PACTL_LIST_SOURCES_REAL_MIC_HEALTHY = """\
Source #60
\tState: SUSPENDED
\tName: alsa_input.pci-0000_04_00.6.analog-stereo
\tMute: no
\tVolume: front-left: 52428 / 80% / -5.18 dB,
\t        front-right: 52428 / 80% / -5.18 dB
"""


class _FakeRunner:
    """Stand-in for ``subprocess.run`` with verb-based dispatch.

    Built around the actual call sites in
    :mod:`_linux_wireplumber_default_source`. Each method returns a
    minimal :class:`subprocess.CompletedProcess`-like object with
    ``returncode``, ``stdout``, ``stderr`` attributes.
    """

    def __init__(
        self,
        *,
        pactl_info_rc: int = 0,
        get_default_stdout: str = _PACTL_GET_DEFAULT_MONITOR,
        get_default_rc: int = 0,
        list_short_sources_stdout: str = _PACTL_LIST_SHORT_SOURCES,
        list_sources_stdout: str = _PACTL_LIST_SOURCES_REAL_MIC_MUTED,
        list_sources_rc: int = 0,
        wpctl_set_default_rc: int = 0,
        pactl_set_default_source_rc: int = 0,
        pactl_set_source_mute_rc: int = 0,
        pactl_set_source_volume_rc: int = 0,
    ) -> None:
        self.pactl_info_rc = pactl_info_rc
        self.get_default_stdout = get_default_stdout
        self.get_default_rc = get_default_rc
        self.list_short_sources_stdout = list_short_sources_stdout
        self.list_sources_stdout = list_sources_stdout
        self.list_sources_rc = list_sources_rc
        self.wpctl_set_default_rc = wpctl_set_default_rc
        self.pactl_set_default_source_rc = pactl_set_default_source_rc
        self.pactl_set_source_mute_rc = pactl_set_source_mute_rc
        self.pactl_set_source_volume_rc = pactl_set_source_volume_rc
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(tuple(argv))
        # argv[0] = "/usr/bin/pactl" or "/usr/bin/wpctl"; argv[1] = verb
        binary = argv[0].rsplit("/", 1)[-1]
        verb = argv[1] if len(argv) > 1 else ""
        sub_verb = argv[2] if len(argv) > 2 else ""

        if binary == "pactl" and verb == "info":
            return _completed(self.pactl_info_rc, _PACTL_INFO_OK_STDOUT)
        if binary == "pactl" and verb == "get-default-source":
            return _completed(self.get_default_rc, self.get_default_stdout)
        if binary == "pactl" and verb == "list" and sub_verb == "short":
            # `pactl list short sources` — argv = (pactl, list, short, sources)
            return _completed(0, self.list_short_sources_stdout)
        if binary == "pactl" and verb == "list" and sub_verb == "sources":
            # `pactl list sources` (long form) — argv = (pactl, list, sources)
            return _completed(self.list_sources_rc, self.list_sources_stdout)
        if binary == "wpctl" and verb == "set-default":
            return _completed(self.wpctl_set_default_rc, "")
        if binary == "pactl" and verb == "set-default-source":
            return _completed(self.pactl_set_default_source_rc, "")
        if binary == "pactl" and verb == "set-source-mute":
            return _completed(self.pactl_set_source_mute_rc, "")
        if binary == "pactl" and verb == "set-source-volume":
            return _completed(self.pactl_set_source_volume_rc, "")
        return _completed(1, "", stderr=f"unhandled argv: {argv!r}")


def _completed(returncode: int, stdout: str, *, stderr: str = "") -> object:
    cp = MagicMock()
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


class _Tuning:
    def __init__(
        self,
        *,
        enabled: bool = True,
        lenient: bool = False,
    ) -> None:
        self.linux_wireplumber_default_source_bypass_enabled = enabled
        self.linux_wireplumber_default_source_bypass_lenient = lenient


def _ctx(*, platform: str = "linux") -> BypassContext:
    return BypassContext(
        endpoint_guid="{guid}",
        endpoint_friendly_name="HD-Audio Generic: SN6180 Analog (hw:1,0)",
        host_api_name="ALSA",
        platform_key=platform,
        capture_task=MagicMock(),
        probe_fn=AsyncMock(),
        current_device_index=7,
        current_device_kind="os_default",
    )


# ── Helper unit tests ───────────────────────────────────────────────


class TestLooksLikeMonitor:
    def test_monitor_suffix_detected(self) -> None:
        assert _looks_like_monitor("alsa_output.foo.bar.analog-stereo.monitor") is True

    def test_real_input_not_monitor(self) -> None:
        assert _looks_like_monitor("alsa_input.pci-0000.analog-stereo") is False

    def test_empty_string_not_monitor(self) -> None:
        assert _looks_like_monitor("") is False


class TestBlockParsing:
    def test_extract_block_by_name(self) -> None:
        block = _extract_source_block(
            _PACTL_LIST_SOURCES_REAL_MIC_MUTED,
            "alsa_input.pci-0000_04_00.6.analog-stereo",
        )
        assert block is not None
        assert "Mute: yes" in block

    def test_extract_block_returns_none_when_not_found(self) -> None:
        block = _extract_source_block(
            _PACTL_LIST_SOURCES_REAL_MIC_MUTED,
            "nonexistent.source.name",
        )
        assert block is None

    def test_block_is_muted_yes(self) -> None:
        block = _extract_source_block(
            _PACTL_LIST_SOURCES_REAL_MIC_MUTED,
            "alsa_input.pci-0000_04_00.6.analog-stereo",
        )
        assert _block_is_muted(block or "") is True

    def test_block_is_muted_no(self) -> None:
        block = _extract_source_block(
            _PACTL_LIST_SOURCES_REAL_MIC_MUTED,
            "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor",
        )
        assert _block_is_muted(block or "") is False

    def test_low_volume_detected_below_threshold(self) -> None:
        block = _extract_source_block(
            _PACTL_LIST_SOURCES_REAL_MIC_LOW_VOLUME,
            "alsa_input.pci-0000_04_00.6.analog-stereo",
        )
        # 2% volume, threshold 5% → below
        assert _block_volume_below_threshold(block or "", 0.05) is True

    def test_high_volume_not_below_threshold(self) -> None:
        block = _extract_source_block(
            _PACTL_LIST_SOURCES_REAL_MIC_HEALTHY,
            "alsa_input.pci-0000_04_00.6.analog-stereo",
        )
        # 80% volume, threshold 5% → above
        assert _block_volume_below_threshold(block or "", 0.05) is False

    def test_partial_below_threshold_returns_false(self) -> None:
        # Mixed: one channel 80%, one channel 2%. NOT all-channels-below
        # → strategy should NOT fire. Empirically the real-world failure
        # mode is uniform low volume (operator dragged a single slider).
        mixed_block = """Source #60
\tName: alsa_input.real
\tMute: no
\tVolume: front-left: 52428 / 80% / -5.18 dB,
\t        front-right: 1310 / 2% / -65.69 dB"""
        assert _block_volume_below_threshold(mixed_block, 0.05) is False


# ── Eligibility ─────────────────────────────────────────────────────


class TestEligibility:
    @pytest.mark.asyncio()
    async def test_not_linux_ineligible(self) -> None:
        strategy = LinuxWirePlumberDefaultSourceBypass()
        elig = await strategy.probe_eligibility(_ctx(platform="win32"))
        assert elig.applicable is False
        assert elig.reason == "not_linux_platform"

    @pytest.mark.asyncio()
    async def test_disabled_by_tuning_ineligible(self) -> None:
        strategy = LinuxWirePlumberDefaultSourceBypass()
        with patch.object(
            mod,
            "VoiceTuningConfig",
            return_value=_Tuning(enabled=False),
        ):
            elig = await strategy.probe_eligibility(_ctx())
        assert elig.applicable is False
        assert elig.reason == "wireplumber_default_source_disabled_by_tuning"

    @pytest.mark.asyncio()
    async def test_pactl_missing_ineligible(self) -> None:
        strategy = LinuxWirePlumberDefaultSourceBypass()
        with (
            patch.object(mod, "VoiceTuningConfig", return_value=_Tuning()),
            patch.object(mod.shutil, "which", return_value=None),
        ):
            elig = await strategy.probe_eligibility(_ctx())
        assert elig.applicable is False
        assert elig.reason == "pactl_unavailable_on_host"

    @pytest.mark.asyncio()
    async def test_pipewire_not_running_ineligible(self) -> None:
        strategy = LinuxWirePlumberDefaultSourceBypass()
        runner = _FakeRunner(pactl_info_rc=1)
        with (
            patch.object(mod, "VoiceTuningConfig", return_value=_Tuning()),
            patch.object(mod.shutil, "which", return_value="/usr/bin/pactl"),
            patch.object(mod.subprocess, "run", side_effect=runner),
        ):
            elig = await strategy.probe_eligibility(_ctx())
        assert elig.applicable is False
        assert elig.reason == "pipewire_not_running"

    @pytest.mark.asyncio()
    async def test_default_already_real_and_unmuted_ineligible(self) -> None:
        strategy = LinuxWirePlumberDefaultSourceBypass()
        runner = _FakeRunner(
            get_default_stdout=_PACTL_GET_DEFAULT_REAL_MIC,
            list_sources_stdout=_PACTL_LIST_SOURCES_REAL_MIC_HEALTHY,
        )
        with (
            patch.object(mod, "VoiceTuningConfig", return_value=_Tuning()),
            patch.object(mod.shutil, "which", return_value="/usr/bin/pactl"),
            patch.object(mod.subprocess, "run", side_effect=runner),
        ):
            elig = await strategy.probe_eligibility(_ctx())
        assert elig.applicable is False
        assert elig.reason == "default_source_already_real_and_unmuted"

    @pytest.mark.asyncio()
    async def test_applicable_when_default_is_monitor(self) -> None:
        strategy = LinuxWirePlumberDefaultSourceBypass()
        runner = _FakeRunner(
            get_default_stdout=_PACTL_GET_DEFAULT_MONITOR,
            list_sources_stdout=_PACTL_LIST_SOURCES_REAL_MIC_HEALTHY,
        )
        with (
            patch.object(mod, "VoiceTuningConfig", return_value=_Tuning()),
            patch.object(mod.shutil, "which", return_value="/usr/bin/pactl"),
            patch.object(mod.subprocess, "run", side_effect=runner),
        ):
            elig = await strategy.probe_eligibility(_ctx())
        assert elig.applicable is True
        assert elig.estimated_cost_ms > 0

    @pytest.mark.asyncio()
    async def test_applicable_when_default_muted(self) -> None:
        strategy = LinuxWirePlumberDefaultSourceBypass()
        runner = _FakeRunner(
            get_default_stdout=_PACTL_GET_DEFAULT_REAL_MIC,
            list_sources_stdout=_PACTL_LIST_SOURCES_REAL_MIC_MUTED,
        )
        with (
            patch.object(mod, "VoiceTuningConfig", return_value=_Tuning()),
            patch.object(mod.shutil, "which", return_value="/usr/bin/pactl"),
            patch.object(mod.subprocess, "run", side_effect=runner),
        ):
            elig = await strategy.probe_eligibility(_ctx())
        assert elig.applicable is True

    @pytest.mark.asyncio()
    async def test_applicable_when_default_near_zero_volume(self) -> None:
        strategy = LinuxWirePlumberDefaultSourceBypass()
        runner = _FakeRunner(
            get_default_stdout=_PACTL_GET_DEFAULT_REAL_MIC,
            list_sources_stdout=_PACTL_LIST_SOURCES_REAL_MIC_LOW_VOLUME,
        )
        with (
            patch.object(mod, "VoiceTuningConfig", return_value=_Tuning()),
            patch.object(mod.shutil, "which", return_value="/usr/bin/pactl"),
            patch.object(mod.subprocess, "run", side_effect=runner),
        ):
            elig = await strategy.probe_eligibility(_ctx())
        assert elig.applicable is True

    @pytest.mark.asyncio()
    async def test_no_real_input_ineligible(self) -> None:
        # Only monitor source exists — no reroute target.
        only_monitor_short = (
            "59\talsa_output.pci-0000_00_1f.3.analog-stereo.monitor\tPipeWire\t"
            "s32le 2ch 48000Hz\tSUSPENDED\n"
        )
        strategy = LinuxWirePlumberDefaultSourceBypass()
        runner = _FakeRunner(
            get_default_stdout=_PACTL_GET_DEFAULT_MONITOR,
            list_short_sources_stdout=only_monitor_short,
        )
        with (
            patch.object(mod, "VoiceTuningConfig", return_value=_Tuning()),
            patch.object(mod.shutil, "which", return_value="/usr/bin/pactl"),
            patch.object(mod.subprocess, "run", side_effect=runner),
        ):
            elig = await strategy.probe_eligibility(_ctx())
        assert elig.applicable is False
        assert elig.reason == "no_real_input_source_available"


# ── Apply (lenient + strict) ────────────────────────────────────────


class TestApplyLenientMode:
    @pytest.mark.asyncio()
    async def test_lenient_emits_would_repair_no_mutation(self) -> None:
        strategy = LinuxWirePlumberDefaultSourceBypass()
        runner = _FakeRunner()
        warning_calls: list[tuple[str, dict]] = []

        with (
            patch.object(
                mod,
                "VoiceTuningConfig",
                return_value=_Tuning(enabled=True, lenient=True),
            ),
            patch.object(
                mod.shutil,
                "which",
                lambda binary: f"/usr/bin/{binary}" if binary in ("pactl", "wpctl") else None,
            ),
            patch.object(mod.subprocess, "run", side_effect=runner),
            patch.object(
                mod.logger,
                "warning",
                side_effect=lambda evt, **kw: warning_calls.append((evt, kw)),
            ),
        ):
            outcome = await strategy.apply(_ctx())

        assert outcome == "lenient_no_repair"
        # No mutating call should have happened — only queries.
        mutating_argvs = [
            c
            for c in runner.calls
            if c[1]
            in ("set-default", "set-default-source", "set-source-mute", "set-source-volume")
        ]
        assert mutating_argvs == [], (
            f"lenient mode must not call any mutating subprocess; got {mutating_argvs!r}"
        )
        # Telemetry event fired exactly once.
        would_repair = [evt for evt, _ in warning_calls if evt == "voice.bypass.would_repair"]
        assert len(would_repair) == 1


class TestApplyStrictMode:
    @pytest.mark.asyncio()
    async def test_strict_runs_set_default_unmute_volume_then_verifies(self) -> None:
        strategy = LinuxWirePlumberDefaultSourceBypass()

        # Sequence the runner so the verify step (final get-default-source)
        # reports the new healthy source.
        verify_calls = {"count": 0}
        original_default_stdouts = [
            _PACTL_GET_DEFAULT_MONITOR,  # snapshot pre-apply
            _PACTL_GET_DEFAULT_REAL_MIC,  # verify post-apply
        ]

        def _stdout_for_default() -> str:
            idx = min(verify_calls["count"], len(original_default_stdouts) - 1)
            verify_calls["count"] += 1
            return original_default_stdouts[idx]

        class _SequencingRunner(_FakeRunner):
            def __call__(self, argv, **_kwargs):  # type: ignore[no-untyped-def]
                if argv[0].endswith("pactl") and len(argv) > 1 and argv[1] == "get-default-source":
                    self.calls.append(tuple(argv))
                    return _completed(0, _stdout_for_default())
                return super().__call__(argv, **_kwargs)

        runner = _SequencingRunner()

        with (
            patch.object(
                mod,
                "VoiceTuningConfig",
                return_value=_Tuning(enabled=True, lenient=False),
            ),
            patch.object(
                mod.shutil,
                "which",
                lambda binary: f"/usr/bin/{binary}" if binary in ("pactl", "wpctl") else None,
            ),
            patch.object(mod.subprocess, "run", side_effect=runner),
        ):
            outcome = await strategy.apply(_ctx())

        assert outcome == "default_source_routed_and_unmuted"
        # All 3 mutating calls happened.
        binaries_verbs = {(c[0].rsplit("/", 1)[-1], c[1]) for c in runner.calls}
        assert ("wpctl", "set-default") in binaries_verbs
        assert ("pactl", "set-source-mute") in binaries_verbs
        assert ("pactl", "set-source-volume") in binaries_verbs

    @pytest.mark.asyncio()
    async def test_strict_falls_back_to_pactl_when_wpctl_missing(self) -> None:
        strategy = LinuxWirePlumberDefaultSourceBypass()
        runner = _FakeRunner(
            get_default_stdout=_PACTL_GET_DEFAULT_REAL_MIC,  # verify shows healthy
        )

        # which() returns pactl path but None for wpctl.
        def _which(binary: str) -> str | None:
            return "/usr/bin/pactl" if binary == "pactl" else None

        with (
            patch.object(
                mod,
                "VoiceTuningConfig",
                return_value=_Tuning(enabled=True, lenient=False),
            ),
            patch.object(mod.shutil, "which", _which),
            patch.object(mod.subprocess, "run", side_effect=runner),
        ):
            outcome = await strategy.apply(_ctx())

        assert outcome == "default_source_routed_and_unmuted"
        # Used pactl set-default-source instead of wpctl set-default.
        binaries_verbs = {(c[0].rsplit("/", 1)[-1], c[1]) for c in runner.calls}
        assert ("pactl", "set-default-source") in binaries_verbs
        assert ("wpctl", "set-default") not in binaries_verbs

    @pytest.mark.asyncio()
    async def test_strict_verify_failure_raises(self) -> None:
        # Verify step still returns a monitor source (mutation didn't take).
        strategy = LinuxWirePlumberDefaultSourceBypass()
        runner = _FakeRunner(get_default_stdout=_PACTL_GET_DEFAULT_MONITOR)

        with (
            patch.object(
                mod,
                "VoiceTuningConfig",
                return_value=_Tuning(enabled=True, lenient=False),
            ),
            patch.object(
                mod.shutil,
                "which",
                lambda binary: f"/usr/bin/{binary}" if binary in ("pactl", "wpctl") else None,
            ),
            patch.object(mod.subprocess, "run", side_effect=runner),
            pytest.raises(BypassApplyError) as exc_info,
        ):
            await strategy.apply(_ctx())

        assert exc_info.value.reason == "wireplumber_verify_after_set_default_unchanged"

    @pytest.mark.asyncio()
    async def test_strict_no_target_raises(self) -> None:
        # No real input source available at apply time — even though
        # eligibility was passed (test fixture forces this race).
        strategy = LinuxWirePlumberDefaultSourceBypass()
        only_monitor_short = (
            "59\talsa_output.foo.monitor\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED\n"
        )
        runner = _FakeRunner(list_short_sources_stdout=only_monitor_short)

        with (
            patch.object(
                mod,
                "VoiceTuningConfig",
                return_value=_Tuning(enabled=True, lenient=False),
            ),
            patch.object(
                mod.shutil,
                "which",
                lambda binary: f"/usr/bin/{binary}" if binary in ("pactl", "wpctl") else None,
            ),
            patch.object(mod.subprocess, "run", side_effect=runner),
            pytest.raises(BypassApplyError) as exc_info,
        ):
            await strategy.apply(_ctx())

        assert exc_info.value.reason == "no_target_source_at_apply"


# ── Revert ──────────────────────────────────────────────────────────


class TestRevert:
    @pytest.mark.asyncio()
    async def test_revert_no_apply_is_noop(self) -> None:
        strategy = LinuxWirePlumberDefaultSourceBypass()
        # No apply() called → _previous_default_source is None.
        # Should NOT raise + should NOT call subprocess.
        with patch.object(mod.subprocess, "run") as mock_run:
            await strategy.revert(_ctx())
        mock_run.assert_not_called()

    @pytest.mark.asyncio()
    async def test_revert_restores_previous_default(self) -> None:
        strategy = LinuxWirePlumberDefaultSourceBypass()
        strategy._previous_default_source = "alsa_output.foo.monitor"
        runner = _FakeRunner()

        with (
            patch.object(mod.shutil, "which", return_value="/usr/bin/pactl"),
            patch.object(mod.subprocess, "run", side_effect=runner),
        ):
            await strategy.revert(_ctx())

        # Should have called pactl set-default-source <previous>.
        verbs = [(c[0].rsplit("/", 1)[-1], c[1]) for c in runner.calls]
        assert ("pactl", "set-default-source") in verbs
        # Idempotent: snapshot cleared.
        assert strategy._previous_default_source is None

    @pytest.mark.asyncio()
    async def test_revert_subprocess_failure_raises_revert_error(self) -> None:
        strategy = LinuxWirePlumberDefaultSourceBypass()
        strategy._previous_default_source = "alsa_output.foo.monitor"
        runner = _FakeRunner(pactl_set_default_source_rc=1)

        with (
            patch.object(mod.shutil, "which", return_value="/usr/bin/pactl"),
            patch.object(mod.subprocess, "run", side_effect=runner),
            pytest.raises(BypassRevertError) as exc_info,
        ):
            await strategy.revert(_ctx())

        assert exc_info.value.reason == "wireplumber_revert_set_default_failed"
        # Snapshot still cleared even on failure (no double-revert).
        assert strategy._previous_default_source is None


# ── Strategy contract sanity ────────────────────────────────────────


class TestStrategyContract:
    def test_strategy_name_is_stable(self) -> None:
        # Treat as external API per CLAUDE.md anti-pattern guidance.
        # A rename here is a breaking change for dashboards.
        assert LinuxWirePlumberDefaultSourceBypass.name == "linux.wireplumber_default_source"

    def test_strategy_implements_protocol(self) -> None:
        from sovyx.voice.health.bypass._strategy import PlatformBypassStrategy

        instance = LinuxWirePlumberDefaultSourceBypass()
        assert isinstance(instance, PlatformBypassStrategy)

    def test_stub_source_names_filtered(self) -> None:
        # Defensive — make sure the well-known stub names aren't picked
        # as reroute targets.
        assert "auto_null" in _STUB_SOURCE_NAMES
        assert any("dummy" in name for name in _STUB_SOURCE_NAMES)
