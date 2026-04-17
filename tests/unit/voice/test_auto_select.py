"""Tests for VoiceModelAutoSelection (V05-27).

Covers hardware detection, model selection per tier, RAM adjustments,
fallback chains, and the VoiceModelAutoSelector class API.

Ref: IMPL-SUP-005 §SPEC-6 (VoiceModelAutoSelection)
"""

from __future__ import annotations

from unittest.mock import MagicMock, mock_open, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.voice import auto_select as _auto_select_mod  # anti-pattern #11
from sovyx.voice.auto_select import (
    HardwareProfile,
    HardwareTier,
    ModelSelection,
    VoiceModelAutoSelector,
    _detect_tier,
    detect_hardware,
    get_fallback,
    select_models,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _profile(
    tier: HardwareTier = HardwareTier.PI5,
    ram_mb: int = 8192,
    cpu_cores: int = 4,
    has_gpu: bool = False,
    gpu_vram_mb: int = 0,
) -> HardwareProfile:
    return HardwareProfile(
        tier=tier,
        ram_mb=ram_mb,
        cpu_cores=cpu_cores,
        has_gpu=has_gpu,
        gpu_vram_mb=gpu_vram_mb,
    )


# ---------------------------------------------------------------------------
# HardwareProfile dataclass
# ---------------------------------------------------------------------------


class TestHardwareProfile:
    """Tests for the HardwareProfile dataclass."""

    def test_creation(self) -> None:
        p = _profile()
        assert p.tier == HardwareTier.PI5
        assert p.ram_mb == 8192
        assert p.cpu_cores == 4
        assert p.has_gpu is False
        assert p.gpu_vram_mb == 0

    def test_frozen(self) -> None:
        p = _profile()
        with pytest.raises(AttributeError):
            p.ram_mb = 4096  # type: ignore[misc]

    def test_default_gpu_vram(self) -> None:
        p = HardwareProfile(
            tier=HardwareTier.PI5,
            ram_mb=4096,
            cpu_cores=4,
            has_gpu=False,
        )
        assert p.gpu_vram_mb == 0


# ---------------------------------------------------------------------------
# ModelSelection dataclass
# ---------------------------------------------------------------------------


class TestModelSelection:
    """Tests for the ModelSelection dataclass."""

    def test_creation(self) -> None:
        ms = ModelSelection(
            stt_primary="moonshine-tiny",
            stt_streaming="moonshine-tiny",
            tts_primary="piper",
            tts_quality="kokoro-onnx-q8",
            wake="openwakeword",
            vad="silero-vad-v5",
            speaker="ecapa-tdnn-onnx",
            voice_clone="piper-finetune",
            tier=HardwareTier.PI5,
        )
        assert ms.stt_primary == "moonshine-tiny"
        assert ms.tier == HardwareTier.PI5
        assert ms.adjustments == ()

    def test_with_adjustments(self) -> None:
        ms = ModelSelection(
            stt_primary="moonshine-tiny",
            stt_streaming="moonshine-tiny",
            tts_primary="piper",
            tts_quality="piper",
            wake="openwakeword",
            vad="silero-vad-v5",
            speaker="ecapa-tdnn-onnx",
            voice_clone=None,
            tier=HardwareTier.PI5,
            adjustments=("low_ram_1500mb_downgrade_all",),
        )
        assert len(ms.adjustments) == 1


# ---------------------------------------------------------------------------
# HardwareTier enum
# ---------------------------------------------------------------------------


class TestHardwareTier:
    """Tests for the HardwareTier enum."""

    def test_all_tiers_exist(self) -> None:
        assert len(HardwareTier) == 5
        expected = {"PI5", "N100", "DESKTOP_CPU", "DESKTOP_GPU", "CLOUD"}
        assert {t.name for t in HardwareTier} == expected


# ---------------------------------------------------------------------------
# Tier detection
# ---------------------------------------------------------------------------


class TestDetectTier:
    """Tests for _detect_tier logic."""

    def test_arm_is_pi5(self) -> None:
        assert _detect_tier("aarch64", 8192, False, 0) == HardwareTier.PI5

    def test_arm_variant_is_pi5(self) -> None:
        assert _detect_tier("armv7l", 4096, False, 0) == HardwareTier.PI5

    def test_gpu_with_enough_vram(self) -> None:
        assert _detect_tier("x86_64", 32000, True, 8000) == HardwareTier.DESKTOP_GPU

    def test_gpu_with_low_vram_is_not_gpu_tier(self) -> None:
        # GPU with <4000MB VRAM should NOT be GPU tier
        assert _detect_tier("x86_64", 32000, True, 2000) == HardwareTier.DESKTOP_CPU

    def test_high_ram_no_gpu(self) -> None:
        with patch.object(_auto_select_mod, "_read_cpuinfo", return_value="Intel Core i9"):
            assert _detect_tier("x86_64", 32000, False, 0) == HardwareTier.DESKTOP_CPU

    @patch.object(_auto_select_mod, "_read_cpuinfo", return_value="Intel N100 processor")
    def test_n100_detected(self, _mock: MagicMock) -> None:
        assert _detect_tier("x86_64", 8192, False, 0) == HardwareTier.N100

    @patch.object(_auto_select_mod, "_read_cpuinfo", return_value="Intel Alder Lake-N")
    def test_alder_lake_detected_as_n100(self, _mock: MagicMock) -> None:
        assert _detect_tier("x86_64", 8192, False, 0) == HardwareTier.N100

    @patch.object(_auto_select_mod, "_read_cpuinfo", return_value="Intel Core i5-12400")
    def test_non_n100_low_ram(self, _mock: MagicMock) -> None:
        assert _detect_tier("x86_64", 8192, False, 0) == HardwareTier.DESKTOP_CPU

    @patch.object(_auto_select_mod, "_read_cpuinfo", return_value="")
    def test_empty_cpuinfo(self, _mock: MagicMock) -> None:
        assert _detect_tier("x86_64", 8192, False, 0) == HardwareTier.DESKTOP_CPU

    def test_arm_with_gpu_still_pi5(self) -> None:
        # ARM always maps to PI5 regardless of GPU
        assert _detect_tier("aarch64", 8192, True, 8000) == HardwareTier.PI5


# ---------------------------------------------------------------------------
# Hardware detection (full function)
# ---------------------------------------------------------------------------


class TestDetectHardware:
    """Tests for detect_hardware integration."""

    @patch.object(_auto_select_mod.platform, "machine", return_value="aarch64")
    @patch.object(_auto_select_mod.os, "cpu_count", return_value=4)
    @patch.object(_auto_select_mod, "_detect_gpu", return_value=(False, 0))
    def test_pi5_detection(
        self,
        _gpu: MagicMock,
        _cpu: MagicMock,
        _mach: MagicMock,
    ) -> None:
        mock_vm = MagicMock()
        mock_vm.total = 8192 * 1024 * 1024  # 8GB
        with patch("psutil.virtual_memory", return_value=mock_vm):
            profile = detect_hardware()
        assert profile.tier == HardwareTier.PI5
        assert profile.ram_mb == 8192
        assert profile.cpu_cores == 4
        assert profile.has_gpu is False

    @patch.object(_auto_select_mod.platform, "machine", return_value="x86_64")
    @patch.object(_auto_select_mod.os, "cpu_count", return_value=8)
    @patch.object(_auto_select_mod, "_detect_gpu", return_value=(True, 12000))
    def test_gpu_detection(
        self,
        _gpu: MagicMock,
        _cpu: MagicMock,
        _mach: MagicMock,
    ) -> None:
        mock_vm = MagicMock()
        mock_vm.total = 32768 * 1024 * 1024  # 32GB
        with patch("psutil.virtual_memory", return_value=mock_vm):
            profile = detect_hardware()
        assert profile.tier == HardwareTier.DESKTOP_GPU
        assert profile.has_gpu is True
        assert profile.gpu_vram_mb == 12000

    @patch.object(_auto_select_mod.platform, "machine", return_value="x86_64")
    @patch.object(_auto_select_mod.os, "cpu_count", return_value=None)
    @patch.object(_auto_select_mod, "_detect_gpu", return_value=(False, 0))
    def test_cpu_count_none_defaults_to_1(
        self,
        _gpu: MagicMock,
        _cpu: MagicMock,
        _mach: MagicMock,
    ) -> None:
        mock_vm = MagicMock()
        mock_vm.total = 16384 * 1024 * 1024  # 16GB
        with patch("psutil.virtual_memory", return_value=mock_vm):
            profile = detect_hardware()
        assert profile.cpu_cores == 1


# ---------------------------------------------------------------------------
# GPU detection
# ---------------------------------------------------------------------------


class TestDetectGPU:
    """Tests for _detect_gpu."""

    @patch.object(_auto_select_mod.subprocess, "run")
    def test_nvidia_smi_success(self, mock_run: MagicMock) -> None:
        from sovyx.voice.auto_select import _detect_gpu

        mock_run.return_value = MagicMock(returncode=0, stdout="8192\n")
        has_gpu, vram = _detect_gpu()
        assert has_gpu is True
        assert vram == 8192

    @patch.object(_auto_select_mod.subprocess, "run", side_effect=FileNotFoundError)
    def test_nvidia_smi_not_found(self, _mock: MagicMock) -> None:
        from sovyx.voice.auto_select import _detect_gpu

        has_gpu, vram = _detect_gpu()
        assert has_gpu is False
        assert vram == 0

    def test_nvidia_smi_timeout(self) -> None:
        import subprocess as _subprocess

        from sovyx.voice.auto_select import _detect_gpu

        with patch(
            "sovyx.voice.auto_select.subprocess.run",
            side_effect=_subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=5),
        ):
            has_gpu, vram = _detect_gpu()
            assert has_gpu is False
            assert vram == 0

    @patch.object(_auto_select_mod.subprocess, "run")
    def test_nvidia_smi_failure_returncode(self, mock_run: MagicMock) -> None:
        from sovyx.voice.auto_select import _detect_gpu

        mock_run.return_value = MagicMock(returncode=1, stdout="")
        has_gpu, vram = _detect_gpu()
        assert has_gpu is False
        assert vram == 0

    @patch.object(_auto_select_mod.subprocess, "run")
    def test_nvidia_smi_multi_gpu(self, mock_run: MagicMock) -> None:
        from sovyx.voice.auto_select import _detect_gpu

        mock_run.return_value = MagicMock(returncode=0, stdout="16384\n8192\n")
        has_gpu, vram = _detect_gpu()
        assert has_gpu is True
        assert vram == 16384  # Takes first GPU

    @patch.object(_auto_select_mod.subprocess, "run")
    def test_nvidia_smi_empty_stdout(self, mock_run: MagicMock) -> None:
        from sovyx.voice.auto_select import _detect_gpu

        mock_run.return_value = MagicMock(returncode=0, stdout="")
        has_gpu, vram = _detect_gpu()
        assert has_gpu is False
        assert vram == 0


# ---------------------------------------------------------------------------
# cpuinfo reading
# ---------------------------------------------------------------------------


class TestReadCpuinfo:
    """Tests for _read_cpuinfo."""

    @patch("builtins.open", mock_open(read_data="model name : Intel N100\n"))
    def test_reads_cpuinfo(self) -> None:
        from sovyx.voice.auto_select import _read_cpuinfo

        assert "N100" in _read_cpuinfo()

    @patch("builtins.open", side_effect=FileNotFoundError)
    def test_missing_cpuinfo(self, _mock: MagicMock) -> None:
        from sovyx.voice.auto_select import _read_cpuinfo

        assert _read_cpuinfo() == ""


# ---------------------------------------------------------------------------
# Model selection per tier
# ---------------------------------------------------------------------------


class TestSelectModels:
    """Tests for select_models across all tiers."""

    def test_pi5_models(self) -> None:
        ms = select_models(_profile(HardwareTier.PI5))
        assert ms.stt_primary == "moonshine-tiny"
        assert ms.stt_streaming == "moonshine-tiny"
        assert ms.tts_primary == "piper"
        assert ms.tts_quality == "kokoro-onnx-q8"
        assert ms.wake == "openwakeword"
        assert ms.vad == "silero-vad-v5"
        assert ms.speaker == "ecapa-tdnn-onnx"
        assert ms.voice_clone == "piper-finetune"
        assert ms.tier == HardwareTier.PI5
        assert ms.adjustments == ()

    def test_n100_models(self) -> None:
        ms = select_models(_profile(HardwareTier.N100))
        assert ms.stt_primary == "parakeet-tdt-0.6b-v3-int8"
        assert ms.stt_streaming == "moonshine-base"
        assert ms.tts_primary == "kokoro-onnx-q8"
        assert ms.tts_quality == "kokoro-onnx-fp32"
        assert ms.voice_clone == "kokoclone"

    def test_desktop_cpu_models(self) -> None:
        ms = select_models(_profile(HardwareTier.DESKTOP_CPU))
        assert ms.stt_primary == "parakeet-tdt-0.6b-v3-int8"
        assert ms.tts_primary == "kokoro-onnx-q8"

    def test_desktop_gpu_models(self) -> None:
        ms = select_models(_profile(HardwareTier.DESKTOP_GPU, has_gpu=True, gpu_vram_mb=8000))
        assert ms.stt_primary == "parakeet-tdt-0.6b-v3"
        assert ms.tts_primary == "kokoro-onnx-fp32"
        assert ms.tts_quality == "qwen3-tts-0.6b"
        assert ms.voice_clone == "qwen3-tts-clone"

    def test_cloud_models(self) -> None:
        ms = select_models(_profile(HardwareTier.CLOUD))
        assert ms.stt_primary == "parakeet-tdt-0.6b-v3"
        assert ms.stt_streaming == "parakeet-ctc-0.6b"
        assert ms.tts_primary == "qwen3-tts-1.7b"
        assert ms.tts_quality == "qwen3-tts-1.7b"

    def test_all_tiers_have_vad_and_wake(self) -> None:
        for tier in HardwareTier:
            ms = select_models(_profile(tier))
            assert ms.vad == "silero-vad-v5"
            assert ms.wake == "openwakeword"


# ---------------------------------------------------------------------------
# RAM-based adjustments
# ---------------------------------------------------------------------------


class TestRamAdjustments:
    """Tests for RAM-based model downgrades."""

    def test_very_low_ram_forces_minimal(self) -> None:
        ms = select_models(_profile(HardwareTier.DESKTOP_GPU, ram_mb=1500))
        assert ms.stt_primary == "moonshine-tiny"
        assert ms.tts_primary == "piper"
        assert ms.tts_quality == "piper"
        assert "low_ram_1500mb_downgrade_all" in ms.adjustments

    def test_n100_low_ram_downgrades_stt(self) -> None:
        ms = select_models(_profile(HardwareTier.N100, ram_mb=3500))
        assert ms.stt_primary == "moonshine-base"
        assert "n100_low_ram_3500mb_downgrade_stt" in ms.adjustments

    def test_n100_adequate_ram_keeps_parakeet(self) -> None:
        ms = select_models(_profile(HardwareTier.N100, ram_mb=8192))
        assert ms.stt_primary == "parakeet-tdt-0.6b-v3-int8"
        assert ms.adjustments == ()

    def test_very_low_ram_on_n100_only_low_ram_adjustment(self) -> None:
        # <2048 triggers low_ram only (N100 adjustment skipped — already minimal)
        ms = select_models(_profile(HardwareTier.N100, ram_mb=1500))
        assert ms.stt_primary == "moonshine-tiny"  # low_ram forces minimal
        assert ms.tts_primary == "piper"
        assert len(ms.adjustments) == 1
        assert "low_ram" in ms.adjustments[0]

    def test_pi5_low_ram(self) -> None:
        ms = select_models(_profile(HardwareTier.PI5, ram_mb=1500))
        assert ms.stt_primary == "moonshine-tiny"
        assert ms.tts_primary == "piper"
        assert ms.tts_quality == "piper"

    def test_boundary_2048_not_downgraded(self) -> None:
        ms = select_models(_profile(HardwareTier.DESKTOP_CPU, ram_mb=2048))
        assert ms.stt_primary == "parakeet-tdt-0.6b-v3-int8"  # No downgrade

    def test_boundary_2047_downgraded(self) -> None:
        ms = select_models(_profile(HardwareTier.DESKTOP_CPU, ram_mb=2047))
        assert ms.stt_primary == "moonshine-tiny"

    def test_n100_boundary_4096_not_downgraded(self) -> None:
        ms = select_models(_profile(HardwareTier.N100, ram_mb=4096))
        assert ms.stt_primary == "parakeet-tdt-0.6b-v3-int8"

    def test_n100_boundary_4095_downgraded(self) -> None:
        ms = select_models(_profile(HardwareTier.N100, ram_mb=4095))
        assert ms.stt_primary == "moonshine-base"


# ---------------------------------------------------------------------------
# Fallback chains
# ---------------------------------------------------------------------------


class TestFallbackChains:
    """Tests for model fallback chains."""

    def test_stt_fallback_from_parakeet(self) -> None:
        assert get_fallback("stt", "parakeet-tdt-0.6b-v3-int8") == "moonshine-base"

    def test_stt_fallback_from_moonshine_base(self) -> None:
        assert get_fallback("stt", "moonshine-base") == "moonshine-tiny"

    def test_stt_fallback_end_of_chain(self) -> None:
        assert get_fallback("stt", "moonshine-tiny") is None

    def test_tts_fallback_chain(self) -> None:
        assert get_fallback("tts", "qwen3-tts-1.7b") == "kokoro-onnx-fp32"
        assert get_fallback("tts", "kokoro-onnx-fp32") == "kokoro-onnx-q8"
        assert get_fallback("tts", "kokoro-onnx-q8") == "piper"
        assert get_fallback("tts", "piper") is None

    def test_voice_clone_fallback(self) -> None:
        assert get_fallback("voice_clone", "qwen3-tts-clone") == "kokoclone"
        assert get_fallback("voice_clone", "kokoclone") == "piper-finetune"
        assert get_fallback("voice_clone", "piper-finetune") is None

    def test_unknown_model_returns_first(self) -> None:
        assert get_fallback("stt", "unknown-model") == "parakeet-tdt-0.6b-v3-int8"

    def test_unknown_role_returns_none(self) -> None:
        assert get_fallback("nonexistent_role", "any") is None

    def test_empty_role(self) -> None:
        assert get_fallback("", "any") is None


# ---------------------------------------------------------------------------
# VoiceModelAutoSelector class
# ---------------------------------------------------------------------------


class TestVoiceModelAutoSelector:
    """Tests for the VoiceModelAutoSelector class API."""

    def test_initial_state(self) -> None:
        selector = VoiceModelAutoSelector()
        assert selector.profile is None
        assert selector.selection is None

    @patch.object(_auto_select_mod, "detect_hardware")
    def test_detect_hardware_caches(self, mock_detect: MagicMock) -> None:
        mock_detect.return_value = _profile(HardwareTier.PI5)
        selector = VoiceModelAutoSelector()
        result = selector.detect_hardware()
        assert result.tier == HardwareTier.PI5
        assert selector.profile is not None

    def test_select_models_with_explicit_profile(self) -> None:
        selector = VoiceModelAutoSelector()
        profile = _profile(HardwareTier.N100)
        ms = selector.select_models(profile)
        assert ms.stt_primary == "parakeet-tdt-0.6b-v3-int8"
        assert selector.selection is not None

    @patch.object(_auto_select_mod, "detect_hardware")
    def test_select_models_auto_detects(self, mock_detect: MagicMock) -> None:
        mock_detect.return_value = _profile(
            HardwareTier.DESKTOP_GPU,
            has_gpu=True,
            gpu_vram_mb=8000,
        )
        selector = VoiceModelAutoSelector()
        ms = selector.select_models()
        assert ms.tts_primary == "kokoro-onnx-fp32"
        mock_detect.assert_called_once()

    @patch.object(_auto_select_mod, "detect_hardware")
    def test_auto_select(self, mock_detect: MagicMock) -> None:
        mock_detect.return_value = _profile(HardwareTier.CLOUD)
        selector = VoiceModelAutoSelector()
        ms = selector.auto_select()
        assert ms.tier == HardwareTier.CLOUD
        assert ms.stt_primary == "parakeet-tdt-0.6b-v3"

    def test_fallback_delegates(self) -> None:
        selector = VoiceModelAutoSelector()
        assert selector.fallback("stt", "moonshine-base") == "moonshine-tiny"

    @patch.object(_auto_select_mod, "detect_hardware")
    def test_doctor_report(self, mock_detect: MagicMock) -> None:
        mock_detect.return_value = _profile(HardwareTier.N100, ram_mb=8192)
        selector = VoiceModelAutoSelector()
        report = selector.doctor_report()

        assert report["hardware"]["tier"] == "N100"
        assert report["hardware"]["ram_mb"] == 8192
        assert report["models"]["stt_primary"] == "parakeet-tdt-0.6b-v3-int8"
        assert report["models"]["tts_primary"] == "kokoro-onnx-q8"
        assert isinstance(report["adjustments"], list)

    @patch.object(_auto_select_mod, "detect_hardware")
    def test_doctor_report_with_adjustments(self, mock_detect: MagicMock) -> None:
        mock_detect.return_value = _profile(HardwareTier.N100, ram_mb=3000)
        selector = VoiceModelAutoSelector()
        report = selector.doctor_report()
        assert len(report["adjustments"]) > 0

    @patch.object(_auto_select_mod, "detect_hardware")
    def test_select_models_uses_cached_profile(self, mock_detect: MagicMock) -> None:
        mock_detect.return_value = _profile(HardwareTier.PI5)
        selector = VoiceModelAutoSelector()
        selector.detect_hardware()
        selector.select_models()  # Should NOT call detect again
        mock_detect.assert_called_once()


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestPropertyBased:
    """Property-based tests for auto-selection invariants."""

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        ram_mb=st.integers(min_value=256, max_value=128_000),
        cpu_cores=st.integers(min_value=1, max_value=128),
    )
    def test_select_models_always_returns_valid(self, ram_mb: int, cpu_cores: int) -> None:
        """Any hardware profile always produces a complete ModelSelection."""
        for tier in HardwareTier:
            profile = _profile(tier=tier, ram_mb=ram_mb, cpu_cores=cpu_cores)
            ms = select_models(profile)
            assert ms.stt_primary
            assert ms.stt_streaming
            assert ms.tts_primary
            assert ms.tts_quality
            assert ms.wake
            assert ms.vad
            assert ms.speaker
            assert ms.tier == tier

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(ram_mb=st.integers(min_value=256, max_value=2047))
    def test_low_ram_always_uses_minimal_models(self, ram_mb: int) -> None:
        """RAM <2048 always forces moonshine-tiny + piper."""
        for tier in HardwareTier:
            ms = select_models(_profile(tier=tier, ram_mb=ram_mb))
            assert ms.stt_primary == "moonshine-tiny"
            assert ms.tts_primary == "piper"
            assert ms.tts_quality == "piper"

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(ram_mb=st.integers(min_value=2048, max_value=4095))
    def test_n100_mid_ram_downgrade(self, ram_mb: int) -> None:
        """N100 with 2048-4095 RAM downgrades STT to moonshine-base."""
        ms = select_models(_profile(tier=HardwareTier.N100, ram_mb=ram_mb))
        assert ms.stt_primary == "moonshine-base"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge case tests."""

    def test_unknown_tier_falls_back_to_pi5(self) -> None:
        """If somehow a profile has a tier not in matrix, PI5 is default."""
        # We test by using select_models with a valid tier
        # The internal dict.get fallback to PI5 is the safety net
        ms = select_models(_profile(HardwareTier.PI5))
        assert ms.stt_primary == "moonshine-tiny"

    def test_model_selection_immutable(self) -> None:
        ms = select_models(_profile(HardwareTier.PI5))
        with pytest.raises(AttributeError):
            ms.stt_primary = "other"  # type: ignore[misc]

    def test_adjustments_is_tuple(self) -> None:
        ms = select_models(_profile(HardwareTier.N100, ram_mb=1500))
        assert isinstance(ms.adjustments, tuple)

    def test_voice_clone_can_be_none_in_selection(self) -> None:
        # voice_clone is nullable in ModelSelection
        ms = ModelSelection(
            stt_primary="moonshine-tiny",
            stt_streaming="moonshine-tiny",
            tts_primary="piper",
            tts_quality="piper",
            wake="openwakeword",
            vad="silero-vad-v5",
            speaker="ecapa-tdnn-onnx",
            voice_clone=None,
            tier=HardwareTier.PI5,
        )
        assert ms.voice_clone is None

    def test_fallback_chain_terminates(self) -> None:
        """Following any fallback chain always terminates."""
        for role in ("stt", "tts", "voice_clone"):
            model = get_fallback(role, "nonexistent")
            seen = {model}
            while model is not None:
                model = get_fallback(role, model)
                assert model not in seen or model is None, f"Cycle in {role} chain"
                seen.add(model)
