"""Voice model registry — mirror-aware download, dep check, path resolution.

SileroVAD and Kokoro TTS are auto-downloaded on first use through the
shared :class:`~sovyx.engine._model_downloader.ModelDownloader`
(mirror failover, Retry-After respect, decorrelated jitter, cooldown
marker, checksum verification). Moonshine STT auto-downloads via
HuggingFace Hub (managed by the ``moonshine-voice`` package). Piper TTS
requires a manual model download.

All cached at ``~/.sovyx/models/voice/``.

**Mirror strategy.** Each model carries a tuple of URLs ordered by
preference:

1. *Primary* — upstream canonical source (HF or upstream GitHub release).
2. *Mirrors* — CDN-independent fallbacks. For hosts whose infra
   periodically 5xxs (we got bitten by a 504 on
   ``github.com/snakers4/silero-vad/raw/master/...`` during onboarding)
   the self-hosted ``sovyx-ai/sovyx`` release is the final fallback,
   mirroring the pattern the brain/embedding downloader already uses.

The SHA-256 pin is asserted against every source — all mirrors MUST
serve byte-exact copies of the primary.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

from sovyx.engine._model_downloader import ModelDownloader
from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from sovyx.engine._model_downloader import DownloadAttempt

logger = get_logger(__name__)


@dataclasses.dataclass(frozen=True, slots=True)
class VoiceModelInfo:
    """Metadata for a voice model.

    ``urls`` is an ordered tuple: first entry is the primary source,
    remaining entries are mirror fallbacks tried with fresh retries
    after the primary exhausts its own retry budget.
    """

    name: str
    category: str
    size_mb: float
    urls: tuple[str, ...]
    filename: str
    sha256: str = ""
    download_available: bool = True
    description: str = ""

    @property
    def url(self) -> str:
        """Primary download URL. Empty string when ``urls`` is empty."""
        return self.urls[0] if self.urls else ""


# ── Model URL tables ─────────────────────────────────────────────────
#
# SHA-256 is pinned — ``ModelDownloader.ensure_model`` rejects any
# mirror that serves a divergent file and surfaces it as a checksum
# error (permanent, no retry). Drift means someone ships a modified
# model under the same filename, which is exactly what we want to
# catch loudly.

_SILERO_URLS: tuple[str, ...] = (
    "https://huggingface.co/istupakov/silero-vad-onnx/resolve/main/silero_vad.onnx",
    "https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx",
    "https://github.com/sovyx-ai/sovyx/releases/download/voice-models-v1/silero_vad.onnx",
)

_KOKORO_MODEL_URLS: tuple[str, ...] = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx",
    "https://github.com/sovyx-ai/sovyx/releases/download/voice-models-v1/kokoro-v1.0.int8.onnx",
)

_KOKORO_VOICES_URLS: tuple[str, ...] = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
    "https://github.com/sovyx-ai/sovyx/releases/download/voice-models-v1/voices-v1.0.bin",
)


VOICE_MODELS: dict[str, VoiceModelInfo] = {
    "silero-vad-v5": VoiceModelInfo(
        name="silero-vad-v5",
        category="vad",
        size_mb=2.3,
        urls=_SILERO_URLS,
        filename="silero_vad.onnx",
        sha256="1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3",
        description="Voice Activity Detection (Silero v5, ONNX)",
    ),
    "moonshine-tiny": VoiceModelInfo(
        name="moonshine-tiny",
        category="stt",
        size_mb=50.0,
        urls=(),
        filename="moonshine-tiny.onnx",
        download_available=False,
        description="Speech-to-Text (managed by moonshine-voice package)",
    ),
    "kokoro-v1.0-int8": VoiceModelInfo(
        name="kokoro-v1.0-int8",
        category="tts",
        size_mb=88.0,
        urls=_KOKORO_MODEL_URLS,
        filename="kokoro-v1.0.int8.onnx",
        sha256="6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb",
        description="Text-to-Speech (Kokoro v1.0, int8 quantized, 26 voices)",
    ),
    "kokoro-voices-v1.0": VoiceModelInfo(
        name="kokoro-voices-v1.0",
        category="tts",
        size_mb=27.0,
        urls=_KOKORO_VOICES_URLS,
        filename="voices-v1.0.bin",
        sha256="bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
        description="Kokoro voice style vectors (26 voices)",
    ),
}


def get_default_model_dir() -> Path:
    """Return the default voice model cache directory."""
    return Path.home() / ".sovyx" / "models" / "voice"


def get_models_for_tier(tier: str) -> list[VoiceModelInfo]:
    """Return recommended models for a hardware tier."""
    return list(VOICE_MODELS.values())


# ── Dependency check ─────────────────────────────────────────────────

_VOICE_DEPS: list[tuple[str, str]] = [
    ("moonshine_voice", "moonshine-voice"),
    ("sounddevice", "sounddevice"),
]


def check_voice_deps() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Check which voice Python packages are installed.

    Catches both ImportError (package not installed) and OSError
    (native library missing, e.g. PortAudio for sounddevice).

    Returns:
        (installed, missing) — each a list of {"module", "package"}.
        On OSError, the entry includes a "message" key with install hint.
    """
    import platform  # noqa: PLC0415

    installed: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    for module, package in _VOICE_DEPS:
        try:
            __import__(module)
            installed.append({"module": module, "package": package})
        except ImportError:
            missing.append({"module": module, "package": package})
        except OSError as exc:
            hint = str(exc)
            if "portaudio" in hint.lower():
                system = platform.system()
                if system == "Linux":
                    hint = "PortAudio not found. Install: sudo apt install libportaudio2"
                elif system == "Darwin":
                    hint = "PortAudio not found. Install: brew install portaudio"
                else:
                    hint = "PortAudio not found. Install the PortAudio system library."
            missing.append({"module": module, "package": package, "message": hint})
    return installed, missing


def detect_tts_engine() -> str:
    """Detect which TTS engine is available.

    Priority: piper > kokoro > none.
    """
    try:
        __import__("piper_phonemize")
        return "piper"
    except ImportError:
        pass
    try:
        __import__("kokoro_onnx")
        return "kokoro"
    except ImportError:
        pass
    return "none"


# ── Auto-download helpers ──────────────────────────────────────────
#
# These wrap the shared ModelDownloader with the voice-tier cooldown
# and our optional OTel attempt hook. Each is idempotent — a hit on
# the fast path (file present + checksum OK) returns without network.


def _otel_attempt_hook(attempt: DownloadAttempt) -> None:
    """Record one mirror attempt to ``sovyx.model.download.attempts``.

    Low-cardinality labels only — ``source`` stays ``primary|mirror-N``,
    ``result`` stays ``ok|transient|permanent``, ``error_type`` is the
    exception class name (finite set). We deliberately drop the full URL
    and HTTP status code from labels; both live in the structured log.
    """
    from sovyx.observability.metrics import get_metrics  # noqa: PLC0415

    metrics = get_metrics()
    metrics.model_download_attempts.add(
        1,
        {
            "model": attempt.filename,
            "source": attempt.source,
            "result": attempt.result,
            "error_type": attempt.error_type or "",
        },
    )


def _compose_attempt_hooks(
    user_hook: Callable[[DownloadAttempt], None] | None,
) -> Callable[[DownloadAttempt], None]:
    """Always record to OTel, and fan out to a caller hook when present."""
    if user_hook is None:
        return _otel_attempt_hook

    def _composed(attempt: DownloadAttempt) -> None:
        _otel_attempt_hook(attempt)
        user_hook(attempt)

    return _composed


def _build_downloader(
    models_dir: Path,
    *,
    on_attempt: Callable[[DownloadAttempt], None] | None = None,
) -> ModelDownloader:
    """Construct a ModelDownloader honouring VoiceTuningConfig defaults.

    The ``on_attempt`` hook is always composed with the OTel counter
    recorder so every mirror attempt — success or failure — shows up in
    ``sovyx.model.download.attempts`` without the caller having to
    remember to wire it.
    """
    tuning = _VoiceTuning()
    return ModelDownloader(
        models_dir,
        cooldown_seconds=float(tuning.model_download_cooldown_seconds),
        on_attempt=_compose_attempt_hooks(on_attempt),
    )


async def ensure_silero_vad(
    model_dir: Path | None = None,
    *,
    on_attempt: Callable[[DownloadAttempt], None] | None = None,
) -> Path:
    """Ensure SileroVAD ONNX model exists on disk, downloading if needed.

    Returns:
        Path to the silero_vad.onnx file.
    """
    target_dir = model_dir or get_default_model_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    info = VOICE_MODELS["silero-vad-v5"]
    downloader = _build_downloader(target_dir, on_attempt=on_attempt)
    return await downloader.ensure_model(
        filename=info.filename,
        url=info.urls[0],
        expected_sha256=info.sha256,
        mirror_urls=info.urls[1:],
    )


async def ensure_kokoro_tts(
    model_dir: Path | None = None,
    *,
    on_attempt: Callable[[DownloadAttempt], None] | None = None,
) -> Path:
    """Ensure Kokoro TTS model + voices exist on disk, downloading if needed.

    Returns:
        Path to the kokoro subdirectory containing the model and voices.
    """
    target_dir = (model_dir or get_default_model_dir()) / "kokoro"
    target_dir.mkdir(parents=True, exist_ok=True)

    model_info = VOICE_MODELS["kokoro-v1.0-int8"]
    voices_info = VOICE_MODELS["kokoro-voices-v1.0"]

    downloader = _build_downloader(target_dir, on_attempt=on_attempt)

    await downloader.ensure_model(
        filename=model_info.filename,
        url=model_info.urls[0],
        expected_sha256=model_info.sha256,
        mirror_urls=model_info.urls[1:],
    )
    await downloader.ensure_model(
        filename=voices_info.filename,
        url=voices_info.urls[0],
        expected_sha256=voices_info.sha256,
        mirror_urls=voices_info.urls[1:],
    )

    return target_dir


__all__ = [
    "VOICE_MODELS",
    "VoiceModelInfo",
    "check_voice_deps",
    "detect_tts_engine",
    "ensure_kokoro_tts",
    "ensure_silero_vad",
    "get_default_model_dir",
    "get_models_for_tier",
]
