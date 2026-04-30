"""Linux sandbox detection (Flatpak / Snap / AppImage) [Phase 5 T5.45].

Modern Linux distros increasingly ship desktop apps as
sandboxed bundles — Flatpak (GNOME-canonical), Snap (Ubuntu-
canonical), AppImage (universal portable). Each sandbox has
its own microphone-access semantics:

* **Flatpak**: ``FLATPAK_ID`` env var set; mic access requires
  ``--device=all`` OR the ``Microphone`` portal permission via
  ``xdg-portal`` IPC.
* **Snap**: ``SNAP`` env var set; mic access requires the
  ``audio-record`` interface to be connected
  (``snap connect <name>:audio-record``).
* **AppImage**: ``APPIMAGE`` env var set; AppImages are NOT
  sandboxed — mic access works as on the host (this detection
  exists so logs show the bundling format for forensics).

Sovyx's deaf-signal cascade can fail mysteriously when a
sandbox blocks PortAudio device enumeration. Surfacing the
sandbox at boot lets operators check the portal/snap-interface
state BEFORE debugging the cryptic empty-device-list path.

Best-effort + read-only: pure environment-variable inspection,
NO IPC calls (a synchronous portal probe would block boot if
the portal daemon is unhealthy). The portal-permission state
itself is detected at the call site that hits a deaf-signal
incident — this module's job is identifying the sandbox kind.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import StrEnum

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


class LinuxSandboxKind(StrEnum):
    """Closed-set verdict of :func:`detect_linux_sandbox`.

    StrEnum (anti-pattern #9) so values serialise verbatim into
    the structured-event ``sandbox_kind`` field that dashboards
    key on. xdist-safe value comparison.
    """

    NONE = "none"
    """Not running inside a known sandbox. Either a native install
    OR a sandbox kind Sovyx doesn't yet recognise."""

    FLATPAK = "flatpak"
    """Flatpak — ``FLATPAK_ID`` env var present. Mic access via
    ``xdg-portal`` IPC + ``Microphone`` permission grant."""

    SNAP = "snap"
    """Snap — ``SNAP`` env var present. Mic access via
    ``snap connect <name>:audio-record`` interface."""

    APPIMAGE = "appimage"
    """AppImage — ``APPIMAGE`` env var present. NOT sandboxed;
    mic access works as on the host. Detection exists for
    forensics + logs."""


@dataclass(frozen=True, slots=True)
class SandboxSnapshot:
    """Single-shot detection result.

    All fields are read-only. ``platform_supported=False`` on
    non-Linux means the rest of the snapshot is meaningless;
    callers can short-circuit.
    """

    platform_supported: bool
    """``True`` only on ``sys.platform.startswith("linux")``.
    Windows + macOS callers receive False + skip the sandbox
    decision tree."""

    kind: LinuxSandboxKind
    """The detected sandbox. ``NONE`` when no sandbox env var
    matched OR when ``platform_supported=False``."""

    flatpak_id: str | None = None
    """The ``FLATPAK_ID`` value when ``kind=FLATPAK``. Operators
    can grep logs by this to find a specific Flatpak install."""

    snap_name: str | None = None
    """The ``SNAP_NAME`` value when ``kind=SNAP`` (or
    ``SNAP_INSTANCE_NAME`` if newer Snap). The ``SNAP`` env var
    itself is the snap's filesystem root; this is the name."""

    appimage_path: str | None = None
    """The ``APPIMAGE`` value when ``kind=APPIMAGE`` — the
    AppImage's mount path. Useful when forensic-grepping logs
    on hosts running multiple AppImages."""


def detect_linux_sandbox() -> SandboxSnapshot:
    """Identify which Linux sandbox (if any) Sovyx is running in.

    Pure env-var inspection; no subprocess, no IPC, no syscalls
    beyond the read of :func:`os.environ`. Always-safe — never
    raises.

    Returns:
        :class:`SandboxSnapshot`. ``kind=NONE`` is the common
        case (native install) — operators distinguish via the
        ``platform_supported`` field whether the detection ran
        at all.
    """
    if not sys.platform.startswith("linux"):
        return SandboxSnapshot(
            platform_supported=False,
            kind=LinuxSandboxKind.NONE,
        )

    # Order matters: a Flatpak instance running an AppImage
    # internally would set both env vars; we treat the OUTERMOST
    # sandbox as the meaningful one. Flatpak is checked first
    # because its sandbox is stronger (mic requires portal grant)
    # than AppImage (mic works natively).
    flatpak_id = os.environ.get("FLATPAK_ID")
    if flatpak_id:
        return SandboxSnapshot(
            platform_supported=True,
            kind=LinuxSandboxKind.FLATPAK,
            flatpak_id=flatpak_id,
        )

    snap_root = os.environ.get("SNAP")
    if snap_root:
        snap_name = os.environ.get("SNAP_INSTANCE_NAME") or os.environ.get("SNAP_NAME")
        return SandboxSnapshot(
            platform_supported=True,
            kind=LinuxSandboxKind.SNAP,
            snap_name=snap_name,
        )

    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return SandboxSnapshot(
            platform_supported=True,
            kind=LinuxSandboxKind.APPIMAGE,
            appimage_path=appimage,
        )

    return SandboxSnapshot(
        platform_supported=True,
        kind=LinuxSandboxKind.NONE,
    )


def log_sandbox_snapshot(snapshot: SandboxSnapshot) -> None:
    """Emit a single boot-time INFO log describing the sandbox.

    Fires nothing on non-Linux (snapshot already has nothing to
    say). On Linux, fires INFO for every kind including NONE so
    dashboards can graph the sandbox-kind distribution across a
    fleet (helps operators identify which fraction of users hit
    the Flatpak/Snap deaf-signal failure modes).

    Args:
        snapshot: The snapshot from :func:`detect_linux_sandbox`.
    """
    if not snapshot.platform_supported:
        return

    payload: dict[str, object] = {
        "voice.sandbox.kind": snapshot.kind.value,
    }

    if snapshot.kind == LinuxSandboxKind.FLATPAK:
        payload["voice.sandbox.flatpak_id"] = snapshot.flatpak_id or ""
        payload["voice.sandbox.remediation"] = (
            "Flatpak: ensure the runtime has --device=all OR grant "
            "Microphone access via Flatseal. Without either, "
            "PortAudio enumerates zero capture devices + Sovyx's "
            "deaf-signal cascade fails immediately."
        )
    elif snapshot.kind == LinuxSandboxKind.SNAP:
        payload["voice.sandbox.snap_name"] = snapshot.snap_name or ""
        payload["voice.sandbox.remediation"] = (
            "Snap: connect the audio-record interface — "
            "`snap connect <snap-name>:audio-record`. Without it, "
            "PortAudio enumerates zero capture devices + Sovyx's "
            "deaf-signal cascade fails immediately."
        )
    elif snapshot.kind == LinuxSandboxKind.APPIMAGE:
        payload["voice.sandbox.appimage_path"] = snapshot.appimage_path or ""
        # AppImage ISN'T sandboxed; no remediation needed. The
        # log fires for forensics + bundle-format attribution.
    # NONE → just the kind, no remediation.

    logger.info("voice.sandbox.detected", **payload)
