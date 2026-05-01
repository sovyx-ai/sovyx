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
    FactorySignature,
    HardwareContext,
    LoadReport,
    MixerApplySnapshot,
    MixerControlRole,
    MixerKBProfile,
    MixerPresetControl,
    MixerPresetSpec,
    MixerPresetValueDb,
    MixerPresetValueFraction,
    MixerPresetValueRaw,
    MixerSanityDecision,
    MixerSanityResult,
    MixerValidationMetrics,
    OverrideEntry,
    ProbeHistoryEntry,
    ProbeMode,
    ProbeResult,
    RemediationHint,
    ValidationGates,
    VerificationRecord,
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

    def test_diagnosis_value_set_present(self) -> None:
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
            "permission_denied",
            "kernel_invalidated",
            # L2.5 mixer sanity diagnoses (ADR-voice-mixer-sanity-l2.5-bidirectional).
            "mixer_zeroed",
            "mixer_saturated",
            "mixer_unknown_pattern",
            "mixer_customized",
            # Phase 6 / T6.2 — driver accepted open + start but ZERO
            # callbacks fired within probe_stream_open_timeout_threshold_ms.
            "stream_open_timeout",
            "unknown",
        }
        assert {d.value for d in Diagnosis} == expected

    def test_mixer_l25_values_are_strenum(self) -> None:
        # xdist-safe value comparison for the new L2.5 members.
        assert Diagnosis.MIXER_ZEROED == "mixer_zeroed"
        assert Diagnosis.MIXER_SATURATED == "mixer_saturated"
        assert Diagnosis.MIXER_UNKNOWN_PATTERN == "mixer_unknown_pattern"
        assert Diagnosis.MIXER_CUSTOMIZED == "mixer_customized"

    def test_mixer_l25_values_distinct_from_apo_degraded(self) -> None:
        # Regression guard — MIXER_SATURATED was split out of APO_DEGRADED
        # so bypass coordinator routes to mixer reset instead of APO bypass.
        assert Diagnosis.APO_DEGRADED is not Diagnosis.MIXER_SATURATED
        assert Diagnosis.APO_DEGRADED.value != Diagnosis.MIXER_SATURATED.value

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


# ── L2.5 mixer sanity contract tests ───────────────────────────────────


class TestMixerControlRole:
    """MixerControlRole is the role-based discovery enum (replaces pattern match)."""

    def test_is_strenum(self) -> None:
        assert issubclass(MixerControlRole, str)

    def test_all_twelve_roles_present(self) -> None:
        expected = {
            "capture_master",
            "internal_mic_boost",
            "preamp_boost",
            "digital_capture",
            "input_source_selector",
            "auto_mute",
            "capture_switch",
            "pga_master",
            "pga_dmic",
            "usb_mic_master",
            "bt_hfp_gain",
            "unknown",
        }
        assert {r.value for r in MixerControlRole} == expected

    def test_string_equality(self) -> None:
        assert MixerControlRole.CAPTURE_MASTER == "capture_master"
        assert MixerControlRole.INTERNAL_MIC_BOOST == "internal_mic_boost"
        assert MixerControlRole.UNKNOWN == "unknown"

    def test_hashable_for_mapping_keys(self) -> None:
        # KB profiles key FactorySignature mappings by role; require hashable.
        mapping: dict[MixerControlRole, str] = {
            MixerControlRole.CAPTURE_MASTER: "x",
            MixerControlRole.PGA_DMIC: "y",
        }
        assert mapping[MixerControlRole.CAPTURE_MASTER] == "x"


class TestMixerSanityDecision:
    """Terminal decision enum — cascade keys off every value."""

    def test_is_strenum(self) -> None:
        assert issubclass(MixerSanityDecision, str)

    def test_all_eight_decisions_present(self) -> None:
        expected = {
            "healed",
            "rolled_back",
            "skipped_healthy",
            "skipped_customized",
            "deferred_no_kb",
            "deferred_ambiguous",
            "deferred_platform",
            "error",
        }
        assert {d.value for d in MixerSanityDecision} == expected

    def test_string_equality(self) -> None:
        assert MixerSanityDecision.HEALED == "healed"
        assert MixerSanityDecision.ROLLED_BACK == "rolled_back"


class TestMixerPresetValue:
    """Tagged-union variants — raw/fraction/db."""

    def test_raw_smoke(self) -> None:
        v = MixerPresetValueRaw(raw=42)
        assert v.raw == 42

    def test_fraction_zero_accepted(self) -> None:
        assert MixerPresetValueFraction(fraction=0.0).fraction == 0.0

    def test_fraction_one_accepted(self) -> None:
        assert MixerPresetValueFraction(fraction=1.0).fraction == 1.0

    def test_fraction_mid_accepted(self) -> None:
        assert MixerPresetValueFraction(fraction=0.5).fraction == 0.5

    def test_fraction_below_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"fraction=-0\.1.*\[0\.0, 1\.0\]"):
            MixerPresetValueFraction(fraction=-0.1)

    def test_fraction_above_one_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"fraction=1\.01.*\[0\.0, 1\.0\]"):
            MixerPresetValueFraction(fraction=1.01)

    def test_db_smoke(self) -> None:
        v = MixerPresetValueDb(db=-12.0)
        assert v.db == -12.0

    def test_all_variants_frozen(self) -> None:
        variants = (
            MixerPresetValueRaw(raw=1),
            MixerPresetValueFraction(fraction=0.5),
            MixerPresetValueDb(db=0.0),
        )
        for v in variants:
            with pytest.raises(Exception):
                v.raw = 99  # type: ignore[misc,union-attr,attr-defined]


class TestMixerPresetControl:
    """MixerPresetControl validates role + channel_policy at construction."""

    def test_well_formed(self) -> None:
        ctl = MixerPresetControl(
            role=MixerControlRole.CAPTURE_MASTER,
            value=MixerPresetValueFraction(fraction=1.0),
        )
        assert ctl.role is MixerControlRole.CAPTURE_MASTER
        assert ctl.channel_policy == "all"  # default

    def test_unknown_role_rejected(self) -> None:
        with pytest.raises(ValueError, match="UNKNOWN is not a valid preset target"):
            MixerPresetControl(
                role=MixerControlRole.UNKNOWN,
                value=MixerPresetValueRaw(raw=0),
            )

    def test_unknown_channel_policy_rejected(self) -> None:
        with pytest.raises(ValueError, match="channel_policy='nope'"):
            MixerPresetControl(
                role=MixerControlRole.CAPTURE_MASTER,
                value=MixerPresetValueRaw(raw=0),
                channel_policy="nope",  # type: ignore[arg-type]
            )

    def test_left_right_equal_policy_accepted(self) -> None:
        ctl = MixerPresetControl(
            role=MixerControlRole.INTERNAL_MIC_BOOST,
            value=MixerPresetValueRaw(raw=0),
            channel_policy="left_right_equal",
        )
        assert ctl.channel_policy == "left_right_equal"


def _good_preset() -> MixerPresetSpec:
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


class TestMixerPresetSpec:
    """MixerPresetSpec enforces non-empty + unique role + literal defaults."""

    def test_well_formed(self) -> None:
        preset = _good_preset()
        assert len(preset.controls) == 2
        assert preset.auto_mute_mode == "leave"
        assert preset.runtime_pm_target == "leave"

    def test_empty_controls_rejected(self) -> None:
        with pytest.raises(ValueError, match="controls must be non-empty"):
            MixerPresetSpec(controls=())

    def test_duplicate_role_rejected(self) -> None:
        dup = (
            MixerPresetControl(
                role=MixerControlRole.CAPTURE_MASTER,
                value=MixerPresetValueFraction(fraction=1.0),
            ),
            MixerPresetControl(
                role=MixerControlRole.CAPTURE_MASTER,
                value=MixerPresetValueFraction(fraction=0.8),
            ),
        )
        with pytest.raises(ValueError, match="'capture_master' appears more than once"):
            MixerPresetSpec(controls=dup)

    def test_unknown_auto_mute_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="auto_mute_mode='on'"):
            MixerPresetSpec(
                controls=_good_preset().controls,
                auto_mute_mode="on",  # type: ignore[arg-type]
            )

    def test_unknown_runtime_pm_target_rejected(self) -> None:
        with pytest.raises(ValueError, match="runtime_pm_target='idle'"):
            MixerPresetSpec(
                controls=_good_preset().controls,
                runtime_pm_target="idle",  # type: ignore[arg-type]
            )

    def test_all_literal_values_accepted(self) -> None:
        for mode in ("disabled", "enabled", "leave"):
            preset = MixerPresetSpec(
                controls=_good_preset().controls,
                auto_mute_mode=mode,  # type: ignore[arg-type]
            )
            assert preset.auto_mute_mode == mode
        for target in ("on", "auto", "leave"):
            preset = MixerPresetSpec(
                controls=_good_preset().controls,
                runtime_pm_target=target,  # type: ignore[arg-type]
            )
            assert preset.runtime_pm_target == target


def _good_gates() -> ValidationGates:
    return ValidationGates(
        rms_dbfs_range=(-30.0, -15.0),
        peak_dbfs_max=-2.0,
        snr_db_vocal_band_min=15.0,
        silero_prob_min=0.5,
        wake_word_stage2_prob_min=0.4,
    )


class TestValidationGates:
    """Post-apply gates — strict boundary validation."""

    def test_well_formed(self) -> None:
        gates = _good_gates()
        assert gates.rms_dbfs_range == (-30.0, -15.0)
        assert gates.silero_prob_min == 0.5

    def test_inverted_rms_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="inverted"):
            ValidationGates(
                rms_dbfs_range=(-10.0, -30.0),
                peak_dbfs_max=-2.0,
                snr_db_vocal_band_min=15.0,
                silero_prob_min=0.5,
                wake_word_stage2_prob_min=0.4,
            )

    def test_positive_rms_bound_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-positive"):
            ValidationGates(
                rms_dbfs_range=(-30.0, 5.0),
                peak_dbfs_max=-2.0,
                snr_db_vocal_band_min=15.0,
                silero_prob_min=0.5,
                wake_word_stage2_prob_min=0.4,
            )

    def test_positive_peak_rejected(self) -> None:
        with pytest.raises(ValueError, match="peak_dbfs_max=3.0.*non-positive"):
            ValidationGates(
                rms_dbfs_range=(-30.0, -15.0),
                peak_dbfs_max=3.0,
                snr_db_vocal_band_min=15.0,
                silero_prob_min=0.5,
                wake_word_stage2_prob_min=0.4,
            )

    def test_negative_snr_rejected(self) -> None:
        with pytest.raises(ValueError, match="snr_db_vocal_band_min=-5.0"):
            ValidationGates(
                rms_dbfs_range=(-30.0, -15.0),
                peak_dbfs_max=-2.0,
                snr_db_vocal_band_min=-5.0,
                silero_prob_min=0.5,
                wake_word_stage2_prob_min=0.4,
            )

    def test_silero_prob_out_of_unit_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"silero_prob_min=1\.5"):
            ValidationGates(
                rms_dbfs_range=(-30.0, -15.0),
                peak_dbfs_max=-2.0,
                snr_db_vocal_band_min=15.0,
                silero_prob_min=1.5,
                wake_word_stage2_prob_min=0.4,
            )

    def test_wake_word_prob_out_of_unit_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"wake_word_stage2_prob_min=-0\.1"):
            ValidationGates(
                rms_dbfs_range=(-30.0, -15.0),
                peak_dbfs_max=-2.0,
                snr_db_vocal_band_min=15.0,
                silero_prob_min=0.5,
                wake_word_stage2_prob_min=-0.1,
            )


class TestFactorySignature:
    """FactorySignature requires at least one expected_*_range."""

    def test_raw_only(self) -> None:
        sig = FactorySignature(
            expected_raw_range=(0, 0),
            expected_fraction_range=None,
            expected_db_range=None,
        )
        assert sig.expected_raw_range == (0, 0)

    def test_fraction_only(self) -> None:
        sig = FactorySignature(
            expected_raw_range=None,
            expected_fraction_range=(0.3, 0.6),
            expected_db_range=None,
        )
        assert sig.expected_fraction_range == (0.3, 0.6)

    def test_db_only(self) -> None:
        sig = FactorySignature(
            expected_raw_range=None,
            expected_fraction_range=None,
            expected_db_range=(-40.0, -20.0),
        )
        assert sig.expected_db_range == (-40.0, -20.0)

    def test_all_none_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one of"):
            FactorySignature(
                expected_raw_range=None,
                expected_fraction_range=None,
                expected_db_range=None,
            )

    def test_inverted_raw_rejected(self) -> None:
        with pytest.raises(ValueError, match="expected_raw_range.*inverted"):
            FactorySignature(
                expected_raw_range=(3, 0),
                expected_fraction_range=None,
                expected_db_range=None,
            )

    def test_inverted_fraction_rejected(self) -> None:
        with pytest.raises(ValueError, match="expected_fraction_range.*inverted"):
            FactorySignature(
                expected_raw_range=None,
                expected_fraction_range=(0.8, 0.2),
                expected_db_range=None,
            )

    def test_out_of_unit_fraction_rejected(self) -> None:
        with pytest.raises(ValueError, match="must lie within"):
            FactorySignature(
                expected_raw_range=None,
                expected_fraction_range=(0.5, 1.2),
                expected_db_range=None,
            )

    def test_inverted_db_rejected(self) -> None:
        with pytest.raises(ValueError, match="expected_db_range.*inverted"):
            FactorySignature(
                expected_raw_range=None,
                expected_fraction_range=None,
                expected_db_range=(0.0, -30.0),
            )


def _good_verification() -> VerificationRecord:
    return VerificationRecord(
        system_product="VJFE69F11X-B0221H",
        codec_id="14F1:5045",
        kernel="6.14.0-37-generic",
        distro="linuxmint-22.2",
        verified_at="2026-04-23",
        verified_by="sovyx-core",
    )


class TestVerificationRecord:
    """Every attestation field must be non-empty (grep-able provenance)."""

    def test_well_formed(self) -> None:
        rec = _good_verification()
        assert rec.verified_by == "sovyx-core"

    @pytest.mark.parametrize(
        "field",
        [
            "system_product",
            "codec_id",
            "kernel",
            "distro",
            "verified_at",
            "verified_by",
        ],
    )
    def test_each_empty_field_rejected(self, field: str) -> None:
        base = {
            "system_product": "VJFE69F11X-B0221H",
            "codec_id": "14F1:5045",
            "kernel": "6.14.0-37",
            "distro": "linuxmint-22.2",
            "verified_at": "2026-04-23",
            "verified_by": "sovyx-core",
        }
        base[field] = ""
        with pytest.raises(ValueError, match=f"{field} must be non-empty"):
            VerificationRecord(**base)  # type: ignore[arg-type]


def _good_kb_profile() -> MixerKBProfile:
    return MixerKBProfile(
        profile_id="vaio_vjfe69_sn6180",
        profile_version=1,
        schema_version=1,
        codec_id_glob="14F1:5045",
        driver_family="hda",
        system_vendor_glob="Sony*",
        system_product_glob="VJFE69*",
        distro_family=None,
        audio_stack="pipewire",
        kernel_major_minor_glob="6.*",
        match_threshold=0.6,
        factory_regime="attenuation",
        factory_signature={
            MixerControlRole.CAPTURE_MASTER: FactorySignature(
                expected_raw_range=None,
                expected_fraction_range=(0.3, 0.6),
                expected_db_range=None,
            ),
            MixerControlRole.INTERNAL_MIC_BOOST: FactorySignature(
                expected_raw_range=(0, 0),
                expected_fraction_range=None,
                expected_db_range=None,
            ),
        },
        recommended_preset=_good_preset(),
        validation_gates=_good_gates(),
        verified_on=(_good_verification(),),
        contributed_by="sovyx-core",
    )


class TestMixerKBProfile:
    """MixerKBProfile is YAML-sourced; validation must be exhaustive."""

    def test_well_formed(self) -> None:
        profile = _good_kb_profile()
        assert profile.profile_id == "vaio_vjfe69_sn6180"
        assert profile.driver_family == "hda"
        assert profile.factory_regime == "attenuation"

    def test_empty_profile_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="profile_id must be non-empty"):
            MixerKBProfile(
                **{**_kb_kwargs(), "profile_id": ""},  # type: ignore[arg-type]
            )

    def test_profile_version_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="profile_version=0"):
            MixerKBProfile(**{**_kb_kwargs(), "profile_version": 0})  # type: ignore[arg-type]

    def test_schema_version_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="schema_version=0"):
            MixerKBProfile(**{**_kb_kwargs(), "schema_version": 0})  # type: ignore[arg-type]

    def test_empty_codec_id_glob_rejected(self) -> None:
        with pytest.raises(ValueError, match="codec_id_glob must be non-empty"):
            MixerKBProfile(**{**_kb_kwargs(), "codec_id_glob": ""})  # type: ignore[arg-type]

    def test_unknown_driver_family_rejected(self) -> None:
        with pytest.raises(ValueError, match="driver_family='firewire'"):
            MixerKBProfile(
                **{**_kb_kwargs(), "driver_family": "firewire"},  # type: ignore[arg-type]
            )

    def test_unknown_audio_stack_rejected(self) -> None:
        with pytest.raises(ValueError, match="audio_stack='oss'"):
            MixerKBProfile(
                **{**_kb_kwargs(), "audio_stack": "oss"},  # type: ignore[arg-type]
            )

    def test_null_audio_stack_accepted(self) -> None:
        profile = MixerKBProfile(**{**_kb_kwargs(), "audio_stack": None})  # type: ignore[arg-type]
        assert profile.audio_stack is None

    def test_unknown_factory_regime_rejected(self) -> None:
        with pytest.raises(ValueError, match="factory_regime='novel'"):
            MixerKBProfile(
                **{**_kb_kwargs(), "factory_regime": "novel"},  # type: ignore[arg-type]
            )

    def test_match_threshold_below_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"match_threshold=-0\.1"):
            MixerKBProfile(**{**_kb_kwargs(), "match_threshold": -0.1})  # type: ignore[arg-type]

    def test_match_threshold_above_one_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"match_threshold=1\.5"):
            MixerKBProfile(**{**_kb_kwargs(), "match_threshold": 1.5})  # type: ignore[arg-type]

    def test_empty_factory_signature_rejected(self) -> None:
        with pytest.raises(ValueError, match="factory_signature must be non-empty"):
            MixerKBProfile(**{**_kb_kwargs(), "factory_signature": {}})  # type: ignore[arg-type]

    def test_unknown_role_in_signature_rejected(self) -> None:
        bad_sig = {
            MixerControlRole.UNKNOWN: FactorySignature(
                expected_raw_range=(0, 1),
                expected_fraction_range=None,
                expected_db_range=None,
            ),
        }
        with pytest.raises(ValueError, match="must not contain"):
            MixerKBProfile(
                **{**_kb_kwargs(), "factory_signature": bad_sig},  # type: ignore[arg-type]
            )

    def test_empty_verified_on_rejected(self) -> None:
        with pytest.raises(ValueError, match="verified_on must be non-empty"):
            MixerKBProfile(**{**_kb_kwargs(), "verified_on": ()})  # type: ignore[arg-type]

    def test_empty_contributed_by_rejected(self) -> None:
        with pytest.raises(ValueError, match="contributed_by must be non-empty"):
            MixerKBProfile(**{**_kb_kwargs(), "contributed_by": ""})  # type: ignore[arg-type]

    def test_frozen(self) -> None:
        profile = _good_kb_profile()
        with pytest.raises(Exception):
            profile.profile_id = "x"  # type: ignore[misc]


def _kb_kwargs() -> dict[str, object]:
    """Return constructor kwargs for a valid KB profile — tests mutate one at a time."""
    return {
        "profile_id": "vaio_vjfe69_sn6180",
        "profile_version": 1,
        "schema_version": 1,
        "codec_id_glob": "14F1:5045",
        "driver_family": "hda",
        "system_vendor_glob": "Sony*",
        "system_product_glob": "VJFE69*",
        "distro_family": None,
        "audio_stack": "pipewire",
        "kernel_major_minor_glob": "6.*",
        "match_threshold": 0.6,
        "factory_regime": "attenuation",
        "factory_signature": {
            MixerControlRole.CAPTURE_MASTER: FactorySignature(
                expected_raw_range=None,
                expected_fraction_range=(0.3, 0.6),
                expected_db_range=None,
            ),
        },
        "recommended_preset": _good_preset(),
        "validation_gates": _good_gates(),
        "verified_on": (_good_verification(),),
        "contributed_by": "sovyx-core",
    }


class TestMixerValidationMetrics:
    """Pure-output dataclass — construction smoke + frozen invariant."""

    def test_construct(self) -> None:
        metrics = MixerValidationMetrics(
            rms_dbfs=-22.0,
            peak_dbfs=-4.0,
            snr_db_vocal_band=18.0,
            silero_max_prob=0.87,
            silero_mean_prob=0.42,
            wake_word_stage2_prob=0.55,
            measurement_duration_ms=2000,
        )
        assert metrics.silero_max_prob == 0.87
        assert metrics.measurement_duration_ms == 2000

    def test_frozen(self) -> None:
        metrics = MixerValidationMetrics(
            rms_dbfs=-22.0,
            peak_dbfs=-4.0,
            snr_db_vocal_band=18.0,
            silero_max_prob=0.87,
            silero_mean_prob=0.42,
            wake_word_stage2_prob=0.55,
            measurement_duration_ms=2000,
        )
        with pytest.raises(Exception):
            metrics.rms_dbfs = 0.0  # type: ignore[misc]


class TestMixerSanityResult:
    """Terminal result record — construction + optional fields."""

    def test_skipped_healthy_shape(self) -> None:
        result = MixerSanityResult(
            decision=MixerSanityDecision.SKIPPED_HEALTHY,
            diagnosis_before=Diagnosis.HEALTHY,
            diagnosis_after=None,
            regime="healthy",
            matched_kb_profile=None,
            kb_match_score=0.0,
            user_customization_score=0.0,
            cards_probed=(0,),
            controls_modified=(),
            rollback_snapshot=None,
            probe_duration_ms=120,
            apply_duration_ms=None,
            validation_passed=None,
            validation_metrics=None,
        )
        assert result.decision is MixerSanityDecision.SKIPPED_HEALTHY
        assert result.rollback_snapshot is None
        assert result.remediation is None
        assert result.error is None

    def test_healed_shape_with_metrics(self) -> None:
        snap = MixerApplySnapshot(
            card_index=0,
            reverted_controls=(("Capture", 40),),
            applied_controls=(("Capture", 80),),
        )
        metrics = MixerValidationMetrics(
            rms_dbfs=-22.0,
            peak_dbfs=-4.0,
            snr_db_vocal_band=18.0,
            silero_max_prob=0.87,
            silero_mean_prob=0.42,
            wake_word_stage2_prob=0.55,
            measurement_duration_ms=2000,
        )
        result = MixerSanityResult(
            decision=MixerSanityDecision.HEALED,
            diagnosis_before=Diagnosis.MIXER_ZEROED,
            diagnosis_after=Diagnosis.HEALTHY,
            regime="attenuation",
            matched_kb_profile="vaio_vjfe69_sn6180",
            kb_match_score=0.87,
            user_customization_score=0.12,
            cards_probed=(0,),
            controls_modified=("Capture", "Internal Mic Boost"),
            rollback_snapshot=snap,
            probe_duration_ms=150,
            apply_duration_ms=120,
            validation_passed=True,
            validation_metrics=metrics,
        )
        assert result.validation_passed is True
        assert result.matched_kb_profile == "vaio_vjfe69_sn6180"
        assert result.rollback_snapshot is snap

    def test_error_shape(self) -> None:
        result = MixerSanityResult(
            decision=MixerSanityDecision.ERROR,
            diagnosis_before=Diagnosis.UNKNOWN,
            diagnosis_after=None,
            regime="unknown",
            matched_kb_profile=None,
            kb_match_score=0.0,
            user_customization_score=0.0,
            cards_probed=(),
            controls_modified=(),
            rollback_snapshot=None,
            probe_duration_ms=0,
            apply_duration_ms=None,
            validation_passed=None,
            validation_metrics=None,
            error="MIXER_SANITY_PROBE_FAILED",
        )
        assert result.error == "MIXER_SANITY_PROBE_FAILED"

    def test_frozen(self) -> None:
        result = MixerSanityResult(
            decision=MixerSanityDecision.SKIPPED_HEALTHY,
            diagnosis_before=Diagnosis.HEALTHY,
            diagnosis_after=None,
            regime="healthy",
            matched_kb_profile=None,
            kb_match_score=0.0,
            user_customization_score=0.0,
            cards_probed=(0,),
            controls_modified=(),
            rollback_snapshot=None,
            probe_duration_ms=120,
            apply_duration_ms=None,
            validation_passed=None,
            validation_metrics=None,
        )
        with pytest.raises(Exception):
            result.decision = MixerSanityDecision.ERROR  # type: ignore[misc]


class TestHardwareContext:
    """HardwareContext carries detected audio-hardware identity to L2.5."""

    def test_driver_family_only(self) -> None:
        hw = HardwareContext(driver_family="hda")
        assert hw.driver_family == "hda"
        assert hw.codec_id is None
        assert hw.audio_stack is None

    def test_full_fields(self) -> None:
        hw = HardwareContext(
            driver_family="hda",
            codec_id="14F1:5045",
            system_vendor="Sony Group Corporation",
            system_product="VJFE69F11X-B0221H",
            distro="linuxmint-22.2",
            audio_stack="pipewire",
            kernel="6.14.0-37-generic",
        )
        assert hw.codec_id == "14F1:5045"
        assert hw.audio_stack == "pipewire"

    @pytest.mark.parametrize(
        "family",
        ["hda", "sof", "usb-audio", "bt", "unknown"],
    )
    def test_all_valid_driver_families(self, family: str) -> None:
        hw = HardwareContext(driver_family=family)  # type: ignore[arg-type]
        assert hw.driver_family == family

    def test_unknown_driver_family_rejected(self) -> None:
        with pytest.raises(ValueError, match="driver_family='firewire'"):
            HardwareContext(driver_family="firewire")  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "stack",
        ["pipewire", "pulseaudio", "alsa"],
    )
    def test_all_valid_audio_stacks(self, stack: str) -> None:
        hw = HardwareContext(driver_family="hda", audio_stack=stack)  # type: ignore[arg-type]
        assert hw.audio_stack == stack

    def test_unknown_audio_stack_rejected(self) -> None:
        with pytest.raises(ValueError, match="audio_stack='oss'"):
            HardwareContext(driver_family="hda", audio_stack="oss")  # type: ignore[arg-type]

    def test_unknown_driver_family_for_detection_failure(self) -> None:
        # First-boot / exotic-hardware detection failure → "unknown" is valid.
        hw = HardwareContext(driver_family="unknown")
        assert hw.driver_family == "unknown"

    def test_frozen(self) -> None:
        hw = HardwareContext(driver_family="hda")
        with pytest.raises(Exception):
            hw.codec_id = "x"  # type: ignore[misc]
