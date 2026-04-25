"""``/api/voice/platform-diagnostics`` — cross-OS aggregated diagnostics.

Composes every Anel-1 (Hardware/OS Isolation) detector into one
endpoint so the dashboard can render a single "Platform Diagnostics"
panel that adapts to the host OS.

Response shape (all per-OS branches optional — only the platform
matching ``sys.platform`` is populated; other branches are ``null``):

.. code:: json

    {
      "platform": "linux|win32|darwin|other",
      "mic_permission": { ... MicPermissionReport ... },
      "linux": {
        "pipewire": { ... PipeWireReport ... },
        "alsa_ucm": { ... UcmReport ... }
      },
      "windows": {
        "audio_service": { ... AudioServiceStatus ... }
      },
      "macos": {
        "hal_plugins": { ... HalReport ... },
        "bluetooth": { ... BluetoothReport ... },
        "code_signing": { ... EntitlementReport ... }
      }
    }

Design contract:

* **Never raises**. Every detector wraps its probe in try/except;
  individual probe failures collapse into per-section UNKNOWN
  reports with structured ``notes``. The endpoint always returns
  200 with as much data as it could collect.
* **Sub-second latency on the happy path**. Every detector uses
  bounded subprocess timeouts (3-8 s), but the endpoint runs them
  in parallel via ``asyncio.gather`` so the tail latency is
  bounded by the slowest probe (~5 s for system_profiler on macOS),
  not the sum.
* **Auth-required** via the shared ``verify_token`` dependency.
  No platform diagnostics leak without auth.

Reference: F1 inventory tasks MA1/MA2/MA5/MA6/F3/F4/WI2/#34 — this
endpoint is the dashboard surface that finally exposes them all.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from sovyx.dashboard.routes._deps import verify_token

router = APIRouter(
    prefix="/api/voice/platform-diagnostics",
    tags=["voice-platform-diagnostics"],
    dependencies=[Depends(verify_token)],
)


# ── Response models ───────────────────────────────────────────────


class MicPermissionPayload(BaseModel):
    status: str
    machine_value: str | None = None
    user_value: str | None = None
    notes: list[str] = Field(default_factory=list)
    remediation_hint: str = ""


class PipeWirePayload(BaseModel):
    status: str
    socket_present: bool = False
    pactl_available: bool = False
    pactl_info_ok: bool = False
    server_name: str | None = None
    modules_loaded: list[str] = Field(default_factory=list)
    echo_cancel_loaded: bool = False
    notes: list[str] = Field(default_factory=list)


class UcmPayload(BaseModel):
    status: str
    card_id: str
    alsaucm_available: bool = False
    verbs: list[str] = Field(default_factory=list)
    active_verb: str | None = None
    notes: list[str] = Field(default_factory=list)


class WindowsServicePayload(BaseModel):
    name: str
    state: str
    raw_state: str = ""
    notes: list[str] = Field(default_factory=list)


class WindowsAudioServicePayload(BaseModel):
    audiosrv: WindowsServicePayload
    audio_endpoint_builder: WindowsServicePayload
    all_healthy: bool
    degraded_services: list[str] = Field(default_factory=list)


class HalPluginPayload(BaseModel):
    bundle_name: str
    path: str
    category: str
    friendly_label: str = ""


class HalPayload(BaseModel):
    plugins: list[HalPluginPayload] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    virtual_audio_active: bool = False
    audio_enhancement_active: bool = False


class BluetoothDevicePayload(BaseModel):
    name: str
    address: str = ""
    profile: str
    is_input_capable: bool = False
    is_output_capable: bool = False


class BluetoothPayload(BaseModel):
    devices: list[BluetoothDevicePayload] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CodeSigningPayload(BaseModel):
    verdict: str
    executable_path: str = ""
    notes: list[str] = Field(default_factory=list)
    remediation_hint: str = ""


class LinuxBranch(BaseModel):
    pipewire: PipeWirePayload
    alsa_ucm: UcmPayload


class WindowsBranch(BaseModel):
    audio_service: WindowsAudioServicePayload


class MacOSBranch(BaseModel):
    hal_plugins: HalPayload
    bluetooth: BluetoothPayload
    code_signing: CodeSigningPayload


class PlatformDiagnosticsResponse(BaseModel):
    platform: str
    mic_permission: MicPermissionPayload
    linux: LinuxBranch | None = None
    windows: WindowsBranch | None = None
    macos: MacOSBranch | None = None


# ── Probe runners (async-wrapped sync) ────────────────────────────


async def _safe_probe(
    fn: Any,  # noqa: ANN401
    *args: Any,  # noqa: ANN401
    **kwargs: Any,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    """Run ``fn(*args, **kwargs)`` on a worker thread. Returns the
    function's return value, or ``None`` if the probe raised — the
    endpoint never propagates probe failures to the HTTP response.

    ``Any`` types are intentional: this helper is generic across N
    detector return types (PipeWireReport, UcmReport, etc.) without
    forcing a circular-import-prone Protocol hierarchy.
    """
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except Exception:  # noqa: BLE001 — endpoint isolation
        return None


def _build_mic_payload(report: Any) -> MicPermissionPayload:  # noqa: ANN401
    if report is None:
        return MicPermissionPayload(
            status="unknown",
            notes=["mic_permission probe failed (returned None)"],
            remediation_hint="",
        )
    return MicPermissionPayload(
        status=report.status.value,
        machine_value=report.machine_value,
        user_value=report.user_value,
        notes=list(report.notes),
        remediation_hint=report.remediation_hint,
    )


def _build_pipewire_payload(report: Any) -> PipeWirePayload:  # noqa: ANN401
    if report is None:
        return PipeWirePayload(
            status="unknown",
            notes=["pipewire probe failed (returned None)"],
        )
    return PipeWirePayload(
        status=report.status.value,
        socket_present=report.socket_present,
        pactl_available=report.pactl_available,
        pactl_info_ok=report.pactl_info_ok,
        server_name=report.server_name,
        modules_loaded=list(report.modules_loaded),
        echo_cancel_loaded=report.echo_cancel_loaded,
        notes=list(report.notes),
    )


def _build_ucm_payload(report: Any) -> UcmPayload:  # noqa: ANN401
    if report is None:
        return UcmPayload(
            status="unknown",
            card_id="0",
            notes=["alsa_ucm probe failed (returned None)"],
        )
    return UcmPayload(
        status=report.status.value,
        card_id=report.card_id,
        alsaucm_available=report.alsaucm_available,
        verbs=list(report.verbs),
        active_verb=report.active_verb,
        notes=list(report.notes),
    )


def _build_audio_service_payload(report: Any) -> WindowsAudioServicePayload:  # noqa: ANN401
    if report is None:
        empty = WindowsServicePayload(
            name="Audiosrv",
            state="unknown",
            notes=["audio service probe failed (returned None)"],
        )
        empty_aeb = WindowsServicePayload(
            name="AudioEndpointBuilder",
            state="unknown",
            notes=["audio service probe failed (returned None)"],
        )
        return WindowsAudioServicePayload(
            audiosrv=empty,
            audio_endpoint_builder=empty_aeb,
            all_healthy=False,
            degraded_services=["Audiosrv", "AudioEndpointBuilder"],
        )
    return WindowsAudioServicePayload(
        audiosrv=WindowsServicePayload(
            name=report.audiosrv.name,
            state=report.audiosrv.state.value,
            raw_state=report.audiosrv.raw_state,
            notes=list(report.audiosrv.notes),
        ),
        audio_endpoint_builder=WindowsServicePayload(
            name=report.audio_endpoint_builder.name,
            state=report.audio_endpoint_builder.state.value,
            raw_state=report.audio_endpoint_builder.raw_state,
            notes=list(report.audio_endpoint_builder.notes),
        ),
        all_healthy=report.all_healthy,
        degraded_services=list(report.degraded_services),
    )


def _build_hal_payload(report: Any) -> HalPayload:  # noqa: ANN401
    if report is None:
        return HalPayload(notes=["hal probe failed (returned None)"])
    return HalPayload(
        plugins=[
            HalPluginPayload(
                bundle_name=p.bundle_name,
                path=p.path,
                category=p.category.value,
                friendly_label=p.friendly_label,
            )
            for p in report.plugins
        ],
        notes=list(report.notes),
        virtual_audio_active=report.virtual_audio_active,
        audio_enhancement_active=report.audio_enhancement_active,
    )


def _build_bluetooth_payload(report: Any) -> BluetoothPayload:  # noqa: ANN401
    if report is None:
        return BluetoothPayload(notes=["bluetooth probe failed (returned None)"])
    return BluetoothPayload(
        devices=[
            BluetoothDevicePayload(
                name=d.name,
                address=d.address,
                profile=d.profile.value,
                is_input_capable=d.is_input_capable,
                is_output_capable=d.is_output_capable,
            )
            for d in report.devices
        ],
        notes=list(report.notes),
    )


def _build_code_signing_payload(report: Any) -> CodeSigningPayload:  # noqa: ANN401
    if report is None:
        return CodeSigningPayload(
            verdict="unknown",
            notes=["codesign probe failed (returned None)"],
        )
    return CodeSigningPayload(
        verdict=report.verdict.value,
        executable_path=report.executable_path,
        notes=list(report.notes),
        remediation_hint=report.remediation_hint,
    )


# ── Endpoint ──────────────────────────────────────────────────────


@router.get("", response_model=PlatformDiagnosticsResponse)
async def get_platform_diagnostics() -> PlatformDiagnosticsResponse:
    """Aggregated cross-OS diagnostics. Always returns 200 with as
    much data as the platform-specific probes could collect."""
    # Lazy imports — keep cold-start light when the endpoint isn't
    # called.
    from sovyx.voice.health._mic_permission import check_microphone_permission

    mic_task = _safe_probe(check_microphone_permission)

    platform = sys.platform
    linux_branch: LinuxBranch | None = None
    windows_branch: WindowsBranch | None = None
    macos_branch: MacOSBranch | None = None

    if platform == "linux":
        from sovyx.voice.health._alsa_ucm import detect_ucm
        from sovyx.voice.health._pipewire import detect_pipewire

        pw_task = _safe_probe(detect_pipewire)
        ucm_task = _safe_probe(detect_ucm, "0")
        mic_report, pw_report, ucm_report = await asyncio.gather(
            mic_task,
            pw_task,
            ucm_task,
        )
        linux_branch = LinuxBranch(
            pipewire=_build_pipewire_payload(pw_report),
            alsa_ucm=_build_ucm_payload(ucm_report),
        )
    elif platform == "win32":
        from sovyx.voice.health._windows_audio_service import (
            query_audio_service_status,
        )

        svc_task = _safe_probe(query_audio_service_status)
        mic_report, svc_report = await asyncio.gather(mic_task, svc_task)
        windows_branch = WindowsBranch(
            audio_service=_build_audio_service_payload(svc_report),
        )
    elif platform == "darwin":
        from sovyx.voice._bluetooth_profile_mac import (
            detect_bluetooth_audio_profile,
        )
        from sovyx.voice._codesign_verify_mac import verify_microphone_entitlement
        from sovyx.voice._hal_detector_mac import detect_hal_plugins

        hal_task = _safe_probe(detect_hal_plugins)
        bt_task = _safe_probe(detect_bluetooth_audio_profile)
        cs_task = _safe_probe(verify_microphone_entitlement)
        mic_report, hal_report, bt_report, cs_report = await asyncio.gather(
            mic_task,
            hal_task,
            bt_task,
            cs_task,
        )
        macos_branch = MacOSBranch(
            hal_plugins=_build_hal_payload(hal_report),
            bluetooth=_build_bluetooth_payload(bt_report),
            code_signing=_build_code_signing_payload(cs_report),
        )
    else:
        # Unknown platform — only mic_permission still runs.
        mic_report = await mic_task

    return PlatformDiagnosticsResponse(
        platform=platform if platform in ("linux", "win32", "darwin") else "other",
        mic_permission=_build_mic_payload(mic_report),
        linux=linux_branch,
        windows=windows_branch,
        macos=macos_branch,
    )


__all__ = ["router"]
