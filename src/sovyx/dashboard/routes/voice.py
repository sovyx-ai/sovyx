"""Voice status + models + setup endpoints."""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger

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

    # Audio device detection (deduplicated, with OS default marker)
    audio_available = False
    input_devices: list[dict[str, object]] = []
    output_devices: list[dict[str, object]] = []
    try:
        import sounddevice as sd  # noqa: PLC0415

        devices = sd.query_devices()
        default_in, default_out = sd.default.device
        seen_in: set[str] = set()
        seen_out: set[str] = set()
        for i, d in enumerate(devices):
            if not isinstance(d, dict):
                continue
            name = str(d.get("name", "unknown"))
            if d.get("max_input_channels", 0) > 0 and name not in seen_in:
                seen_in.add(name)
                input_devices.append({"index": i, "name": name, "is_default": i == default_in})
            if d.get("max_output_channels", 0) > 0 and name not in seen_out:
                seen_out.add(name)
                output_devices.append({"index": i, "name": name, "is_default": i == default_out})
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
    # 0. Parse optional device selection
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
    if registry is not None:
        from sovyx.engine.events import EventBus

        if registry.is_registered(EventBus):
            event_bus = await registry.resolve(EventBus)

    try:
        bundle = await create_voice_pipeline(
            event_bus=event_bus,
            wake_word_enabled=False,
            mind_id=getattr(request.app.state, "mind_id", "default"),
            input_device=input_device,
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
    try:
        await bundle.capture_task.start()
    except Exception as exc:  # noqa: BLE001
        logger.exception("voice_capture_start_failed")
        with contextlib.suppress(Exception):
            await bundle.pipeline.stop()
        return JSONResponse(
            {"ok": False, "error": f"Audio capture failed to start: {exc}"},
            status_code=500,
        )

    if registry is not None:
        from sovyx.voice._capture_task import AudioCaptureTask
        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        registry.register_instance(VoicePipeline, bundle.pipeline)
        registry.register_instance(AudioCaptureTask, bundle.capture_task)

    # 6. Persist config
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

    logger.info("voice_pipeline_hot_enabled", tts=tts_engine)
    return JSONResponse({"ok": True, "status": "active", "tts_engine": tts_engine})


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
        from sovyx.voice.pipeline._orchestrator import VoicePipeline

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
