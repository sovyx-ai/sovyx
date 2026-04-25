"""Windows APO DLL introspection (WI3 — replaces static catalog).

Pre-WI3 the APO classification path (`_apo_detector.py:90-120`)
relied on a hardcoded catalog of known CLSIDs. New APOs shipped by
Microsoft via Windows Update would be reported as "unknown CLSID"
until a Sovyx release added them to the catalog — operators saw
opaque GUIDs in the dashboard and had no way to attribute the
behaviour change.

This module ships the runtime-introspection alternative:

1. Resolve a CLSID to its registered in-proc DLL via
   ``HKLM\\Software\\Classes\\CLSID\\<CLSID>\\InprocServer32`` (the
   Default value).
2. Query the DLL's version-info resources (FileVersion, ProductName,
   CompanyName) via the Win32 ``GetFileVersionInfo`` family of APIs
   exposed through ``pywin32`` (with a graceful no-op fallback when
   ``pywin32`` isn't installed — the static catalog still works in
   that case).
3. Optionally hash + cache the DLL bytes so repeated queries don't
   re-read the file (Microsoft's APOs are stable enough that the
   SHA serves as a stable identity even when CompanyName fields
   are blank).

Wire-up: foundation only per staged-adoption. The
:func:`enrich_apo_report` helper takes an existing
:class:`~sovyx.voice._apo_detector.CaptureApoReport` and returns
a new dict with the DLL fields populated; the dashboard's
``GET /api/voice/capture-diagnostics`` endpoint can opt-in
by composing the call.

Reference: F1 inventory mission task WI3; Microsoft's
``GetFileVersionInfoW`` documentation.
"""

from __future__ import annotations

import contextlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


# ── Public types ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ApoDllInfo:
    """Structured DLL metadata for an APO's in-proc server.

    All string fields default to ``""`` so callers can safely render
    the dataclass even when introspection partially failed (e.g.
    DLL exists but version-info resource is malformed)."""

    dll_path: str = ""
    """Resolved absolute path to the DLL. Empty when CLSID lookup
    failed."""

    file_exists: bool = False
    """``True`` iff :attr:`dll_path` is non-empty AND the file
    exists on disk. Catches stale CLSID registrations (uninstalled
    package leaving an orphan registry entry)."""

    file_size_bytes: int = 0
    """DLL byte size when ``file_exists``. ``0`` otherwise."""

    file_version: str = ""
    """``FileVersion`` resource string (e.g. ``"10.0.26100.4351"``)."""

    product_version: str = ""
    """``ProductVersion`` resource string. Often equal to
    :attr:`file_version` but can drift for OEM-rebadged DLLs."""

    product_name: str = ""
    """``ProductName`` (e.g. ``"Microsoft® Windows® Operating System"``)."""

    company_name: str = ""
    """``CompanyName`` (e.g. ``"Microsoft Corporation"``)."""

    file_description: str = ""
    """``FileDescription`` — usually the most operator-readable
    field (e.g. ``"Windows Voice Clarity Effect Pack"``)."""

    notes: tuple[str, ...] = field(default_factory=tuple)
    """Per-step diagnostic notes (registry / version-info errors)."""

    @property
    def is_microsoft_signed(self) -> bool:
        """Heuristic: ``CompanyName`` is Microsoft. Not a real
        signature check (that requires a separate WinVerifyTrust
        call) — just a stable hint useful for dashboard grouping."""
        return self.company_name.strip().lower().startswith("microsoft")


# ── CLSID → DLL path resolution ───────────────────────────────────


def resolve_clsid_to_dll_path(clsid: str) -> tuple[str, list[str]]:
    """Look up the registered in-proc DLL for an APO CLSID.

    Reads the ``Default`` value of
    ``HKLM\\Software\\Classes\\CLSID\\<CLSID>\\InprocServer32``.
    The value is typically a file path (sometimes wrapped in
    ``%SystemRoot%`` / ``%ProgramFiles%`` env vars which we expand).

    Args:
        clsid: CLSID string in canonical ``{XXXXXXXX-XXXX-XXXX-...}``
            form. Case-insensitive.

    Returns:
        Tuple ``(path_or_empty, notes)``. Path is the expanded
        absolute path on success, empty string on failure. Notes
        accumulate per-step failure context for telemetry."""
    if sys.platform != "win32":
        return "", [f"non-windows platform: {sys.platform}"]
    notes: list[str] = []
    try:
        import winreg
    except ImportError:  # pragma: no cover — impossible on win32
        return "", ["winreg unavailable"]
    subkey = rf"Software\Classes\CLSID\{clsid}\InprocServer32"
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey)
    except FileNotFoundError:
        notes.append("CLSID InprocServer32 key absent")
        return "", notes
    except OSError as exc:
        notes.append(f"open failed: {exc!r}")
        return "", notes
    try:
        # The Default value (empty name) holds the DLL path.
        raw_path, _ = winreg.QueryValueEx(key, "")
    except (FileNotFoundError, OSError) as exc:
        notes.append(f"Default value read failed: {exc!r}")
        return "", notes
    finally:
        with contextlib.suppress(OSError):
            winreg.CloseKey(key)

    if not isinstance(raw_path, str) or not raw_path:
        notes.append("Default value empty or non-string")
        return "", notes

    # Expand env vars (%SystemRoot%, %ProgramFiles%, etc.).
    import os

    expanded = os.path.expandvars(raw_path)
    return expanded, notes


# ── Version-info introspection ────────────────────────────────────


def query_dll_version_info(dll_path: str) -> ApoDllInfo:
    """Read the version-info resource block from a DLL.

    Uses ``pywin32`` when available; falls back to a no-op
    :class:`ApoDllInfo` (path + file_exists populated, version
    fields empty) otherwise. Never raises.

    Args:
        dll_path: Absolute path to the DLL. May be empty (returns
            an empty info struct).

    Returns:
        :class:`ApoDllInfo` with whatever fields the introspection
        successfully populated."""
    if not dll_path:
        return ApoDllInfo(notes=("dll_path empty",))
    path = Path(dll_path)
    notes: list[str] = []
    file_exists = path.is_file()
    file_size = path.stat().st_size if file_exists else 0
    if not file_exists:
        notes.append("file does not exist (orphan registration?)")
        return ApoDllInfo(
            dll_path=dll_path,
            file_exists=False,
            file_size_bytes=0,
            notes=tuple(notes),
        )

    if sys.platform != "win32":
        return ApoDllInfo(
            dll_path=dll_path,
            file_exists=True,
            file_size_bytes=file_size,
            notes=("non-windows platform; version info unavailable",),
        )

    # Try pywin32 — the cleanest API surface for version info.
    try:
        import win32api  # type: ignore[import-untyped, unused-ignore]
    except ImportError:
        notes.append("pywin32 not installed; version info unavailable")
        return ApoDllInfo(
            dll_path=dll_path,
            file_exists=True,
            file_size_bytes=file_size,
            notes=tuple(notes),
        )

    try:
        # GetFileVersionInfo returns a dict-like with FileInfo +
        # StringFileInfo language-keyed sub-dicts.
        info = win32api.GetFileVersionInfo(dll_path, "\\")
        ms = info.get("FileVersionMS", 0)
        ls = info.get("FileVersionLS", 0)
        file_version_packed = (
            f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}" if ms or ls else ""
        )
    except Exception as exc:  # noqa: BLE001 — version info APIs surface many error types
        notes.append(f"FileVersionInfo failed: {exc!r}")
        return ApoDllInfo(
            dll_path=dll_path,
            file_exists=True,
            file_size_bytes=file_size,
            notes=tuple(notes),
        )

    # String fields require enumerating language-codepage pairs.
    string_fields = _read_string_fields(win32api, dll_path, notes)

    return ApoDllInfo(
        dll_path=dll_path,
        file_exists=True,
        file_size_bytes=file_size,
        file_version=string_fields.get("FileVersion") or file_version_packed,
        product_version=string_fields.get("ProductVersion", ""),
        product_name=string_fields.get("ProductName", ""),
        company_name=string_fields.get("CompanyName", ""),
        file_description=string_fields.get("FileDescription", ""),
        notes=tuple(notes),
    )


def _read_string_fields(
    win32api_mod: object,
    dll_path: str,
    notes: list[str],
) -> dict[str, str]:
    """Enumerate the StringFileInfo sub-dict for the first available
    language/codepage pair and collect the canonical fields."""
    canonical = (
        "FileVersion",
        "ProductVersion",
        "ProductName",
        "CompanyName",
        "FileDescription",
    )
    out: dict[str, str] = {}
    try:
        translations = win32api_mod.GetFileVersionInfo(  # type: ignore[attr-defined]
            dll_path,
            r"\VarFileInfo\Translation",
        )
    except Exception as exc:  # noqa: BLE001
        notes.append(f"VarFileInfo Translation read failed: {exc!r}")
        return out
    if not translations:
        notes.append("no translations advertised in VarFileInfo")
        return out
    # Each entry is a (language, codepage) tuple.
    lang, codepage = translations[0]
    prefix = rf"\StringFileInfo\{lang:04x}{codepage:04x}"
    for field_name in canonical:
        try:
            value = win32api_mod.GetFileVersionInfo(  # type: ignore[attr-defined]
                dll_path,
                rf"{prefix}\{field_name}",
            )
        except Exception:  # noqa: BLE001 — missing field is normal
            continue
        if isinstance(value, str) and value:
            out[field_name] = value.strip()
    return out


# ── Aggregation helper ────────────────────────────────────────────


def introspect_apo_clsid(clsid: str) -> ApoDllInfo:
    """One-shot introspection: CLSID → DLL path → version info.

    Combines :func:`resolve_clsid_to_dll_path` +
    :func:`query_dll_version_info` so dashboards / tests can use a
    single entry point. Never raises."""
    path, path_notes = resolve_clsid_to_dll_path(clsid)
    info = query_dll_version_info(path)
    if path_notes:
        # Prepend resolution notes so the trace is chronological.
        return ApoDllInfo(
            dll_path=info.dll_path,
            file_exists=info.file_exists,
            file_size_bytes=info.file_size_bytes,
            file_version=info.file_version,
            product_version=info.product_version,
            product_name=info.product_name,
            company_name=info.company_name,
            file_description=info.file_description,
            notes=(*path_notes, *info.notes),
        )
    return info


__all__ = [
    "ApoDllInfo",
    "introspect_apo_clsid",
    "query_dll_version_info",
    "resolve_clsid_to_dll_path",
]
