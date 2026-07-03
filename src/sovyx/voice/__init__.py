"""voice — Voice activity detection, wake word, speech processing, and TTS pipeline."""

from __future__ import annotations

from sovyx.voice._event_names import (
    CAPTURE_INTEGRITY_EVENT_NAMES,
    LEGACY_EVENT_NAMES,
    LEGACY_TWIN_MAP,
    CaptureIntegrityEvent,
)
from sovyx.voice._platform_metadata import (
    PlatformAudioFamily,
    PlatformToken,
    current_platform_token,
    is_mixed_platform_strategy_list,
    resolve_family_from_strategies,
    resolve_family_from_strategy_name,
)
from sovyx.voice.audio import (
    AudioCapture,
    AudioCaptureConfig,
    AudioDucker,
    AudioOutput,
    AudioOutputConfig,
    AudioPlatform,
    OutputChunk,
    OutputPriority,
    RingBuffer,
    detect_platform,
    normalize_lufs,
)
from sovyx.voice.auto_select import (
    HardwareProfile,
    HardwareTier,
    ModelSelection,
    VoiceModelAutoSelector,
    detect_hardware,
    get_fallback,
    select_models,
)
from sovyx.voice.jarvis import (
    FILLER_BANK,
    FillerCategory,
    JarvisConfig,
    JarvisIllusion,
    split_at_boundaries,
    synthesize_beep,
    validate_jarvis_config,
)
from sovyx.voice.pipeline import (
    AudioOutputQueue,
    BargeInDetector,
    VoicePipeline,
    VoicePipelineConfig,
    VoicePipelineState,
)
from sovyx.voice.stt import (
    MoonshineConfig,
    MoonshineSTT,
    PartialTranscription,
    STTEngine,
    STTState,
    TranscriptionResult,
    TranscriptionSegment,
)

# v0.32.4 Phase 3.C.1 — CloudSTT is no longer re-exported here. The
# module is genuinely orphan in production (no factory wire-up + no
# auto-fallback chain from MoonshineSTT). Operators who deliberately
# want Whisper API STT must import from ``sovyx.voice.stt_cloud``
# directly — that interface is documented for advanced/manual use.
# See module docstring + AUDIT.md §P0.C1.
from sovyx.voice.tts_kokoro import KokoroConfig, KokoroTTS
from sovyx.voice.tts_piper import AudioChunk, PiperConfig, PiperTTS, TTSEngine
from sovyx.voice.vad import SileroVAD, VADConfig, VADEvent, VADState
from sovyx.voice.wake_word import (
    VerificationResult,
    WakeWordConfig,
    WakeWordDetector,
    WakeWordEvent,
    WakeWordState,
)
from sovyx.voice.wyoming import (
    SovyxWyomingServer,
    STTResult,
    TTSResult,
    WakeWordResult,
    WyomingClientHandler,
    WyomingConfig,
    WyomingEvent,
    build_service_info,
    get_local_ip,
    ndarray_to_pcm_bytes,
    pcm_bytes_to_ndarray,
    write_event,
)

__all__ = [
    "CAPTURE_INTEGRITY_EVENT_NAMES",
    "LEGACY_EVENT_NAMES",
    "LEGACY_TWIN_MAP",
    "CaptureIntegrityEvent",
    "PlatformAudioFamily",
    "PlatformToken",
    "current_platform_token",
    "is_mixed_platform_strategy_list",
    "resolve_family_from_strategies",
    "resolve_family_from_strategy_name",
    "AudioCapture",
    "HardwareProfile",
    "HardwareTier",
    "ModelSelection",
    "VoiceModelAutoSelector",
    "detect_hardware",
    "get_fallback",
    "select_models",
    "AudioCaptureConfig",
    "AudioChunk",
    # v0.32.4 Phase 3.C.1 — CloudSTT / CloudSTTConfig / CloudSTTError
    # removed from public exports (still importable from
    # ``sovyx.voice.stt_cloud`` directly for operators who deliberately
    # wire it up). The auto-fallback chain referenced in past docstrings
    # was never shipped; removing the re-export prevents future readers
    # from assuming an automatic Moonshine→Whisper fallback exists.
    "AudioDucker",
    "AudioOutput",
    "AudioOutputConfig",
    "AudioOutputQueue",
    "AudioPlatform",
    "BargeInDetector",
    "FILLER_BANK",
    "FillerCategory",
    "JarvisConfig",
    "JarvisIllusion",
    "KokoroConfig",
    "KokoroTTS",
    "OutputChunk",
    "OutputPriority",
    "RingBuffer",
    "detect_platform",
    "normalize_lufs",
    "MoonshineConfig",
    "MoonshineSTT",
    "PartialTranscription",
    "PiperConfig",
    "PiperTTS",
    "STTEngine",
    "STTState",
    "SileroVAD",
    "TTSEngine",
    "TranscriptionResult",
    "TranscriptionSegment",
    "VADConfig",
    "VADEvent",
    "VADState",
    "VerificationResult",
    "VoicePipeline",
    "VoicePipelineConfig",
    "VoicePipelineState",
    "WakeWordConfig",
    "WakeWordDetector",
    "WakeWordEvent",
    "WakeWordState",
    "split_at_boundaries",
    "synthesize_beep",
    "validate_jarvis_config",
    "STTResult",
    "SovyxWyomingServer",
    "TTSResult",
    "WakeWordResult",
    "WyomingClientHandler",
    "WyomingConfig",
    "WyomingEvent",
    "build_service_info",
    "get_local_ip",
    "ndarray_to_pcm_bytes",
    "pcm_bytes_to_ndarray",
    "write_event",
]
