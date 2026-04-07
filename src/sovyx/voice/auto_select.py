"""VoiceModelAutoSelection — Hardware detection and optimal model selection.

Probes system capabilities at startup and selects the best voice model
combination for STT, TTS, VAD, wake word, and speaker identification.

Includes fallback chains for graceful degradation when primary models fail.

Ref: IMPL-SUP-005 §SPEC-6 (VoiceModelAutoSelection), ADR-002 (hardware tiers)
"""

from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NVIDIA_SMI_TIMEOUT_S = 5
_MIN_GPU_VRAM_MB = 4000
_HIGH_RAM_THRESHOLD_MB = 16_000
_LOW_RAM_THRESHOLD_MB = 2048
_N100_LOW_RAM_THRESHOLD_MB = 4096


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class HardwareTier(Enum):
    """Supported hardware tiers for voice processing."""

    PI5 = auto()  # BCM2712, Cortex A76, 4-8GB
    N100 = auto()  # Intel Alder Lake-N, 8-16GB
    DESKTOP_CPU = auto()  # Modern x86, no GPU
    DESKTOP_GPU = auto()  # x86 + NVIDIA GPU
    CLOUD = auto()  # Cloud instance with GPU


@dataclass(frozen=True, slots=True)
class HardwareProfile:
    """Detected hardware capabilities.

    Attributes:
        tier: Detected hardware tier.
        ram_mb: Total RAM in megabytes.
        cpu_cores: Number of CPU cores.
        has_gpu: Whether an NVIDIA GPU is present.
        gpu_vram_mb: GPU VRAM in megabytes (0 if no GPU).
    """

    tier: HardwareTier
    ram_mb: int
    cpu_cores: int
    has_gpu: bool
    gpu_vram_mb: int = 0


@dataclass(frozen=True, slots=True)
class ModelSelection:
    """Selected voice models for a hardware profile.

    Attributes:
        stt_primary: Primary speech-to-text model.
        stt_streaming: Streaming STT model.
        tts_primary: Primary text-to-speech model.
        tts_quality: High-quality TTS model (may be same as primary).
        wake: Wake word detection model.
        vad: Voice activity detection model.
        speaker: Speaker identification model.
        voice_clone: Voice cloning model (None if unsupported).
        tier: Hardware tier these models were selected for.
        adjustments: List of adjustments made from base selection.
    """

    stt_primary: str
    stt_streaming: str
    tts_primary: str
    tts_quality: str
    wake: str
    vad: str
    speaker: str
    voice_clone: str | None
    tier: HardwareTier
    adjustments: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Model matrix
# ---------------------------------------------------------------------------

_MODEL_MATRIX: dict[HardwareTier, dict[str, str | None]] = {
    HardwareTier.PI5: {
        "stt_primary": "moonshine-tiny",
        "stt_streaming": "moonshine-tiny",
        "tts_primary": "piper",
        "tts_quality": "kokoro-onnx-q8",
        "wake": "openwakeword",
        "vad": "silero-vad-v5",
        "speaker": "ecapa-tdnn-onnx",
        "voice_clone": "piper-finetune",
    },
    HardwareTier.N100: {
        "stt_primary": "parakeet-tdt-0.6b-v3-int8",
        "stt_streaming": "moonshine-base",
        "tts_primary": "kokoro-onnx-q8",
        "tts_quality": "kokoro-onnx-fp32",
        "wake": "openwakeword",
        "vad": "silero-vad-v5",
        "speaker": "ecapa-tdnn-onnx",
        "voice_clone": "kokoclone",
    },
    HardwareTier.DESKTOP_CPU: {
        "stt_primary": "parakeet-tdt-0.6b-v3-int8",
        "stt_streaming": "moonshine-base",
        "tts_primary": "kokoro-onnx-q8",
        "tts_quality": "kokoro-onnx-fp32",
        "wake": "openwakeword",
        "vad": "silero-vad-v5",
        "speaker": "ecapa-tdnn-onnx",
        "voice_clone": "kokoclone",
    },
    HardwareTier.DESKTOP_GPU: {
        "stt_primary": "parakeet-tdt-0.6b-v3",
        "stt_streaming": "moonshine-base",
        "tts_primary": "kokoro-onnx-fp32",
        "tts_quality": "qwen3-tts-0.6b",
        "wake": "openwakeword",
        "vad": "silero-vad-v5",
        "speaker": "ecapa-tdnn-onnx",
        "voice_clone": "qwen3-tts-clone",
    },
    HardwareTier.CLOUD: {
        "stt_primary": "parakeet-tdt-0.6b-v3",
        "stt_streaming": "parakeet-ctc-0.6b",
        "tts_primary": "qwen3-tts-1.7b",
        "tts_quality": "qwen3-tts-1.7b",
        "wake": "openwakeword",
        "vad": "silero-vad-v5",
        "speaker": "ecapa-tdnn-onnx",
        "voice_clone": "qwen3-tts-clone",
    },
}

_FALLBACK_CHAINS: dict[str, list[str | None]] = {
    "stt": ["parakeet-tdt-0.6b-v3-int8", "moonshine-base", "moonshine-tiny"],
    "tts": ["qwen3-tts-1.7b", "kokoro-onnx-fp32", "kokoro-onnx-q8", "piper"],
    "voice_clone": ["qwen3-tts-clone", "kokoclone", "piper-finetune", None],
}


# ---------------------------------------------------------------------------
# Hardware detection
# ---------------------------------------------------------------------------


def _detect_gpu() -> tuple[bool, int]:
    """Detect NVIDIA GPU and VRAM.

    Returns:
        Tuple of (has_gpu, vram_mb).
    """
    try:
        result = subprocess.run(  # noqa: S603, S607
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=_NVIDIA_SMI_TIMEOUT_S,
        )
        if result.returncode == 0 and result.stdout.strip():
            vram = int(result.stdout.strip().split("\n")[0])
            return True, vram
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    return False, 0


def _read_cpuinfo() -> str:
    """Read /proc/cpuinfo content.

    Returns:
        CPU info string, or empty string if unavailable.
    """
    try:
        with open("/proc/cpuinfo") as f:  # noqa: PTH123
            return f.read()
    except FileNotFoundError:
        return ""


def _detect_tier(
    machine: str,
    ram_mb: int,
    has_gpu: bool,
    gpu_vram_mb: int,
) -> HardwareTier:
    """Determine hardware tier from system properties.

    Args:
        machine: Platform machine string (e.g., 'x86_64', 'aarch64').
        ram_mb: Total system RAM in megabytes.
        has_gpu: Whether an NVIDIA GPU is present.
        gpu_vram_mb: GPU VRAM in megabytes.

    Returns:
        Detected hardware tier.
    """
    if "aarch64" in machine or "arm" in machine:
        return HardwareTier.PI5

    if has_gpu and gpu_vram_mb >= _MIN_GPU_VRAM_MB:
        return HardwareTier.DESKTOP_GPU

    if ram_mb >= _HIGH_RAM_THRESHOLD_MB:
        return HardwareTier.DESKTOP_CPU

    # Check for N100 / Alder Lake
    cpuinfo = _read_cpuinfo()
    if "N100" in cpuinfo or "Alder Lake" in cpuinfo:
        return HardwareTier.N100

    return HardwareTier.DESKTOP_CPU


def detect_hardware() -> HardwareProfile:
    """Detect hardware capabilities of the current system.

    Returns:
        HardwareProfile with tier, RAM, CPU cores, and GPU info.
    """
    ram_mb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024)
    cpu_cores = os.cpu_count() or 1
    has_gpu, gpu_vram_mb = _detect_gpu()
    machine = platform.machine()

    tier = _detect_tier(machine, ram_mb, has_gpu, gpu_vram_mb)

    logger.info(
        "hardware_detected",
        tier=tier.name,
        ram_mb=ram_mb,
        cpu_cores=cpu_cores,
        has_gpu=has_gpu,
        gpu_vram_mb=gpu_vram_mb,
    )

    return HardwareProfile(
        tier=tier,
        ram_mb=ram_mb,
        cpu_cores=cpu_cores,
        has_gpu=has_gpu,
        gpu_vram_mb=gpu_vram_mb,
    )


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------


def select_models(profile: HardwareProfile) -> ModelSelection:
    """Select optimal voice models for the given hardware profile.

    Applies RAM-based adjustments to ensure models fit in available memory.

    Args:
        profile: Detected hardware profile.

    Returns:
        ModelSelection with optimal models for the hardware.
    """
    base = dict(_MODEL_MATRIX.get(profile.tier, _MODEL_MATRIX[HardwareTier.PI5]))
    adjustments: list[str] = []

    # Very low RAM — minimize everything
    if profile.ram_mb < _LOW_RAM_THRESHOLD_MB:
        base["stt_primary"] = "moonshine-tiny"
        base["tts_primary"] = "piper"
        base["tts_quality"] = "piper"
        adjustments.append(f"low_ram_{profile.ram_mb}mb_downgrade_all")

    # N100 with only 4GB — can't fit Parakeet + Kokoro simultaneously
    # Only applies if RAM >= 2048 (otherwise already downgraded above)
    if (
        _LOW_RAM_THRESHOLD_MB <= profile.ram_mb < _N100_LOW_RAM_THRESHOLD_MB
        and profile.tier == HardwareTier.N100
    ):
        base["stt_primary"] = "moonshine-base"
        adjustments.append(f"n100_low_ram_{profile.ram_mb}mb_downgrade_stt")

    logger.info(
        "models_selected",
        tier=profile.tier.name,
        stt=base["stt_primary"],
        tts=base["tts_primary"],
        adjustments=adjustments,
    )

    return ModelSelection(
        stt_primary=str(base["stt_primary"]),
        stt_streaming=str(base["stt_streaming"]),
        tts_primary=str(base["tts_primary"]),
        tts_quality=str(base["tts_quality"]),
        wake=str(base["wake"]),
        vad=str(base["vad"]),
        speaker=str(base["speaker"]),
        voice_clone=base.get("voice_clone"),
        tier=profile.tier,
        adjustments=tuple(adjustments),
    )


def get_fallback(role: str, failed_model: str) -> str | None:
    """Get the next fallback model when a primary model fails.

    Walks the fallback chain for the given role, returning the next
    model after the failed one.

    Args:
        role: Model role ('stt', 'tts', or 'voice_clone').
        failed_model: The model that failed.

    Returns:
        Next fallback model name, or None if no fallback available.
    """
    chain = _FALLBACK_CHAINS.get(role, [])
    try:
        idx = chain.index(failed_model)
        next_model = chain[idx + 1] if idx + 1 < len(chain) else None
    except ValueError:
        # Model not in chain — return first in chain
        next_model = chain[0] if chain else None

    if next_model is not None:
        logger.info(
            "model_fallback",
            role=role,
            failed=failed_model,
            next=next_model,
        )
    else:
        logger.warning(
            "model_fallback_exhausted",
            role=role,
            failed=failed_model,
        )

    return next_model


# ---------------------------------------------------------------------------
# VoiceModelAutoSelector (class API)
# ---------------------------------------------------------------------------


class VoiceModelAutoSelector:
    """Auto-detect hardware and select optimal voice models.

    Provides a stateful interface that caches hardware detection results.
    Use the module-level functions for stateless access.
    """

    def __init__(self) -> None:
        self._profile: HardwareProfile | None = None
        self._selection: ModelSelection | None = None

    @property
    def profile(self) -> HardwareProfile | None:
        """Last detected hardware profile."""
        return self._profile

    @property
    def selection(self) -> ModelSelection | None:
        """Last computed model selection."""
        return self._selection

    def detect_hardware(self) -> HardwareProfile:
        """Detect hardware and cache the result.

        Returns:
            Detected hardware profile.
        """
        self._profile = detect_hardware()
        return self._profile

    def select_models(self, profile: HardwareProfile | None = None) -> ModelSelection:
        """Select models for a given or cached profile.

        Args:
            profile: Hardware profile. If None, uses cached or detects.

        Returns:
            Optimal model selection.
        """
        if profile is None:
            if self._profile is None:
                self.detect_hardware()
            profile = self._profile
        assert profile is not None  # noqa: S101
        self._selection = select_models(profile)
        return self._selection

    def fallback(self, role: str, failed_model: str) -> str | None:
        """Get next fallback model.

        Args:
            role: Model role ('stt', 'tts', or 'voice_clone').
            failed_model: The model that failed.

        Returns:
            Next fallback model name, or None if chain exhausted.
        """
        return get_fallback(role, failed_model)

    def auto_select(self) -> ModelSelection:
        """Detect hardware and select models in one call.

        Returns:
            Optimal model selection for detected hardware.
        """
        self.detect_hardware()
        return self.select_models()

    def doctor_report(self) -> dict[str, Any]:
        """Generate a diagnostic report for ``sovyx doctor --voice``.

        Returns:
            Dict with hardware profile and selected models.
        """
        if self._profile is None:
            self.detect_hardware()
        if self._selection is None:
            self.select_models()

        assert self._profile is not None  # noqa: S101
        assert self._selection is not None  # noqa: S101

        return {
            "hardware": {
                "tier": self._profile.tier.name,
                "ram_mb": self._profile.ram_mb,
                "cpu_cores": self._profile.cpu_cores,
                "has_gpu": self._profile.has_gpu,
                "gpu_vram_mb": self._profile.gpu_vram_mb,
            },
            "models": {
                "stt_primary": self._selection.stt_primary,
                "stt_streaming": self._selection.stt_streaming,
                "tts_primary": self._selection.tts_primary,
                "tts_quality": self._selection.tts_quality,
                "wake": self._selection.wake,
                "vad": self._selection.vad,
                "speaker": self._selection.speaker,
                "voice_clone": self._selection.voice_clone,
            },
            "adjustments": list(self._selection.adjustments),
        }
