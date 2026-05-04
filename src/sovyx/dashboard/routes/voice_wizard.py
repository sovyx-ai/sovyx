"""Voice setup wizard endpoints — Phase 7 / T7.21-T7.24.

Operator-facing wizard that runs the first time a user enables voice
in the dashboard. Four endpoints:

* ``GET /api/voice/wizard/devices`` (T7.21) — list candidate input
  devices with friendly names + per-device diagnosis hints. Used by
  the wizard's device-picker.

* ``POST /api/voice/wizard/test-record`` (T7.22) — kick off a 3-second
  capture-and-analyse session ("Zoom pattern" — record yourself, see
  the analysis). Returns a session_id + the immediate analysis.
  Synchronous: the 3 s recording happens inside the request.

* ``GET /api/voice/wizard/test-result/{session_id}`` (T7.23) — re-read
  a previously-completed session's result. The session store is
  bounded (last 64 sessions per app instance) so dashboards
  navigating away + back can reload without re-recording. Sessions
  are not persisted across daemon restarts (in-memory only).

* ``GET /api/voice/wizard/diagnostic`` (T7.24) — same data as
  ``GET /api/voice/capture-diagnostics`` but with the wizard-friendly
  shape (single ``ready: bool`` + ``recommendations`` list instead
  of the raw APO endpoint dump). CLI parity with
  ``sovyx doctor voice_capture_apo``.

Dependency-injection design:
  Recording requires a real microphone — not testable in headless CI
  without elaborate audio mocks. The test-record endpoint takes a
  ``WizardRecorder`` protocol-typed dependency from
  ``request.app.state.wizard_recorder``. Unit tests inject a fake
  recorder that returns deterministic synthetic audio; production
  daemon registers a real ``SoundDeviceWizardRecorder`` at boot. When
  no recorder is registered, the endpoint returns 503 with a clear
  "voice capture not available" detail — same pattern as the
  existing ``/api/voice/forget`` endpoint when the registry isn't
  ready.

Reference: master mission §Phase 7 / T7.21-T7.24.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

import numpy as np
import numpy.typing as npt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger
from sovyx.observability.metrics import get_metrics

logger = get_logger(__name__)

router = APIRouter(prefix="/api/voice/wizard", dependencies=[Depends(verify_token)])


# ── Constants ────────────────────────────────────────────────────────


_DEFAULT_RECORD_DURATION_S = 3.0
"""Wizard test-record default duration. 3 s matches the Zoom-style
"say something" pattern + leaves enough headroom for a full
sentence's worth of SNR analysis."""

_MIN_RECORD_DURATION_S = 1.0
_MAX_RECORD_DURATION_S = 10.0
_TARGET_SAMPLE_RATE = 16000
"""All wizard analysis runs at 16 kHz mono — same as the rest of the
voice subsystem (Moonshine / Silero / OpenWakeWord). Resampling
happens inside the recorder."""

_SESSION_STORE_MAX = 64
"""Bounded LRU cache for completed recording results. 64 sessions
covers a typical wizard-debugging session of 5-10 attempts × 6
operators dashboard-sharing without per-instance memory pressure
(< 1 MB cap at full capacity)."""

_SESSION_TTL_S = 3600.0
"""Sessions expire after 1 hour. Beyond that the wizard treats the
session_id as "not found" + the operator restarts the test-record
flow. Bounded TTL prevents memory creep across long-running
daemons."""


# ── Recorder protocol (dependency injection point) ──────────────────


@runtime_checkable
class WizardRecorder(Protocol):
    """Protocol for the test-record dependency.

    Production wires ``SoundDeviceWizardRecorder`` (uses ``sounddevice``
    against the operator's actual hardware). Tests inject a stub
    that returns synthetic audio — fully testable without a mic.
    """

    def record(
        self,
        *,
        duration_s: float,
        device_id: str | None,
    ) -> npt.NDArray[np.float32]:
        """Capture mono float32 audio at 16 kHz for ``duration_s``.

        Args:
            duration_s: Capture duration. Caller bounds to
                [1.0, 10.0]; the recorder honours it precisely
                — over-capture wastes time, under-capture truncates
                the operator's utterance.
            device_id: PortAudio device index (as a string) or
                ``None`` for the system default. Stringly-typed
                because PortAudio device IDs change across reboots
                + the wizard surfaces friendly names.

        Returns:
            Mono float32 ndarray of shape ``(int(duration_s * 16000),)``.
            Values bounded to [-1.0, 1.0]. Empty / silent capture
            returns an array of zeros.

        Raises:
            RuntimeError: When the recorder is unable to open the
                device (permission denied, device busy, etc.).
                Error message is operator-facing.
        """
        ...


@dataclass(frozen=True, slots=True)
class _SessionRecord:
    """One completed recording session, cached for retrieval by ID."""

    session_id: str
    response: WizardTestResultResponse
    created_at_monotonic: float


class _SessionStore:
    """In-memory LRU + TTL cache for completed sessions.

    Thread-safe via internal :class:`threading.Lock` because the
    test-record endpoint runs the recording in :func:`asyncio.to_thread`
    + the cache write happens after the await — both the test-record
    and test-result handlers may touch the cache concurrently.
    """

    def __init__(
        self,
        *,
        max_size: int = _SESSION_STORE_MAX,
        ttl_s: float = _SESSION_TTL_S,
    ) -> None:
        self._max_size = max_size
        self._ttl_s = ttl_s
        self._store: OrderedDict[str, _SessionRecord] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, record: _SessionRecord) -> None:
        with self._lock:
            self._store[record.session_id] = record
            self._store.move_to_end(record.session_id)
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)

    def get(self, session_id: str) -> _SessionRecord | None:
        with self._lock:
            record = self._store.get(session_id)
            if record is None:
                return None
            now = time.monotonic()
            if now - record.created_at_monotonic > self._ttl_s:
                # Expired — evict + treat as missing.
                self._store.pop(session_id, None)
                return None
            self._store.move_to_end(session_id)
            return record

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


def _get_session_store(request: Request) -> _SessionStore:
    """Get or lazily create the per-app session store."""
    state = request.app.state
    store: _SessionStore | None = getattr(state, "wizard_session_store", None)
    if store is None:
        store = _SessionStore()
        state.wizard_session_store = store
    return store


# ── Request / response models ────────────────────────────────────────


class WizardDeviceInfo(BaseModel):
    """One input device row in the wizard's picker."""

    device_id: str = Field(..., description="PortAudio device index as string.")
    name: str = Field(..., description="OS-reported device name.")
    friendly_name: str = Field(
        ...,
        description=(
            "Operator-readable label. Prefers the friendly name when "
            "the OS exposes one; falls back to the raw name."
        ),
    )
    max_input_channels: int = Field(
        ...,
        ge=0,
        description="Maximum capture channels the OS reports.",
    )
    default_sample_rate: int = Field(..., ge=0)
    is_default: bool = Field(..., description="Whether this is the OS default input.")
    diagnosis_hint: str = Field(
        ...,
        description=(
            "One of ``ready``, ``warning_low_channels``, "
            "``warning_high_sample_rate``, ``error_unavailable``. "
            "Drives the wizard UI's per-row colour code."
        ),
    )


class WizardDevicesResponse(BaseModel):
    devices: list[WizardDeviceInfo]
    total_count: int = Field(..., ge=0)
    default_device_id: str | None = None


class WizardTestRecordRequest(BaseModel):
    device_id: str | None = Field(
        None,
        description=(
            "PortAudio device index as string (matches ``WizardDeviceInfo.device_id``). "
            "``None`` uses the system default input."
        ),
    )
    duration_seconds: float = Field(
        default=_DEFAULT_RECORD_DURATION_S,
        ge=_MIN_RECORD_DURATION_S,
        le=_MAX_RECORD_DURATION_S,
        description="Capture duration. Bounded [1, 10] s.",
    )


class WizardTestResultResponse(BaseModel):
    """Synchronous result of a test-record session."""

    session_id: str
    success: bool
    duration_actual_s: float = Field(..., ge=0.0)
    sample_rate_hz: int = Field(..., ge=0)
    level_rms_dbfs: float | None = Field(None, description="RMS dBFS or null on no signal.")
    level_peak_dbfs: float | None = Field(None, description="Peak dBFS or null on no signal.")
    snr_db: float | None = Field(None, description="SNR estimate in dB.")
    clipping_detected: bool = Field(
        ...,
        description="True when peak ≥ -0.1 dBFS (clip-warning threshold).",
    )
    silent_capture: bool = Field(
        ...,
        description="True when peak < -50 dBFS (no usable signal).",
    )
    diagnosis: str = Field(
        ...,
        description=(
            "Closed-set verdict: ``ok``, ``low_signal``, ``clipping``, "
            "``no_audio``, ``recorder_unavailable``, ``device_error``."
        ),
    )
    diagnosis_hint: str = Field(
        ...,
        description="Human-readable next-step hint for the operator.",
    )
    recorded_at_utc: str = Field(..., description="ISO-8601 UTC timestamp.")
    error: str | None = Field(
        None,
        description=("Error message when ``success`` is False; None otherwise."),
    )


class WizardDiagnosticResponse(BaseModel):
    """Wizard-shaped capture diagnostic.

    Distilled view of ``GET /api/voice/capture-diagnostics`` for
    direct consumption by the wizard UI. The full APO endpoint dump
    remains available at the original URL for the troubleshooting
    panel.
    """

    ready: bool = Field(
        ...,
        description=(
            "True when the active capture endpoint is unmolested "
            "by Voice Clarity APO + has no other known interferers."
        ),
    )
    voice_clarity_active: bool = Field(
        ...,
        description="True when Voice Clarity APO is registered on the active mic.",
    )
    active_device_name: str | None = None
    platform: str = Field(..., description="``win32`` / ``linux`` / ``darwin``.")
    recommendations: list[str] = Field(
        default_factory=list,
        description=(
            "Operator-actionable hints, ordered by priority. Empty when the system is ``ready``."
        ),
    )


# ── Helpers ──────────────────────────────────────────────────────────


def _safe_db(linear: float) -> float | None:
    """Convert linear amplitude to dBFS, with a floor of -120 dB.

    Returns ``None`` for exactly 0 so JSON consumers can render
    "no signal" rather than the misleading ``-inf`` placeholder.
    """
    if linear <= 0.0:
        return None
    return float(20.0 * np.log10(max(linear, 1e-6)))


def _analyse_audio(
    samples: npt.NDArray[np.float32],
    sample_rate: int,
) -> dict[str, float | None]:
    """Compute RMS / peak / SNR over a captured buffer.

    SNR estimation uses the simple "top quartile vs bottom quartile"
    heuristic: sort frame energies, take the mean of the top 25% as
    "signal" and the mean of the bottom 25% as "noise". Adequate
    for the wizard's UX-grade decision (good vs bad mic) — not a
    precision instrument.
    """
    if samples.size == 0:
        return {
            "rms_dbfs": None,
            "peak_dbfs": None,
            "snr_db": None,
        }
    abs_samples = np.abs(samples)
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    peak = float(np.max(abs_samples))

    # Frame-energy SNR estimate. 20 ms frames at 16 kHz = 320 samples.
    frame_size = max(1, int(0.020 * sample_rate))
    if samples.size < 4 * frame_size:
        snr_db: float | None = None
    else:
        n_frames = samples.size // frame_size
        frames = samples[: n_frames * frame_size].reshape(n_frames, frame_size)
        energies = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
        sorted_e = np.sort(energies)
        q = max(1, n_frames // 4)
        signal = float(np.mean(sorted_e[-q:]))
        noise = float(np.mean(sorted_e[:q]))
        if noise <= 0.0 or signal <= 0.0:
            snr_db = None
        else:
            snr_db = float(20.0 * np.log10(signal / max(noise, 1e-6)))

    return {
        "rms_dbfs": _safe_db(rms),
        "peak_dbfs": _safe_db(peak),
        "snr_db": snr_db,
    }


def _diagnose(
    *,
    peak_dbfs: float | None,
    snr_db: float | None,
) -> tuple[str, str, bool, bool]:
    """Closed-set diagnosis from the analysis numbers.

    Returns ``(diagnosis, hint, clipping_detected, silent_capture)``.
    Threshold sources:

    * Clipping at ≥ -0.1 dBFS — standard "almost-full-scale" guard
      used by every audio toolkit.
    * Silent capture below -50 dBFS — a -50 dB peak is well below
      any human voice + indicates a muted / disconnected mic.
    * Low signal between -50 and -30 dBFS — usable but quiet;
      operator should speak louder or turn up the mic gain.
    * SNR < 10 dB → noisy.
    """
    clipping = peak_dbfs is not None and peak_dbfs >= -0.1  # noqa: PLR2004
    silent = peak_dbfs is None or peak_dbfs < -50.0  # noqa: PLR2004

    if silent:
        return (
            "no_audio",
            "No usable signal captured. Check the mic is connected, "
            "unmuted, and selected in your OS sound settings.",
            False,
            True,
        )
    if clipping:
        return (
            "clipping",
            "Signal is clipping. Move further from the mic or lower "
            "your input gain in OS sound settings.",
            True,
            False,
        )
    if peak_dbfs is not None and peak_dbfs < -30.0:  # noqa: PLR2004
        return (
            "low_signal",
            "Signal is usable but quiet. Speak louder or raise mic input gain.",
            False,
            False,
        )
    if snr_db is not None and snr_db < 10.0:  # noqa: PLR2004
        return (
            "noisy",
            "Background noise is high relative to your voice. Move "
            "to a quieter room or use a headset mic.",
            False,
            False,
        )
    return ("ok", "Microphone looks good.", False, False)


# ── T7.21 — list devices ─────────────────────────────────────────────


@router.get("/devices", response_model=WizardDevicesResponse)
async def list_wizard_devices(request: Request) -> WizardDevicesResponse:
    """List candidate input devices with friendly names + per-row hints.

    The wizard's device picker calls this on mount. Returns ``[]``
    when no input devices are detected — UI surfaces "No microphone
    found" with a "Refresh" button.
    """
    try:
        # Lazy import to keep ``sounddevice`` off the path of dashboards
        # served on hosts without audio hardware.
        from sovyx.voice.audio import AudioCapture  # noqa: PLC0415

        raw_devices = await asyncio.to_thread(AudioCapture.list_devices)
    except Exception as exc:  # noqa: BLE001
        logger.warning("voice_wizard_devices_enumeration_failed", error=str(exc))
        return WizardDevicesResponse(devices=[], total_count=0, default_device_id=None)

    default_device_id: str | None = None
    try:
        import sounddevice as sd  # noqa: PLC0415

        default = sd.default.device
        if isinstance(default, (list, tuple)) and len(default) >= 1:
            default_device_id = str(default[0])
        elif isinstance(default, int):
            default_device_id = str(default)
    except Exception:  # noqa: BLE001
        default_device_id = None

    devices_out: list[WizardDeviceInfo] = []
    for d in raw_devices:
        device_id = str(d.get("index", ""))
        name = str(d.get("name", "")).strip() or "Unknown"
        max_channels = int(d.get("channels", 0))
        sample_rate = int(d.get("rate", 0))

        # Per-row diagnosis hint — drives the colour code in the UI.
        if max_channels == 0:
            hint = "error_unavailable"
        elif max_channels == 1:
            hint = "warning_low_channels"
        elif sample_rate not in (16000, 24000, 32000, 44100, 48000, 88200, 96000):
            hint = "warning_high_sample_rate"
        else:
            hint = "ready"

        devices_out.append(
            WizardDeviceInfo(
                device_id=device_id,
                name=name,
                friendly_name=name,  # OS-reported name = friendly name
                max_input_channels=max_channels,
                default_sample_rate=sample_rate,
                is_default=(device_id == default_device_id),
                diagnosis_hint=hint,
            )
        )

    return WizardDevicesResponse(
        devices=devices_out,
        total_count=len(devices_out),
        default_device_id=default_device_id,
    )


# ── T7.22 — test-record ──────────────────────────────────────────────


def _resolve_recorder(request: Request) -> WizardRecorder | None:
    """Get the wizard recorder from app.state. None when unset."""
    return getattr(request.app.state, "wizard_recorder", None)


@router.post("/test-record", response_model=WizardTestResultResponse)
async def post_wizard_test_record(
    request: Request,
    body: WizardTestRecordRequest,
) -> WizardTestResultResponse:
    """Synchronously record + analyse a 3-second capture.

    The recording happens inside the request — the operator clicks
    "Test Record" and the response arrives 3 s later with the
    analysis. Subsequent ``GET /test-result/{session_id}`` calls
    return the same payload from the in-memory cache.

    Returns 503 when no ``WizardRecorder`` is registered
    (production daemon registers ``SoundDeviceWizardRecorder`` at
    boot; pre-init / tests without injection get the 503).
    """
    recorder = _resolve_recorder(request)
    if recorder is None:
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Voice capture not available — the daemon's wizard "
                "recorder is not registered. Wait for boot to complete "
                "and retry."
            ),
        )

    session_id = secrets.token_urlsafe(16)
    started = time.monotonic()
    recorded_at_iso = datetime.now(UTC).isoformat()

    try:
        samples = await asyncio.to_thread(
            recorder.record,
            duration_s=body.duration_seconds,
            device_id=body.device_id,
        )
    except Exception as exc:  # noqa: BLE001
        # T7.27 / T7.28 — translate raw OS error to operator-facing
        # plain-language guidance. Fallback path returns the raw
        # error verbatim (truncated) when the translation table has
        # no match, so operators always see SOMETHING actionable.
        from sovyx.voice._error_messages import translate_audio_error  # noqa: PLC0415

        translation = translate_audio_error(exc)
        logger.warning(
            "voice_wizard_test_record_failed",
            session_id=session_id,
            device_id=body.device_id,
            error=str(exc),
            error_class=translation.error_class.value,
        )
        response = WizardTestResultResponse(
            session_id=session_id,
            success=False,
            duration_actual_s=0.0,
            sample_rate_hz=_TARGET_SAMPLE_RATE,
            level_rms_dbfs=None,
            level_peak_dbfs=None,
            snr_db=None,
            clipping_detected=False,
            silent_capture=True,
            diagnosis="device_error",
            diagnosis_hint=f"{translation.user_message} {translation.actionable_hint}",
            recorded_at_utc=recorded_at_iso,
            error=str(exc),
        )
        _get_session_store(request).put(
            _SessionRecord(
                session_id=session_id,
                response=response,
                created_at_monotonic=started,
            )
        )
        return response

    duration_actual = time.monotonic() - started
    analysis = _analyse_audio(samples, _TARGET_SAMPLE_RATE)
    diagnosis, hint, clipping, silent = _diagnose(
        peak_dbfs=analysis["peak_dbfs"],
        snr_db=analysis["snr_db"],
    )

    response = WizardTestResultResponse(
        session_id=session_id,
        success=True,
        duration_actual_s=duration_actual,
        sample_rate_hz=_TARGET_SAMPLE_RATE,
        level_rms_dbfs=analysis["rms_dbfs"],
        level_peak_dbfs=analysis["peak_dbfs"],
        snr_db=analysis["snr_db"],
        clipping_detected=clipping,
        silent_capture=silent,
        diagnosis=diagnosis,
        diagnosis_hint=hint,
        recorded_at_utc=recorded_at_iso,
        error=None,
    )
    _get_session_store(request).put(
        _SessionRecord(
            session_id=session_id,
            response=response,
            created_at_monotonic=started,
        )
    )
    logger.info(
        "voice_wizard_test_record_complete",
        session_id=session_id,
        diagnosis=diagnosis,
        duration_actual_s=duration_actual,
    )
    return response


# ── T7.23 — test-result by session_id ────────────────────────────────


@router.get(
    "/test-result/{session_id}",
    response_model=WizardTestResultResponse,
)
async def get_wizard_test_result(
    request: Request,
    session_id: str,
) -> WizardTestResultResponse:
    """Re-read a previously-completed test-record result by session id.

    Sessions live in an in-memory LRU cache (last 64 sessions per
    daemon) with a 1-hour TTL; expired or evicted sessions return
    404 + the operator runs ``test-record`` again. Sessions are not
    persisted across daemon restarts.
    """
    if not session_id.strip():
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail="session_id must be a non-empty string",
        )
    store = _get_session_store(request)
    record = store.get(session_id)
    if record is None:
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail=(
                f"session_id={session_id!r} not found. Sessions expire "
                f"after 1 hour or 64 newer sessions; run /test-record "
                f"again to get a fresh session."
            ),
        )
    return record.response


# ── T7.24 — diagnostic (capture-APO summary) ─────────────────────────


@router.get("/diagnostic", response_model=WizardDiagnosticResponse)
async def get_wizard_diagnostic(request: Request) -> WizardDiagnosticResponse:
    """Wizard-friendly capture diagnostic.

    Distilled view of ``GET /api/voice/capture-diagnostics``: a
    single ``ready: bool`` + ``recommendations: list[str]`` that the
    wizard UI can render directly without parsing the full APO
    endpoint dump. CLI parity:
    ``sovyx doctor voice_capture_apo --json``.

    The full per-endpoint structure remains at the original endpoint
    for the troubleshooting panel and external auditors.
    """
    import sys  # noqa: PLC0415

    platform = sys.platform

    try:
        from sovyx.voice._apo_detector import detect_capture_apos  # noqa: PLC0415

        reports = await asyncio.to_thread(detect_capture_apos)
    except Exception as exc:  # noqa: BLE001
        logger.warning("voice_wizard_diagnostic_apo_scan_failed", error=str(exc))
        reports = []

    voice_clarity_active = any(getattr(r, "voice_clarity_active", False) for r in reports)

    active_device_name: str | None = None
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        try:
            from sovyx.voice._capture_task import AudioCaptureTask  # noqa: PLC0415

            if registry.is_registered(AudioCaptureTask):
                capture = await registry.resolve(AudioCaptureTask)
                active_device_name = getattr(capture, "input_device_name", None)
        except Exception:  # noqa: BLE001 — best-effort lookup
            active_device_name = None

    recommendations: list[str] = []
    ready = True

    if voice_clarity_active:
        ready = False
        recommendations.append(
            "Windows Voice Clarity APO is active on your microphone. "
            "Sovyx auto-bypasses it via WASAPI exclusive mode. If wake "
            "detection fails, run 'sovyx doctor voice --fix --yes' to "
            "force the bypass."
        )

    if platform == "linux" and not reports:
        # Linux: APO detection is a no-op. We don't have anything to
        # add here that's wizard-friendly without a PulseAudio probe;
        # leave recommendations empty + ready=True.
        pass

    return WizardDiagnosticResponse(
        ready=ready,
        voice_clarity_active=voice_clarity_active,
        active_device_name=active_device_name,
        platform=platform,
        recommendations=recommendations,
    )


# ── Wizard A/B telemetry ingestion (Mission v0.30.1 §T1.2) ──────────


_VALID_STEPS: frozenset[str] = frozenset({"devices", "record", "results", "save", "done"})
"""Wizard step enum — must match the discriminated-union ``WizardStep``
in ``dashboard/src/components/setup-wizard/VoiceSetupWizard.tsx``. Both
metric attributes (step / exit_step) are bounded to this enum so the
OTel scrape series count stays predictable (5 distinct values × 2
metrics × 2 outcomes = ≤ 20 series total)."""

_MAX_DURATION_MS: int = 3_600_000
"""1 h cap on a single step dwell. Anything longer is operator left
the tab open + walked away — telemetry is meaningless beyond the cap
and admitting it would stretch histogram buckets without insight."""


class WizardTelemetryStepDwell(BaseModel):
    """Step-dwell discriminated payload."""

    event: Literal["step_dwell"]
    step: str = Field(description="Wizard step the dwell ended on.")
    duration_ms: int = Field(
        ge=0,
        le=_MAX_DURATION_MS,
        description="Time spent on the step before transitioning.",
    )


class WizardTelemetryCompletion(BaseModel):
    """Completion discriminated payload."""

    event: Literal["completion"]
    outcome: Literal["completed", "abandoned"]
    exit_step: str = Field(description="Step the wizard was on at exit.")


@router.post("/telemetry", status_code=204)
async def emit_wizard_telemetry(
    request: Request,
    body: WizardTelemetryStepDwell | WizardTelemetryCompletion,
) -> None:
    """Record one wizard A/B telemetry event.

    Frontend instrumentation in ``VoiceSetupWizard.tsx`` posts here on
    every step transition (``step_dwell``) and on wizard exit
    (``completion``). The endpoint is best-effort: a 4xx on payload
    errors is informative, but the wizard doesn't block on the
    response. Series cardinality is capped via the ``_VALID_STEPS``
    enum + ``outcome`` literal — operators uploading random strings
    via curl are rejected with 400 before any metric instrument is
    touched.
    """
    if body.event == "step_dwell":
        if body.step not in _VALID_STEPS:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"step must be one of {sorted(_VALID_STEPS)}",
            )
        get_metrics().voice_wizard_step_dwell_ms.record(
            body.duration_ms, attributes={"step": body.step}
        )
    else:
        if body.exit_step not in _VALID_STEPS:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"exit_step must be one of {sorted(_VALID_STEPS)}",
            )
        get_metrics().voice_wizard_completion_rate.add(
            1,
            attributes={
                "outcome": body.outcome,
                "exit_step": body.exit_step,
            },
        )


# ── Production recorder (lazy-bound to sounddevice) ─────────────────


class SoundDeviceWizardRecorder:
    """Production :class:`WizardRecorder` backed by ``sounddevice``.

    Daemon registers an instance of this at boot via
    ``app.state.wizard_recorder = SoundDeviceWizardRecorder()``.
    Tests inject a stub instead.

    Resamples to 16 kHz mono inside ``record()`` so callers always
    get the canonical pipeline format regardless of the device's
    native rate.
    """

    def record(
        self,
        *,
        duration_s: float,
        device_id: str | None,
    ) -> npt.NDArray[np.float32]:
        import sounddevice as sd  # noqa: PLC0415

        device: int | None = None
        if device_id is not None and device_id.strip():
            try:
                device = int(device_id)
            except ValueError as exc:
                msg = f"device_id must be a numeric PortAudio index; got {device_id!r}"
                raise RuntimeError(msg) from exc

        # Negotiate sample rate: prefer 16 kHz native; else fallback.
        native_rate = _TARGET_SAMPLE_RATE
        try:
            sd.check_input_settings(device=device, samplerate=_TARGET_SAMPLE_RATE)
        except sd.PortAudioError:
            try:
                info: object = sd.query_devices(device, "input")
                native_rate = int(getattr(info, "default_samplerate", 48000))
            except Exception:  # noqa: BLE001
                native_rate = 48000

        try:
            captured = sd.rec(
                int(duration_s * native_rate),
                samplerate=native_rate,
                channels=1,
                dtype="float32",
                device=device,
                blocking=True,
            )
        except sd.PortAudioError as exc:
            msg = f"PortAudio error opening device {device_id!r}: {exc}"
            raise RuntimeError(msg) from exc

        mono = np.asarray(captured, dtype=np.float32).flatten()

        if native_rate != _TARGET_SAMPLE_RATE:
            # Simple linear resampling — adequate for the wizard's
            # level/SNR analysis. The voice pipeline uses scipy.signal
            # for its own resampling but that's overkill here.
            target_len = int(len(mono) * _TARGET_SAMPLE_RATE / native_rate)
            if target_len > 0 and len(mono) > 1:
                indices = np.linspace(0, len(mono) - 1, target_len)
                mono = np.interp(indices, np.arange(len(mono)), mono).astype(np.float32)

        return mono


# Re-export the protocol so tests can build their own stubs without
# importing private names.
__all__ = [
    "SoundDeviceWizardRecorder",
    "WizardDeviceInfo",
    "WizardDevicesResponse",
    "WizardDiagnosticResponse",
    "WizardRecorder",
    "WizardTestRecordRequest",
    "WizardTestResultResponse",
    "router",
]
