"""Voice model registry -- download, dependency check, and path resolution.

SileroVAD and Kokoro TTS are auto-downloaded on first use.
Moonshine STT auto-downloads via HuggingFace Hub (managed by moonshine-voice).
Piper TTS requires manual model download.

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
    sha256: str = ""
    download_available: bool = True
    description: str = ""


VOICE_MODELS: dict[str, VoiceModelInfo] = {
    "silero-vad-v5": VoiceModelInfo(
        name="silero-vad-v5",
        category="vad",
        size_mb=2.3,
        url="https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx",
        filename="silero_vad.onnx",
        sha256="1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3",
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
        sha256="6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb",
        description="Text-to-Speech (Kokoro v1.0, int8 quantized, 26 voices)",
    ),
    "kokoro-voices-v1.0": VoiceModelInfo(
        name="kokoro-voices-v1.0",
        category="tts",
        size_mb=27.0,
        url="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
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


async def ensure_silero_vad(model_dir: Path | None = None) -> Path:
    """Ensure SileroVAD ONNX model exists on disk, downloading if needed.

    Returns:
        Path to the silero_vad.onnx file.
    """
    import asyncio  # noqa: PLC0415

    target_dir = model_dir or get_default_model_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    info = VOICE_MODELS["silero-vad-v5"]
    model_path = target_dir / info.filename

    if model_path.exists():
        logger.debug("silero_vad_model_exists", path=str(model_path))
        return model_path

    await asyncio.to_thread(
        lambda: _self._download_model(info, model_path),
    )
    return model_path


async def ensure_kokoro_tts(model_dir: Path | None = None) -> Path:
    """Ensure Kokoro TTS model + voices exist on disk, downloading if needed.

    Returns:
        Path to the kokoro subdirectory containing model and voices files.
    """
    import asyncio  # noqa: PLC0415

    target_dir = (model_dir or get_default_model_dir()) / "kokoro"
    target_dir.mkdir(parents=True, exist_ok=True)

    model_info = VOICE_MODELS["kokoro-v1.0-int8"]
    voices_info = VOICE_MODELS["kokoro-voices-v1.0"]

    model_path = target_dir / model_info.filename
    voices_path = target_dir / voices_info.filename

    if not model_path.exists():
        await asyncio.to_thread(
            lambda: _self._download_model(model_info, model_path),
        )

    if not voices_path.exists():
        await asyncio.to_thread(
            lambda: _self._download_model(voices_info, voices_path),
        )

    return target_dir


# ── Download engine (sync — called via to_thread) ──────────────────

_MAX_RETRIES = 3
_BACKOFF_BASE_S = 1.0
_PROGRESS_INTERVAL_BYTES = 10 * 1024 * 1024  # log every ~10 MB


def _download_model(info: VoiceModelInfo, dest: Path) -> None:
    """Download a model file with retry, SHA256 verification, and progress logging."""
    import time  # noqa: PLC0415

    last_exc: BaseException | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            logger.info(
                "model_download_start",
                model=info.name,
                size_mb=info.size_mb,
                destination=str(dest),
                attempt=attempt,
            )
            _download_file(info.url, dest, expected_sha256=info.sha256, label=info.name)
            logger.info("model_download_complete", model=info.name, path=str(dest))
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < _MAX_RETRIES:
                delay = _BACKOFF_BASE_S * (2 ** (attempt - 1))
                logger.warning(
                    "model_download_retry",
                    model=info.name,
                    attempt=attempt,
                    error=str(exc),
                    retry_in_s=delay,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "model_download_failed",
                    model=info.name,
                    attempts=_MAX_RETRIES,
                    error=str(exc),
                )
    msg = f"Failed to download {info.name} after {_MAX_RETRIES} attempts"
    raise RuntimeError(msg) from last_exc


def _download_file(
    url: str,
    dest: Path,
    *,
    expected_sha256: str = "",
    label: str = "",
) -> None:
    """Blocking download with SHA256 verification and progress logging."""
    import hashlib  # noqa: PLC0415
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    tmp_path = None
    try:
        fd, tmp_path_str = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp", prefix=".dl_")
        os.close(fd)
        tmp_path = Path(tmp_path_str)

        hasher = hashlib.sha256() if expected_sha256 else None
        downloaded = 0
        last_progress = 0

        with (
            httpx.Client(timeout=300.0, follow_redirects=True) as client,
            client.stream("GET", url) as resp,
        ):
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            tag = label or dest.name

            with tmp_path.open("wb") as f:
                for chunk in resp.iter_bytes(chunk_size=65536):
                    f.write(chunk)
                    if hasher is not None:
                        hasher.update(chunk)
                    downloaded += len(chunk)

                    if total > 0 and downloaded - last_progress >= _PROGRESS_INTERVAL_BYTES:
                        pct = int(downloaded * 100 / total)
                        logger.info(
                            "model_download_progress",
                            model=tag,
                            downloaded_mb=round(downloaded / (1024 * 1024), 1),
                            total_mb=round(total / (1024 * 1024), 1),
                            percent=pct,
                        )
                        last_progress = downloaded

        if hasher is not None and expected_sha256:
            actual = hasher.hexdigest()
            if actual != expected_sha256:
                tmp_path.unlink(missing_ok=True)
                msg = (
                    f"SHA256 mismatch for {dest.name}: "
                    f"expected {expected_sha256[:16]}..., got {actual[:16]}..."
                )
                raise ValueError(msg)

        tmp_path.replace(dest)
    except BaseException:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise
