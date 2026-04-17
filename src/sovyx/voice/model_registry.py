"""Voice model registry -- download, dependency check, and path resolution.

SileroVAD is auto-downloaded (2.3 MB, GitHub URL).
Moonshine STT auto-downloads via HuggingFace Hub (managed by moonshine-voice).
Piper TTS requires manual model download; Kokoro TTS is a zero-config fallback.

Model files cached at ~/.sovyx/models/voice/.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

from sovyx.observability.logging import get_logger

_self = sys.modules[__name__]

logger = get_logger(__name__)


@dataclasses.dataclass(frozen=True, slots=True)
class VoiceModelInfo:
    """Metadata for a voice model."""

    name: str
    category: str
    size_mb: float
    url: str
    filename: str
    download_available: bool = True
    description: str = ""


VOICE_MODELS: dict[str, VoiceModelInfo] = {
    "silero-vad-v5": VoiceModelInfo(
        name="silero-vad-v5",
        category="vad",
        size_mb=2.3,
        url="https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx",
        filename="silero_vad.onnx",
        description="Voice Activity Detection (Silero v5, ONNX)",
    ),
    "moonshine-tiny": VoiceModelInfo(
        name="moonshine-tiny",
        category="stt",
        size_mb=50.0,
        url="",
        filename="moonshine-tiny.onnx",
        download_available=False,
        description="Speech-to-Text (managed by moonshine-voice package)",
    ),
    "kokoro-v1.0-int8": VoiceModelInfo(
        name="kokoro-v1.0-int8",
        category="tts",
        size_mb=88.0,
        url="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx",
        filename="kokoro-v1.0.int8.onnx",
        description="Text-to-Speech (Kokoro v1.0, int8 quantized, 26 voices)",
    ),
    "kokoro-voices-v1.0": VoiceModelInfo(
        name="kokoro-voices-v1.0",
        category="tts",
        size_mb=3.2,
        url="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
        filename="voices-v1.0.bin",
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


# ── SileroVAD auto-download ─────────────────────────────────────────


async def ensure_silero_vad(model_dir: Path | None = None) -> Path:
    """Ensure SileroVAD ONNX model exists on disk, downloading if needed.

    Returns:
        Path to the silero_vad.onnx file.
    """
    import asyncio

    target_dir = model_dir or get_default_model_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    model_path = target_dir / "silero_vad.onnx"

    if model_path.exists():
        logger.debug("silero_vad_model_exists", path=str(model_path))
        return model_path

    info = VOICE_MODELS["silero-vad-v5"]
    logger.info(
        "downloading_silero_vad",
        url=info.url,
        size_mb=info.size_mb,
        destination=str(model_path),
    )

    await asyncio.to_thread(lambda: _self._download_file(info.url, model_path))
    logger.info("silero_vad_downloaded", path=str(model_path))
    return model_path


async def ensure_kokoro_tts(model_dir: Path | None = None) -> Path:
    """Ensure Kokoro TTS model + voices exist on disk, downloading if needed.

    Returns:
        Path to the kokoro subdirectory containing model and voices files.
    """
    import asyncio

    target_dir = (model_dir or get_default_model_dir()) / "kokoro"
    target_dir.mkdir(parents=True, exist_ok=True)

    model_info = VOICE_MODELS["kokoro-v1.0-int8"]
    voices_info = VOICE_MODELS["kokoro-voices-v1.0"]

    model_path = target_dir / model_info.filename
    voices_path = target_dir / voices_info.filename

    if not model_path.exists():
        logger.info(
            "downloading_kokoro_model",
            url=model_info.url,
            size_mb=model_info.size_mb,
            destination=str(model_path),
        )
        await asyncio.to_thread(lambda: _self._download_file(model_info.url, model_path))
        logger.info("kokoro_model_downloaded", path=str(model_path))

    if not voices_path.exists():
        logger.info(
            "downloading_kokoro_voices",
            url=voices_info.url,
            destination=str(voices_path),
        )
        await asyncio.to_thread(lambda: _self._download_file(voices_info.url, voices_path))
        logger.info("kokoro_voices_downloaded", path=str(voices_path))

    return target_dir


def _download_file(url: str, dest: Path) -> None:
    """Blocking download — called via to_thread."""
    import tempfile

    import httpx

    tmp_path = None
    try:
        fd, tmp_path_str = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp", prefix=".dl_")
        import os

        os.close(fd)
        tmp_path = Path(tmp_path_str)

        with (
            httpx.Client(timeout=300.0, follow_redirects=True) as client,
            client.stream("GET", url) as resp,
        ):
            resp.raise_for_status()
            with tmp_path.open("wb") as f:
                for chunk in resp.iter_bytes(chunk_size=65536):
                    f.write(chunk)

        tmp_path.replace(dest)
    except BaseException:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise
