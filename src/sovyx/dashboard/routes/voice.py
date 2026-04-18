"""Voice status + models + setup endpoints."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.voice.cognitive_bridge import VoiceCognitiveBridge

logger = get_logger(__name__)

router = APIRouter(prefix="/api/voice", dependencies=[Depends(verify_token)])


@router.get("/status")
async def get_voice_status_endpoint(request: Request) -> JSONResponse:
    """Voice pipeline status — running state, models, hardware tier."""
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return JSONResponse(
            {"error": "Engine not running"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    from sovyx.dashboard.voice_status import get_voice_status

    status = await get_voice_status(registry)
    return JSONResponse(status)


@router.get("/models")
async def get_voice_models_endpoint(request: Request) -> JSONResponse:
    """Available voice models by hardware tier, with detected/active info."""
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return JSONResponse(
            {"error": "Engine not running"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    from sovyx.dashboard.voice_status import get_voice_models

    models = await get_voice_models(registry)
    return JSONResponse(models)


@router.get("/models/status")
async def get_voice_models_disk_status(request: Request) -> JSONResponse:
    """Return per-model disk presence + aggregate missing-size.

    Drives the setup-wizard's "model installed" green check — unlike
    :func:`get_voice_models_endpoint` (which serves the static tier
    matrix), this endpoint stats the filesystem and reports what's
    actually on disk.
    """
    from sovyx.voice.model_status import check_voice_models_status

    status = await asyncio.to_thread(check_voice_models_status)
    return JSONResponse(
        {
            "model_dir": status.model_dir,
            "all_installed": status.all_installed,
            "missing_count": status.missing_count,
            "missing_download_mb": status.missing_download_mb,
            "models": [
                {
                    "name": m.name,
                    "category": m.category,
                    "description": m.description,
                    "installed": m.installed,
                    "path": m.path,
                    "size_mb": m.size_mb,
                    "expected_size_mb": m.expected_size_mb,
                    "download_available": m.download_available,
                }
                for m in status.models
            ],
        }
    )


def _get_model_download_tracker(request: Request) -> dict[str, object]:
    """Lazy-init the per-app download tracker dict."""
    tracker = getattr(request.app.state, "voice_model_download_tracker", None)
    if tracker is None:
        tracker = {}
        request.app.state.voice_model_download_tracker = tracker
    assert isinstance(tracker, dict)  # noqa: S101  # invariant: always dict[str, _DownloadEntry]
    return tracker


@router.get("/voices")
async def list_voice_catalog() -> JSONResponse:
    """List every voice the Kokoro v1.0 model exposes, grouped by language.

    Drives the setup-wizard's language + voice picker. The wizard uses
    ``recommended_per_language`` as the default pick whenever the user
    changes language but hasn't yet picked a voice explicitly, and falls
    back to ``by_language`` to populate the per-language voice dropdown.

    The response shape is intentionally flat — no pagination, no
    filtering — because the catalog is 54 entries total and static
    across a release. Fetching once at wizard mount is cheap.
    """
    from sovyx.voice.voice_catalog import (
        SUPPORTED_LANGUAGES,
        all_voices,
        recommended_voice,
    )

    by_language: dict[str, list[dict[str, str]]] = {lang: [] for lang in SUPPORTED_LANGUAGES}
    for v in all_voices():
        by_language[v.language].append(
            {
                "id": v.id,
                "display_name": v.display_name,
                "language": v.language,
                "gender": v.gender,
            },
        )

    recommended_per_language: dict[str, str] = {}
    for lang in SUPPORTED_LANGUAGES:
        info = recommended_voice(lang)
        if info is not None:
            recommended_per_language[lang] = info.id

    return JSONResponse(
        {
            "supported_languages": sorted(SUPPORTED_LANGUAGES),
            "by_language": by_language,
            "recommended_per_language": recommended_per_language,
        },
    )


@router.post("/models/download")
async def start_voice_models_download(request: Request) -> JSONResponse:
    """Start a background download of all missing voice models.

    Returns the task_id immediately so the UI can poll
    ``GET /api/voice/models/download/{task_id}`` for progress. Parallel
    clicks return the existing task — the downloader is single-flight.
    """
    from sovyx.voice.model_status import (
        prune_finished,
        start_download,
    )

    tracker = _get_model_download_tracker(request)
    prune_finished(tracker)  # type: ignore[arg-type]
    entry = start_download(tracker)  # type: ignore[arg-type]
    p = entry.progress
    return JSONResponse(
        {
            "task_id": p.task_id,
            "status": p.status,
            "total_models": p.total_models,
            "completed_models": p.completed_models,
            "current_model": p.current_model,
            "error": p.error,
        }
    )


@router.get("/models/download/{task_id}")
async def get_voice_models_download_status(request: Request, task_id: str) -> JSONResponse:
    """Poll the progress of a background download task."""
    from sovyx.voice.model_status import prune_finished

    tracker = _get_model_download_tracker(request)
    prune_finished(tracker)  # type: ignore[arg-type]
    entry = tracker.get(task_id)
    if entry is None:
        return JSONResponse(
            {"error": "task_not_found", "detail": f"Task {task_id} not found or expired"},
            status_code=404,
        )
    p = entry.progress  # type: ignore[attr-defined]
    return JSONResponse(
        {
            "task_id": p.task_id,
            "status": p.status,
            "total_models": p.total_models,
            "completed_models": p.completed_models,
            "current_model": p.current_model,
            "error": p.error,
        }
    )


@router.get("/hardware-detect")
async def hardware_detect(request: Request) -> JSONResponse:
    """Detect hardware capabilities for voice pipeline.

    Returns CPU, RAM, GPU info, detected hardware tier, recommended
    models with sizes, and whether audio I/O devices are available.
    """
    from sovyx.voice.auto_select import detect_hardware
    from sovyx.voice.model_registry import get_models_for_tier

    try:
        hw = await asyncio.to_thread(detect_hardware)
    except Exception as exc:  # noqa: BLE001
        logger.warning("hardware_detection_failed", error=str(exc))
        return JSONResponse({"error": f"Hardware detection failed: {exc}"}, status_code=500)

    # Audio device detection — dedup by canonical name, prefer WASAPI over
    # MME/DirectSound/WDM-KS on Windows. See device_enum.py for *why* MME
    # gets demoted (silent-mic bug with non-native sample rates).
    audio_available = False
    input_devices: list[dict[str, object]] = []
    output_devices: list[dict[str, object]] = []
    try:
        from sovyx.voice.device_enum import enumerate_devices, pick_preferred

        entries = await asyncio.to_thread(enumerate_devices)
        in_preferred = pick_preferred(entries, kind="input")
        out_preferred = pick_preferred(entries, kind="output")
        input_devices = [
            {
                "index": e.index,
                "name": e.name,
                "is_default": e.is_os_default,
                "host_api": e.host_api_name,
            }
            for e in in_preferred
        ]
        output_devices = [
            {
                "index": e.index,
                "name": e.name,
                "is_default": e.is_os_default,
                "host_api": e.host_api_name,
            }
            for e in out_preferred
        ]
        audio_available = bool(input_devices and output_devices)
    except ImportError:
        logger.debug("sounddevice_not_installed")
    except Exception:  # noqa: BLE001
        logger.warning("audio_device_detection_failed", exc_info=True)

    # Recommended models for detected tier
    tier_name = hw.tier.name if hasattr(hw, "tier") else "DESKTOP_CPU"
    models = get_models_for_tier(tier_name)

    total_download_mb = sum(m.size_mb for m in models if m.download_available)

    return JSONResponse(
        {
            "hardware": {
                "cpu_cores": hw.cpu_cores,
                "ram_mb": hw.ram_mb,
                "has_gpu": hw.has_gpu,
                "gpu_vram_mb": hw.gpu_vram_mb,
                "tier": tier_name,
            },
            "audio": {
                "available": audio_available,
                "input_devices": input_devices,
                "output_devices": output_devices,
            },
            "recommended_models": [
                {
                    "name": m.name,
                    "category": m.category,
                    "size_mb": m.size_mb,
                    "download_available": m.download_available,
                    "description": m.description,
                }
                for m in models
            ],
            "total_download_mb": round(total_download_mb, 1),
        }
    )


@router.post("/enable")
async def enable_voice(request: Request) -> JSONResponse:
    """Enable the voice pipeline (hot-enable, no restart needed).

    Flow:
        1. Check Python voice deps (moonshine-voice, sounddevice).
        2. Check audio hardware availability.
        3. Check if pipeline already running (idempotent).
        4. Instantiate all components (VAD, STT, TTS, WakeWord).
        5. Register in ServiceRegistry.
        6. Persist to mind.yaml.
        7. Return active status.
    """
    # 0. Parse optional device selection + voice/language override
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    raw_input = body.get("input_device")
    raw_output = body.get("output_device")
    input_device: int | None = raw_input if isinstance(raw_input, int) else None
    output_device: int | None = raw_output if isinstance(raw_output, int) else None

    # Stable device identity — prefer name + host_api over index because
    # PortAudio indices are unstable across reboots / USB replugs.
    raw_input_name = body.get("input_device_name")
    raw_input_host_api = body.get("input_device_host_api")
    input_device_name: str | None = (
        raw_input_name if isinstance(raw_input_name, str) and raw_input_name else None
    )
    input_device_host_api: str | None = (
        raw_input_host_api if isinstance(raw_input_host_api, str) and raw_input_host_api else None
    )

    # voice_id + language come from the wizard's VoiceTestPicker. When
    # either is present, validate it against the catalog BEFORE we spin
    # up any models — a bad id here would otherwise surface as an opaque
    # ONNX error at first synthesis.
    raw_voice = body.get("voice_id")
    raw_language = body.get("language")
    request_voice_id: str | None = raw_voice if isinstance(raw_voice, str) and raw_voice else None
    request_language: str | None = (
        raw_language if isinstance(raw_language, str) and raw_language else None
    )

    if request_voice_id is not None or request_language is not None:
        from sovyx.voice import voice_catalog

        if request_voice_id is not None and voice_catalog.voice_info(request_voice_id) is None:
            return JSONResponse(
                {"ok": False, "error": f"Unknown voice id: {request_voice_id}"},
                status_code=400,
            )
        if request_language is not None:
            canonical = voice_catalog.normalize_language(request_language)
            if canonical not in voice_catalog.SUPPORTED_LANGUAGES:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": (
                            f"Unsupported language: {request_language!r}. "
                            f"Supported: {sorted(voice_catalog.SUPPORTED_LANGUAGES)}"
                        ),
                    },
                    status_code=400,
                )

    # 1. Check deps
    from sovyx.voice.model_registry import check_voice_deps, detect_tts_engine

    _installed, missing = check_voice_deps()
    tts_engine = detect_tts_engine()
    if missing:
        return JSONResponse(
            {
                "ok": False,
                "error": "missing_deps",
                "missing_deps": missing,
                "install_command": "pip install sovyx[voice]",
            },
            status_code=400,
        )
    if tts_engine == "none":
        return JSONResponse(
            {
                "ok": False,
                "error": "missing_deps",
                "missing_deps": [
                    {"module": "piper_phonemize or kokoro_onnx", "package": "piper-tts"}
                ],
                "install_command": "pip install piper-tts",
            },
            status_code=400,
        )

    # 2. Check audio
    audio_ok = False
    try:
        import sounddevice as sd  # noqa: PLC0415

        devices = sd.query_devices()
        has_in = any(d.get("max_input_channels", 0) > 0 for d in devices if isinstance(d, dict))
        has_out = any(d.get("max_output_channels", 0) > 0 for d in devices if isinstance(d, dict))
        audio_ok = has_in and has_out
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        logger.warning("audio_device_check_failed", exc_info=True)

    if not audio_ok:
        return JSONResponse(
            {"ok": False, "error": "No audio devices detected (microphone + speaker required)"},
            status_code=400,
        )

    # 3. Idempotent check
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        if registry.is_registered(VoicePipeline):
            return JSONResponse({"ok": True, "status": "already_active"})

    # 4. Create pipeline
    from sovyx.voice.factory import VoiceFactoryError, create_voice_pipeline

    event_bus = None
    cognitive_loop = None
    if registry is not None:
        from sovyx.cognitive.loop import CognitiveLoop
        from sovyx.engine.events import EventBus

        if registry.is_registered(EventBus):
            event_bus = await registry.resolve(EventBus)
        if registry.is_registered(CognitiveLoop):
            cognitive_loop = await registry.resolve(CognitiveLoop)

    # Closure holder — the bridge needs the pipeline, the pipeline needs the
    # callback. Fill the holder after the bundle is built.
    bridge_ref: list[VoiceCognitiveBridge | None] = [None]

    async def _on_perception(text: str, mind_id_str: str) -> None:
        """Feed a transcription into the cognitive loop via the bridge."""
        bridge = bridge_ref[0]
        if bridge is None or not text.strip():
            return
        from uuid import uuid4

        from sovyx.cognitive.gate import CognitiveRequest
        from sovyx.cognitive.perceive import Perception
        from sovyx.engine.types import ConversationId, MindId, PerceptionType

        cog_request = CognitiveRequest(
            perception=Perception(
                id=str(uuid4()),
                type=PerceptionType.USER_MESSAGE,
                source="voice",
                content=text,
            ),
            mind_id=MindId(mind_id_str),
            conversation_id=ConversationId(f"voice-{mind_id_str}"),
            conversation_history=[],
            person_name=None,
        )
        try:
            await bridge.process(cog_request)
        except Exception:  # noqa: BLE001
            logger.exception("voice_cognitive_bridge_failed")

    on_perception_cb = _on_perception if cognitive_loop is not None else None

    # Resolve per-mind language + voice so the pipeline TTS matches the
    # user's personality picks. Precedence: request body (wizard live
    # pick) > MindConfig (persisted from a previous /enable) > English
    # defaults. The dashboard-only "no mind loaded" branch keeps the
    # pipeline bootable on a fresh install before mind.yaml exists.
    mind_language = "en"
    mind_voice_id = ""
    mind_device_name = ""
    mind_device_host_api = ""
    mind_config_obj = getattr(request.app.state, "mind_config", None)
    if mind_config_obj is not None:
        mind_language = getattr(mind_config_obj, "language", "en") or "en"
        mind_voice_id = getattr(mind_config_obj, "voice_id", "") or ""
        mind_device_name = getattr(mind_config_obj, "voice_input_device_name", "") or ""
        mind_device_host_api = getattr(mind_config_obj, "voice_input_device_host_api", "") or ""

    effective_language = request_language or mind_language
    effective_voice_id = request_voice_id if request_voice_id is not None else mind_voice_id
    # Prefer stable (name, host_api) over index. Request > MindConfig > index-only.
    effective_device_name = input_device_name or mind_device_name or None
    effective_device_host_api = input_device_host_api or mind_device_host_api or None

    try:
        bundle = await create_voice_pipeline(
            event_bus=event_bus,
            on_perception=on_perception_cb,
            language=effective_language,
            voice_id=effective_voice_id,
            wake_word_enabled=False,
            mind_id=getattr(request.app.state, "mind_id", "default"),
            input_device=input_device,
            input_device_name=effective_device_name,
            input_device_host_api=effective_device_host_api,
            output_device=output_device,
        )
    except VoiceFactoryError as exc:
        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
                "missing_models": exc.missing_models,
            },
            status_code=400,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("voice_enable_failed")
        return JSONResponse(
            {"ok": False, "error": f"Pipeline creation failed: {exc}"},
            status_code=500,
        )

    # 5. Start capture + register. Capture start is the step that can
    # actually fail with a device error — if it does, tear the pipeline
    # down so we don't leave a half-wired registry.
    #
    # ``start_capture_with_fallback`` catches :class:`CaptureSilenceError`
    # (MME + non-native rate = zeros) and retries on preferred host APIs
    # before giving up. That single helper is what turns the previous
    # "pipeline running, mic silent, no logs" nightmare into a real
    # error path the UI can act on.
    from sovyx.voice._capture_task import CaptureSilenceError, start_capture_with_fallback

    try:
        await start_capture_with_fallback(
            bundle.capture_task,
            device_name=effective_device_name,
        )
    except CaptureSilenceError as exc:
        logger.error(
            "voice_capture_all_host_apis_silent",
            device=exc.device,
            host_api=exc.host_api,
            observed_peak_rms_db=exc.observed_peak_rms_db,
        )
        with contextlib.suppress(Exception):
            await bundle.pipeline.stop()
        return JSONResponse(
            {
                "ok": False,
                "error": "capture_silence",
                "detail": str(exc),
                "device": exc.device,
                "host_api": exc.host_api,
                "observed_peak_rms_db": exc.observed_peak_rms_db,
            },
            status_code=503,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("voice_capture_start_failed")
        with contextlib.suppress(Exception):
            await bundle.pipeline.stop()
        return JSONResponse(
            {"ok": False, "error": f"Audio capture failed to start: {exc}"},
            status_code=500,
        )

    # 5.5 Wire the cognitive bridge now that the pipeline exists. Streaming
    # (Jarvis illusion) defaults to the mind's LLM setting.
    if cognitive_loop is not None:
        from sovyx.voice.cognitive_bridge import (
            VoiceCognitiveBridge as _VoiceCognitiveBridge,
        )

        streaming = True
        mind_config = getattr(request.app.state, "mind_config", None)
        if mind_config is not None:
            llm_cfg = getattr(mind_config, "llm", None)
            if llm_cfg is not None:
                streaming = bool(getattr(llm_cfg, "streaming", True))
        bridge_ref[0] = _VoiceCognitiveBridge(
            cognitive_loop,
            bundle.pipeline,
            streaming=streaming,
        )

    if registry is not None:
        from sovyx.voice._capture_task import AudioCaptureTask
        from sovyx.voice.cognitive_bridge import VoiceCognitiveBridge
        from sovyx.voice.pipeline._orchestrator import VoicePipeline
        from sovyx.voice.stt import STTEngine
        from sovyx.voice.tts_piper import TTSEngine
        from sovyx.voice.vad import SileroVAD
        from sovyx.voice.wake_word import WakeWordDetector

        registry.register_instance(VoicePipeline, bundle.pipeline)
        registry.register_instance(AudioCaptureTask, bundle.capture_task)
        # Register each sub-component so /api/voice/status can report real
        # engine names instead of "No engine configured".
        registry.register_instance(SileroVAD, bundle.pipeline.vad)
        registry.register_instance(STTEngine, bundle.pipeline.stt)
        registry.register_instance(TTSEngine, bundle.pipeline.tts)
        if bundle.pipeline.config.wake_word_enabled:
            registry.register_instance(WakeWordDetector, bundle.pipeline.wake_word)
        if bridge_ref[0] is not None:
            registry.register_instance(VoiceCognitiveBridge, bridge_ref[0])

    # 6. Persist config
    #
    # Three writes happen here:
    #
    # * ``voice:`` section — legacy device metadata (kept for UI state).
    # * top-level ``voice_id`` / ``language`` — the real MindConfig
    #   fields consumed by the factory on next boot. Without this the
    #   wizard's voice pick evaporates when the daemon restarts.
    # * top-level ``voice_input_device_name`` / ``voice_input_device_host_api``
    #   — stable identity so a USB replug / reboot doesn't re-break the
    #   MME-silent-mic bug the wizard just worked around. We read what
    #   the capture task *actually* landed on (post-fallback), which may
    #   differ from the caller's request if a sibling host API rescued
    #   the open.
    #
    # We also update ``app.state.mind_config`` in place so the next
    # ``/enable`` (or any route that reads ``mind_config.voice_id``) sees
    # the new values without needing a restart.
    # On a real capture task ``host_api_name`` is ``str | None``; coerce here
    # so a mocked value can never leak into YAML / JSON serialisation.
    captured_host_api = bundle.capture_task.host_api_name
    persisted_host_api = (
        captured_host_api if isinstance(captured_host_api, str) else None
    ) or effective_device_host_api
    persisted_device_name = effective_device_name

    mind_yaml_path = getattr(request.app.state, "mind_yaml_path", None)
    if mind_yaml_path is not None:
        from pathlib import Path

        from sovyx.engine.config_editor import ConfigEditor

        editor = ConfigEditor()
        voice_cfg: dict[str, object] = {"enabled": True}
        if input_device is not None:
            voice_cfg["input_device"] = input_device
        if output_device is not None:
            voice_cfg["output_device"] = output_device
        await editor.update_section(Path(mind_yaml_path), "voice", voice_cfg)

        if request_voice_id is not None:
            await editor.set_scalar(Path(mind_yaml_path), "voice_id", request_voice_id)
        if request_language is not None:
            await editor.set_scalar(Path(mind_yaml_path), "language", request_language)
        if persisted_device_name:
            await editor.set_scalar(
                Path(mind_yaml_path),
                "voice_input_device_name",
                persisted_device_name,
            )
        if persisted_host_api:
            await editor.set_scalar(
                Path(mind_yaml_path),
                "voice_input_device_host_api",
                persisted_host_api,
            )

    if mind_config_obj is not None:
        if request_voice_id is not None:
            with contextlib.suppress(Exception):
                mind_config_obj.voice_id = request_voice_id
        if request_language is not None:
            with contextlib.suppress(Exception):
                mind_config_obj.language = request_language
        if persisted_device_name:
            with contextlib.suppress(Exception):
                mind_config_obj.voice_input_device_name = persisted_device_name
        if persisted_host_api:
            with contextlib.suppress(Exception):
                mind_config_obj.voice_input_device_host_api = persisted_host_api

    # ``host_api_name`` is ``str | None`` on a real :class:`AudioCaptureTask`
    # but tests pass bare ``MagicMock()`` stand-ins, so coerce to a JSON-safe
    # type before returning — a response body is never the right place to
    # leak a mock object into.
    host_api_for_response = (
        bundle.capture_task.host_api_name
        if isinstance(bundle.capture_task.host_api_name, str)
        else None
    )
    logger.info(
        "voice_pipeline_hot_enabled",
        tts=tts_engine,
        language=effective_language,
        voice_id=effective_voice_id or "<auto>",
        host_api=host_api_for_response or "unknown",
    )
    return JSONResponse(
        {
            "ok": True,
            "status": "active",
            "tts_engine": tts_engine,
            "host_api": host_api_for_response,
        },
    )


@router.post("/disable")
async def disable_voice(request: Request) -> JSONResponse:
    """Disable the voice pipeline (graceful shutdown).

    Order of operations:
        1. Stop the audio capture task (closes mic stream).
        2. Stop the pipeline (drains TTS, resets state).
        3. Deregister both so the next enable creates fresh instances.
    """
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        from sovyx.voice._capture_task import AudioCaptureTask
        from sovyx.voice.cognitive_bridge import VoiceCognitiveBridge
        from sovyx.voice.pipeline._orchestrator import VoicePipeline
        from sovyx.voice.stt import STTEngine
        from sovyx.voice.tts_piper import TTSEngine
        from sovyx.voice.vad import SileroVAD
        from sovyx.voice.wake_word import WakeWordDetector

        if registry.is_registered(AudioCaptureTask):
            try:
                capture = await registry.resolve(AudioCaptureTask)
                await capture.stop()
                logger.info("voice_capture_stopped")
            except Exception:  # noqa: BLE001
                logger.warning("voice_capture_stop_failed", exc_info=True)
            finally:
                registry.deregister(AudioCaptureTask)

        if registry.is_registered(VoicePipeline):
            try:
                pipeline = await registry.resolve(VoicePipeline)
                await pipeline.stop()
                logger.info("voice_pipeline_stopped")
            except Exception:  # noqa: BLE001
                logger.warning("voice_pipeline_stop_failed", exc_info=True)
            finally:
                registry.deregister(VoicePipeline)

        # Deregister sub-components so the next enable re-registers fresh
        # instances bound to the new pipeline.
        for interface in (
            SileroVAD,
            STTEngine,
            TTSEngine,
            WakeWordDetector,
            VoiceCognitiveBridge,
        ):
            if registry.is_registered(interface):
                registry.deregister(interface)

    # Persist config
    mind_yaml_path = getattr(request.app.state, "mind_yaml_path", None)
    if mind_yaml_path is not None:
        from pathlib import Path

        from sovyx.engine.config_editor import ConfigEditor

        editor = ConfigEditor()
        await editor.update_section(
            Path(mind_yaml_path),
            "voice",
            {"enabled": False},
        )
        logger.info("voice_disabled_via_wizard")
        return JSONResponse({"ok": True})

    return JSONResponse(
        {"ok": False, "error": "No mind.yaml path available"},
        status_code=503,
    )
