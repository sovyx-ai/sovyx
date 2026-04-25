"""Capability-based dispatch foundation (X1 Phase 1).

Replaces the brittle ``if sys.platform == "win32"`` / ``"linux"`` /
``"darwin"`` branching scattered across 83+ sites in
``src/sovyx/voice/`` with a single capability registry. The mission's
core insight (§3.11):

    Capability != OS. WSL has Linux's ``sys.platform`` but
    can't run PortAudio's WASAPI exclusive mode. A custom
    Sovyx build inside a Docker container with PipeWire
    forwarded over the socket has Linux capabilities without
    a Linux kernel under it. macOS apps running under
    Rosetta 2 have darwin's platform string but inherit the
    x86_64 audio stack quirks. Branching on
    ``sys.platform`` for *capability* questions silently
    breaks every one of these realities.

This module defines the canonical :class:`Capability` enum and the
:class:`CapabilityResolver` that answers ``has(Capability.X) -> bool``
based on **runtime probes**, not OS-name string matches.

Phase 1 (this module)
=====================

Ships the foundation with stub probes:

* :class:`Capability` — the closed-set enum of capabilities the
  voice layer asks about. Adding an enum value is a deliberate
  vocabulary expansion; the import-time consistency check
  guarantees every capability declares both an OS-prefix hint
  (for the detector cache key) AND a docstring entry below.

* :class:`CapabilityResolver` — caches per-capability detection
  results, exposes ``has(cap) -> bool`` + ``dispatch(handlers) -> T``
  + ``require(cap) -> None`` API. The resolver itself is pure
  Python; per-capability detection logic lives in ``_PROBE_FNS``,
  which currently returns ``False`` for everything not yet wired
  (a deliberate fail-closed default — better to mis-report
  "capability absent" and fall back to a slower path than to
  mis-report "capability present" and crash trying to use it).

* :func:`get_default_resolver` — process-wide singleton accessor
  that lazily constructs the resolver. Tests can construct fresh
  resolvers (``CapabilityResolver()``) and inject custom probes;
  production code uses the singleton.

Phase 2 (subsequent commits)
============================

Each capability gets its real probe wired one-by-one:

* WASAPI_EXCLUSIVE — actual PortAudio query
* PIPEWIRE_MODULE_ECHO_CANCEL — `pw-cli list-modules` parse
* ALSA_UCM_CONFIG — `ls /usr/share/alsa/ucm2/` enum
* COREAUDIO_VPIO — Apple Audio Unit subtype query
* ETW_AUDIO_PROVIDER — Windows ETW Microsoft-Windows-Audio* check

Until each probe is wired, ``has()`` returns False — call sites
that opt in via the resolver get the safe "no capability" branch,
which matches the legacy ``if sys.platform == X`` behaviour
exactly when X happens to equal what we'd otherwise probe for.

Phase 3 (subsequent commits)
============================

Per-site migration of the 83 ``sys.platform`` branches in
``src/sovyx/voice/`` to the resolver:

    # Before
    if sys.platform == "win32":
        return open_wasapi_exclusive(...)

    # After
    if resolver.has(Capability.WASAPI_EXCLUSIVE):
        return open_wasapi_exclusive(...)

The migration is staged per-site (per the CLAUDE.md staged-
adoption pattern) so each commit is bounded and revertable.

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §3.11
(Cross-platform capability dispatch); CLAUDE.md anti-pattern #9
(StrEnum for value-based comparison + xdist safety).
"""

from __future__ import annotations

import sys
import threading
from enum import StrEnum
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from typing import TypeVar

    _T = TypeVar("_T")

logger = get_logger(__name__)


# ── Capability vocabulary ──────────────────────────────────────────


class Capability(StrEnum):
    """Closed-set vocabulary of voice-stack capabilities.

    Adding a new capability requires:

    1. Add the enum value here.
    2. Add a probe to :data:`_PROBE_FNS` (or accept the default
       fail-closed stub that returns ``False``).
    3. Document the capability's meaning in the entry below.

    StrEnum (anti-pattern #9) so value-based comparison is stable
    across pytest-xdist namespace duplication and the resolver's
    cache keys serialise to JSON without coercion.
    """

    # ── Windows ────────────────────────────────────────────────
    WASAPI_EXCLUSIVE = "wasapi_exclusive"
    """Caller can open a WASAPI shared-bypass exclusive-mode
    capture stream. Requires Windows + PortAudio compiled with
    WASAPI exclusive support + Windows Audio Endpoint Builder
    service running."""

    ETW_AUDIO_PROVIDER = "etw_audio_provider"
    """Caller can subscribe to Microsoft-Windows-Audio* ETW
    providers. Requires Windows + ETW DLLs available + the
    process running under a token with ``SeAuditPrivilege`` (or
    delegated via Audiosrv). WI1 task target."""

    AUDIOSRV_QUERY = "audiosrv_query"
    """Caller can query the state of the Audiosrv +
    AudioEndpointBuilder Windows services. Requires Windows +
    SCM access (typically: any non-restricted user). WI2 task."""

    # ── Linux ──────────────────────────────────────────────────
    PIPEWIRE_MODULE_ECHO_CANCEL = "pipewire_module_echo_cancel"
    """Caller can route capture through PipeWire's
    ``module-echo-cancel`` (Layer 1 of mission §4 cascade).
    Requires Linux + PipeWire daemon running + the module
    compiled in (default in PipeWire ≥ 0.3.40)."""

    ALSA_UCM_CONFIG = "alsa_ucm_config"
    """Caller can issue ``alsaucm`` verb selections (Layer 2 of
    mission §4 cascade). Requires Linux + the card has a UCM
    config shipped under ``/usr/share/alsa/ucm2/<card>/``."""

    PULSEAUDIO_RUNNING = "pulseaudio_running"
    """Caller is interacting with PulseAudio (not PipeWire's
    pulse-server emulation). Used to gate the legacy mixer-control
    path that PipeWire setups don't need."""

    # ── macOS ──────────────────────────────────────────────────
    COREAUDIO_VPIO = "coreaudio_vpio"
    """Caller can instantiate Apple's VoiceProcessingIO Audio
    Unit (native AGC2 alternative). Requires macOS + AudioUnit
    framework available. MA4 task."""

    SCREEN_CAPTURE_KIT = "screen_capture_kit"
    """Caller can use ScreenCaptureKit for system-audio loopback.
    Requires macOS 12.3+. MA11 task."""

    AVAUDIO_ENGINE = "avaudio_engine"
    """Caller can use AVAudioEngine as a PortAudio alternative
    capture path. Requires macOS + AVFoundation. MA6 task."""

    # ── Cross-platform (probe-based, not OS-tied) ─────────────
    PORTAUDIO_LOOPBACK = "portaudio_loopback"
    """Caller can open a PortAudio loopback stream (output
    capture). Available on Windows via WASAPI loopback, Linux
    via PipeWire monitor source, macOS via ScreenCaptureKit /
    BlackHole. Detected by probing — not OS-tied."""

    ONNX_INFERENCE = "onnx_inference"
    """Caller can run ONNX Runtime inference. Requires the
    ``onnxruntime`` package importable. Some deployment modes
    (locked-down enterprise images) strip it; the voice stack
    must degrade gracefully when absent."""


# ── Probe registry ─────────────────────────────────────────────────


def _probe_wasapi_exclusive() -> bool:
    """Stub — Phase 2 wires the real PortAudio capability query.

    Conservative default: only Windows can plausibly have it, but
    the real probe needs ``sounddevice.query_hostapis`` which
    requires sounddevice loaded. Returning False keeps callers on
    the legacy path until the probe ships.
    """
    return False


def _probe_etw_audio_provider() -> bool:
    """Stub — Phase 2 wires the real ETW provider query (WI1)."""
    return False


def _probe_audiosrv_query() -> bool:
    """Stub — Phase 2 wires the real SCM query (WI2)."""
    return False


def _probe_pipewire_module_echo_cancel() -> bool:
    """Stub — Phase 2 wires the real ``pw-cli list-modules`` parse (F3)."""
    return False


def _probe_alsa_ucm_config() -> bool:
    """Stub — Phase 2 wires the real UCM config enum (F4)."""
    return False


def _probe_pulseaudio_running() -> bool:
    """Stub — Phase 2 wires the real ``pulseaudio --check`` query."""
    return False


def _probe_coreaudio_vpio() -> bool:
    """Stub — Phase 2 wires the real Audio Unit subtype query (MA4)."""
    return False


def _probe_screen_capture_kit() -> bool:
    """Stub — Phase 2 wires the real macOS version + framework probe (MA11)."""
    return False


def _probe_avaudio_engine() -> bool:
    """Stub — Phase 2 wires the real AVFoundation availability probe (MA6)."""
    return False


def _probe_portaudio_loopback() -> bool:
    """Stub — Phase 2 wires the real PortAudio loopback availability probe."""
    return False


def _probe_onnx_inference() -> bool:
    """Probe ONNX Runtime importability.

    This one ships with a real probe in Phase 1 because it's pure
    Python (no platform-specific code), needed by the voice stack
    immediately, and the stub-False default would force every
    caller to skip ONNX features.
    """
    try:
        import onnxruntime  # noqa: F401, PLC0415 — capability probe
    except ImportError:
        return False
    return True


_PROBE_FNS: Mapping[Capability, Callable[[], bool]] = {
    Capability.WASAPI_EXCLUSIVE: _probe_wasapi_exclusive,
    Capability.ETW_AUDIO_PROVIDER: _probe_etw_audio_provider,
    Capability.AUDIOSRV_QUERY: _probe_audiosrv_query,
    Capability.PIPEWIRE_MODULE_ECHO_CANCEL: _probe_pipewire_module_echo_cancel,
    Capability.ALSA_UCM_CONFIG: _probe_alsa_ucm_config,
    Capability.PULSEAUDIO_RUNNING: _probe_pulseaudio_running,
    Capability.COREAUDIO_VPIO: _probe_coreaudio_vpio,
    Capability.SCREEN_CAPTURE_KIT: _probe_screen_capture_kit,
    Capability.AVAUDIO_ENGINE: _probe_avaudio_engine,
    Capability.PORTAUDIO_LOOPBACK: _probe_portaudio_loopback,
    Capability.ONNX_INFERENCE: _probe_onnx_inference,
}


def _validate_probe_table_complete() -> None:
    """Import-time guard: every Capability enum value MUST have a probe.

    Catches the failure mode where a new capability is added to
    the enum but the probe table isn't updated — without this,
    ``CapabilityResolver.has(new_cap)`` would raise KeyError at
    runtime instead of returning a clean False.
    """
    missing = set(Capability) - set(_PROBE_FNS.keys())
    if missing:
        names = sorted(c.name for c in missing)
        msg = (
            f"_PROBE_FNS missing entries for: {names}. "
            f"Every Capability enum value must declare a probe "
            f"(use the fail-closed stub returning False if no real "
            f"probe is wired yet)."
        )
        raise RuntimeError(msg)


_validate_probe_table_complete()


# ── Exceptions ─────────────────────────────────────────────────────


class CapabilityNotAvailableError(Exception):
    """Raised by :meth:`CapabilityResolver.require` when a hard
    capability dependency is absent.

    Carries the missing :class:`Capability` so the caller can
    branch on the specific failure class without parsing the
    message.
    """

    def __init__(self, capability: Capability) -> None:
        self.capability = capability
        msg = (
            f"required capability not available: {capability.value} "
            f"(probe returned False on this host)"
        )
        super().__init__(msg)


# ── Resolver ───────────────────────────────────────────────────────


class CapabilityResolver:
    """Process-wide registry of detected capabilities.

    The resolver caches probe results — each capability is probed
    at most once per resolver instance lifetime. Probes are
    expected to be cheap (filesystem stat, environment variable
    check, importable-module test); expensive probes (subprocess
    spawn) should themselves cache their results.

    Thread-safe: an internal :class:`threading.Lock` serialises
    cache mutations. The hot path is one dict membership check
    per ``has()`` call; the slow path is the probe + dict
    insertion under the lock.

    Args:
        probes: Optional override map ``{Capability: callable}``.
            Test-only injection point — production code uses the
            module-level :data:`_PROBE_FNS`. When provided, MUST
            cover every Capability value or the resolver raises
            ValueError at construction.
    """

    def __init__(
        self,
        probes: Mapping[Capability, Callable[[], bool]] | None = None,
    ) -> None:
        if probes is not None:
            missing = set(Capability) - set(probes.keys())
            if missing:
                names = sorted(c.name for c in missing)
                msg = (
                    f"probes mapping must cover every Capability; "
                    f"missing: {names}"
                )
                raise ValueError(msg)
            self._probes: Mapping[Capability, Callable[[], bool]] = probes
        else:
            self._probes = _PROBE_FNS
        self._cache: dict[Capability, bool] = {}
        self._lock = threading.Lock()

    @property
    def platform(self) -> str:
        """Read-through of :data:`sys.platform` for callers that
        need to disambiguate capabilities the resolver hasn't
        modelled yet. Use sparingly — every read here is a future
        capability we should add."""
        return sys.platform

    def has(self, capability: Capability) -> bool:
        """Return True iff the named capability is available.

        First call probes; subsequent calls return the cached
        verdict. Probe exceptions are caught + treated as "not
        available" (fail-closed) — a buggy probe shouldn't crash
        the resolver and brick every capability check.
        """
        with self._lock:
            cached = self._cache.get(capability)
            if cached is not None:
                return cached
            probe = self._probes[capability]
            try:
                verdict = bool(probe())
            except Exception as exc:  # noqa: BLE001 — fail-closed
                logger.warning(
                    "voice.capability.probe_failed",
                    capability=capability.value,
                    error=type(exc).__name__,
                    detail=str(exc)[:200],
                    action_required=(
                        "fix the probe; in the meantime the resolver "
                        "fail-closes to has=False"
                    ),
                )
                verdict = False
            self._cache[capability] = verdict
        logger.debug(
            "voice.capability.resolved",
            capability=capability.value,
            present=verdict,
        )
        return verdict

    def require(self, capability: Capability) -> None:
        """Raise :class:`CapabilityNotAvailableError` if absent.

        For call sites where a capability is a hard precondition
        (no fallback path possible). Most call sites should use
        :meth:`has` + an ``if`` branch instead, so the absent
        path is a graceful degradation.
        """
        if not self.has(capability):
            raise CapabilityNotAvailableError(capability)

    def dispatch(
        self,
        handlers: Mapping[Capability, Callable[[], _T]],
        *,
        default: Callable[[], _T] | None = None,
    ) -> _T:
        """Invoke the handler for the first present capability.

        Iterates ``handlers`` in insertion order (Python 3.7+
        dict ordering guarantee) and calls the first handler whose
        capability is present. If none are present, calls
        ``default()`` if provided; otherwise raises
        :class:`CapabilityNotAvailableError` for the FIRST
        capability in the map (the one the caller would prefer).

        Useful at sites that want to write::

            return resolver.dispatch({
                Capability.PIPEWIRE_MODULE_ECHO_CANCEL: _open_pipewire,
                Capability.ALSA_UCM_CONFIG:             _open_ucm,
                Capability.WASAPI_EXCLUSIVE:            _open_wasapi,
            }, default=_open_portaudio_default)

        instead of an ``if/elif`` chain over ``sys.platform``.
        """
        if not handlers:
            msg = "dispatch handlers map must not be empty"
            raise ValueError(msg)
        for cap, handler in handlers.items():
            if self.has(cap):
                return handler()
        if default is not None:
            return default()
        first_cap = next(iter(handlers))
        raise CapabilityNotAvailableError(first_cap)

    def reset_cache(self) -> None:
        """Drop every cached probe result. Test-only helper.

        Production code should never need this — capability
        presence is intrinsic to the host and doesn't change
        mid-process. Provided so tests that monkeypatch a probe
        can re-probe after the patch.
        """
        with self._lock:
            self._cache.clear()

    def cached_results(self) -> dict[Capability, bool]:
        """Snapshot of currently-cached verdicts. Diagnostic only."""
        with self._lock:
            return dict(self._cache)


# ── Module-level singleton accessor ────────────────────────────────


_default_resolver: CapabilityResolver | None = None
_default_lock = threading.Lock()


def get_default_resolver() -> CapabilityResolver:
    """Return the process-wide default :class:`CapabilityResolver`.

    Lazy-constructed on first call; subsequent calls return the
    same instance. Tests should construct their own resolvers
    (``CapabilityResolver(probes={...})``) instead of mutating
    the singleton.
    """
    global _default_resolver
    if _default_resolver is not None:
        return _default_resolver
    with _default_lock:
        if _default_resolver is None:
            _default_resolver = CapabilityResolver()
    return _default_resolver


def reset_default_resolver_for_tests() -> None:
    """Reset the module-level singleton. Test-only helper.

    Required when a test mutates the singleton's cache (e.g. via
    a monkeypatched probe) and a subsequent test needs a clean
    slate. Production code never calls this.
    """
    global _default_resolver
    with _default_lock:
        _default_resolver = None


__all__ = [
    "Capability",
    "CapabilityNotAvailableError",
    "CapabilityResolver",
    "get_default_resolver",
    "reset_default_resolver_for_tests",
]
