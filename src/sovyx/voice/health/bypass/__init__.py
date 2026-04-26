"""Pluggable APO-bypass strategies — one per platform path.

The :class:`~sovyx.voice.health.capture_integrity.CaptureIntegrityCoordinator`
iterates a platform-filtered list of :class:`PlatformBypassStrategy`
instances when a live capture stream degrades into
:class:`~sovyx.voice.health.contract.IntegrityVerdict.APO_DEGRADED`.

Shipped strategies:

* :class:`WindowsWASAPIExclusiveBypass` — re-opens the capture stream
  in WASAPI exclusive mode, bypassing the Windows Voice Clarity /
  VocaEffectPack APO chain entirely.
* :class:`LinuxALSAMixerResetBypass` — resets saturated pre-ADC gain
  controls (``Internal Mic Boost`` + ``Capture``) on the active Linux
  capture card, fixing the clipping pattern observed on laptop
  onboard codecs. Default-on.
* :class:`LinuxPipeWireDirectBypass` — opt-in; bypasses the Linux
  session manager (PipeWire / PulseAudio / JACK) by reopening the
  stream directly against the ALSA kernel device. Covers user-added
  filter chains and distro policies that insert DSP on the capture
  path.

Phase 4 will add:

* Windows ``DisableSysFx`` registry fallback for endpoints that cannot
  negotiate exclusive mode (driver limitation, policy gate).
* macOS ``kAudioDevicePropertyVoiceActivityDetectionEnabled`` toggle
  that disables CoreAudio VPIO.

Each strategy implements :class:`PlatformBypassStrategy` and lives in
its own underscore-prefixed module so the public surface of this
subpackage stays small.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.voice.health.bypass._strategy import (
    BypassApplyError,
    PlatformBypassStrategy,
)

if TYPE_CHECKING:
    from sovyx.voice.health.bypass._linux_alsa_mixer import (
        LinuxALSAMixerResetBypass,
    )
    from sovyx.voice.health.bypass._linux_pipewire_direct import (
        LinuxPipeWireDirectBypass,
    )
    from sovyx.voice.health.bypass._linux_session_manager_escape import (
        LinuxSessionManagerEscapeBypass,
    )
    from sovyx.voice.health.bypass._win_host_api_rotate_then_exclusive import (
        WindowsHostApiRotateThenExclusiveBypass,
    )
    from sovyx.voice.health.bypass._win_raw_communications import (
        WindowsRawCommunicationsBypass,
    )
    from sovyx.voice.health.bypass._win_wasapi_exclusive import (
        WindowsWASAPIExclusiveBypass,
    )

__all__ = [
    "BypassApplyError",
    "LinuxALSAMixerResetBypass",
    "LinuxPipeWireDirectBypass",
    "LinuxSessionManagerEscapeBypass",
    "PlatformBypassStrategy",
    "WindowsHostApiRotateThenExclusiveBypass",
    "WindowsRawCommunicationsBypass",
    "WindowsWASAPIExclusiveBypass",
]


# Lazy re-exports — concrete strategy modules depend on helpers that
# live outside this subpackage (e.g. ``_linux_mixer_apply``) which in
# turn import :class:`BypassApplyError` from this package. Eager imports
# here would turn the module graph into a cycle; deferring the bindings
# until first attribute access breaks it without changing the public
# surface. Pattern mirrors ``sovyx.observability.__init__``.
_LAZY_EXPORTS: dict[str, str] = {
    "LinuxALSAMixerResetBypass": "sovyx.voice.health.bypass._linux_alsa_mixer",
    "LinuxPipeWireDirectBypass": "sovyx.voice.health.bypass._linux_pipewire_direct",
    "LinuxSessionManagerEscapeBypass": ("sovyx.voice.health.bypass._linux_session_manager_escape"),
    "WindowsHostApiRotateThenExclusiveBypass": (
        "sovyx.voice.health.bypass._win_host_api_rotate_then_exclusive"
    ),
    "WindowsRawCommunicationsBypass": (
        "sovyx.voice.health.bypass._win_raw_communications"
    ),
    "WindowsWASAPIExclusiveBypass": "sovyx.voice.health.bypass._win_wasapi_exclusive",
}


def __getattr__(name: str) -> type[PlatformBypassStrategy]:
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module = import_module(module_path)
    value: type[PlatformBypassStrategy] = getattr(module, name)
    globals()[name] = value
    return value
