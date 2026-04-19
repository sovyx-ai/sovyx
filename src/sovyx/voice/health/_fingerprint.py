"""§5.13 — audio-subsystem fingerprinting for combo-store invalidation.

The :class:`~sovyx.voice.health.contract.ComboStore` invalidation rules
R6/R7 fire when the *signal path between the OS and PortAudio* has
changed: a Windows cumulative update reshuffled MMDevices, a
PulseAudio config edit re-routed the default sink, a CoreAudio HAL
plugin was installed/removed. ``platform.version()`` is too coarse —
plenty of cumulative updates ship the same NT build number while
swapping the APO chain underneath. We compute a SHA256 over the actual
configuration tree instead.

Design
======

* **Pure & deterministic.** Same input → same SHA forever. No
  timestamps, no PIDs, no random salts in the hashed bytes (the
  ``computed_at`` field on :class:`AudioSubsystemFingerprint` is
  diagnostic-only and never participates in equality).
* **Best-effort.** Any I/O failure collapses that field to the empty
  string. An empty SHA is treated by R6/R7 as "unknown — keep the
  entry but flag for re-validation". A missing config file (PulseAudio
  on a PipeWire-only system, no HAL plugins on a fresh macOS) is the
  common case, not an error.
* **Cross-platform stub safety.** Importing this module on a non-target
  platform must not blow up — every platform-specific helper is
  guarded and returns the empty string when its OS is absent.
* **Cheap.** Whole-system fingerprint < 50 ms on a healthy box
  (measured: ~8 ms on Win11 with 4 active endpoints). Per-endpoint
  fingerprint < 5 ms.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path

from sovyx.observability.logging import get_logger
from sovyx.voice.health.contract import AudioSubsystemFingerprint

logger = get_logger(__name__)


_CAPTURE_ROOT = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
_RENDER_ROOT = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"

_PULSE_CONFIG_PATHS: tuple[Path, ...] = (
    Path("/etc/pulse/default.pa"),
    Path("/etc/pulse/system.pa"),
    Path("/etc/pulse/daemon.conf"),
)
_PIPEWIRE_CONFIG_DIRS: tuple[Path, ...] = (
    Path("/etc/pipewire"),
    Path("/usr/share/pipewire"),
)

_COREAUDIO_HAL_DIRS: tuple[Path, ...] = (
    Path("/Library/Audio/Plug-Ins/HAL"),
    Path.home() / "Library" / "Audio" / "Plug-Ins" / "HAL",
)


def compute_audio_subsystem_fingerprint() -> AudioSubsystemFingerprint:
    """Snapshot the OS-level audio configuration as SHA256 fields.

    The returned :class:`AudioSubsystemFingerprint` populates exactly
    the field for the current platform; the others stay empty. Callers
    that persist the fingerprint into a :class:`ComboEntry` must read
    only the field for the platform that wrote it (the
    :class:`ComboStore` invalidation rules already do this).
    """
    win_endpoints = ""
    win_fx_global = ""
    linux_pulse = ""
    macos_hal = ""

    if sys.platform == "win32":
        win_endpoints = _hash_windows_mmdevices_subtree()
        win_fx_global = _hash_windows_fxproperties_global()
    elif sys.platform == "darwin":
        macos_hal = _hash_macos_coreaudio_plugins()
    else:
        linux_pulse = _hash_linux_pulse_pipewire_config()

    return AudioSubsystemFingerprint(
        windows_audio_endpoints_sha=win_endpoints,
        windows_fxproperties_global_sha=win_fx_global,
        linux_pulseaudio_config_sha=linux_pulse,
        macos_coreaudio_plugins_sha=macos_hal,
        computed_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def compute_endpoint_fxproperties_sha(endpoint_guid: str) -> str:
    """SHA256 over one endpoint's FxProperties + Properties subtree.

    Used by R8 to invalidate a single entry when its endpoint's APO
    chain changed (e.g. user toggled "Audio Enhancements" in Sound
    settings). Returns empty string on non-Windows or when the endpoint
    is absent.
    """
    if sys.platform != "win32":
        return ""
    try:
        import winreg
    except ImportError:  # pragma: no cover — impossible on win32
        return ""

    hasher = hashlib.sha256()
    try:
        ep = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{_CAPTURE_ROOT}\\{endpoint_guid}")
    except OSError as exc:
        logger.debug(
            "voice_fingerprint_endpoint_missing",
            endpoint=endpoint_guid,
            detail=str(exc),
        )
        return ""
    try:
        _hash_winreg_subtree(winreg, ep, hasher)
    finally:
        winreg.CloseKey(ep)
    return hasher.hexdigest()


# ── Windows helpers ─────────────────────────────────────────────────────


def _hash_windows_mmdevices_subtree() -> str:
    """Hash the entire MMDevices Capture+Render subtree (recursive)."""
    try:
        import winreg
    except ImportError:  # pragma: no cover — impossible on win32
        return ""

    hasher = hashlib.sha256()
    for root_path in (_CAPTURE_ROOT, _RENDER_ROOT):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root_path)
        except OSError as exc:
            logger.debug(
                "voice_fingerprint_root_missing",
                root=root_path,
                detail=str(exc),
            )
            continue
        try:
            hasher.update(root_path.encode("utf-8"))
            hasher.update(b"\x1f")
            _hash_winreg_subtree(winreg, root, hasher)
        finally:
            winreg.CloseKey(root)
    return hasher.hexdigest()


def _hash_windows_fxproperties_global() -> str:
    """Hash every endpoint's FxProperties subkey across Capture+Render.

    Tighter signal than the full MMDevices hash: this fingerprint
    changes only when an APO chain is added/removed/reordered, not
    when (e.g.) DeviceState flips because the user un/replugged a mic.
    """
    try:
        import winreg
    except ImportError:  # pragma: no cover — impossible on win32
        return ""

    hasher = hashlib.sha256()
    for root_path in (_CAPTURE_ROOT, _RENDER_ROOT):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root_path)
        except OSError:
            continue
        try:
            idx = 0
            while True:
                try:
                    endpoint_guid = winreg.EnumKey(root, idx)
                except OSError:
                    break
                idx += 1
                hasher.update(root_path.encode("utf-8"))
                hasher.update(b"\x1f")
                hasher.update(endpoint_guid.encode("utf-8"))
                hasher.update(b"\x1f")
                try:
                    ep = winreg.OpenKey(root, endpoint_guid)
                except OSError:
                    continue
                try:
                    try:
                        fx = winreg.OpenKey(ep, "FxProperties")
                    except OSError:
                        continue
                    try:
                        _hash_winreg_values(winreg, fx, hasher)
                    finally:
                        winreg.CloseKey(fx)
                finally:
                    winreg.CloseKey(ep)
        finally:
            winreg.CloseKey(root)
    return hasher.hexdigest()


def _hash_winreg_subtree(winreg_mod: object, key: object, hasher: object) -> None:
    """Recursively hash all values + subkeys under ``key`` in canonical order.

    Sub-keys are visited in sorted order so the hash is stable across
    enumeration-order quirks (Windows does not guarantee insertion
    order). Values within a key are likewise sorted by name.
    """
    wr: object = winreg_mod
    h: object = hasher

    _hash_winreg_values(wr, key, h)

    sub_names: list[str] = []
    idx = 0
    while True:
        try:
            sub_names.append(wr.EnumKey(key, idx))  # type: ignore[attr-defined]
        except OSError:
            break
        idx += 1
    sub_names.sort()
    for name in sub_names:
        try:
            sub = wr.OpenKey(key, name)  # type: ignore[attr-defined]
        except OSError:
            continue
        try:
            h.update(b"K\x1f")  # type: ignore[attr-defined]
            h.update(name.encode("utf-8"))  # type: ignore[attr-defined]
            h.update(b"\x1f")  # type: ignore[attr-defined]
            _hash_winreg_subtree(wr, sub, h)
        finally:
            wr.CloseKey(sub)  # type: ignore[attr-defined]


def _hash_winreg_values(winreg_mod: object, key: object, hasher: object) -> None:
    """Hash all values under ``key`` in canonical (sorted-by-name) order."""
    wr: object = winreg_mod
    h: object = hasher

    entries: list[tuple[str, object, int]] = []
    idx = 0
    while True:
        try:
            name, data, vtype = wr.EnumValue(key, idx)  # type: ignore[attr-defined]
        except OSError:
            break
        idx += 1
        entries.append((name, data, vtype))
    entries.sort(key=lambda e: e[0])
    for name, data, vtype in entries:
        h.update(b"V\x1f")  # type: ignore[attr-defined]
        h.update(name.encode("utf-8"))  # type: ignore[attr-defined]
        h.update(b"\x1f")  # type: ignore[attr-defined]
        h.update(str(vtype).encode("utf-8"))  # type: ignore[attr-defined]
        h.update(b"\x1f")  # type: ignore[attr-defined]
        h.update(_canonical_bytes(data))  # type: ignore[attr-defined]
        h.update(b"\x1e")  # type: ignore[attr-defined]


def _canonical_bytes(data: object) -> bytes:
    """Coerce a registry value into stable bytes for hashing.

    Strings → UTF-8. Bytes → as-is. Lists of strings (REG_MULTI_SZ) →
    each element NUL-joined. Everything else → ``str(value)``.
    """
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, list):
        return b"\x00".join(
            (item.encode("utf-8") if isinstance(item, str) else str(item).encode("utf-8"))
            for item in data
        )
    if isinstance(data, int):
        return str(data).encode("utf-8")
    return str(data).encode("utf-8")


# ── Linux helpers ───────────────────────────────────────────────────────


def _hash_linux_pulse_pipewire_config() -> str:
    """Hash PulseAudio + PipeWire config files (content + sorted paths).

    Hashing content (not mtimes) survives package re-installs that
    rewrite the same bytes with a fresh mtime — those should NOT
    invalidate the combo store. A user edit that actually changes
    behavior does change content.
    """
    hasher = hashlib.sha256()
    files: list[Path] = []

    for path in _PULSE_CONFIG_PATHS:
        if path.is_file():
            files.append(path)

    for cfg_dir in _PIPEWIRE_CONFIG_DIRS:
        if cfg_dir.is_dir():
            try:
                for path in sorted(cfg_dir.rglob("*.conf")):
                    if path.is_file():
                        files.append(path)
            except OSError as exc:
                logger.debug(
                    "voice_fingerprint_pipewire_walk_failed",
                    dir=str(cfg_dir),
                    detail=str(exc),
                )

    for path in sorted(set(files), key=lambda p: str(p)):
        hasher.update(str(path).encode("utf-8"))
        hasher.update(b"\x1f")
        try:
            hasher.update(path.read_bytes())
        except OSError as exc:
            logger.debug(
                "voice_fingerprint_file_read_failed",
                path=str(path),
                detail=str(exc),
            )
            continue
        hasher.update(b"\x1e")
    return hasher.hexdigest()


# ── macOS helpers ───────────────────────────────────────────────────────


def _hash_macos_coreaudio_plugins() -> str:
    """Hash the CoreAudio HAL plugin list (sorted paths + Info.plist content).

    A new HAL plugin (BlackHole, Loopback, Voicemod, etc.) re-shapes
    every device list in CoreAudio — the cached combo for the built-in
    mic may stop being valid because the default device shifted. We
    hash plugin names + their Info.plist (when readable) so swap-in
    /swap-out is detected, but a plugin's transient resource files
    (caches, logs) do not invalidate.
    """
    hasher = hashlib.sha256()
    plugins: list[Path] = []
    for hal_dir in _COREAUDIO_HAL_DIRS:
        if hal_dir.is_dir():
            try:
                plugins.extend(p for p in hal_dir.iterdir() if p.suffix == ".driver")
            except OSError as exc:
                logger.debug(
                    "voice_fingerprint_hal_walk_failed",
                    dir=str(hal_dir),
                    detail=str(exc),
                )

    for plugin in sorted(plugins, key=lambda p: str(p)):
        hasher.update(str(plugin).encode("utf-8"))
        hasher.update(b"\x1f")
        info_plist = plugin / "Contents" / "Info.plist"
        if info_plist.is_file():
            try:
                hasher.update(info_plist.read_bytes())
            except OSError:
                continue
        hasher.update(b"\x1e")
    return hasher.hexdigest()


__all__ = [
    "compute_audio_subsystem_fingerprint",
    "compute_endpoint_fxproperties_sha",
]
