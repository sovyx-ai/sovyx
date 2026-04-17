"""Voice device test — live meter WS + TTS playback endpoints.

This router powers the setup-wizard's browser-side device test. The four
endpoints it exposes are:

``GET  /api/voice/test/devices``
    Enumerate PortAudio input + output devices. Mirrors the payload of
    ``/api/voice/hardware-detect`` but without the hardware probe.

``WS   /api/voice/test/input``
    Live RMS/peak/hold meter stream. Auth via query param ``?token=...``;
    device via ``?device_id=...`` (optional — None = system default).

``POST /api/voice/test/output``
    Kicks off a TTS synth+play job for the selected output device. Returns
    a ``job_id`` + status; actual playback happens in a background task
    so the HTTP response is non-blocking.

``GET  /api/voice/test/output/{job_id}``
    Polls the job result — status, timing, peak dB, or error code.

The router refuses to start a session while the production voice pipeline
is active (:class:`VoicePipeline` registered in :class:`ServiceRegistry`)
to avoid racing PortAudio between two owners on the same device. It
also enforces a per-token reconnect budget + a singleton session registry
so idle browser tabs cannot churn audio streams forever.

Kill-switch: :attr:`VoiceTuningConfig.device_test_enabled` — when False,
every endpoint returns 503 ``disabled`` regardless of auth.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger
from sovyx.voice.device_test import (
    PROTOCOL_VERSION,
    WS_CLOSE_DISABLED,
    WS_CLOSE_PIPELINE_ACTIVE,
    WS_CLOSE_RATE_LIMITED,
    WS_CLOSE_REPLACED,
    WS_CLOSE_UNAUTHORIZED,
    AudioSinkError,
    CloseReason,
    DeviceInfo,
    DevicesResponse,
    ErrorCode,
    ErrorResponse,
    NoopLimiter,
    SessionConfig,
    SessionRegistry,
    SoundDeviceInputSource,
    SoundDeviceOutputSink,
    TestOutputJob,
    TestOutputRequest,
    TestOutputResult,
    TestSession,
    TokenReconnectLimiter,
    WSSender,
    hash_token,
    new_session_id,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import numpy as np
    import numpy.typing as npt

    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice.device_test import AudioOutputSink

logger = get_logger(__name__)

router = APIRouter(prefix="/api/voice/test")

# Localised default phrases. The UI normally sends the resolved phrase
# via ``phrase_key`` but a tiny server-side fallback protects against a
# mis-configured client.
_DEFAULT_PHRASES: dict[str, dict[str, str]] = {
    "default": {
        "en": "Audio test successful. Your voice assistant is ready.",
        "pt": "Teste de áudio bem-sucedido. Sua assistente de voz está pronta.",
        "es": "Prueba de audio exitosa. Su asistente de voz está listo.",
    },
}


# ---------------------------------------------------------------------------
# Per-app state (lazy — each app gets its own limiter + registry)
# ---------------------------------------------------------------------------


def _get_tuning(request_or_ws: Request | WebSocket) -> VoiceTuningConfig:
    """Resolve :class:`VoiceTuningConfig` from the app state.

    Follows the same resolution pattern as
    :meth:`sovyx.dashboard.server._DashboardServer._resolve_log_file`:
    prefer the registered :class:`EngineConfig`, fall back to a fresh
    instance if the registry isn't wired (e.g. unit tests).
    """
    from sovyx.engine.config import EngineConfig

    registry = getattr(request_or_ws.app.state, "registry", None)
    if registry is not None and registry.is_registered(EngineConfig):
        # This is the production path — synchronous access to the already
        # resolved EngineConfig via the cached instance on app.state.
        cached = getattr(request_or_ws.app.state, "engine_config", None)
        if cached is not None:
            voice_cfg: VoiceTuningConfig = cached.tuning.voice
            return voice_cfg
    # Test / fallback path.
    cfg = EngineConfig()
    return cfg.tuning.voice


def _is_pipeline_active(request_or_ws: Request | WebSocket) -> bool:
    """True when :class:`VoicePipeline` is registered (production voice on)."""
    registry = getattr(request_or_ws.app.state, "registry", None)
    if registry is None:
        return False
    from sovyx.voice.pipeline._orchestrator import VoicePipeline

    return bool(registry.is_registered(VoicePipeline))


def _get_session_registry(request_or_ws: Request | WebSocket) -> SessionRegistry:
    """Lazily create + cache the :class:`SessionRegistry` on app state."""
    existing = getattr(request_or_ws.app.state, "voice_test_registry", None)
    if isinstance(existing, SessionRegistry):
        return existing
    tuning = _get_tuning(request_or_ws)
    reg = SessionRegistry(max_per_token=tuning.device_test_max_sessions_per_token)
    request_or_ws.app.state.voice_test_registry = reg
    return reg


def _get_limiter(request_or_ws: Request | WebSocket) -> TokenReconnectLimiter | NoopLimiter:
    """Lazily create + cache the reconnect limiter on app state."""
    existing = getattr(request_or_ws.app.state, "voice_test_limiter", None)
    if isinstance(existing, (TokenReconnectLimiter, NoopLimiter)):
        return existing
    tuning = _get_tuning(request_or_ws)
    limiter: TokenReconnectLimiter | NoopLimiter = TokenReconnectLimiter(
        limit=tuning.device_test_reconnect_limit_per_min,
        window_seconds=60,
    )
    request_or_ws.app.state.voice_test_limiter = limiter
    return limiter


def _get_output_jobs(request_or_ws: Request | WebSocket) -> dict[str, _JobEntry]:
    """Lazily create + cache the output-job dict on app state."""
    existing = getattr(request_or_ws.app.state, "voice_test_output_jobs", None)
    if isinstance(existing, dict):
        return existing
    jobs: dict[str, _JobEntry] = {}
    request_or_ws.app.state.voice_test_output_jobs = jobs
    return jobs


# ---------------------------------------------------------------------------
# Output-job bookkeeping
# ---------------------------------------------------------------------------


class _JobEntry:
    """In-memory record of a TTS test-playback job.

    TTL cleanup is lazy — the GET endpoint prunes entries older than
    ``device_test_output_job_ttl_seconds`` when it runs.
    """

    __slots__ = ("created_at", "result", "task")

    def __init__(self) -> None:
        self.created_at: float = time.monotonic()
        self.result: TestOutputResult | None = None
        self.task: asyncio.Task[None] | None = None


class _SynthResult:
    """Typed return value from the TTS resolver — audio + its sample rate."""

    __slots__ = ("audio", "sample_rate")

    def __init__(self, *, audio: npt.NDArray[np.int16], sample_rate: int) -> None:
        self.audio = audio
        self.sample_rate = sample_rate


# ---------------------------------------------------------------------------
# WebSocket sender adapter
# ---------------------------------------------------------------------------


class _FastAPIWSSender(WSSender):
    """Adapts :class:`fastapi.WebSocket` to the :class:`WSSender` protocol."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        self._closed = False

    async def send_json(self, payload: dict[str, object]) -> None:
        if self._closed:
            return
        await self._ws.send_json(payload)

    async def close(self, code: int, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._ws.close(code=code, reason=reason)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/devices",
    dependencies=[Depends(verify_token)],
)
async def list_devices(request: Request) -> JSONResponse:
    """Enumerate input + output devices available on the host."""
    tuning = _get_tuning(request)
    if not tuning.device_test_enabled:
        return _err_response(
            ErrorCode.DISABLED,
            "Voice device test is disabled by configuration",
            HTTP_503_SERVICE_UNAVAILABLE,
        )

    input_devices, output_devices = await asyncio.to_thread(_enumerate_devices)
    body = DevicesResponse(
        input_devices=input_devices,
        output_devices=output_devices,
    )
    return JSONResponse(body.model_dump())


def _enumerate_devices() -> tuple[list[DeviceInfo], list[DeviceInfo]]:
    """Run PortAudio ``query_devices`` off the event loop."""
    try:
        import sounddevice as sd  # noqa: PLC0415
    except (ImportError, OSError):
        logger.debug("voice_test_sounddevice_unavailable")
        return [], []

    try:
        devices = sd.query_devices()
        default_in_raw, default_out_raw = sd.default.device
    except Exception:  # noqa: BLE001
        logger.warning("voice_test_device_enum_failed", exc_info=True)
        return [], []

    default_in = _coerce_default_index(default_in_raw)
    default_out = _coerce_default_index(default_out_raw)

    input_devices: list[DeviceInfo] = []
    output_devices: list[DeviceInfo] = []
    for i, d in enumerate(devices):
        if not isinstance(d, dict):
            continue
        name = str(d.get("name", "unknown"))
        sr = int(d.get("default_samplerate", 0) or 0)
        in_ch = int(d.get("max_input_channels", 0) or 0)
        out_ch = int(d.get("max_output_channels", 0) or 0)
        if in_ch > 0:
            input_devices.append(
                DeviceInfo(
                    index=i,
                    name=name,
                    is_default=i == default_in,
                    max_input_channels=in_ch,
                    max_output_channels=out_ch,
                    default_samplerate=sr,
                ),
            )
        if out_ch > 0:
            output_devices.append(
                DeviceInfo(
                    index=i,
                    name=name,
                    is_default=i == default_out,
                    max_input_channels=in_ch,
                    max_output_channels=out_ch,
                    default_samplerate=sr,
                ),
            )
    return input_devices, output_devices


def _coerce_default_index(raw: object) -> int:
    """``sd.default.device`` returns a sentinel / int — normalise to int."""
    if not isinstance(raw, (int, str)):
        return -1
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


@router.websocket("/input")
async def websocket_input_meter(
    websocket: WebSocket,
    token: str | None = Query(default=None),
    device_id: int | None = Query(default=None),
    sample_rate: int = Query(default=16_000, ge=8_000, le=48_000),
) -> None:
    """Live RMS/peak/hold meter over WebSocket (auth via query)."""
    expected = websocket.app.state.auth_token
    if not token or not secrets.compare_digest(token, expected):
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="unauthorized")
        return

    tuning = _get_tuning(websocket)
    if not tuning.device_test_enabled:
        await websocket.close(code=WS_CLOSE_DISABLED, reason="disabled")
        return

    if _is_pipeline_active(websocket):
        await websocket.close(code=WS_CLOSE_PIPELINE_ACTIVE, reason="pipeline_active")
        return

    limiter = _get_limiter(websocket)
    token_key = hash_token(token)
    if not await limiter.try_acquire(token_key):
        await websocket.close(code=WS_CLOSE_RATE_LIMITED, reason="rate_limited")
        return

    await websocket.accept()
    sender = _FastAPIWSSender(websocket)
    session_id = new_session_id()
    source = SoundDeviceInputSource(
        device_id=device_id,
        sample_rate=sample_rate,
    )
    session = TestSession(
        session_id=session_id,
        source=source,
        sender=sender,
        config=SessionConfig(
            frame_rate_hz=tuning.device_test_frame_rate_hz,
            peak_hold_ms=tuning.device_test_peak_hold_ms,
            peak_decay_db_per_sec=tuning.device_test_peak_decay_db_per_sec,
            vad_trigger_db=tuning.device_test_vad_trigger_db,
            clipping_db=tuning.device_test_clipping_db,
        ),
    )

    registry = _get_session_registry(websocket)
    superseded = await registry.register(token_key, session)
    for old in superseded:
        with contextlib.suppress(Exception):
            await old.stop(CloseReason.SESSION_REPLACED)
        # Give the old session a chance to emit its ClosedFrame before we
        # start pumping new frames on the same token.
        await asyncio.sleep(0)

    # Signal the superseded sessions via their own WS close code so the
    # browser can distinguish "you were replaced" from "device failed".
    for old in superseded:
        _close_superseded(old)

    logger.info(
        "voice_test_session_opened",
        session_id=session_id,
        device_id=device_id,
        sample_rate=sample_rate,
        token_hash=token_key[:8],
    )
    try:
        await session.run()
    finally:
        await registry.unregister(token_key, session)


def _close_superseded(session: TestSession) -> None:  # noqa: ARG001
    """Placeholder hook for future OTel counter wiring."""
    # The TestSession.stop() call above will make run() exit with
    # SESSION_REPLACED and the _finalize() path will close the WS with
    # WS_CLOSE_REPLACED. Nothing more to do here; the function exists so
    # we can wire metrics without touching the hot path.
    _ = WS_CLOSE_REPLACED


# ---------------------------------------------------------------------------
# TTS playback endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/output",
    dependencies=[Depends(verify_token)],
)
async def start_output_test(request: Request, body: TestOutputRequest) -> JSONResponse:
    """Queue a TTS synth+play job and return a poll token."""
    tuning = _get_tuning(request)
    if not tuning.device_test_enabled:
        return _err_response(
            ErrorCode.DISABLED,
            "Voice device test is disabled by configuration",
            HTTP_503_SERVICE_UNAVAILABLE,
        )

    # Resolve + validate the test phrase.
    phrase = _DEFAULT_PHRASES.get(body.phrase_key, _DEFAULT_PHRASES["default"])
    text = phrase.get(body.language, phrase["en"])
    if len(text) > tuning.device_test_max_phrase_chars:
        return _err_response(
            ErrorCode.INVALID_REQUEST,
            f"Phrase exceeds {tuning.device_test_max_phrase_chars} chars",
            HTTP_400_BAD_REQUEST,
        )

    # Resolve TTS from the registry. If the pipeline is active we refuse —
    # the engine is owned by the live pipeline and concurrent synth would
    # race the speech queue.
    if _is_pipeline_active(request):
        return _err_response(
            ErrorCode.PIPELINE_ACTIVE,
            "Voice pipeline is active; disable it to run the test",
            HTTP_409_CONFLICT,
        )

    tts = await _resolve_tts(request)
    if tts is None:
        return _err_response(
            ErrorCode.TTS_UNAVAILABLE,
            "No TTS engine available",
            HTTP_503_SERVICE_UNAVAILABLE,
        )

    jobs = _get_output_jobs(request)
    _prune_expired_jobs(jobs, ttl_s=tuning.device_test_output_job_ttl_seconds)

    job_id = secrets.token_hex(8)
    entry = _JobEntry()
    jobs[job_id] = entry

    sink: AudioOutputSink = (
        getattr(
            request.app.state,
            "voice_test_output_sink",
            None,
        )
        or SoundDeviceOutputSink()
    )

    entry.task = asyncio.create_task(
        _run_output_job(
            entry=entry,
            job_id=job_id,
            text=text,
            voice=body.voice,
            device_id=body.device_id,
            tts=tts,
            sink=sink,
        ),
    )

    return JSONResponse(
        TestOutputJob(job_id=job_id, status="queued").model_dump(),
    )


@router.get(
    "/output/{job_id}",
    dependencies=[Depends(verify_token)],
)
async def get_output_result(request: Request, job_id: str) -> JSONResponse:
    """Return the final result of a TTS test playback job."""
    tuning = _get_tuning(request)
    if not tuning.device_test_enabled:
        return _err_response(
            ErrorCode.DISABLED,
            "Voice device test is disabled by configuration",
            HTTP_503_SERVICE_UNAVAILABLE,
        )

    jobs = _get_output_jobs(request)
    _prune_expired_jobs(jobs, ttl_s=tuning.device_test_output_job_ttl_seconds)

    entry = jobs.get(job_id)
    if entry is None:
        return _err_response(
            ErrorCode.JOB_NOT_FOUND,
            f"Job {job_id} not found or expired",
            HTTP_404_NOT_FOUND,
        )

    if entry.result is None:
        # Still running.
        return JSONResponse(
            TestOutputResult(
                ok=True,
                job_id=job_id,
                status="running",
            ).model_dump(),
        )

    return JSONResponse(entry.result.model_dump())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _err_response(code: ErrorCode, detail: str, status: int) -> JSONResponse:
    """Shape a machine-readable error response."""
    body = ErrorResponse(code=code, detail=detail)
    return JSONResponse(body.model_dump(), status_code=status)


def _prune_expired_jobs(jobs: dict[str, _JobEntry], *, ttl_s: int) -> None:
    """Lazy GC of finished jobs older than ``ttl_s`` seconds."""
    now = time.monotonic()
    expired = [jid for jid, e in jobs.items() if now - e.created_at > ttl_s]
    for jid in expired:
        jobs.pop(jid, None)


async def _resolve_tts(
    request: Request,
) -> Callable[[str, str | None], Awaitable[_SynthResult]] | None:
    """Return an async ``(text, voice) -> _SynthResult`` synth, or None.

    Resolution order:

    #. ``app.state.voice_test_tts_factory`` override (test hook — used by
       the unit tests to avoid touching ONNX).
    #. Registered :class:`TTSEngine` in the service registry. Populated
       whenever the pipeline is on **or** the voice-test already built a
       standalone engine in a prior request (cached on ``app.state``).
    #. Standalone construction using :func:`detect_tts_engine` +
       :func:`get_default_model_dir`. The resulting engine is cached on
       ``app.state.voice_test_cached_tts`` so subsequent requests don't
       re-initialise ONNX.

    Returning ``None`` surfaces as :attr:`ErrorCode.TTS_UNAVAILABLE`.
    """
    override = getattr(request.app.state, "voice_test_tts_factory", None)
    if callable(override):
        return override  # type: ignore[no-any-return]

    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        from sovyx.voice.tts_piper import TTSEngine

        if registry.is_registered(TTSEngine):
            engine = await registry.resolve(TTSEngine)

            async def _synth_from_registry(
                text: str,
                voice: str | None,  # noqa: ARG001
            ) -> _SynthResult:
                chunk = await engine.synthesize(text)
                return _SynthResult(audio=chunk.audio, sample_rate=chunk.sample_rate)

            return _synth_from_registry

    return await _resolve_standalone_tts(request)


async def _resolve_standalone_tts(
    request: Request,
) -> Callable[[str, str | None], Awaitable[_SynthResult]] | None:
    """Build + cache a standalone TTS engine for the wizard flow.

    The setup wizard is used *before* the pipeline is enabled, so the
    registry path above won't hit. We mirror the factory's resolution
    logic (:mod:`sovyx.voice.factory._create_piper_tts` /
    ``_create_kokoro_tts``) without pulling the rest of the pipeline
    (VAD, STT, wake-word) — those are irrelevant to a one-shot TTS test.
    """
    cached = getattr(request.app.state, "voice_test_cached_tts", None)
    if cached is not None:
        return cached  # type: ignore[no-any-return]

    from sovyx.voice.model_registry import detect_tts_engine, get_default_model_dir

    tts_kind = await asyncio.to_thread(detect_tts_engine)
    if tts_kind == "none":
        return None

    model_dir = get_default_model_dir()
    from sovyx.voice.tts_piper import TTSEngine

    engine: TTSEngine
    try:
        if tts_kind == "kokoro":
            from sovyx.voice.tts_kokoro import KokoroTTS

            engine = KokoroTTS(model_dir=model_dir / "kokoro")
        else:
            from sovyx.voice.tts_piper import PiperTTS

            engine = PiperTTS(model_dir=model_dir / "piper")
        await engine.initialize()
    except FileNotFoundError:
        logger.info("voice_test_tts_models_missing", tts_kind=tts_kind)
        return None
    except Exception:  # noqa: BLE001
        logger.warning("voice_test_tts_init_failed", tts_kind=tts_kind, exc_info=True)
        return None

    async def _synth(
        text: str,
        voice: str | None,  # noqa: ARG001
    ) -> _SynthResult:
        chunk = await engine.synthesize(text)
        return _SynthResult(audio=chunk.audio, sample_rate=chunk.sample_rate)

    request.app.state.voice_test_cached_tts = _synth
    return _synth


async def _run_output_job(
    *,
    entry: _JobEntry,
    job_id: str,
    text: str,
    voice: str | None,
    device_id: int | None,
    tts: Callable[[str, str | None], Awaitable[_SynthResult]],
    sink: AudioOutputSink,
) -> None:
    """Background task: synth → play → store result."""
    synth_ms: float = 0.0
    play_ms: float = 0.0
    peak_db: float | None = None
    try:
        start = asyncio.get_running_loop().time()
        synth = await tts(text, voice)
        synth_ms = (asyncio.get_running_loop().time() - start) * 1000

        if synth.audio.size > 0:
            import numpy as np  # noqa: PLC0415

            peak_lin = float(np.max(np.abs(synth.audio.astype(np.float32))) / 32_768.0)
            peak_db = _lin_to_db_safe(peak_lin)

        play_ms = await sink.play(
            synth.audio,
            sample_rate=synth.sample_rate,
            device_id=device_id,
        )
    except AudioSinkError as exc:
        entry.result = TestOutputResult(
            ok=False,
            job_id=job_id,
            status="error",
            code=exc.code,
            detail=exc.detail,
            phrase=text,
            synthesis_ms=round(synth_ms, 1),
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("voice_test_output_job_failed", job_id=job_id)
        entry.result = TestOutputResult(
            ok=False,
            job_id=job_id,
            status="error",
            code=ErrorCode.INTERNAL_ERROR,
            detail=f"Playback failed: {exc}",
            phrase=text,
            synthesis_ms=round(synth_ms, 1),
        )
        return

    entry.result = TestOutputResult(
        ok=True,
        job_id=job_id,
        status="done",
        phrase=text,
        synthesis_ms=round(synth_ms, 1),
        playback_ms=round(play_ms, 1),
        peak_db=round(peak_db, 1) if peak_db is not None else None,
    )
    logger.info(
        "voice_test_output_completed",
        job_id=job_id,
        synthesis_ms=round(synth_ms, 1),
        playback_ms=round(play_ms, 1),
        peak_db=peak_db,
    )


def _lin_to_db_safe(x: float) -> float:
    import math  # noqa: PLC0415

    if x <= 0.0 or math.isnan(x):
        return -120.0
    db = 20.0 * math.log10(x)
    return max(-120.0, db)


# ---------------------------------------------------------------------------
# HTTPException→JSON error shaping (kept explicit so the machine code is
# always present in the body)
# ---------------------------------------------------------------------------


def _raise_http(code: ErrorCode, detail: str, status: int) -> None:
    """Raise a shaped :class:`HTTPException` (helper for future refactors)."""
    raise HTTPException(
        status_code=status,
        detail={
            "ok": False,
            "code": code.value,
            "detail": detail,
        },
    )


__all__ = [
    "PROTOCOL_VERSION",
    "router",
]
