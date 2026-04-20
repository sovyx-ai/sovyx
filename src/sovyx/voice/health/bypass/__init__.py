"""Pluggable APO-bypass strategies — one per platform path.

The :class:`~sovyx.voice.health.capture_integrity.CaptureIntegrityCoordinator`
iterates a platform-filtered list of :class:`PlatformBypassStrategy`
instances when a live capture stream degrades into
:class:`~sovyx.voice.health.contract.IntegrityVerdict.APO_DEGRADED`.

Phase 1 ships the single strategy that definitively fixes the Windows
Voice Clarity / VocaEffectPack regression observed on the Razer
BlackShark V2 Pro:

* :class:`WindowsWASAPIExclusiveBypass` — re-opens the capture stream
  in WASAPI exclusive mode, bypassing the platform APO chain entirely.

Phase 3 will add:

* Windows ``DisableSysFx`` registry fallback for endpoints that cannot
  negotiate exclusive mode (driver limitation, policy gate).
* Linux ALSA ``hw:`` direct-node fallback that routes around a
  PulseAudio ``module-echo-cancel`` / PipeWire filter chain.
* macOS ``kAudioDevicePropertyVoiceActivityDetectionEnabled`` toggle
  that disables CoreAudio VPIO.

Each strategy implements :class:`PlatformBypassStrategy` and lives in
its own underscore-prefixed module so the public surface of this
subpackage stays small.
"""

from __future__ import annotations

from sovyx.voice.health.bypass._strategy import (
    BypassApplyError,
    PlatformBypassStrategy,
)
from sovyx.voice.health.bypass._win_wasapi_exclusive import (
    WindowsWASAPIExclusiveBypass,
)

__all__ = [
    "BypassApplyError",
    "PlatformBypassStrategy",
    "WindowsWASAPIExclusiveBypass",
]
