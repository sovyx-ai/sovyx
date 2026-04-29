"""Voice status + models + setup endpoints."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sovyx.voice.cognitive_bridge import VoiceCognitiveBridge
    from sovyx.voice.health.contract import MixerCardSnapshot

logger = get_logger(__name__)

router = APIRouter(prefix="/api/voice", dependencies=[Depends(verify_token)])


@router.get("/frame-history")
async def get_voice_frame_history(
    request: Request,
    limit: int = 100,
) -> JSONResponse:
    """Return the recent typed frames recorded by the voice pipeline.

    Mission §1.1 Hybrid Option C — Pipecat-aligned typed frames
    (UserStartedSpeakingFrame, TranscriptionFrame, etc.) recorded at
    the 5 transition sites + the BargeInInterruptionFrame emitted at
    every cancel_speech_chain exit. The dashboard's call-flow widget
    consumes this endpoint to render the per-utterance frame timeline.

    Args:
        limit: Maximum number of frames to return. Bounds: 1-256.
            Out-of-range values are clamped at the boundary.

    Returns:
        JSON: ``{"frames": [{frame_type, timestamp_monotonic,
        utterance_id, ...}, ...], "total_recorded": int}``.

        ``total_recorded`` is the size of the bounded ring buffer at
        snapshot time (≤ 256 by default); ``frames`` is the newest
        ``limit`` frames in oldest-first order.

        Returns 503 when the pipeline registry is not yet available
        (boot in progress or voice disabled).
    """
    bounded_limit = max(1, min(256, limit))
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return JSONResponse(
            {"error": "Engine not running"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        from sovyx.voice.pipeline._frame_types import _frame_to_dict
        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        pipeline = registry.get(VoicePipeline)
    except (KeyError, LookupError):
        return JSONResponse(
            {"error": "Voice pipeline not registered"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    history = pipeline.frame_history
    total = len(history)
    # Newest-first slice: tail of the deque is newest, take last
    # ``bounded_limit`` frames.
    selected = history[-bounded_limit:] if bounded_limit < total else history
    serialised = [_frame_to_dict(f) for f in selected]
    return JSONResponse(
        {
            "frames": serialised,
            "total_recorded": total,
            "limit_applied": bounded_limit,
        },
    )


@router.get("/restart-history")
async def get_voice_restart_history(
    request: Request,
    limit: int = 50,
) -> JSONResponse:
    """Return recent ``CaptureRestartFrame`` entries from the pipeline ring buffer.

    Voice Windows Paranoid Mission §C — capture-task restart events
    (substrate change, APO bypass engaged, overflow, manual). The
    dashboard's restart-history widget renders one timeline of "what
    happened on the mic" for post-incident forensics.

    Wired-up in T33 (v0.25.0) — emitters in
    :class:`sovyx.voice.capture.RestartMixin` populate the ring
    buffer at every successful restart (T32 wire-up). Frames are
    filtered to ``CaptureRestartFrame`` subclass instances and
    serialised via :func:`_frame_to_dict` (which delegates to
    :func:`dataclasses.asdict`) so every dataclass field reaches
    the wire — the dashboard's
    ``VoiceRestartHistoryResponseSchema`` pins the contract.

    Args:
        limit: Maximum number of restart frames to return. Bounds:
            1-256. Out-of-range values are clamped at the boundary.

    Returns:
        JSON: ``{"frames": [...CaptureRestartFrame...], "total": int,
        "limit": int}``. ``total`` is the FULL count of restart
        frames in the bounded ring buffer (≤ 256 by default);
        ``frames`` is the newest ``limit`` frames in newest-first
        order so the dashboard's timeline can render top-down.

        Returns 200 with ``frames=[], total=0`` when no restart has
        occurred since the daemon started OR the pipeline is not
        yet registered (boot still in progress).

        Returns 503 when the engine registry is not yet available
        (boot in progress or voice disabled).
    """
    bounded_limit = max(1, min(256, limit))
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return JSONResponse(
            {"error": "Engine not running"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    # T33 — wire-up to the real payload. Filters
    # ``VoicePipeline.frame_history`` for ``CaptureRestartFrame``
    # subclass instances (T32 emitters land them as the substrate
    # mutates), serialises via the canonical ``_frame_to_dict``
    # helper, and returns the most recent ``bounded_limit`` entries.
    #
    # Empty-frame-history fallback: when the pipeline isn't
    # registered yet (boot in progress) OR when no restart has
    # occurred since the daemon started, ``frames`` is the empty
    # list and ``total`` is 0. The dashboard's restart-history
    # widget renders the empty-state placeholder in that case.
    from sovyx.voice.pipeline._frame_types import (
        CaptureRestartFrame,
        _frame_to_dict,
    )
    from sovyx.voice.pipeline._orchestrator import VoicePipeline

    if not registry.is_registered(VoicePipeline):
        return JSONResponse(
            {
                "frames": [],
                "total": 0,
                "limit": bounded_limit,
            },
        )
    pipeline = registry.get(VoicePipeline)
    restart_frames = [f for f in pipeline.frame_history if isinstance(f, CaptureRestartFrame)]
    total = len(restart_frames)
    # Most recent first — the dashboard's timeline renders newest at
    # the top by default. ``frame_history`` is a deque ordered by
    # insertion (oldest first), so we take the tail and reverse.
    selected = list(reversed(restart_frames[-bounded_limit:]))
    return JSONResponse(
        {
            "frames": [_frame_to_dict(f) for f in selected],
            "total": total,
            "limit": bounded_limit,
        },
    )


@router.get("/bypass-tier-status")
async def get_voice_bypass_tier_status(request: Request) -> JSONResponse:
    """Return current bypass-tier health snapshot (Tier 1 / 2 / 3).

    Voice Windows Paranoid Mission §B — a single snapshot of which
    bypass tier is currently engaged on the active capture endpoint
    plus per-tier attempt / success counters since pipeline start.
    The dashboard renders a "current bypass tier" badge + a per-tier
    success-rate widget for operator validation pilots.

    v0.24.0 (foundation): no bypass strategies are wired yet (Tier 1
    + Tier 2 stubs land in T13/T14 of the wire-up phase). The
    endpoint always returns the empty-state shape with
    ``current_bypass_tier=null``. Wire-up in v0.25.0 populates from
    the metric counters
    (``voice.health.bypass.tier1_raw.{attempted,outcome}``,
    ``voice.health.bypass.tier2_host_api_rotate.{attempted,outcome}``,
    ``voice.health.bypass_strategy.verdicts``) and the
    ``CaptureIntegrityCoordinator`` strategy state. Shipping the stub
    now pins the wire contract via
    ``VoiceBypassTierStatusResponseSchema``
    (``dashboard/src/types/schemas.ts``).

    Returns:
        JSON: ``{"current_bypass_tier": null|1|2|3,
        "tier1_raw_attempted": int, "tier1_raw_succeeded": int,
        "tier2_host_api_rotate_attempted": int,
        "tier2_host_api_rotate_succeeded": int,
        "tier3_wasapi_exclusive_attempted": int,
        "tier3_wasapi_exclusive_succeeded": int}``.

        Returns 503 when the engine registry is not yet available.
    """
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return JSONResponse(
            {"error": "Engine not running"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    # v0.24.0 stub. Wire-up in v0.25.0 reads the counter data points
    # from the metrics registry and the active strategy from
    # CaptureIntegrityCoordinator. Empty-state shape matches
    # VoiceBypassTierStatusResponseSchema in dashboard zod.
    return JSONResponse(
        {
            "current_bypass_tier": None,
            "tier1_raw_attempted": 0,
            "tier1_raw_succeeded": 0,
            "tier2_host_api_rotate_attempted": 0,
            "tier2_host_api_rotate_succeeded": 0,
            "tier3_wasapi_exclusive_attempted": 0,
            "tier3_wasapi_exclusive_succeeded": 0,
        },
    )


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
            "error_code": p.error_code,
            "retry_after_seconds": p.retry_after_seconds,
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
            "error_code": p.error_code,
            "retry_after_seconds": p.retry_after_seconds,
        }
    )


@router.get("/capture-diagnostics")
async def capture_diagnostics(request: Request) -> JSONResponse:
    """Report the Windows capture-APO chain attached to the active mic.

    Surfaces the same data that powers the pipeline's auto-bypass logic:
    every active capture endpoint, its friendly name, the list of known
    APOs loaded by the Windows audio engine, and a single
    ``voice_clarity_active`` bit per endpoint. The setup wizard and the
    "voice not hearing me" troubleshooting panel call this to decide
    whether to offer the "enable WASAPI exclusive" action.

    On non-Windows platforms the underlying detector returns an empty
    list, so ``endpoints`` is ``[]`` and ``voice_clarity_active`` is
    ``False`` — the endpoint is safe to call unconditionally.

    The response also echoes the currently-resolved input device name
    (from an already-running pipeline, when one is registered) so the
    UI can highlight the *active* endpoint without re-running device
    resolution from the frontend.
    """
    from sovyx.voice._apo_detector import detect_capture_apos, find_endpoint_report

    try:
        reports = await asyncio.to_thread(detect_capture_apos)
    except Exception as exc:  # noqa: BLE001
        logger.warning("voice_apo_detection_failed", error=str(exc))
        return JSONResponse(
            {
                "error": f"Capture-APO scan failed: {exc}",
                "endpoints": [],
                "voice_clarity_active": False,
            }
        )

    # Prefer the device name from the running capture task; fall back to
    # the mind.yaml-persisted name so diagnostics still work before enable.
    active_device_name: str | None = None
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        with contextlib.suppress(Exception):
            from sovyx.voice._capture_task import AudioCaptureTask

            if registry.is_registered(AudioCaptureTask):
                capture = await registry.resolve(AudioCaptureTask)
                active_device_name = getattr(capture, "input_device_name", None)

    active_report = find_endpoint_report(reports, device_name=active_device_name)

    endpoints = [
        {
            "endpoint_id": r.endpoint_id,
            "endpoint_name": r.endpoint_name,
            "device_interface_name": r.device_interface_name,
            "enumerator": r.enumerator,
            "fx_binding_count": r.fx_binding_count,
            "known_apos": list(r.known_apos),
            "raw_clsids": list(r.raw_clsids),
            "voice_clarity_active": r.voice_clarity_active,
            "is_active_device": active_report is not None
            and r.endpoint_id == active_report.endpoint_id,
        }
        for r in reports
    ]

    any_clarity = any(r.voice_clarity_active for r in reports)
    active_clarity = bool(active_report is not None and active_report.voice_clarity_active)

    # T10 — Linux session-manager grab report. No-op on Windows/macOS
    # (the detector itself returns has_grab=None with
    # detection_method="unavailable" on those platforms).
    session_manager_grab = await _collect_session_manager_grab_report()

    return JSONResponse(
        {
            "platform_supported": bool(reports) or _is_windows(),
            "active_device_name": active_device_name,
            "active_endpoint": (
                {
                    "endpoint_id": active_report.endpoint_id,
                    "endpoint_name": active_report.endpoint_name,
                    "device_interface_name": active_report.device_interface_name,
                    "known_apos": list(active_report.known_apos),
                    "voice_clarity_active": active_report.voice_clarity_active,
                }
                if active_report is not None
                else None
            ),
            "voice_clarity_active": active_clarity,
            "any_voice_clarity_active": any_clarity,
            "endpoints": endpoints,
            "session_manager_grab": session_manager_grab,
            "fix_suggestion": (
                "Open the mic in WASAPI exclusive mode to bypass the APO chain. "
                "Set SOVYX_TUNING__VOICE__CAPTURE_WASAPI_EXCLUSIVE=true, or leave "
                "voice_clarity_autofix enabled (default) and Sovyx will switch "
                "automatically after the pipeline goes deaf."
            )
            if active_clarity
            else None,
        }
    )


async def _collect_session_manager_grab_report() -> dict[str, object]:
    """Invoke the Linux session-manager detector and return a JSON-safe payload.

    Never raises. On non-Linux platforms the detector returns
    ``has_grab=None`` with ``detection_method="unavailable"`` which
    we surface verbatim — the dashboard renders a neutral "not
    applicable" state.
    """
    import dataclasses as _dc

    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice._session_manager_detector import detect_session_manager_grab

    try:
        report = await detect_session_manager_grab(tuning=VoiceTuningConfig())
    except Exception as exc:  # noqa: BLE001 — diagnostics endpoint must never 500
        logger.debug("voice_capture_diagnostics_detector_failed", exc_info=True)
        return {
            "has_grab": None,
            "detection_method": "unavailable",
            "grabbing_processes": [],
            "evidence": f"detector invocation failed: {exc}",
        }
    return {
        "has_grab": report.has_grab,
        "detection_method": report.detection_method,
        "grabbing_processes": [_dc.asdict(p) for p in report.grabbing_processes],
        "evidence": report.evidence,
    }


def _is_windows() -> bool:
    """Return True when running on Windows (module-level for patchability)."""
    import sys as _sys

    return _sys.platform == "win32"


def _enumerate_alternative_devices() -> list[dict[str, object]]:
    """Return session-manager-virtual / OS-default inputs for the UI banner.

    Used by the ``CaptureDeviceContendedError`` handler (T7) so the
    dashboard can render clickable "try this device instead" chips.
    Fails soft: returns ``[]`` when enumeration fails (no ``sounddevice``,
    headless CI) — the UI just shows the suggested-action tokens
    without device-specific chips.
    """
    try:
        from sovyx.voice.device_enum import DeviceKind, enumerate_devices
    except Exception:  # noqa: BLE001 — import failure on exotic builds
        return []

    try:
        devices = enumerate_devices()
    except Exception:  # noqa: BLE001
        return []

    alternatives: list[dict[str, object]] = []
    for entry in devices:
        if entry.max_input_channels <= 0:
            continue
        if entry.kind not in {DeviceKind.SESSION_MANAGER_VIRTUAL, DeviceKind.OS_DEFAULT}:
            continue
        alternatives.append(
            {
                "index": entry.index,
                "name": entry.name,
                "host_api": entry.host_api_name,
                "kind": str(entry.kind),
                "max_input_channels": entry.max_input_channels,
                "default_samplerate": entry.default_samplerate,
            }
        )
    return alternatives


@router.post("/capture-exclusive")
async def set_capture_exclusive(request: Request) -> JSONResponse:
    """Persist + hot-apply the WASAPI-exclusive capture flag.

    Body: ``{"enabled": bool}`` (default ``True``).

    Flow:
        1. Persist ``tuning.voice.capture_wasapi_exclusive`` to
           ``system.yaml`` via :class:`ConfigEditor` so the choice
           survives restart.
        2. Mutate the in-memory ``EngineConfig`` so any code path that
           reads the live config sees the new value immediately.
        3. If ``enabled=True`` and a capture task is running, call
           :meth:`AudioCaptureTask.request_exclusive_restart` to
           re-open the mic in exclusive mode *without* a full pipeline
           restart — same code path the auto-bypass uses.
        4. If ``enabled=False`` while the pipeline is running, we
           persist but do not tear down the current stream (that
           requires a full pipeline restart). The response reports
           ``applied_immediately=False`` so the UI can prompt the user.

    Returns:
        ``{"ok": True, "enabled": bool, "persisted": bool,
        "applied_immediately": bool}``.
    """
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    enabled = bool(body.get("enabled", True))

    # 1. Persist to system.yaml
    persisted = False
    config_path = getattr(request.app.state, "config_path", None)
    if config_path is not None:
        from pathlib import Path

        from sovyx.engine.config_editor import ConfigEditor

        try:
            editor = ConfigEditor()
            await editor.update_section(
                Path(config_path),
                "tuning.voice",
                {"capture_wasapi_exclusive": enabled},
            )
            persisted = True
        except Exception:  # noqa: BLE001
            logger.warning("capture_exclusive_persist_failed", exc_info=True)

    # 2. Hot-update in-memory config so live readers see the change.
    engine_config = getattr(request.app.state, "engine_config", None)
    if engine_config is not None:
        with contextlib.suppress(Exception):
            engine_config.tuning.voice.capture_wasapi_exclusive = enabled

    # 3. Apply immediately if enabling and pipeline is running.
    # v0.20.2 / Bug C — ``applied_immediately`` now reflects whether
    # WASAPI actually granted exclusive mode, not merely whether the
    # restart code path ran. ``engaged`` and ``verdict`` let the UI
    # surface a warning banner when the reopen landed in shared mode
    # (device held by another app, policy denied) — in which case the
    # APO chain is still in the signal path and the user has to close
    # the competing app or disable the APO manually.
    applied_immediately = False
    restart_verdict: str | None = None
    restart_detail: str | None = None
    if enabled:
        registry = getattr(request.app.state, "registry", None)
        if registry is not None:
            with contextlib.suppress(Exception):
                from sovyx.voice._capture_task import AudioCaptureTask

                if registry.is_registered(AudioCaptureTask):
                    capture = await registry.resolve(AudioCaptureTask)
                    result = await capture.request_exclusive_restart()
                    applied_immediately = result.engaged
                    restart_verdict = result.verdict.value
                    restart_detail = result.detail

    logger.info(
        "capture_exclusive_updated",
        enabled=enabled,
        persisted=persisted,
        applied_immediately=applied_immediately,
        verdict=restart_verdict,
    )
    response: dict[str, object] = {
        "ok": True,
        "enabled": enabled,
        "persisted": persisted,
        "applied_immediately": applied_immediately,
    }
    if restart_verdict is not None:
        response["verdict"] = restart_verdict
    if restart_detail is not None:
        response["detail"] = restart_detail
    return JSONResponse(response)


def _is_linux() -> bool:
    """Return True when running on Linux (module-level for patchability)."""
    import sys as _sys

    return _sys.platform == "linux"


def _amixer_available() -> bool:
    """Return True when ``amixer`` is resolvable on ``PATH``.

    Extracted to a module-level helper so tests can patch it without
    shelling out to the real filesystem. Used by both diagnostics and
    reset endpoints to distinguish "Linux host without alsa-utils"
    (graceful no-op) from "Linux host with alsa-utils but nothing
    saturating" (healthy state).
    """
    import shutil

    return shutil.which("amixer") is not None


def _serialize_mixer_snapshots(
    snapshots: Sequence[MixerCardSnapshot],
) -> list[dict[str, object]]:
    """Render :class:`MixerCardSnapshot` list for JSON transport.

    Keeps field names aligned with
    ``dashboard/src/types/schemas.ts::LinuxMixerCardSchema`` — a rename
    here requires a matching rename on the zod schema and the card
    component's prop type.
    """
    payload: list[dict[str, object]] = []
    for snap in snapshots:
        controls_payload: list[dict[str, object]] = []
        for ctl in snap.controls:
            controls_payload.append(
                {
                    "name": ctl.name,
                    "min_raw": ctl.min_raw,
                    "max_raw": ctl.max_raw,
                    "current_raw": ctl.current_raw,
                    "current_db": ctl.current_db,
                    "max_db": ctl.max_db,
                    "is_boost_control": ctl.is_boost_control,
                    "saturation_risk": ctl.saturation_risk,
                    "asymmetric": ctl.asymmetric,
                }
            )
        payload.append(
            {
                "card_index": snap.card_index,
                "card_id": snap.card_id,
                "card_longname": snap.card_longname,
                "aggregated_boost_db": round(snap.aggregated_boost_db, 2),
                "saturation_warning": snap.saturation_warning,
                "controls": controls_payload,
            }
        )
    return payload


@router.get("/linux-mixer-diagnostics")
async def linux_mixer_diagnostics(request: Request) -> JSONResponse:
    """Snapshot of every ALSA card's gain state — Linux-only.

    Drives the dashboard's ``LinuxMicGainCard`` component. Non-Linux
    hosts receive ``platform_supported=False`` so the UI can render a
    disabled card with an explanatory tooltip rather than erroring.

    Response shape (Linux):
        ``{"platform_supported": True, "amixer_available": bool,
        "snapshots": [MixerCardSchema], "aggregated_boost_db_ceiling":
        float, "saturation_ratio_ceiling": float,
        "reset_enabled_by_default": bool}``

    Side-effect-free: calls
    :func:`sovyx.voice.health._linux_mixer_probe.enumerate_alsa_mixer_snapshots`
    behind :func:`asyncio.to_thread` so the subprocess fan-out never
    blocks the event loop. Runs unauthenticated-free because the router
    already enforces ``verify_token``.
    """
    del request  # unused — kept for FastAPI dependency resolution parity

    from sovyx.engine.config import VoiceTuningConfig

    tuning = VoiceTuningConfig()

    if not _is_linux():
        return JSONResponse(
            {
                "platform_supported": False,
                "amixer_available": False,
                "snapshots": [],
                "aggregated_boost_db_ceiling": (tuning.linux_mixer_aggregated_boost_db_ceiling),
                "saturation_ratio_ceiling": (tuning.linux_mixer_saturation_ratio_ceiling),
                "reset_enabled_by_default": (tuning.linux_alsa_mixer_reset_enabled),
            }
        )

    from sovyx.voice.health._linux_mixer_probe import (
        enumerate_alsa_mixer_snapshots,
    )

    amixer_available = _amixer_available()
    try:
        snapshots = await asyncio.to_thread(enumerate_alsa_mixer_snapshots)
    except Exception:  # noqa: BLE001
        logger.warning("linux_mixer_diagnostics_probe_failed", exc_info=True)
        snapshots = []

    return JSONResponse(
        {
            "platform_supported": True,
            "amixer_available": amixer_available,
            "snapshots": _serialize_mixer_snapshots(snapshots),
            "aggregated_boost_db_ceiling": (tuning.linux_mixer_aggregated_boost_db_ceiling),
            "saturation_ratio_ceiling": (tuning.linux_mixer_saturation_ratio_ceiling),
            "reset_enabled_by_default": tuning.linux_alsa_mixer_reset_enabled,
        }
    )


@router.post("/linux-mixer-reset")
async def linux_mixer_reset(request: Request) -> JSONResponse:
    """Reset saturated ALSA gain controls on one card — user-initiated.

    Body (all fields optional)::

        {"card_index": int}

    When ``card_index`` is omitted, the endpoint auto-selects the
    saturating card if exactly one card has
    :attr:`MixerCardSnapshot.saturation_warning=True`; multiple
    saturating cards produce ``ok=False`` with ``reason="ambiguous_card"``
    so the user can retry with an explicit index.

    Applied controls are the ones flagged
    :attr:`MixerControlSnapshot.saturation_risk=True` on the target
    card — never the full control list. The write is atomic:
    :func:`apply_mixer_reset` rolls back every successful mutation if
    any individual ``amixer sset`` call fails.

    Response (success)::

        {"ok": True, "card_index": int, "card_id": str,
         "card_longname": str, "applied_controls": [[name, raw], ...],
         "reverted_controls": [[name, raw], ...]}

    Response (failure modes)::

        {"ok": False, "reason":
            "not_linux" | "amixer_unavailable" | "no_snapshots" |
            "ambiguous_card" | "card_not_found" | "not_saturating" |
            "no_controls_to_reset" | "apply_failed",
         "detail": str, "reason_code": str (from BypassApplyError when
         applicable)}

    The ALSA mixer change persists until reboot or manual override.
    The endpoint does not persist anything to ``system.yaml`` — this is
    a one-shot remediation, independent of the coordinator-driven
    auto-bypass (governed by
    :attr:`VoiceTuningConfig.linux_alsa_mixer_reset_enabled`).
    """
    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice.health._linux_mixer_apply import apply_mixer_reset
    from sovyx.voice.health._linux_mixer_probe import (
        enumerate_alsa_mixer_snapshots,
    )
    from sovyx.voice.health.bypass._strategy import BypassApplyError

    if not _is_linux():
        return JSONResponse(
            {
                "ok": False,
                "reason": "not_linux",
                "detail": ("Linux ALSA mixer reset is only available on Linux hosts."),
            }
        )
    if not _amixer_available():
        return JSONResponse(
            {
                "ok": False,
                "reason": "amixer_unavailable",
                "detail": (
                    "`amixer` not found on PATH — install the alsa-utils "
                    "package to enable mixer remediation."
                ),
            }
        )

    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    raw_card_index = body.get("card_index")
    requested_card_index: int | None
    if raw_card_index is None:
        requested_card_index = None
    else:
        try:
            requested_card_index = int(raw_card_index)
        except (TypeError, ValueError):
            return JSONResponse(
                {
                    "ok": False,
                    "reason": "invalid_card_index",
                    "detail": ("card_index must be an integer from /proc/asound/cards."),
                }
            )

    snapshots = await asyncio.to_thread(enumerate_alsa_mixer_snapshots)
    if not snapshots:
        return JSONResponse(
            {
                "ok": False,
                "reason": "no_snapshots",
                "detail": (
                    "amixer returned no cards — the audio subsystem may be "
                    "unreachable or no card exposes a mixer."
                ),
            }
        )

    target = None
    if requested_card_index is not None:
        for snap in snapshots:
            if snap.card_index == requested_card_index:
                target = snap
                break
        if target is None:
            return JSONResponse(
                {
                    "ok": False,
                    "reason": "card_not_found",
                    "detail": (
                        f"No ALSA card with index {requested_card_index} was reported by amixer."
                    ),
                }
            )
    else:
        saturating = [s for s in snapshots if s.saturation_warning]
        if not saturating:
            return JSONResponse(
                {
                    "ok": False,
                    "reason": "not_saturating",
                    "detail": (
                        "No ALSA card currently reports a saturation warning — nothing to reset."
                    ),
                }
            )
        if len(saturating) > 1:
            return JSONResponse(
                {
                    "ok": False,
                    "reason": "ambiguous_card",
                    "detail": (
                        "Multiple cards report saturation — re-submit with an explicit card_index."
                    ),
                    "candidate_card_indexes": [s.card_index for s in saturating],
                }
            )
        target = saturating[0]

    controls_to_reset = [c for c in target.controls if c.saturation_risk]
    if not controls_to_reset:
        return JSONResponse(
            {
                "ok": False,
                "reason": "no_controls_to_reset",
                "detail": (
                    f"Card {target.card_index} ({target.card_id}) has no "
                    "individual control flagged as saturating — nothing to "
                    "reset."
                ),
                "card_index": target.card_index,
                "card_id": target.card_id,
            }
        )

    tuning = VoiceTuningConfig()
    try:
        result = await apply_mixer_reset(
            card_index=target.card_index,
            controls_to_reset=controls_to_reset,
            tuning=tuning,
        )
    except BypassApplyError as exc:
        logger.warning(
            "linux_mixer_reset_apply_failed",
            card_index=target.card_index,
            reason=exc.reason,
            detail=str(exc),
        )
        return JSONResponse(
            {
                "ok": False,
                "reason": "apply_failed",
                "reason_code": exc.reason,
                "detail": str(exc),
                "card_index": target.card_index,
                "card_id": target.card_id,
            }
        )

    logger.info(
        "linux_mixer_reset_applied",
        card_index=result.card_index,
        controls_reset=[name for name, _ in result.applied_controls],
        controls_count=len(result.applied_controls),
    )
    return JSONResponse(
        {
            "ok": True,
            "card_index": result.card_index,
            "card_id": target.card_id,
            "card_longname": target.card_longname,
            "applied_controls": [[name, raw] for name, raw in result.applied_controls],
            "reverted_controls": [[name, raw] for name, raw in result.reverted_controls],
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

    # 3.5 v0.20.2 / Bug B + voice-linux-cascade-root-fix T8 — close AND
    # AWAIT any live voice_test meter sessions BEFORE the factory probes
    # the mic. ``SessionRegistry.close_all`` is a cooperative shutdown:
    # it stops each session, waits up to
    # ``tuning.device_test_force_close_grace_s`` for the PortAudio stream
    # to drain, then force-closes on timeout. The await is what makes
    # the next probe reliable — without it the session's ``stream.close``
    # would race the cascade's reopen on the same ``hw:X,Y`` node and
    # produce spurious ``DEVICE_BUSY`` on the first candidate.
    voice_test_registry = getattr(request.app.state, "voice_test_registry", None)
    if voice_test_registry is not None:
        from sovyx.voice.device_test import CloseReason, SessionRegistry

        if isinstance(voice_test_registry, SessionRegistry):
            logger.info("voice_enable_test_session_handoff_begin")
            with contextlib.suppress(Exception):
                await voice_test_registry.close_all(reason=CloseReason.SERVER_SHUTDOWN)
            logger.info("voice_enable_test_session_handoff_done")

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
    # Gap 2 — track the most-recent in-flight cogloop task so the
    # capture consumer is freed (it would otherwise block on
    # ``await bridge.process(...)`` for the entire LLM + TTS duration,
    # making barge-in detection structurally impossible because
    # ``feed_frame`` cannot reach ``_handle_speaking`` while parked
    # deep in the perception await chain). The orchestrator can now
    # process the next frames during streaming, which is exactly when
    # the user might barge in.
    cogloop_task_ref: list[asyncio.Task[Any] | None] = [None]

    async def _on_perception(text: str, mind_id_str: str) -> None:
        """Feed a transcription into the cognitive loop via the bridge.

        Spawns the bridge call as a fire-and-forget task and returns
        immediately so the orchestrator's capture consumer is freed
        between turns. The spawned task is tracked in
        ``cogloop_task_ref`` so :func:`disable_voice` can wait / cancel
        it on shutdown, and so tests can assert on completion.
        """
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

        async def _run_bridge_isolated() -> None:
            try:
                await bridge.process(cog_request)
            except asyncio.CancelledError:
                # Expected on barge-in (bridge.process converts to a
                # sentinel internally; this branch only fires if the
                # task was cancelled BEFORE bridge.process registered
                # its own internal handler — defensive belt).
                logger.info(
                    "voice_cognitive_bridge_task_cancelled",
                    mind_id=mind_id_str,
                )
            except Exception:  # noqa: BLE001
                logger.exception("voice_cognitive_bridge_failed")

        # If a previous turn's task is somehow still in flight (the
        # user spoke a new utterance before the prior LLM/TTS finished
        # AND the orchestrator did NOT trigger cancel_speech_chain —
        # rare but possible on degraded streams), stop it explicitly so
        # we never accumulate orphaned tasks. cancel() is idempotent
        # against a done task.
        previous_task = cogloop_task_ref[0]
        if previous_task is not None and not previous_task.done():
            previous_task.cancel()

        cogloop_task_ref[0] = asyncio.create_task(
            _run_bridge_isolated(),
            name=f"voice-perception-{mind_id_str}",
        )

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

    from sovyx.voice._capture_task import CaptureInoperativeError

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
    except CaptureInoperativeError as exc:
        # v0.20.2 / Bug D — the VCHL boot cascade exhausted every viable
        # combo (or kernel-invalidated fail-over found no alternative
        # endpoint). Return 503 with the structured reason so the UI can
        # show a real "no working microphone" prompt instead of the
        # generic 500 stack-trace path.
        logger.error(
            "voice_enable_capture_inoperative",
            device=exc.device,
            host_api=exc.host_api,
            reason=exc.reason,
            attempts=exc.attempts,
        )
        return JSONResponse(
            {
                "ok": False,
                "error": "capture_inoperative",
                "detail": str(exc),
                "device": exc.device,
                "host_api": exc.host_api,
                "reason": exc.reason,
                "attempts": exc.attempts,
            },
            status_code=503,
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
    # The unified :mod:`sovyx.voice._stream_opener` pyramid walks every
    # host-API sibling of the selected device automatically, so a silent
    # MME variant falls through to WASAPI (or DirectSound) without any
    # caller-side retry bookkeeping. ``CaptureSilenceError`` is only
    # raised when *every* viable variant delivered zeros.
    from sovyx.voice._capture_task import (
        CaptureDeviceContendedError,
        CaptureSilenceError,
    )

    try:
        await bundle.capture_task.start()
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
    except CaptureDeviceContendedError as exc:
        # T7 — session-manager contention pattern detected. Return a
        # 503 with actionable alternatives so the frontend can render
        # clickable chips instead of a generic "Audio capture failed"
        # banner.
        logger.error(
            "voice_capture_device_contended",
            device=exc.device,
            host_api=exc.host_api,
            suggested_actions=exc.suggested_actions,
            contending_process_hint=exc.contending_process_hint,
        )
        with contextlib.suppress(Exception):
            await bundle.pipeline.stop()
        alternatives = _enumerate_alternative_devices()
        return JSONResponse(
            {
                "ok": False,
                "error": "capture_device_contended",
                "detail": str(exc),
                "device": exc.device,
                "host_api": exc.host_api,
                "suggested_actions": exc.suggested_actions,
                "contending_process_hint": exc.contending_process_hint,
                "alternative_devices": alternatives,
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
        from sovyx.voice.health import BootPreflightWarningsStore
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

        # v1.3 §4.6 L6 — publish the factory's boot preflight warnings
        # through a ``BootPreflightWarningsStore`` service. Callers
        # (``get_voice_status``) access it via ``registry.resolve``,
        # keeping parity with every other voice component. Re-enable
        # lifecycle: if a store is already registered (user disabled
        # then re-enabled), we refresh the snapshot in place rather
        # than append — disable_voice will deregister if stopped
        # cleanly; a crash-and-re-enable also converges to a fresh
        # snapshot without accumulating state.
        if registry.is_registered(BootPreflightWarningsStore):
            preflight_store = await registry.resolve(BootPreflightWarningsStore)
        else:
            preflight_store = BootPreflightWarningsStore()
            registry.register_instance(BootPreflightWarningsStore, preflight_store)
        preflight_store.set_warnings(list(bundle.boot_preflight_warnings))

        # v1.3 §4.6 L6 — WebSocket push mirrors the status-endpoint
        # payload so dashboards listening live (not polling) receive
        # the same signal. Best-effort: a WS failure never blocks the
        # enable handshake.
        ws_manager = getattr(request.app.state, "ws_manager", None)
        if ws_manager is not None and bundle.boot_preflight_warnings:
            for warning in bundle.boot_preflight_warnings:
                with contextlib.suppress(Exception):
                    await ws_manager.broadcast(
                        channel="voice",
                        event="voice_preflight_warning",
                        payload=warning,
                    )

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
        from sovyx.voice.health import BootPreflightWarningsStore
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
        # instances bound to the new pipeline. ``BootPreflightWarningsStore``
        # is part of this set: disabling voice invalidates the prior
        # boot-warning snapshot, and the next enable rebuilds it from a
        # fresh ``_run_boot_preflight`` call (v1.3 §4.6.7).
        for interface in (
            SileroVAD,
            STTEngine,
            TTSEngine,
            WakeWordDetector,
            VoiceCognitiveBridge,
            BootPreflightWarningsStore,
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
