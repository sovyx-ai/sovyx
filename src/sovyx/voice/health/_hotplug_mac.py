"""macOS hot-plug listener stub — real implementation lands in Sprint 4.

ADR §4.4.2 commits to
``AudioObjectAddPropertyListener(kAudioHardwarePropertyDevices)`` on
macOS, but Sprint 2 intentionally ships without it: the CoreAudio
bindings (:mod:`pyobjc-framework-CoreAudio`) are a non-trivial extra
and the cross-platform cascade / probe peers are Sprint 4 territory
too. Shipping an honest no-op here keeps the factory wiring identical
across platforms and surfaces a single INFO line so operators can tell
the listener wasn't silently lost.

Task #28 replaces :func:`build_macos_hotplug_listener` with a native
backend that sits next to :mod:`_hotplug_linux` / :mod:`_hotplug_win`.
"""

from __future__ import annotations

from sovyx.observability.logging import get_logger
from sovyx.voice.health._hotplug import HotplugListener, NoopHotplugListener

logger = get_logger(__name__)


def build_macos_hotplug_listener() -> HotplugListener:
    """Return a :class:`NoopHotplugListener`; Sprint 4 swaps in CoreAudio."""
    logger.info(
        "voice_hotplug_listener_unavailable",
        platform="darwin",
        reason="macos_backend_pending_sprint4",
    )
    return NoopHotplugListener(reason="macOS backend pending Sprint 4 (Task #28)")


__all__ = [
    "build_macos_hotplug_listener",
]
