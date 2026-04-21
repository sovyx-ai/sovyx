"""Startup self-diagnosis cascade — emits ordered ``startup.*`` events.

Single source of truth for "what is this machine and what does the daemon see"
at boot. Replaces the .ps1 forensic scripts (``diag_voice_razer.ps1``,
``diag_reboot_forensics.ps1`` …) with a structured cascade that runs inside
the daemon and ships every datum through the same observability pipeline as
runtime logs — operators query the saga in the dashboard instead of copy-
pasting console output.

The cascade is bracketed by a ``startup`` saga so every emit carries the
same ``saga_id``; the dashboard renders the boot story by filtering on that
id (see ``GET /api/logs/sagas/{id}``).

Each ``_emit_*`` helper is intentionally narrow: one capability per event,
all OS-portable (every helper degrades to an empty payload when the platform
or library is missing). Failures inside a helper are logged at WARNING and
do **not** break the cascade — a missing psutil import or unreadable WMI key
must not stall startup.

Aligned with IMPL-OBSERVABILITY-001 §15 (Phase 4).
"""

from __future__ import annotations

import asyncio
import os
import platform
import socket
import sys
import time
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger
from sovyx.observability.saga import async_saga_scope

if TYPE_CHECKING:
    from sovyx.engine.config import EngineConfig
    from sovyx.engine.events import EventBus
    from sovyx.engine.registry import ServiceRegistry

logger = get_logger(__name__)


async def run_startup_cascade(
    config: EngineConfig,
    registry: ServiceRegistry,
    event_bus: EventBus | None = None,
) -> None:
    """Emit the ordered ``startup.*`` cascade inside a ``startup`` saga.

    The saga groups every emit so the dashboard can render the full boot
    story from a single ``saga_id``. Helpers run sequentially (rather than
    via :func:`asyncio.gather`) because the order is operator-meaningful:
    platform/hardware before audio devices before APO scan before models.
    Total budget is <300 ms on a warm Linux container; helpers wrap each
    blocking lib call in :func:`asyncio.to_thread` so the event loop is
    never starved.

    Args:
        config: Resolved engine configuration. ``data_dir`` is consulted
            by :func:`_emit_filesystem`; ``observability.fts_index_path``
            and ``log.log_file`` are checked for write-availability.
        registry: Service registry. Used by :func:`_emit_models` and
            :func:`_emit_health_snapshot` to discover registered
            sub-systems without a hard import dependency.
        event_bus: Reserved for future use — the cascade currently emits
            via the structlog pipeline. Kept in the signature so callers
            (bootstrap, doctor) can pass it once the bus grows hooks
            for cascade events.
    """
    del event_bus  # Reserved for future hooks; structlog handles emission.

    async with async_saga_scope("startup", kind="diagnosis"):
        await _safe_run("startup.platform", _emit_platform)
        await _safe_run("startup.hardware", _emit_hardware)
        await _safe_run("startup.audio.devices", _emit_audio_devices)
        await _safe_run("startup.audio.apo_scan", _emit_apo_scan)
        await _safe_run("startup.network", _emit_network, config)
        await _safe_run("startup.filesystem", _emit_filesystem, config)
        await _safe_run("startup.models", _emit_models, registry)
        await _safe_run(
            "startup.config.provenance",
            _emit_config_provenance,
            config,
        )
        await _safe_run(
            "startup.health.snapshot",
            _emit_health_snapshot,
            registry,
        )
        logger.info("startup.completed")


async def _safe_run(name: str, fn: Any, *args: Any) -> None:  # noqa: ANN401 — variadic helper dispatch.
    """Run a ``_emit_*`` helper, swallowing exceptions as WARNINGs.

    The cascade must never raise — a single helper failure (missing
    library, unreadable system file) must not abort the boot. Each
    helper's outcome is timed so operators can see which step is slow.
    """
    started = time.perf_counter()
    try:
        result = fn(*args)
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:  # noqa: BLE001 — cascade isolation.
        duration_ms = (time.perf_counter() - started) * 1000.0
        logger.warning(
            "startup.step.failed",
            **{
                "startup.step": name,
                "startup.duration_ms": round(duration_ms, 2),
                "startup.error": str(exc),
                "startup.error_type": type(exc).__name__,
            },
        )


# ---------------------------------------------------------------------------
# Cascade steps — each emits exactly one structured event.
# ---------------------------------------------------------------------------


async def _emit_platform() -> None:
    """Emit OS / Python / interpreter fingerprint."""
    uname = platform.uname()
    logger.info(
        "startup.platform",
        **{
            "platform.system": uname.system,
            "platform.release": uname.release,
            "platform.version": uname.version,
            "platform.machine": uname.machine,
            "platform.processor": uname.processor,
            "platform.node": uname.node,
            "platform.python_version": platform.python_version(),
            "platform.python_implementation": platform.python_implementation(),
            "platform.sys_platform": sys.platform,
        },
    )


async def _emit_hardware() -> None:
    """Emit CPU/memory snapshot via psutil (best-effort)."""
    try:
        import psutil  # noqa: PLC0415 — optional at boot scope.
    except ImportError:
        logger.info(
            "startup.hardware",
            **{"hardware.psutil_available": False},
        )
        return

    cpu_count_logical = psutil.cpu_count(logical=True) or 0
    cpu_count_physical = psutil.cpu_count(logical=False) or 0
    vm = await asyncio.to_thread(psutil.virtual_memory)
    sm = await asyncio.to_thread(psutil.swap_memory)
    try:
        cpu_freq = await asyncio.to_thread(psutil.cpu_freq)
        freq_current = float(cpu_freq.current) if cpu_freq is not None else 0.0
        freq_max = float(cpu_freq.max) if cpu_freq is not None else 0.0
    except (NotImplementedError, FileNotFoundError, OSError):
        # Some virtualized environments don't expose cpufreq nodes.
        freq_current = 0.0
        freq_max = 0.0

    logger.info(
        "startup.hardware",
        **{
            "hardware.psutil_available": True,
            "hardware.cpu_count_logical": cpu_count_logical,
            "hardware.cpu_count_physical": cpu_count_physical,
            "hardware.cpu_freq_current_mhz": round(freq_current, 1),
            "hardware.cpu_freq_max_mhz": round(freq_max, 1),
            "hardware.memory_total_mb": round(vm.total / (1024 * 1024), 1),
            "hardware.memory_available_mb": round(vm.available / (1024 * 1024), 1),
            "hardware.memory_percent_used": round(vm.percent, 1),
            "hardware.swap_total_mb": round(sm.total / (1024 * 1024), 1),
            "hardware.swap_percent_used": round(sm.percent, 1),
        },
    )


async def _emit_audio_devices() -> None:
    """Emit PortAudio device inventory (capture + playback)."""
    from sovyx.voice.device_enum import enumerate_devices  # noqa: PLC0415

    entries = await asyncio.to_thread(enumerate_devices)
    capture: list[dict[str, Any]] = []
    playback: list[dict[str, Any]] = []
    for entry in entries:
        record = {
            "index": entry.index,
            "name": entry.name,
            "host_api": entry.host_api_name,
            "samplerate": entry.default_samplerate,
            "is_os_default": entry.is_os_default,
        }
        if entry.max_input_channels > 0:
            capture.append({**record, "channels": entry.max_input_channels})
        if entry.max_output_channels > 0:
            playback.append({**record, "channels": entry.max_output_channels})

    logger.info(
        "startup.audio.devices",
        **{
            "audio.capture_count": len(capture),
            "audio.playback_count": len(playback),
            "audio.capture": capture,
            "audio.playback": playback,
        },
    )


async def _emit_apo_scan() -> None:
    """Emit Windows capture-APO report (no-op on non-Windows)."""
    from sovyx.voice._apo_detector import detect_capture_apos  # noqa: PLC0415

    reports = await asyncio.to_thread(detect_capture_apos)
    endpoints = [
        {
            "endpoint_id": r.endpoint_id,
            "name": r.endpoint_name,
            "interface_name": r.device_interface_name,
            "enumerator": r.enumerator,
            "fx_binding_count": r.fx_binding_count,
            "known_apos": list(r.known_apos),
            "raw_clsids": list(r.raw_clsids),
            "voice_clarity_active": r.voice_clarity_active,
        }
        for r in reports
    ]
    voice_clarity_any = any(r.voice_clarity_active for r in reports)
    logger.info(
        "startup.audio.apo_scan",
        **{
            "audio.platform": sys.platform,
            "audio.endpoint_count": len(endpoints),
            "audio.endpoints": endpoints,
            "audio.voice_clarity_detected": voice_clarity_any,
        },
    )


async def _emit_network(config: EngineConfig) -> None:
    """Emit hostname + interface listing + dashboard bind probe."""
    hostname = socket.gethostname()
    try:
        fqdn = socket.getfqdn()
    except OSError:
        fqdn = hostname

    interfaces: list[dict[str, Any]] = []
    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        interfaces = []
    else:
        addrs = await asyncio.to_thread(psutil.net_if_addrs)
        for iface, addr_list in addrs.items():
            interfaces.append(
                {
                    "name": iface,
                    "addresses": [
                        {
                            "family": str(getattr(a, "family", "")),
                            "address": getattr(a, "address", ""),
                        }
                        for a in addr_list
                    ],
                }
            )

    dashboard_cfg = getattr(config, "dashboard", None)
    dashboard_host = getattr(dashboard_cfg, "host", None) if dashboard_cfg else None
    dashboard_port = getattr(dashboard_cfg, "port", None) if dashboard_cfg else None

    logger.info(
        "startup.network",
        **{
            "network.hostname": hostname,
            "network.fqdn": fqdn,
            "network.interface_count": len(interfaces),
            "network.interfaces": interfaces,
            "network.dashboard_host": dashboard_host,
            "network.dashboard_port": dashboard_port,
        },
    )


async def _emit_filesystem(config: EngineConfig) -> None:
    """Emit data_dir / log_file / disk-free snapshot."""
    data_dir = config.data_dir
    log_file = getattr(config.log, "log_file", None)

    data_exists = data_dir.exists()
    data_writable = os.access(data_dir, os.W_OK) if data_exists else False
    log_exists = log_file.exists() if log_file is not None else False

    free_bytes = 0
    total_bytes = 0
    try:
        usage = await asyncio.to_thread(_disk_usage, data_dir if data_exists else data_dir.parent)
        free_bytes = usage[0]
        total_bytes = usage[1]
    except OSError:
        pass

    logger.info(
        "startup.filesystem",
        **{
            "fs.data_dir": str(data_dir),
            "fs.data_dir_exists": data_exists,
            "fs.data_dir_writable": data_writable,
            "fs.log_file": str(log_file) if log_file else None,
            "fs.log_file_exists": log_exists,
            "fs.disk_free_mb": round(free_bytes / (1024 * 1024), 1),
            "fs.disk_total_mb": round(total_bytes / (1024 * 1024), 1),
        },
    )


def _disk_usage(path: Any) -> tuple[int, int]:  # noqa: ANN401 — Path-like duck typed.
    """Return (free_bytes, total_bytes) for the filesystem holding *path*."""
    import shutil  # noqa: PLC0415 — boot-time import isolation.

    usage = shutil.disk_usage(str(path))
    return usage.free, usage.total


async def _emit_models(registry: ServiceRegistry) -> None:
    """Emit registered service inventory (proxy for "what models are loaded")."""
    services = sorted(
        list(registry._instances.keys())  # noqa: SLF001 — read-only diagnostic snapshot.
        + list(registry._factories.keys())  # noqa: SLF001
    )
    logger.info(
        "startup.models",
        **{
            "models.service_count": len(services),
            "models.services": services,
        },
    )


async def _emit_config_provenance(config: EngineConfig) -> None:
    """Emit per-field config provenance via :mod:`config_provenance`.

    Walks the resolved :class:`EngineConfig` and emits one
    ``config.value.resolved`` event per field carrying the value, its
    source (default / env_var / file / cli / dashboard), and the
    canonical env-var key the value would respond to. A trailing
    ``startup.config.provenance`` summarizes the field count and how
    many were overridden, so the dashboard saga has a single anchor
    line that introduces the per-field stream.
    """
    from sovyx.engine.config_provenance import (  # noqa: PLC0415 — boot-time scope.
        ConfigSource,
        track_provenance,
    )

    provenance = track_provenance(config)
    overridden = 0
    for field_path, prov in provenance.items():
        if prov.source != ConfigSource.DEFAULT:
            overridden += 1
        logger.info(
            "config.value.resolved",
            **{
                "cfg.field": field_path,
                "cfg.source": str(prov.source),
                "cfg.value": prov.resolved_value,
                "cfg.env_key": prov.env_key,
            },
        )

    logger.info(
        "startup.config.provenance",
        **{
            "cfg.field_count": len(provenance),
            "cfg.overridden_count": overridden,
        },
    )


async def _emit_health_snapshot(registry: ServiceRegistry) -> None:
    """Emit a HealthRegistry snapshot if one is registered.

    Phase 11 Task 11.5 wires :class:`HealthRegistry` as a singleton in
    the ServiceRegistry. When the cascade runs *before* that wireup
    (early startup ordering, or a partial bootstrap that aborted),
    ``registry.resolve`` raises ``ServiceNotRegisteredError`` — we
    swallow it and emit a ``registry_present=False`` payload so the
    cascade contract stays stable.
    """
    health_registered = False
    health_summary: dict[str, Any] = {}
    try:
        from sovyx.observability.health import HealthRegistry  # noqa: PLC0415
    except ImportError:
        pass
    else:
        health_registry: HealthRegistry | None
        try:
            health_registry = await registry.resolve(HealthRegistry)
        except Exception:  # noqa: BLE001 — registry miss is expected pre-bootstrap.
            health_registry = None
        if health_registry is not None:
            health_registered = True
            try:
                raw = await health_registry.snapshot()
            except Exception as exc:  # noqa: BLE001 — diagnostic degrades.
                health_summary = {"error": str(exc)}
            else:
                health_summary = raw

    logger.info(
        "startup.health.snapshot",
        **{
            "health.registry_present": health_registered,
            "health.summary": health_summary,
        },
    )


__all__ = ["run_startup_cascade"]
