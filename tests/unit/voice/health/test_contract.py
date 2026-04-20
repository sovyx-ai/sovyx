"""Tests for the L0 Voice Capture Health Lifecycle contract."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sovyx.voice.health import (
    ALLOWED_FORMATS,
    ALLOWED_HOST_APIS_BY_PLATFORM,
    ALLOWED_SAMPLE_RATES,
    AudioSubsystemFingerprint,
    CascadeResult,
    Combo,
    ComboEntry,
    ComboStoreStats,
    Diagnosis,
    LoadReport,
    OverrideEntry,
    ProbeHistoryEntry,
    ProbeMode,
    ProbeResult,
    RemediationHint,
)


def _good_combo(**overrides: object) -> Combo:
    """Build a Combo with WASAPI defaults; tests override one field at a time."""
    base: dict[str, object] = {
        "host_api": "WASAPI",
        "sample_rate": 16_000,
        "channels": 1,
        "sample_format": "int16",
        "exclusive": True,
        "auto_convert": False,
        "frames_per_buffer": 480,
        "platform_key": "win32",
    }
    base.update(overrides)
    return Combo(**base)  # type: ignore[arg-type]


class TestDiagnosisEnum:
    """Diagnosis must be a stable StrEnum (anti-pattern #9)."""

    def test_is_strenum(self) -> None:
        assert issubclass(Diagnosis, str)
        assert Diagnosis.HEALTHY == "healthy"

    def test_string_equality(self) -> None:
        # xdist-safe: value comparison must work even if class is reimported.
        assert Diagnosis.HEALTHY == "healthy"
        assert Diagnosis.MUTED == "muted"
        assert Diagnosis.NO_SIGNAL.value == "no_signal"

    def test_all_fourteen_values_present(self) -> None:
        expected = {
            "healthy",
            "muted",
            "no_signal",
            "low_signal",
            "format_mismatch",
            "apo_degraded",
            "vad_insensitive",
            "driver_error",
            "device_busy",
            "hot_unplugged",
            "self_feedback",
            "permission_denied",
            "kernel_invalidated",
            "unknown",
        }
        assert {d.value for d in Diagnosis} == expected

    def test_membership(self) -> None:
        assert "healthy" in {d.value for d in Diagnosis}
        assert "definitely_not_a_diagnosis" not in {d.value for d in Diagnosis}


class TestProbeModeEnum:
    """ProbeMode is the cold/warm gate that drives Diagnosis surface area."""

    def test_is_strenum(self) -> None:
        assert issubclass(ProbeMode, str)

    def test_two_modes(self) -> None:
        assert {p.value for p in ProbeMode} == {"cold", "warm"}

    def test_string_equality(self) -> None:
        assert ProbeMode.COLD == "cold"
        assert ProbeMode.WARM == "warm"


class TestValidationTables:
    """The frozen tables that gate Combo construction."""

    def test_sample_rates_immutable(self) -> None:
        assert isinstance(ALLOWED_SAMPLE_RATES, frozenset)
        assert 16_000 in ALLOWED_SAMPLE_RATES
        assert 48_000 in ALLOWED_SAMPLE_RATES
        assert 12_345 not in ALLOWED_SAMPLE_RATES

    def test_formats_immutable(self) -> None:
        assert isinstance(ALLOWED_FORMATS, frozenset)
        assert set(ALLOWED_FORMATS) == {"int16", "int24", "float32"}

    def test_host_apis_per_platform(self) -> None:
        assert "WASAPI" in ALLOWED_HOST_APIS_BY_PLATFORM["win32"]
        assert "Windows WASAPI" in ALLOWED_HOST_APIS_BY_PLATFORM["win32"]
        assert "PulseAudio" in ALLOWED_HOST_APIS_BY_PLATFORM["linux"]
        assert "CoreAudio" in ALLOWED_HOST_APIS_BY_PLATFORM["darwin"]
        assert "Core Audio" in ALLOWED_HOST_APIS_BY_PLATFORM["darwin"]

    def test_no_cross_platform_leak(self) -> None:
        # WASAPI must never appear under linux/darwin tables.
        assert "WASAPI" not in ALLOWED_HOST_APIS_BY_PLATFORM["linux"]
        assert "WASAPI" not in ALLOWED_HOST_APIS_BY_PLATFORM["darwin"]
        assert "ALSA" not in ALLOWED_HOST_APIS_BY_PLATFORM["win32"]


class TestComboValidation:
    """Combo.__post_init__ must reject malformed tuples at construction."""

    def test_well_formed_wasapi(self) -> None:
        combo = _good_combo()
        assert combo.host_api == "WASAPI"
        assert combo.sample_rate == 16_000

    def test_portaudio_formatted_host_api_accepted(self) -> None:
        combo = _good_combo(host_api="Windows WASAPI")
        assert combo.host_api == "Windows WASAPI"

    def test_empty_host_api_rejected(self) -> None:
        with pytest.raises(ValueError, match="host_api must be"):
            _good_combo(host_api="")

    def test_unknown_host_api_for_platform(self) -> None:
        with pytest.raises(ValueError, match="not allowed on platform"):
            _good_combo(host_api="ALSA")  # ALSA on win32

    def test_unknown_sample_rate(self) -> None:
        with pytest.raises(ValueError, match="sample_rate=12345"):
            _good_combo(sample_rate=12_345)

    def test_channels_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="channels=0 out of range"):
            _good_combo(channels=0)

    def test_channels_too_high_rejected(self) -> None:
        with pytest.raises(ValueError, match="channels=99 out of range"):
            _good_combo(channels=99)

    def test_unknown_sample_format(self) -> None:
        with pytest.raises(ValueError, match="sample_format='int8'"):
            _good_combo(sample_format="int8")

    def test_frames_per_buffer_too_small(self) -> None:
        with pytest.raises(ValueError, match="frames_per_buffer=32 out of range"):
            _good_combo(frames_per_buffer=32)

    def test_frames_per_buffer_too_large(self) -> None:
        with pytest.raises(ValueError, match="frames_per_buffer=99999 out of range"):
            _good_combo(frames_per_buffer=99_999)

    def test_platform_key_override_for_linux(self) -> None:
        combo = _good_combo(host_api="ALSA", platform_key="linux")
        assert combo.host_api == "ALSA"

    def test_platform_key_override_for_darwin(self) -> None:
        combo = _good_combo(host_api="CoreAudio", platform_key="darwin")
        assert combo.host_api == "CoreAudio"

    def test_unknown_platform_key_skips_host_api_check(self) -> None:
        # Unknown platform → empty allowed set → host_api accepted.
        combo = _good_combo(host_api="LiterallyAnything", platform_key="haiku-os")
        assert combo.host_api == "LiterallyAnything"

    def test_default_platform_uses_sys_platform(self) -> None:
        # When platform_key is "", validation falls back to sys.platform.
        combo = Combo(
            host_api={"win32": "WASAPI", "linux": "ALSA", "darwin": "CoreAudio"}[
                "win32"
                if sys.platform.startswith("win")
                else ("darwin" if sys.platform == "darwin" else "linux")
            ],
            sample_rate=16_000,
            channels=1,
            sample_format="int16",
            exclusive=False,
            auto_convert=False,
            frames_per_buffer=480,
        )
        assert combo.platform_key == ""  # not stored

    def test_frozen(self) -> None:
        combo = _good_combo()
        with pytest.raises(Exception):  # FrozenInstanceError; xdist-safe wide catch
            combo.sample_rate = 48_000  # type: ignore[misc]


class TestRemediationHint:
    """RemediationHint validates code+severity at construction."""

    def test_well_formed(self) -> None:
        hint = RemediationHint(
            code="remediation.muted",
            severity="warn",
            cli_action="sovyx doctor voice --fix",
        )
        assert hint.code == "remediation.muted"
        assert hint.cli_action is not None

    def test_empty_code_rejected(self) -> None:
        with pytest.raises(ValueError, match="remediation code must be"):
            RemediationHint(code="", severity="info")

    def test_unknown_severity_rejected(self) -> None:
        with pytest.raises(ValueError, match="severity='critical'"):
            RemediationHint(code="x", severity="critical")

    def test_each_known_severity_accepted(self) -> None:
        for sev in ("info", "warn", "error"):
            hint = RemediationHint(code="x", severity=sev)
            assert hint.severity == sev

    def test_cli_action_optional(self) -> None:
        hint = RemediationHint(code="x", severity="info")
        assert hint.cli_action is None


class TestProbeResult:
    """ProbeResult is a value object — only smoke construction tested."""

    def test_construct_cold(self) -> None:
        combo = _good_combo()
        result = ProbeResult(
            diagnosis=Diagnosis.HEALTHY,
            mode=ProbeMode.COLD,
            combo=combo,
            vad_max_prob=None,
            vad_mean_prob=None,
            rms_db=-42.0,
            callbacks_fired=10,
            duration_ms=300,
        )
        assert result.diagnosis is Diagnosis.HEALTHY
        assert result.mode is ProbeMode.COLD
        assert result.error is None

    def test_construct_warm_with_remediation(self) -> None:
        combo = _good_combo()
        result = ProbeResult(
            diagnosis=Diagnosis.MUTED,
            mode=ProbeMode.WARM,
            combo=combo,
            vad_max_prob=0.0,
            vad_mean_prob=0.0,
            rms_db=-90.0,
            callbacks_fired=20,
            duration_ms=2_000,
            error=None,
            remediation=RemediationHint(code="remediation.muted", severity="warn"),
        )
        assert result.remediation is not None
        assert result.remediation.code == "remediation.muted"


class TestAudioSubsystemFingerprint:
    """Plain SHA snapshot dataclass — frozen + slots."""

    def test_defaults_empty(self) -> None:
        fp = AudioSubsystemFingerprint()
        assert fp.windows_audio_endpoints_sha == ""
        assert fp.computed_at == ""

    def test_construct_full(self) -> None:
        fp = AudioSubsystemFingerprint(
            windows_audio_endpoints_sha="a" * 64,
            windows_fxproperties_global_sha="b" * 64,
            computed_at="2026-04-19T12:00:00Z",
        )
        assert fp.windows_audio_endpoints_sha == "a" * 64

    def test_frozen(self) -> None:
        fp = AudioSubsystemFingerprint()
        with pytest.raises(Exception):
            fp.windows_audio_endpoints_sha = "x"  # type: ignore[misc]


class TestProbeHistoryEntry:
    """Bounded probe history element."""

    def test_construct(self) -> None:
        entry = ProbeHistoryEntry(
            ts="2026-04-19T12:00:00Z",
            mode=ProbeMode.WARM,
            diagnosis=Diagnosis.HEALTHY,
            vad_max_prob=0.94,
            rms_db=-25.0,
            duration_ms=1_500,
        )
        assert entry.diagnosis is Diagnosis.HEALTHY


class TestComboEntry:
    """ComboEntry is a row of the persisted ComboStore."""

    def test_construct_minimal(self) -> None:
        entry = ComboEntry(
            endpoint_guid="{0.0.1.00000000}.{abcd}",
            device_friendly_name="Microphone (Realtek)",
            device_interface_name="\\\\?\\SWD#MMDEVAPI#…",
            device_class="microphone",
            endpoint_fxproperties_sha="c" * 64,
            winning_combo=_good_combo(),
            validated_at="2026-04-19T12:00:00Z",
            validation_mode=ProbeMode.WARM,
            vad_max_prob_at_validation=0.91,
            vad_mean_prob_at_validation=0.42,
            rms_db_at_validation=-22.0,
            probe_duration_ms=1_500,
            detected_apos_at_validation=("VocaEffectPack",),
            cascade_attempts_before_success=3,
            boots_validated=12,
            last_boot_validated="2026-04-19T08:00:00Z",
            last_boot_diagnosis=Diagnosis.HEALTHY,
        )
        assert entry.pinned is False
        assert entry.needs_revalidation is False
        assert entry.probe_history == ()

    def test_with_history_and_pin(self) -> None:
        history = (
            ProbeHistoryEntry(
                ts="2026-04-19T12:00:00Z",
                mode=ProbeMode.WARM,
                diagnosis=Diagnosis.HEALTHY,
                vad_max_prob=0.92,
                rms_db=-24.0,
                duration_ms=1_400,
            ),
        )
        entry = ComboEntry(
            endpoint_guid="g",
            device_friendly_name="d",
            device_interface_name="i",
            device_class="microphone",
            endpoint_fxproperties_sha="d" * 64,
            winning_combo=_good_combo(),
            validated_at="2026-04-19T12:00:00Z",
            validation_mode=ProbeMode.WARM,
            vad_max_prob_at_validation=0.92,
            vad_mean_prob_at_validation=0.40,
            rms_db_at_validation=-24.0,
            probe_duration_ms=1_400,
            detected_apos_at_validation=(),
            cascade_attempts_before_success=0,
            boots_validated=1,
            last_boot_validated="2026-04-19T12:00:00Z",
            last_boot_diagnosis=Diagnosis.HEALTHY,
            probe_history=history,
            pinned=True,
        )
        assert entry.pinned is True
        assert len(entry.probe_history) == 1


class TestOverrideEntry:
    """OverrideEntry is the user-pinned combo row in capture_overrides.json."""

    def test_construct(self) -> None:
        entry = OverrideEntry(
            endpoint_guid="g",
            device_friendly_name="Mic",
            pinned_combo=_good_combo(),
            pinned_at="2026-04-19T12:00:00Z",
            pinned_by="wizard",
        )
        assert entry.reason == ""
        assert entry.pinned_by == "wizard"


class TestCascadeResult:
    """CascadeResult is the cascade's authoritative outcome."""

    def test_winner(self) -> None:
        combo = _good_combo()
        probe = ProbeResult(
            diagnosis=Diagnosis.HEALTHY,
            mode=ProbeMode.WARM,
            combo=combo,
            vad_max_prob=0.93,
            vad_mean_prob=0.41,
            rms_db=-22.0,
            callbacks_fired=20,
            duration_ms=1_500,
        )
        result = CascadeResult(
            endpoint_guid="g",
            winning_combo=combo,
            winning_probe=probe,
            attempts=(probe,),
            attempts_count=1,
            budget_exhausted=False,
            source="cascade",
        )
        assert result.source == "cascade"
        assert result.winning_combo is combo

    def test_exhausted(self) -> None:
        result = CascadeResult(
            endpoint_guid="g",
            winning_combo=None,
            winning_probe=None,
            attempts=(),
            attempts_count=7,
            budget_exhausted=True,
            source="none",
        )
        assert result.winning_combo is None
        assert result.budget_exhausted is True


class TestLoadReport:
    """LoadReport summarises ComboStore.load() outcomes."""

    def test_construct(self) -> None:
        report = LoadReport(
            rules_applied=(("R6", "endpoint-1"), ("R12", "<global>")),
            entries_loaded=4,
            entries_dropped=1,
            backup_used=False,
            archived_to=None,
        )
        assert report.entries_loaded == 4
        assert report.archived_to is None

    def test_with_archive(self, tmp_path: Path) -> None:
        archive = tmp_path / "combos.corrupt-2026-04-19T12-00-00Z.json"
        report = LoadReport(
            rules_applied=(),
            entries_loaded=0,
            entries_dropped=0,
            backup_used=True,
            archived_to=archive,
        )
        assert report.archived_to == archive


class TestComboStoreStats:
    """ComboStoreStats is mutable on purpose so counters can be incremented."""

    def test_defaults_zero(self) -> None:
        stats = ComboStoreStats()
        assert stats.fast_path_hits == 0
        assert stats.fast_path_misses == 0
        assert stats.invalidations_by_reason == {}

    def test_mutable(self) -> None:
        stats = ComboStoreStats()
        stats.fast_path_hits += 1
        stats.invalidations_by_reason["R6"] = 2
        assert stats.fast_path_hits == 1
        assert stats.invalidations_by_reason == {"R6": 2}

    def test_separate_dicts_per_instance(self) -> None:
        # field(default_factory=dict) — mutable defaults must NOT alias.
        a = ComboStoreStats()
        b = ComboStoreStats()
        a.invalidations_by_reason["R7"] = 1
        assert b.invalidations_by_reason == {}
