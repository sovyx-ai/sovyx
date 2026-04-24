"""L2 — Cascading open strategies.

See ADR §4.2 + §5.5 + §5.6. Given an endpoint the cascade tries combos in
priority order until a probe returns :attr:`~sovyx.voice.health.contract.Diagnosis.HEALTHY`:

1. :class:`~sovyx.voice.health.capture_overrides.CaptureOverrides` — the
   user-pinned combo for this endpoint, if one exists (source ``"pinned"``).
2. :class:`~sovyx.voice.health.combo_store.ComboStore` fast path — the last
   known-good combo for this endpoint, if one exists and isn't flagged
   ``needs_revalidation`` (source ``"store"``).
3. Platform cascade — :data:`WINDOWS_CASCADE` / :data:`LINUX_CASCADE` /
   :data:`MACOS_CASCADE`, tried in declaration order (source ``"cascade"``).

The cascade is wrapped in two safety rails:

* **Lifecycle lock** (ADR §5.5). Per-endpoint :class:`asyncio.Lock`
  stored in an :class:`~sovyx.engine._lock_dict.LRULockDict` so only one
  cascade / invalidation / record-winning ever runs against a given
  endpoint at a time. Prevents hot-plug races and doctor-vs-daemon
  races. Bounded to 64 endpoints to satisfy CLAUDE.md anti-pattern #15.

* **Time budget** (ADR §5.6). Total 30 s wall-clock for the whole
  cascade (6 default attempts × ~5 s each, 8 for the opt-in aggressive
  variant); per-attempt 5 s via the probe's hard timeout. On
  total-budget exhaustion the cascade returns with
  ``budget_exhausted=True`` and the best attempt so far (or none).

On a HEALTHY winner the cascade records the combo to the ComboStore
(unless the winner came from the store already) so the next boot hits
the fast path.

Cross-platform note: Linux and macOS cascade tables are defined here
but marked empty for Sprint 1 — Tasks #27 / #28 populate them with the
ALSA / CoreAudio-specific entries from ADR §4.2. A cascade on an
unsupported platform returns ``source="none"`` with no attempts; the
caller is expected to fall back to the legacy single-open path.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Protocol

from sovyx.engine._lock_dict import LRULockDict
from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.voice.health._metrics import (
    record_cascade_attempt,
    record_combo_store_hit,
    record_kernel_invalidated_event,
    record_probe_result,
)
from sovyx.voice.health._quarantine import (
    EndpointQuarantine,
    get_default_quarantine,
)
from sovyx.voice.health.contract import (
    CandidateEndpoint,
    CandidateSource,
    CascadeResult,
    Combo,
    Diagnosis,
    ProbeMode,
    ProbeResult,
)
from sovyx.voice.health.probe import (
    _classify_open_error,
)
from sovyx.voice.health.probe import (
    probe as _default_probe,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sovyx.voice.health._mixer_sanity import MixerSanitySetup
    from sovyx.voice.health.capture_overrides import CaptureOverrides
    from sovyx.voice.health.combo_store import ComboStore

logger = get_logger(__name__)


# ── Cascade tuning defaults ─────────────────────────────────────────────
#
# Sourced from :class:`VoiceTuningConfig` so every knob is overridable via
# ``SOVYX_TUNING__VOICE__CASCADE_*`` env vars. CLAUDE.md anti-pattern #17.

_DEFAULT_TOTAL_BUDGET_S = _VoiceTuning().cascade_total_budget_s
"""Total cascade wall-clock budget. ADR §5.6."""

_DEFAULT_ATTEMPT_BUDGET_S = _VoiceTuning().cascade_attempt_budget_s
"""Per-attempt budget passed to the probe's ``hard_timeout_s``. ADR §5.6."""

_DEFAULT_WIZARD_TOTAL_BUDGET_S = _VoiceTuning().cascade_wizard_total_budget_s
"""Wizard user-facing budget. ADR §5.6 — a human is watching."""

_LIFECYCLE_LOCK_MAX = _VoiceTuning().cascade_lifecycle_lock_max
"""Max concurrent endpoints tracked by the lifecycle lock dict."""

_VOICE_CLARITY_AUTOFIX_FIRST_ATTEMPT = 3
"""When ``voice_clarity_autofix=False``, skip indices 0..2 (WASAPI exclusive)
and start at attempt 3 (shared best-effort). ADR §5.11/§5.12.

This is a cascade-table index, not a tuning knob — changing it requires
re-ordering the :data:`WINDOWS_CASCADE` tuple. It belongs here, not in
:class:`VoiceTuningConfig`.
"""


# ── Platform cascade tables ─────────────────────────────────────────────


def _windows_cascade() -> tuple[Combo, ...]:
    """Build the default Windows cascade.

    ``sample_rate`` is nominal — callers that need a device's actual
    "native" rate (attempt 2) override the tuple entry at the call site
    via ``cascade_override``. 48 kHz is the overwhelming default on
    modern Windows hardware, so it doubles as attempt 2's nominal
    native rate for the default cascade.

    WDM-KS (kernel streaming) is intentionally *not* part of the default
    cascade. See :func:`_windows_cascade_aggressive` for the opt-in tuple
    that includes it and the rationale against the default.
    """
    w32 = "win32"
    return (
        Combo(
            host_api="WASAPI",
            sample_rate=16_000,
            channels=1,
            sample_format="int16",
            exclusive=True,
            auto_convert=False,
            frames_per_buffer=480,
            platform_key=w32,
        ),
        Combo(
            host_api="WASAPI",
            sample_rate=48_000,
            channels=1,
            sample_format="int16",
            exclusive=True,
            auto_convert=False,
            frames_per_buffer=480,
            platform_key=w32,
        ),
        Combo(
            host_api="WASAPI",
            sample_rate=48_000,
            channels=1,
            sample_format="int16",
            exclusive=True,
            auto_convert=False,
            frames_per_buffer=960,
            platform_key=w32,
        ),
        Combo(
            host_api="WASAPI",
            sample_rate=16_000,
            channels=1,
            sample_format="int16",
            exclusive=False,
            auto_convert=True,
            frames_per_buffer=480,
            platform_key=w32,
        ),
        Combo(
            host_api="DirectSound",
            sample_rate=16_000,
            channels=1,
            sample_format="int16",
            exclusive=False,
            auto_convert=False,
            frames_per_buffer=480,
            platform_key=w32,
        ),
        Combo(
            host_api="MME",
            sample_rate=16_000,
            channels=1,
            sample_format="int16",
            exclusive=False,
            auto_convert=False,
            frames_per_buffer=480,
            platform_key=w32,
        ),
    )


def _windows_cascade_aggressive() -> tuple[Combo, ...]:
    """Build the opt-in aggressive Windows cascade with WDM-KS.

    Same as :func:`_windows_cascade` but with two additional WDM-KS
    (Windows Driver Model Kernel Streaming) attempts inserted between
    the WASAPI-exclusive trio and the shared-mode fallback chain.

    WDM-KS issues IOCTLs directly to the audio miniport at kernel level.
    On well-behaved drivers it provides another APO-bypass surface on
    top of the WASAPI-exclusive path. On *misbehaving* drivers (notably
    some USB-audio class drivers: Razer BlackShark V2 Pro / VID_1532 is
    a confirmed case), an IOCTL on an endpoint whose upstream
    ``IAudioClient::Initialize`` just failed with
    ``AUDCLNT_E_DEVICE_INVALIDATED`` can leave the driver's event-queue
    thread wedged. Windows then fires a kernel resource watchdog
    (``LiveKernelEvent 0x1CC``) and hard-resets the machine
    (``Kernel-Power 41``, ``BugcheckCode=0``, no dump).

    Because WDM-KS attempts add *no* APO-bypass capability beyond what
    WASAPI exclusive (attempts 0-2) already covers, they are off by
    default. Callers wanting the aggressive table pass
    ``cascade_override=WINDOWS_CASCADE_AGGRESSIVE`` to
    :func:`run_cascade` — e.g. an opt-in aggressive wizard path for
    operators who have verified their drivers are safe against WDM-KS.
    """
    base = _windows_cascade()
    w32 = "win32"
    kernel_streaming = (
        Combo(
            host_api="WDM-KS",
            sample_rate=16_000,
            channels=1,
            sample_format="int16",
            exclusive=False,
            auto_convert=False,
            frames_per_buffer=480,
            platform_key=w32,
        ),
        Combo(
            host_api="WDM-KS",
            sample_rate=48_000,
            channels=1,
            sample_format="int16",
            exclusive=False,
            auto_convert=False,
            frames_per_buffer=480,
            platform_key=w32,
        ),
    )
    # Insert kernel-streaming attempts between WASAPI-exclusive (0-2)
    # and WASAPI-shared (3) so the aggressive table preserves the
    # "exclusive → kernel → shared → legacy" ordering of the pre-v0.20.4
    # default cascade.
    return base[:3] + kernel_streaming + base[3:]


WINDOWS_CASCADE: tuple[Combo, ...] = _windows_cascade()
"""Default Windows 6-attempt cascade. Exclusive WASAPI → shared → legacy.

Ordering rationale (ADR §4.2, updated 2026-04-20 after the Razer
BlackShark V2 Pro / ``usbaudio`` kernel hard-reset incident — see
:func:`_windows_cascade_aggressive` for the ``LiveKernelEvent 0x1CC``
post-mortem):

* Attempts 0-2: exclusive WASAPI bypasses the entire capture APO chain
  (Voice Clarity, OEM DSPs). Most hostile environments resolve here.
* Attempt 3: shared WASAPI with ``auto_convert`` — graceful fallback
  when the driver rejects exclusive mode; used as the first attempt
  when ``voice_clarity_autofix=False``.
* Attempts 4-5: DirectSound + MME — legacy fallbacks for ancient
  hardware. Signal still flows but resampler-rich and lossy.

WDM-KS (kernel streaming) has been **removed from the default** because
it adds no APO-bypass capability beyond WASAPI exclusive but can lock
up fragile USB-audio drivers into a kernel resource timeout that the
OS resolves with an unrecoverable hard-reset. The aggressive variant is
still available via :data:`WINDOWS_CASCADE_AGGRESSIVE`.
"""


WINDOWS_CASCADE_AGGRESSIVE: tuple[Combo, ...] = _windows_cascade_aggressive()
"""Opt-in 8-attempt Windows cascade including WDM-KS.

Callers that explicitly want kernel streaming in the mix (aggressive
wizard modes, power-user diagnostic runs) pass this as
``cascade_override`` to :func:`run_cascade`. Never used as the default
cascade — see :func:`_windows_cascade_aggressive` for why.
"""


def _linux_cascade() -> tuple[Combo, ...]:
    """Build the Linux cascade per ADR §4.2.

    Ordering rationale:

    * Attempts 0-1: ALSA direct (``hw:``, ``exclusive=True``) bypasses
      every user-space mixing layer — PulseAudio, PipeWire, or any
      ``module-echo-cancel`` / ``filter-chain`` stage. On distros where
      WebRTC-AEC is the default capture path this is the only way to
      get a raw mic signal to Silero VAD.
    * Attempt 2: JACK — low-latency pro-audio path, typically no AEC
      inline. Only reachable when the user has ``jackd`` / ``pipewire-jack``
      running; falls through silently otherwise.
    * Attempts 3-4: PipeWire native — modern distro default. Shared access
      through the session manager; ``auto_convert=True`` asks the server
      to resample transparently so we don't depend on the node's native rate.
    * Attempt 5: PulseAudio shared — last-resort fallback for systems
      still running the legacy daemon. Almost always lossy (8 kHz
      auto-resample on laptops) but signal still flows.
    """
    lnx = "linux"
    return (
        Combo(
            host_api="ALSA",
            sample_rate=16_000,
            channels=1,
            sample_format="int16",
            exclusive=True,
            auto_convert=False,
            frames_per_buffer=480,
            platform_key=lnx,
        ),
        Combo(
            host_api="ALSA",
            sample_rate=48_000,
            channels=1,
            sample_format="int16",
            exclusive=True,
            auto_convert=False,
            frames_per_buffer=480,
            platform_key=lnx,
        ),
        Combo(
            host_api="JACK",
            sample_rate=48_000,
            channels=1,
            sample_format="float32",
            exclusive=False,
            auto_convert=False,
            frames_per_buffer=480,
            platform_key=lnx,
        ),
        Combo(
            host_api="PipeWire",
            sample_rate=16_000,
            channels=1,
            sample_format="int16",
            exclusive=False,
            auto_convert=True,
            frames_per_buffer=480,
            platform_key=lnx,
        ),
        Combo(
            host_api="PipeWire",
            sample_rate=48_000,
            channels=1,
            sample_format="int16",
            exclusive=False,
            auto_convert=True,
            frames_per_buffer=480,
            platform_key=lnx,
        ),
        Combo(
            host_api="PulseAudio",
            sample_rate=16_000,
            channels=1,
            sample_format="int16",
            exclusive=False,
            auto_convert=True,
            frames_per_buffer=480,
            platform_key=lnx,
        ),
    )


LINUX_CASCADE: tuple[Combo, ...] = _linux_cascade()
"""Linux 6-attempt cascade. ALSA direct → JACK → PipeWire → PulseAudio.

Ordering rationale (ADR §4.2): ALSA ``hw:`` bypasses every user-space
APO (``module-echo-cancel``, PipeWire ``filter-chain``); JACK is the
pro-audio escape hatch; PipeWire is the modern shared default;
PulseAudio is the legacy last-resort.

The ``exclusive`` flag on Linux is interpreted by the stream opener as
"request direct ``hw:`` access" rather than mixed plughw/pulse access.
``auto_convert`` signals "let the server resample/rechannel" on the
mixing-layer entries.
"""


def build_linux_cascade_for_device(
    device_default_samplerate: int,
    device_kind: str,
    *,
    tuning: _VoiceTuning | None = None,
) -> tuple[Combo, ...]:
    """Return the Linux cascade with an optional native-rate prepend.

    **VLX-005 fix.** ALSA ``hw:X,Y`` nodes that report a non-canonical
    native sample rate (most commonly 44 100 Hz on HDMI audio and 32 000
    Hz on some Bluetooth codecs) reject the cascade's default 16 000 Hz
    first-attempt with ``paInvalidSampleRate`` (-9997), burning one
    probe per combo before ever reaching the 48 000 Hz fallback. When
    the device exposes a sensible native rate that the default cascade
    doesn't already cover, we prepend a dedicated exclusive combo at
    that rate so the first probe stands a chance.

    Args:
        device_default_samplerate: ``DeviceEntry.default_samplerate`` —
            what PortAudio advertises as the hardware's native rate.
        device_kind: :class:`~sovyx.voice.device_enum.DeviceKind` as a
            string. Only HARDWARE devices get the prepend; session-
            manager virtuals and OS-default aliases do their own
            resampling internally and are happy with any rate in the
            default table.
        tuning: Optional :class:`VoiceTuningConfig` for the bounds
            ``cascade_native_rate_min_hz`` / ``cascade_native_rate_max_hz``.
            When ``None`` the current process tuning is read once.

    Returns:
        Either :data:`LINUX_CASCADE` unchanged (most common) or a new
        tuple with a prepended native-rate combo. Never mutates
        :data:`LINUX_CASCADE`.
    """
    if device_kind != "hardware":
        return LINUX_CASCADE

    effective_tuning = tuning or _VoiceTuning()
    min_hz = effective_tuning.cascade_native_rate_min_hz
    max_hz = effective_tuning.cascade_native_rate_max_hz

    if not (min_hz <= device_default_samplerate <= max_hz):
        # Driver reporting junk (0, 4, ultrasonic) or a rate higher than
        # what the cascade table supports. Skip prepend; default table
        # handles 16k and 48k fallbacks the usual way.
        return LINUX_CASCADE

    # Skip if the rate is already canonical — the default cascade's
    # attempts 0 (16k) and 1 (48k) already cover those paths.
    if device_default_samplerate in {16_000, 48_000}:
        return LINUX_CASCADE

    # Guard: the rate must be an allowed Combo rate (else Combo ctor
    # raises). Rates outside ALLOWED_SAMPLE_RATES are silently dropped.
    from sovyx.voice.health.contract import ALLOWED_SAMPLE_RATES

    if device_default_samplerate not in ALLOWED_SAMPLE_RATES:
        return LINUX_CASCADE

    native_combo = Combo(
        host_api="ALSA",
        sample_rate=device_default_samplerate,
        channels=1,
        sample_format="int16",
        exclusive=True,
        auto_convert=False,
        frames_per_buffer=480,
        platform_key="linux",
    )
    return (native_combo, *LINUX_CASCADE)


def _macos_cascade() -> tuple[Combo, ...]:
    """Build the macOS cascade per ADR §4.2.

    Ordering rationale:

    * Attempt 0: 48 kHz int16 — native mixer rate on every modern
      macOS build (CoreAudio mixes at 48 kHz internally since macOS 10.9).
      The system doesn't insert voice-processing on a plain HAL input
      unit, so PortAudio's default CoreAudio path is already bypass-clean.
    * Attempt 1: 48 kHz float32 — Apple-silicon Macs and AirPods in
      A2DP-sink mode default to floating-point. Same buffer size, so
      the fallback is cheap.
    * Attempt 2: 44.1 kHz int16 — legacy USB interfaces (Focusrite
      Scarlett 1st-gen, older Presonus) lock to 44.1 kHz; stream opener
      falls through to this rate before giving up.
    * Attempt 3: 16 kHz int16 — last-resort narrow-band that matches
      Bluetooth SCO/HFP's native rate. Only used when the HFP guard
      (:mod:`sovyx.voice._hfp_guard`) has cleared the endpoint — we
      never *intentionally* open HFP because the compression kills VAD.

    macOS has no APO-chain to bypass (voice-processing is opt-in via
    ``kAUVoiceIOProperty_BypassVoiceProcessing``; PortAudio never opts
    in), so the cascade is purely a sample-rate / format fallback ladder
    rather than a "try exclusive first" sequence.
    """
    mac = "darwin"
    return (
        Combo(
            host_api="CoreAudio",
            sample_rate=48_000,
            channels=1,
            sample_format="int16",
            exclusive=False,
            auto_convert=False,
            frames_per_buffer=480,
            platform_key=mac,
        ),
        Combo(
            host_api="CoreAudio",
            sample_rate=48_000,
            channels=1,
            sample_format="float32",
            exclusive=False,
            auto_convert=False,
            frames_per_buffer=480,
            platform_key=mac,
        ),
        Combo(
            host_api="CoreAudio",
            sample_rate=44_100,
            channels=1,
            sample_format="int16",
            exclusive=False,
            auto_convert=True,
            frames_per_buffer=441,
            platform_key=mac,
        ),
        Combo(
            host_api="CoreAudio",
            sample_rate=16_000,
            channels=1,
            sample_format="int16",
            exclusive=False,
            auto_convert=False,
            frames_per_buffer=480,
            platform_key=mac,
        ),
    )


MACOS_CASCADE: tuple[Combo, ...] = _macos_cascade()
"""macOS 4-attempt cascade. Native-rate CoreAudio with format fallbacks.

Ordering rationale (ADR §4.2): CoreAudio at 48 kHz int16 → 48 kHz
float32 → 44.1 kHz int16 → 16 kHz int16. No exclusive/shared
distinction on macOS — HAL input units are single-client by default —
so the ``exclusive`` flag is always ``False``. ``auto_convert`` is set
only on the 44.1 kHz entry because that rate requires a sample-rate
converter to reach the 16 kHz VAD pipeline downstream.

The 16 kHz attempt exists for HFP/SCO interop; the stream opener must
pair it with the :mod:`sovyx.voice._hfp_guard` check to avoid
silently accepting the 8 kHz Bluetooth SCO compression on headset mics.
"""


_PLATFORM_CASCADES: dict[str, tuple[Combo, ...]] = {
    "win32": WINDOWS_CASCADE,
    "linux": LINUX_CASCADE,
    "darwin": MACOS_CASCADE,
}


# ── Probe callable typing ────────────────────────────────────────────────


class ProbeCallable(Protocol):
    """Structural type for the probe function used by the cascade.

    Tests inject a fake matching this shape; production calls
    :func:`sovyx.voice.health.probe.probe`.
    """

    async def __call__(
        self,
        *,
        combo: Combo,
        mode: ProbeMode,
        device_index: int,
        hard_timeout_s: float,
    ) -> ProbeResult: ...


async def _call_probe(
    probe_fn: ProbeCallable,
    *,
    combo: Combo,
    mode: ProbeMode,
    device_index: int,
    hard_timeout_s: float,
) -> ProbeResult:
    """Invoke the probe with just the cascade's required kwargs.

    Trims the interface so tests don't have to mock every optional
    keyword of :func:`sovyx.voice.health.probe.probe` — only the four
    that the cascade explicitly drives are forwarded.
    """
    return await probe_fn(
        combo=combo,
        mode=mode,
        device_index=device_index,
        hard_timeout_s=hard_timeout_s,
    )


# ── Entry point ─────────────────────────────────────────────────────────


async def run_cascade(
    *,
    endpoint_guid: str,
    device_index: int,
    mode: ProbeMode,
    platform_key: str,
    device_friendly_name: str = "",
    device_interface_name: str = "",
    device_class: str = "",
    endpoint_fxproperties_sha: str = "",
    detected_apos: Sequence[str] = (),
    physical_device_id: str = "",
    combo_store: ComboStore | None = None,
    capture_overrides: CaptureOverrides | None = None,
    probe_fn: ProbeCallable | None = None,
    lifecycle_locks: LRULockDict[str] | None = None,
    total_budget_s: float = _DEFAULT_TOTAL_BUDGET_S,
    attempt_budget_s: float = _DEFAULT_ATTEMPT_BUDGET_S,
    voice_clarity_autofix: bool = True,
    cascade_override: Sequence[Combo] | None = None,
    clock: Callable[[], float] = time.monotonic,
    quarantine: EndpointQuarantine | None = None,
    kernel_invalidated_failover_enabled: bool | None = None,
    mixer_sanity: MixerSanitySetup | None = None,
    tuning: _VoiceTuning | None = None,
) -> CascadeResult:
    """Run the L2 cascade for ``endpoint_guid`` and return the outcome.

    Ordered attempts (any HEALTHY short-circuits):

    1. :class:`CaptureOverrides` pinned combo, if any (source ``"pinned"``).
    2. :class:`ComboStore` fast path, if any (source ``"store"``).
    3. Platform cascade (source ``"cascade"``).

    The whole call holds a per-endpoint :class:`asyncio.Lock` from
    ``lifecycle_locks`` (created automatically if not supplied). A
    module-level fallback dict is used when the caller doesn't pass one
    so standalone ``run_cascade`` calls from tests remain race-safe.

    Args:
        endpoint_guid: Stable GUID of the capture endpoint (Windows
            MMDevice id, Linux ALSA card+device, macOS CoreAudio UID).
        device_index: PortAudio device index to pass to the probe.
        mode: :attr:`ProbeMode.COLD` at boot, :attr:`ProbeMode.WARM`
            during the wizard or on first user interaction.
        platform_key: ``"win32"`` / ``"linux"`` / ``"darwin"``. Picks
            the cascade table and is echoed back to the probe for
            combo construction.
        device_friendly_name, device_interface_name, device_class,
        endpoint_fxproperties_sha, detected_apos: Forwarded to
            :meth:`ComboStore.record_winning` on a successful run so
            the store entry contains the full fingerprint for the 13
            invalidation rules.
        physical_device_id: Canonical physical-device identity
            (:attr:`~sovyx.voice.device_enum.DeviceEntry.canonical_name`)
            of the microphone behind ``endpoint_guid``. Propagated into
            the §4.4.7 quarantine entry so
            :meth:`~sovyx.voice.health._quarantine.EndpointQuarantine.is_quarantined_physical`
            can reject every host-API alias of the same wedged driver
            during fail-over selection. Empty disables physical-scope
            guarding (legacy callers).
        combo_store: Persistent fast-path store. ``None`` disables
            both fast-path lookup and the post-cascade record-winning
            side-effect.
        capture_overrides: User-pinned combos. ``None`` disables
            pinned lookup.
        probe_fn: Probe entry point. Defaults to
            :func:`sovyx.voice.health.probe.probe`; tests inject a fake
            that doesn't touch PortAudio or ONNX.
        lifecycle_locks: Pre-existing per-endpoint lock dict. Created
            at ``maxsize=64`` if omitted.
        total_budget_s: Cascade wall-clock budget. On exhaustion the
            best attempt so far is returned with ``budget_exhausted=True``.
        attempt_budget_s: Per-probe hard timeout. Matches the probe's
            ``hard_timeout_s`` so a hung driver can't stall the cascade.
        voice_clarity_autofix: When ``False`` (user disabled the APO
            bypass), skip attempts 0..4 and start at shared-mode.
        cascade_override: Override the platform cascade for this call.
            Mainly for ``--aggressive`` mode where the caller wants to
            try every combo rather than short-circuit on first HEALTHY.
        clock: Monotonic clock. Swappable for deterministic tests.
        quarantine: §4.4.7 kernel-invalidated quarantine store. When
            ``None`` the process-wide default (via
            :func:`~sovyx.voice.health._quarantine.get_default_quarantine`)
            is used if the kill-switch is on, otherwise quarantine is
            skipped. Tests pass a fresh :class:`EndpointQuarantine` to
            avoid cross-test state bleed.
        kernel_invalidated_failover_enabled: Master toggle for the
            quarantine behaviour. ``None`` resolves to
            :attr:`VoiceTuningConfig.kernel_invalidated_failover_enabled`
            at call time. When ``False``, KERNEL_INVALIDATED results
            fall through to the next cascade combo as normal — preserves
            the pre-§4.4.7 behaviour for operators who want to opt out.
        mixer_sanity: Optional L2.5 dependency bundle. When set AND
            ``platform_key == "linux"``, the cascade runs
            :func:`~sovyx.voice.health._mixer_sanity.check_and_maybe_heal`
            between the ComboStore fast-path and the platform cascade
            walk. On ``HEALED`` the mixer is corrected and the
            subsequent platform walk validates a working combo; on any
            other decision the cascade proceeds unchanged. Default
            ``None`` preserves pre-L2.5 behaviour for every existing
            caller.
    """
    # `or` treats an empty `LRULockDict` as falsy (``__len__ == 0``) and
    # silently drops the caller's shared lock — use an identity check.
    locks = lifecycle_locks if lifecycle_locks is not None else _default_locks()
    lock = locks[endpoint_guid]

    resolved_failover = (
        _VoiceTuning().kernel_invalidated_failover_enabled
        if kernel_invalidated_failover_enabled is None
        else kernel_invalidated_failover_enabled
    )
    resolved_quarantine: EndpointQuarantine | None
    if quarantine is not None:
        resolved_quarantine = quarantine
    elif resolved_failover:
        resolved_quarantine = get_default_quarantine()
    else:
        resolved_quarantine = None

    async with lock:
        return await _run_cascade_locked(
            endpoint_guid=endpoint_guid,
            device_index=device_index,
            mode=mode,
            mixer_sanity=mixer_sanity,
            tuning=tuning,
            platform_key=platform_key,
            device_friendly_name=device_friendly_name,
            device_interface_name=device_interface_name,
            device_class=device_class,
            endpoint_fxproperties_sha=endpoint_fxproperties_sha,
            detected_apos=detected_apos,
            physical_device_id=physical_device_id,
            combo_store=combo_store,
            capture_overrides=capture_overrides,
            probe_fn=probe_fn or _default_probe,
            total_budget_s=total_budget_s,
            attempt_budget_s=attempt_budget_s,
            voice_clarity_autofix=voice_clarity_autofix,
            cascade_override=cascade_override,
            clock=clock,
            quarantine=resolved_quarantine,
        )


async def _run_cascade_locked(
    *,
    endpoint_guid: str,
    device_index: int,
    mode: ProbeMode,
    platform_key: str,
    device_friendly_name: str,
    device_interface_name: str,
    device_class: str,
    endpoint_fxproperties_sha: str,
    detected_apos: Sequence[str],
    physical_device_id: str,
    combo_store: ComboStore | None,
    capture_overrides: CaptureOverrides | None,
    probe_fn: ProbeCallable,
    total_budget_s: float,
    attempt_budget_s: float,
    voice_clarity_autofix: bool,
    cascade_override: Sequence[Combo] | None,
    clock: Callable[[], float],
    quarantine: EndpointQuarantine | None,
    mixer_sanity: MixerSanitySetup | None,
    tuning: _VoiceTuning | None = None,
) -> CascadeResult:
    deadline = clock() + total_budget_s
    attempts: list[ProbeResult] = []
    attempts_count = 0

    # §4.4.7 / §4.4.8 short-circuit: a previously quarantined endpoint
    # is known to be in a state that no *boot-time* cascade can cure —
    # either kernel-invalidated (reason ``"probe_*"`` /
    # ``"watchdog_recheck"`` / ``"factory_integration"``) or APO-degraded
    # (reason ``"apo_degraded"``). Skip every attempt — the factory
    # integration layer will fail-over to the next viable
    # :class:`DeviceEntry` and the watchdog recheck loop retries after
    # the quarantine TTL. The log surfaces the live entry's ``reason``
    # token so operators can distinguish the two root causes without
    # reading two separate events.
    if quarantine is not None and quarantine.is_quarantined(endpoint_guid):
        entry = quarantine.get(endpoint_guid)
        logger.warning(
            "voice_cascade_skipped_quarantined",
            endpoint=endpoint_guid,
            friendly_name=device_friendly_name,
            reason=entry.reason if entry is not None else "unknown",
        )
        return _make_result(
            endpoint_guid=endpoint_guid,
            winning_combo=None,
            winning_probe=None,
            attempts=attempts,
            attempts_count=attempts_count,
            budget_exhausted=False,
            source="quarantined",
        )

    # 1. Pinned override.
    pinned = _lookup_override(capture_overrides, endpoint_guid, platform_key)
    if pinned is not None:
        logger.info(
            "voice_cascade_pinned_lookup",
            endpoint=endpoint_guid,
            combo=_combo_tag(pinned),
        )
        _log_probe_call(
            endpoint_guid=endpoint_guid,
            attempt=0,
            device_index=device_index,
            combo=pinned,
            mode=mode,
            attempt_budget_s=attempt_budget_s,
        )
        result = await _try_combo(
            probe_fn=probe_fn,
            combo=pinned,
            mode=mode,
            device_index=device_index,
            attempt_budget_s=attempt_budget_s,
        )
        _log_probe_result(
            endpoint_guid=endpoint_guid,
            attempt=0,
            device_index=device_index,
            combo=pinned,
            result=result,
        )
        attempts.append(result)
        attempts_count += 1
        record_cascade_attempt(
            platform=platform_key,
            host_api=pinned.host_api,
            success=result.diagnosis is Diagnosis.HEALTHY,
            source="pinned",
        )
        if result.diagnosis is Diagnosis.HEALTHY:
            # T1 — uniform winner telemetry across pinned/store/cascade.
            logger.info(
                "voice_cascade_winner_selected",
                endpoint=endpoint_guid,
                source="pinned",
                attempts=1,
                combo_host_api=pinned.host_api,
                combo_sample_rate=pinned.sample_rate,
                combo_channels=pinned.channels,
                combo_exclusive=pinned.exclusive,
                combo_auto_convert=pinned.auto_convert,
                device_index=device_index,
                device_friendly_name=device_friendly_name,
            )
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=pinned,
                winning_probe=result,
                attempts=attempts,
                attempts_count=0,
                budget_exhausted=False,
                source="pinned",
            )
        # §4.4.7 — kernel-invalidated state. Every host API will fail
        # equally; trying the ComboStore or the cascade loop just wastes
        # the user's time. Quarantine + short-circuit.
        if result.diagnosis is Diagnosis.KERNEL_INVALIDATED and _quarantine_endpoint(
            quarantine=quarantine,
            endpoint_guid=endpoint_guid,
            device_friendly_name=device_friendly_name,
            device_interface_name=device_interface_name,
            host_api=pinned.host_api,
            platform_key=platform_key,
            reason="probe_pinned",
            physical_device_id=physical_device_id,
        ):
            logger.warning(
                "voice_cascade_kernel_invalidated",
                endpoint=endpoint_guid,
                friendly_name=device_friendly_name,
                host_api=pinned.host_api,
                source="pinned",
            )
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=None,
                winning_probe=None,
                attempts=attempts,
                attempts_count=attempts_count,
                budget_exhausted=False,
                source="quarantined",
            )
        logger.warning(
            "voice_cascade_pinned_failed",
            endpoint=endpoint_guid,
            host_api=pinned.host_api,
            combo=_combo_tag(pinned),
            diagnosis=str(result.diagnosis),
        )

    # 2. ComboStore fast path.
    store_combo = _lookup_store(combo_store, endpoint_guid)
    if store_combo is None:
        record_combo_store_hit(
            endpoint_class=device_class or "unknown",
            result="miss",
        )
    if store_combo is not None:
        if clock() >= deadline:
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=None,
                winning_probe=None,
                attempts=attempts,
                attempts_count=attempts_count,
                budget_exhausted=True,
                source="none",
            )
        logger.info(
            "voice_cascade_store_lookup",
            endpoint=endpoint_guid,
            combo=_combo_tag(store_combo),
        )
        _log_probe_call(
            endpoint_guid=endpoint_guid,
            attempt=0,
            device_index=device_index,
            combo=store_combo,
            mode=mode,
            attempt_budget_s=attempt_budget_s,
        )
        result = await _try_combo(
            probe_fn=probe_fn,
            combo=store_combo,
            mode=mode,
            device_index=device_index,
            attempt_budget_s=attempt_budget_s,
        )
        _log_probe_result(
            endpoint_guid=endpoint_guid,
            attempt=0,
            device_index=device_index,
            combo=store_combo,
            result=result,
        )
        attempts.append(result)
        success = result.diagnosis is Diagnosis.HEALTHY
        record_cascade_attempt(
            platform=platform_key,
            host_api=store_combo.host_api,
            success=success,
            source="store",
        )
        record_combo_store_hit(
            endpoint_class=device_class or "unknown",
            result="hit" if success else "needs_revalidation",
        )
        if success:
            # Fast-path hit: do NOT re-record (combo already in store).
            # T1 — uniform winner telemetry across pinned/store/cascade.
            logger.info(
                "voice_cascade_winner_selected",
                endpoint=endpoint_guid,
                source="store",
                attempts=1,
                combo_host_api=store_combo.host_api,
                combo_sample_rate=store_combo.sample_rate,
                combo_channels=store_combo.channels,
                combo_exclusive=store_combo.exclusive,
                combo_auto_convert=store_combo.auto_convert,
                device_index=device_index,
                device_friendly_name=device_friendly_name,
            )
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=store_combo,
                winning_probe=result,
                attempts=attempts,
                attempts_count=0,
                budget_exhausted=False,
                source="store",
            )
        # §4.4.7 — kernel-invalidated state observed on the fast path.
        # Invalidate the (now misleading) store entry too, then quarantine
        # the endpoint and short-circuit the rest of the cascade.
        if result.diagnosis is Diagnosis.KERNEL_INVALIDATED and _quarantine_endpoint(
            quarantine=quarantine,
            endpoint_guid=endpoint_guid,
            device_friendly_name=device_friendly_name,
            device_interface_name=device_interface_name,
            host_api=store_combo.host_api,
            platform_key=platform_key,
            reason="probe_store",
            physical_device_id=physical_device_id,
        ):
            if combo_store is not None:
                combo_store.invalidate(endpoint_guid, reason="kernel_invalidated")
            logger.warning(
                "voice_cascade_kernel_invalidated",
                endpoint=endpoint_guid,
                friendly_name=device_friendly_name,
                host_api=store_combo.host_api,
                source="store",
            )
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=None,
                winning_probe=None,
                attempts=attempts,
                attempts_count=attempts_count,
                budget_exhausted=False,
                source="quarantined",
            )
        # Invalidate the stale store entry so the next boot runs the
        # full cascade fresh rather than re-probing the known-bad combo.
        # The metric is emitted inside ``ComboStore.invalidate`` — single
        # source of truth for every invalidation path.
        if combo_store is not None:
            combo_store.invalidate(endpoint_guid, reason="fast_path_probe_failed")
            logger.warning(
                "voice_cascade_store_invalidated",
                endpoint=endpoint_guid,
                host_api=store_combo.host_api,
                combo=_combo_tag(store_combo),
                diagnosis=str(result.diagnosis),
            )

    # 2.5. L2.5 mixer sanity — runs only when the caller opts in via
    # ``mixer_sanity`` AND we are on Linux. Fire-and-forget from the
    # cascade's perspective: on HEALED the ALSA mixer is corrected and
    # the subsequent platform walk succeeds against the healed state;
    # on any other decision the cascade proceeds unchanged. L2.5 does
    # NOT pick a PortAudio combo (that's the platform cascade's
    # responsibility) — it only repairs the mixer state so the
    # platform walk has a chance. See ADR-voice-mixer-sanity-l2.5-
    # bidirectional + V2 Master Plan Part C.1.
    #
    # The ``try/except BaseException`` here is defence-in-depth:
    # ``_run_mixer_sanity`` already catches ``check_and_maybe_heal``
    # errors internally, but a failure in its setup code (e.g.,
    # ``CandidateEndpoint`` construction with malformed inputs) or a
    # misbehaving DI callable injected by the user would otherwise
    # abort the cascade — defeating the whole point of keeping L2.5
    # an opt-in, side-channel layer.
    if mixer_sanity is not None and platform_key == "linux":
        try:
            await _run_mixer_sanity(
                mixer_sanity=mixer_sanity,
                endpoint_guid=endpoint_guid,
                device_index=device_index,
                device_friendly_name=device_friendly_name,
                combo_store=combo_store,
                capture_overrides=capture_overrides,
                tuning=tuning,
            )
        except asyncio.CancelledError:
            # Paranoid-QA CRITICAL #1: cancellation must propagate —
            # the cascade loop may want to short-circuit.
            raise
        except Exception as exc:  # noqa: BLE001 — cascade must continue on non-cancel error
            logger.warning(
                "voice_cascade_mixer_sanity_helper_raised",
                endpoint=endpoint_guid,
                error_type=type(exc).__name__,
                detail=str(exc)[:200],
            )

    # 3. Platform cascade.
    cascade = (
        tuple(cascade_override)
        if cascade_override is not None
        else _platform_cascade(platform_key)
    )
    start_idx = 0 if voice_clarity_autofix else _VOICE_CLARITY_AUTOFIX_FIRST_ATTEMPT
    if platform_key != "win32":
        # voice_clarity_autofix is Windows-only; on Linux/macOS start at 0.
        start_idx = 0

    for idx, combo in enumerate(cascade):
        if idx < start_idx:
            continue
        if clock() >= deadline:
            logger.warning(
                "voice_cascade_budget_exhausted",
                endpoint=endpoint_guid,
                attempts_run=attempts_count,
                total_budget_s=total_budget_s,
            )
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=None,
                winning_probe=None,
                attempts=attempts,
                attempts_count=attempts_count,
                budget_exhausted=True,
                source="none",
            )
        attempts_count += 1
        logger.info(
            "voice_cascade_attempt",
            endpoint=endpoint_guid,
            attempt=idx,
            combo=_combo_tag(combo),
        )
        _log_probe_call(
            endpoint_guid=endpoint_guid,
            attempt=idx,
            device_index=device_index,
            combo=combo,
            mode=mode,
            attempt_budget_s=attempt_budget_s,
        )
        result = await _try_combo(
            probe_fn=probe_fn,
            combo=combo,
            mode=mode,
            device_index=device_index,
            attempt_budget_s=attempt_budget_s,
        )
        _log_probe_result(
            endpoint_guid=endpoint_guid,
            attempt=idx,
            device_index=device_index,
            combo=combo,
            result=result,
        )
        attempts.append(result)
        record_cascade_attempt(
            platform=platform_key,
            host_api=combo.host_api,
            success=result.diagnosis is Diagnosis.HEALTHY,
            source="cascade",
        )
        # §4.4.7 — kernel-invalidated state. Every remaining host API in
        # the cascade table will fail identically because the failure is
        # at IAudioClient::Initialize, upstream of the host-API layer.
        # Quarantine + break the loop instead of burning the per-attempt
        # budget on combos we already know will fail.
        if result.diagnosis is Diagnosis.KERNEL_INVALIDATED and _quarantine_endpoint(
            quarantine=quarantine,
            endpoint_guid=endpoint_guid,
            device_friendly_name=device_friendly_name,
            device_interface_name=device_interface_name,
            host_api=combo.host_api,
            platform_key=platform_key,
            reason="probe_cascade",
            physical_device_id=physical_device_id,
        ):
            logger.warning(
                "voice_cascade_kernel_invalidated",
                endpoint=endpoint_guid,
                friendly_name=device_friendly_name,
                host_api=combo.host_api,
                source="cascade",
                attempt=idx,
            )
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=None,
                winning_probe=None,
                attempts=attempts,
                attempts_count=attempts_count,
                budget_exhausted=False,
                source="quarantined",
            )
        if result.diagnosis is Diagnosis.HEALTHY:
            _record_winner(
                combo_store=combo_store,
                endpoint_guid=endpoint_guid,
                device_friendly_name=device_friendly_name,
                device_interface_name=device_interface_name,
                device_class=device_class,
                endpoint_fxproperties_sha=endpoint_fxproperties_sha,
                detected_apos=detected_apos,
                combo=combo,
                probe=result,
                cascade_attempts_before_success=attempts_count,
            )
            # T1 — DoD #3 requires this event to be present in the log
            # after a successful cascade run. Future T3 will extend it
            # with ``winning_candidate`` / ``candidate_source`` fields
            # once the candidate-set refactor lands.
            logger.info(
                "voice_cascade_winner_selected",
                endpoint=endpoint_guid,
                source="cascade",
                attempts=attempts_count,
                combo_host_api=combo.host_api,
                combo_sample_rate=combo.sample_rate,
                combo_channels=combo.channels,
                combo_exclusive=combo.exclusive,
                combo_auto_convert=combo.auto_convert,
                device_index=device_index,
                device_friendly_name=device_friendly_name,
            )
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=combo,
                winning_probe=result,
                attempts=attempts,
                attempts_count=attempts_count,
                budget_exhausted=False,
                source="cascade",
            )

    logger.error(
        "voice_cascade_exhausted",
        endpoint=endpoint_guid,
        attempts=attempts_count,
    )
    return _make_result(
        endpoint_guid=endpoint_guid,
        winning_combo=None,
        winning_probe=None,
        attempts=attempts,
        attempts_count=attempts_count,
        budget_exhausted=False,
        source="none",
    )


# ── helpers ─────────────────────────────────────────────────────────────


_DEFAULT_LOCKS: LRULockDict[str] | None = None


async def run_cascade_for_candidates(
    *,
    candidates: Sequence[CandidateEndpoint],
    mode: ProbeMode,
    platform_key: str,
    combo_store: ComboStore | None = None,
    capture_overrides: CaptureOverrides | None = None,
    probe_fn: ProbeCallable | None = None,
    lifecycle_locks: LRULockDict[str] | None = None,
    total_budget_s: float = _DEFAULT_TOTAL_BUDGET_S,
    attempt_budget_s: float = _DEFAULT_ATTEMPT_BUDGET_S,
    voice_clarity_autofix: bool = True,
    clock: Callable[[], float] = time.monotonic,
    quarantine: EndpointQuarantine | None = None,
    kernel_invalidated_failover_enabled: bool | None = None,
    mixer_sanity: MixerSanitySetup | None = None,
    tuning: _VoiceTuning | None = None,
) -> CascadeResult:
    """Run the cascade against an ordered set of capture candidates.

    This is the candidate-set entry point introduced by the
    ``voice-linux-cascade-root-fix`` mission (VLX-002). It iterates the
    caller-supplied :class:`~sovyx.voice.health.contract.CandidateEndpoint`
    list in order, delegating each to :func:`run_cascade` with the
    candidate's per-endpoint identity. The first healthy winner wins.

    Division of labour vs. :func:`run_cascade`:

    * :func:`run_cascade` — cross-combo, single endpoint. Pinned →
      ComboStore fast-path → platform cascade table walk.
    * :func:`run_cascade_for_candidates` — cross-endpoint, delegates to
      :func:`run_cascade` per candidate. Source of truth for the
      session-manager-escape path at boot time on Linux (VLX-002).

    The total wall-clock ``total_budget_s`` is shared across all
    candidates. Each :func:`run_cascade` call gets the remaining budget,
    so the last candidate may get a shorter window than the first. This
    matches the pre-refactor behaviour (one endpoint, one budget) when
    called with ``len(candidates) == 1``.

    Args:
        candidates: Ordered list from
            :func:`~sovyx.voice.health._candidate_builder.build_capture_candidates`.
            Must be non-empty; the first candidate is the user-preferred
            one (``CandidateSource.USER_PREFERRED``).
        mode: :attr:`ProbeMode.COLD` at boot, :attr:`ProbeMode.WARM`
            during the wizard.
        platform_key: ``"win32"`` / ``"linux"`` / ``"darwin"``.
        combo_store: Persistent fast-path store — forwarded verbatim to
            each :func:`run_cascade` invocation. Each candidate hits the
            store under its own ``endpoint_guid``, so a stored combo for
            ``pipewire`` is still consulted when the user-preferred
            hardware candidate's own fast-path is stale.
        capture_overrides: User-pinned combos — forwarded verbatim.
        probe_fn: Probe entry point. Defaults to
            :func:`~sovyx.voice.health.probe.probe`.
        lifecycle_locks: Per-endpoint lock dict. Each candidate gets its
            own lock; parallel invocations of this function against
            disjoint candidate sets do not serialize.
        total_budget_s: Shared wall-clock budget across all candidates.
            On exhaustion the function returns ``budget_exhausted=True``
            with attempts from candidates tried so far.
        attempt_budget_s: Per-probe hard timeout.
        voice_clarity_autofix: Forwarded to each :func:`run_cascade` call.
        clock: Monotonic clock. Swappable for deterministic tests.
        quarantine: Shared quarantine store. All candidates check the
            same instance — a quarantined ``pipewire`` endpoint does
            not re-probe even if ``hw:1,0`` just finished quarantining.
        kernel_invalidated_failover_enabled: Master toggle for the
            §4.4.7 quarantine behaviour.
        mixer_sanity: Optional L2.5 dependency bundle. When set AND
            ``platform_key == "linux"``, L2.5 runs ONCE for the
            whole candidate-set pass (using the first candidate's
            identity) before the per-candidate cascade loop. The
            inner :func:`run_cascade` invocations receive
            ``mixer_sanity=None`` so healing is not re-attempted
            for every candidate. Default ``None`` preserves pre-L2.5
            behaviour.

    Returns:
        :class:`CascadeResult` with:

        * ``winning_candidate`` populated when any candidate produced a
          healthy combo.
        * ``endpoint_guid`` set to the winning candidate's guid, or the
          first candidate's guid on exhaustion (log correlation).
        * ``attempts`` containing the concatenation of every attempt
          across all tried candidates, in iteration order.

    Raises:
        ValueError: ``candidates`` is empty.
    """
    if not candidates:
        msg = "candidates must be non-empty (build_capture_candidates contract)"
        raise ValueError(msg)

    deadline = clock() + total_budget_s
    aggregated_attempts: list[ProbeResult] = []
    total_attempts_count = 0
    last_result: CascadeResult | None = None

    logger.info(
        "voice_cascade_candidate_set_started",
        platform=platform_key,
        candidate_count=len(candidates),
        candidate_kinds=[str(c.kind) for c in candidates],
        candidate_sources=[str(c.source) for c in candidates],
    )

    # 2.5 — L2.5 mixer sanity runs ONCE per candidate-set pass (the ALSA
    # mixer is system-wide state; healing per-candidate would repeat work).
    # Uses the first candidate's identity for telemetry / endpoint_guid
    # (by candidate-builder contract that's the user-preferred one). We
    # pass mixer_sanity=None to the inner run_cascade calls so L2.5 does
    # NOT fire again under each per-endpoint lock — the healing already
    # happened (or was skipped) at this layer.
    if mixer_sanity is not None and platform_key == "linux":
        try:
            await _run_mixer_sanity(
                mixer_sanity=mixer_sanity,
                endpoint_guid=candidates[0].endpoint_guid,
                device_index=candidates[0].device_index,
                device_friendly_name=candidates[0].friendly_name,
                combo_store=combo_store,
                capture_overrides=capture_overrides,
                tuning=tuning,
            )
        except asyncio.CancelledError:
            # Paranoid-QA CRITICAL #1: cancellation propagates.
            raise
        except Exception as exc:  # noqa: BLE001 — cascade must continue
            logger.warning(
                "voice_cascade_candidate_set_mixer_sanity_raised",
                error_type=type(exc).__name__,
                detail=str(exc)[:200],
            )

    # T4 — defensive invariant: dedup by (device_index, host_api_name)
    # must already hold (build_capture_candidates guarantees this), but
    # an ill-behaved injected builder in tests or a future refactor could
    # re-introduce collisions. Log-warn + continue rather than raise; the
    # cascade loop is already O(n×m) and probe idempotency absorbs dupes.
    seen_candidate_keys: set[tuple[int, str]] = set()

    for candidate_idx, candidate in enumerate(candidates):
        remaining = max(0.0, deadline - clock())
        if remaining <= 0.0:
            logger.warning(
                "voice_cascade_candidate_set_budget_exhausted",
                tried=candidate_idx,
                remaining_candidates=len(candidates) - candidate_idx,
            )
            break

        dedup_key = (candidate.device_index, candidate.host_api_name)
        if dedup_key in seen_candidate_keys:
            logger.warning(
                "voice_cascade_candidate_duplicate",
                candidate_rank=candidate.preference_rank,
                device_index=candidate.device_index,
                host_api=candidate.host_api_name,
            )
        seen_candidate_keys.add(dedup_key)

        logger.info(
            "voice_cascade_candidate_started",
            candidate_rank=candidate.preference_rank,
            candidate_source=str(candidate.source),
            candidate_kind=str(candidate.kind),
            device_index=candidate.device_index,
            host_api=candidate.host_api_name,
            friendly_name=candidate.friendly_name,
            endpoint_guid=candidate.endpoint_guid,
            remaining_budget_s=remaining,
        )

        # T5 — per-candidate native-rate cascade. Only prepends when
        # the candidate is HARDWARE and reports a non-canonical rate
        # that the default Linux cascade would waste attempts on.
        per_candidate_cascade: Sequence[Combo] | None = None
        if platform_key == "linux":
            tailored = build_linux_cascade_for_device(
                candidate.default_samplerate,
                str(candidate.kind),
            )
            if tailored is not LINUX_CASCADE:
                per_candidate_cascade = tailored
                logger.info(
                    "voice_cascade_native_rate_prepended",
                    candidate_rank=candidate.preference_rank,
                    device_index=candidate.device_index,
                    native_rate=candidate.default_samplerate,
                )

        per_candidate_result = await run_cascade(
            endpoint_guid=candidate.endpoint_guid,
            device_index=candidate.device_index,
            mode=mode,
            platform_key=platform_key,
            device_friendly_name=candidate.friendly_name,
            device_interface_name=candidate.canonical_name,
            physical_device_id=candidate.canonical_name,
            combo_store=combo_store,
            capture_overrides=capture_overrides,
            probe_fn=probe_fn,
            lifecycle_locks=lifecycle_locks,
            total_budget_s=remaining,
            attempt_budget_s=attempt_budget_s,
            voice_clarity_autofix=voice_clarity_autofix,
            cascade_override=per_candidate_cascade,
            clock=clock,
            quarantine=quarantine,
            kernel_invalidated_failover_enabled=kernel_invalidated_failover_enabled,
        )
        aggregated_attempts.extend(per_candidate_result.attempts)
        total_attempts_count += per_candidate_result.attempts_count
        last_result = per_candidate_result

        if per_candidate_result.winning_combo is not None:
            logger.info(
                "voice_cascade_candidate_set_resolved",
                winning_rank=candidate.preference_rank,
                winning_source=str(candidate.source),
                winning_kind=str(candidate.kind),
                device_index=candidate.device_index,
                host_api=candidate.host_api_name,
                endpoint_guid=candidate.endpoint_guid,
                tried=candidate_idx + 1,
                total=len(candidates),
            )
            return CascadeResult(
                endpoint_guid=candidate.endpoint_guid,
                winning_combo=per_candidate_result.winning_combo,
                winning_probe=per_candidate_result.winning_probe,
                attempts=tuple(aggregated_attempts),
                attempts_count=total_attempts_count,
                budget_exhausted=False,
                source=per_candidate_result.source,
                winning_candidate=candidate,
            )

        # Non-healthy candidate — advance to the next one unless budget
        # is already exhausted (we'll break on the next iteration's
        # ``remaining <= 0`` guard).
        logger.info(
            "voice_cascade_candidate_failed",
            candidate_rank=candidate.preference_rank,
            candidate_source=str(candidate.source),
            device_index=candidate.device_index,
            source_label=per_candidate_result.source,
            budget_exhausted=per_candidate_result.budget_exhausted,
        )

    # Exhausted — return aggregated result keyed on the first candidate
    # so log correlation is stable.
    logger.error(
        "voice_cascade_candidate_set_exhausted",
        candidate_count=len(candidates),
        attempts_total=total_attempts_count,
    )
    first = candidates[0]
    return CascadeResult(
        endpoint_guid=first.endpoint_guid,
        winning_combo=None,
        winning_probe=None,
        attempts=tuple(aggregated_attempts),
        attempts_count=total_attempts_count,
        budget_exhausted=last_result.budget_exhausted if last_result else False,
        source="none",
        winning_candidate=None,
    )


def _default_locks() -> LRULockDict[str]:
    """Lazy singleton for callers that didn't pass a lock dict.

    Created on first use so importing this module in environments that
    don't need cascade locking (tests, doctor CLI sub-commands) doesn't
    allocate anything.
    """
    global _DEFAULT_LOCKS  # noqa: PLW0603 — lazy singleton, not user-mutable state
    if _DEFAULT_LOCKS is None:
        _DEFAULT_LOCKS = LRULockDict(maxsize=_LIFECYCLE_LOCK_MAX)
    return _DEFAULT_LOCKS


def _platform_cascade(platform_key: str) -> tuple[Combo, ...]:
    return _PLATFORM_CASCADES.get(platform_key, ())


def _quarantine_endpoint(
    *,
    quarantine: EndpointQuarantine | None,
    endpoint_guid: str,
    device_friendly_name: str,
    device_interface_name: str,
    host_api: str,
    platform_key: str,
    reason: str,
    physical_device_id: str = "",
) -> bool:
    """Add ``endpoint_guid`` to the §4.4.7 quarantine and emit the L4 metric.

    Returns ``True`` when the endpoint was registered (caller short-circuits
    the cascade and returns ``source="quarantined"``); ``False`` when no
    quarantine store is configured (operator opted out via
    :attr:`VoiceTuningConfig.kernel_invalidated_failover_enabled` ``=False``).

    ``physical_device_id`` is the caller's best canonical-name identity
    for the underlying microphone. When supplied, it is stored on the
    quarantine entry so
    :func:`~sovyx.voice.health._factory_integration.select_alternative_endpoint`
    can reject every host-API alias of the same wedged driver during
    fail-over, preventing the Razer-class kernel-reset failure mode.

    Centralising this lets the cascade's three probe sites — pinned override,
    ComboStore fast path, and platform cascade loop — all register quarantine
    entries through one consistent path so the metric / log surface stays
    uniform.
    """
    if quarantine is None:
        return False
    quarantine.add(
        endpoint_guid=endpoint_guid,
        device_friendly_name=device_friendly_name,
        device_interface_name=device_interface_name,
        host_api=host_api or "unknown",
        reason=reason,
        physical_device_id=physical_device_id,
    )
    record_kernel_invalidated_event(
        platform=platform_key,
        host_api=host_api or "unknown",
        action="quarantine",
    )
    return True


async def _run_mixer_sanity(
    *,
    mixer_sanity: MixerSanitySetup,
    endpoint_guid: str,
    device_index: int,
    device_friendly_name: str,
    combo_store: ComboStore | None,
    capture_overrides: CaptureOverrides | None,
    tuning: _VoiceTuning | None = None,
) -> None:
    """Invoke L2.5 ``check_and_maybe_heal`` for this endpoint.

    Fire-and-forget from the cascade's perspective: the outcome is
    logged + telemetry'd internally (via the ``_mixer_sanity`` module),
    but we return no value — the cascade continues with its platform
    walk regardless. L2.5 heals the ALSA mixer state; the platform
    cascade still picks the PortAudio combo.

    Builds a minimal :class:`CandidateEndpoint` on the fly so the
    orchestrator has an endpoint identity to key telemetry on. Full
    candidate metadata (source, preference_rank, canonical_name)
    isn't needed for L2.5 — it operates on mixer state, not endpoint
    enumeration.

    Any unexpected error inside L2.5 is swallowed (already logged by
    the orchestrator) so a misbehaving KB or probe cannot abort the
    cascade — invariant P6 applied at the integration boundary.
    """
    from sovyx.voice.device_enum import (
        DeviceKind,  # noqa: PLC0415 — lazy; only Linux path needs it
    )
    from sovyx.voice.health._mixer_sanity import (
        check_and_maybe_heal,  # noqa: PLC0415 — lazy to avoid Linux import cost on Windows cold-start
    )

    endpoint = CandidateEndpoint(
        device_index=device_index,
        host_api_name="ALSA",
        kind=DeviceKind.HARDWARE,
        canonical_name=device_friendly_name or f"endpoint-{endpoint_guid}",
        friendly_name=device_friendly_name or f"endpoint-{endpoint_guid}",
        source=CandidateSource.USER_PREFERRED,
        preference_rank=0,
        endpoint_guid=endpoint_guid,
    )
    # Paranoid-QA CRITICAL #8: use the caller's tuning when
    # provided — discarding it here would silently ignore every
    # SOVYX_TUNING__VOICE__LINUX_MIXER_SANITY_* env override and
    # violate anti-pattern #17 ("Hardcoded tuning constants").
    effective_tuning = tuning if tuning is not None else _VoiceTuning()
    try:
        result = await check_and_maybe_heal(
            endpoint,
            mixer_sanity.hw,
            kb_lookup=mixer_sanity.kb_lookup,
            role_resolver=mixer_sanity.role_resolver,
            validation_probe_fn=mixer_sanity.validation_probe_fn,
            tuning=effective_tuning,
            mixer_probe_fn=mixer_sanity.mixer_probe_fn,
            mixer_apply_fn=mixer_sanity.mixer_apply_fn,
            mixer_restore_fn=mixer_sanity.mixer_restore_fn,
            persist_fn=mixer_sanity.persist_fn,
            telemetry=mixer_sanity.telemetry,
            combo_store=combo_store,
            capture_overrides=capture_overrides,
        )
    except asyncio.CancelledError:
        # Paranoid-QA CRITICAL #1: cancel propagates past the cascade
        # integration layer — the cascade itself decides whether to
        # swallow or re-raise.
        raise
    except Exception as exc:  # noqa: BLE001 — Exception-only post-QA
        logger.warning(
            "voice_cascade_mixer_sanity_unexpected",
            endpoint=endpoint_guid,
            error_type=type(exc).__name__,
            detail=str(exc)[:200],
        )
        return
    logger.info(
        "voice_cascade_mixer_sanity_outcome",
        endpoint=endpoint_guid,
        decision=result.decision.value,
        matched_profile=result.matched_kb_profile,
        score=round(result.kb_match_score, 3),
        regime=result.regime,
        apply_duration_ms=result.apply_duration_ms,
        validation_passed=result.validation_passed,
        error=result.error,
    )


def _lookup_override(
    overrides: CaptureOverrides | None,
    endpoint_guid: str,
    platform_key: str,
) -> Combo | None:
    if overrides is None:
        return None
    try:
        combo = overrides.get(endpoint_guid)
    except Exception:  # noqa: BLE001 — cascade must fall through on any store-side failure (ADR I4)
        logger.warning(
            "voice_cascade_pinned_lookup_failed",
            endpoint=endpoint_guid,
            exc_info=True,
        )
        return None
    if combo is None:
        return None
    # Sanity: reject an override that isn't valid for this platform.
    if combo.platform_key and combo.platform_key != platform_key:
        logger.warning(
            "voice_cascade_pinned_platform_mismatch",
            endpoint=endpoint_guid,
            combo_platform=combo.platform_key,
            runtime_platform=platform_key,
        )
        return None
    return combo


def _lookup_store(
    combo_store: ComboStore | None,
    endpoint_guid: str,
) -> Combo | None:
    if combo_store is None:
        return None
    try:
        entry = combo_store.get(endpoint_guid)
    except Exception:  # noqa: BLE001 — cascade must fall through on any store-side failure (ADR I4)
        logger.warning(
            "voice_cascade_store_lookup_failed",
            endpoint=endpoint_guid,
            exc_info=True,
        )
        return None
    if entry is None:
        return None
    if combo_store.needs_revalidation(endpoint_guid):
        logger.info(
            "voice_cascade_store_needs_revalidation",
            endpoint=endpoint_guid,
        )
    return entry.winning_combo


async def _try_combo(
    *,
    probe_fn: ProbeCallable,
    combo: Combo,
    mode: ProbeMode,
    device_index: int,
    attempt_budget_s: float,
) -> ProbeResult:
    """Invoke the probe and convert unexpected exceptions into DRIVER_ERROR results.

    The probe already classifies all known PortAudio failures into the
    :class:`Diagnosis` enum. This wrapper guards against a probe-side
    bug / test misconfiguration turning into a cascade abort — any
    exception becomes a synthetic DRIVER_ERROR so the cascade can
    still fall through.
    """
    try:
        return await _call_probe(
            probe_fn,
            combo=combo,
            mode=mode,
            device_index=device_index,
            hard_timeout_s=attempt_budget_s,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Belt-and-braces: after v0.20.2 Phase 1, the probe classifies
        # stream.start() failures internally, so this path should only
        # fire for genuine probe-side bugs (numpy errors in analysis,
        # test misconfiguration). Still, running the classifier on the
        # raised exception recovers the correct Diagnosis when a future
        # probe-side bug re-introduces a leak (e.g. a kernel-invalidated
        # error escaping a new analysis phase), rather than silently
        # coarsening into DRIVER_ERROR.
        #
        # Gate the classifier on OSError (PortAudio surfaces failures as
        # ``sd.PortAudioError(OSError)``) so an unrelated coding-bug
        # ``TypeError("... format ...")`` or ``AttributeError`` whose
        # message accidentally contains a keyword like "format" / "in use"
        # / "access" cannot be misclassified as a structured Diagnosis.
        # Non-OSError stays DRIVER_ERROR — the original cascade contract.
        if isinstance(exc, OSError):
            diagnosis = _classify_open_error(exc)
        else:
            diagnosis = Diagnosis.DRIVER_ERROR
        logger.error(
            "voice_cascade_probe_raised",
            host_api=combo.host_api,
            combo=_combo_tag(combo),
            diagnosis=str(diagnosis),
            error=repr(exc),
            exc_info=True,
        )
        synthetic = ProbeResult(
            diagnosis=diagnosis,
            mode=mode,
            combo=combo,
            vad_max_prob=None,
            vad_mean_prob=None,
            rms_db=float("-inf"),
            callbacks_fired=0,
            duration_ms=0,
            error=f"probe raised: {exc!r}",
        )
        # Also emit the probe-result telemetry so synthetic results
        # appear in the same dashboards as first-class probe outcomes.
        record_probe_result(synthetic)
        return synthetic


def _record_winner(
    *,
    combo_store: ComboStore | None,
    endpoint_guid: str,
    device_friendly_name: str,
    device_interface_name: str,
    device_class: str,
    endpoint_fxproperties_sha: str,
    detected_apos: Sequence[str],
    combo: Combo,
    probe: ProbeResult,
    cascade_attempts_before_success: int,
) -> None:
    if combo_store is None:
        return
    try:
        combo_store.record_winning(
            endpoint_guid,
            device_friendly_name=device_friendly_name,
            device_interface_name=device_interface_name,
            device_class=device_class,
            endpoint_fxproperties_sha=endpoint_fxproperties_sha,
            combo=combo,
            probe=probe,
            detected_apos=detected_apos,
            cascade_attempts_before_success=cascade_attempts_before_success,
        )
    except Exception:  # noqa: BLE001 — persisting a win is advisory; don't crash the cascade
        logger.warning(
            "voice_cascade_record_winning_failed",
            endpoint=endpoint_guid,
            exc_info=True,
        )


def _make_result(
    *,
    endpoint_guid: str,
    winning_combo: Combo | None,
    winning_probe: ProbeResult | None,
    attempts: list[ProbeResult],
    attempts_count: int,
    budget_exhausted: bool,
    source: str,
) -> CascadeResult:
    return CascadeResult(
        endpoint_guid=endpoint_guid,
        winning_combo=winning_combo,
        winning_probe=winning_probe,
        attempts=tuple(attempts),
        attempts_count=attempts_count,
        budget_exhausted=budget_exhausted,
        source=source,
    )


def _combo_tag(combo: Combo) -> str:
    """Compact string representation for structured log fields."""
    excl = "excl" if combo.exclusive else "shared"
    return (
        f"{combo.host_api}/{combo.sample_rate}Hz/{combo.channels}ch/"
        f"{combo.sample_format}/{excl}/{combo.frames_per_buffer}f"
    )


_LOG_DETAIL_MAX_CHARS = 512
"""Cap on ``error_detail`` truncation in cascade/probe events (T1).

Matches the cap used by ``anomaly.latency_spike`` so structured fields
stay within OTLP attribute-size limits without surprising operators.
"""


def _truncate_detail(detail: str | None) -> str:
    """Clamp ``detail`` for structured log fields; safe for ``None``."""
    if not detail:
        return ""
    if len(detail) <= _LOG_DETAIL_MAX_CHARS:
        return detail
    return detail[: _LOG_DETAIL_MAX_CHARS - 1] + "…"


def _log_probe_call(
    *,
    endpoint_guid: str,
    attempt: int,
    device_index: int,
    combo: Combo,
    mode: ProbeMode,
    attempt_budget_s: float,
) -> None:
    """Emit ``voice_cascade_probe_call`` before every probe invocation (T1).

    Uniform across cascade/pinned/store paths so post-mortem log greps
    see the same structured key set regardless of which source fed the
    probe call.
    """
    logger.info(
        "voice_cascade_probe_call",
        endpoint=endpoint_guid,
        attempt=attempt,
        device_index=device_index,
        combo_host_api=combo.host_api,
        combo_sample_rate=combo.sample_rate,
        combo_channels=combo.channels,
        combo_sample_format=combo.sample_format,
        combo_exclusive=combo.exclusive,
        combo_auto_convert=combo.auto_convert,
        combo_frames_per_buffer=combo.frames_per_buffer,
        mode=str(mode),
        attempt_budget_s=attempt_budget_s,
    )


def _log_probe_result(
    *,
    endpoint_guid: str,
    attempt: int,
    device_index: int,
    combo: Combo,
    result: ProbeResult,
) -> None:
    """Emit ``voice_cascade_probe_result`` after every probe invocation (T1)."""
    logger.info(
        "voice_cascade_probe_result",
        endpoint=endpoint_guid,
        attempt=attempt,
        device_index=device_index,
        combo_host_api=combo.host_api,
        combo_sample_rate=combo.sample_rate,
        diagnosis=str(result.diagnosis),
        rms_db=result.rms_db,
        callbacks_fired=result.callbacks_fired,
        duration_ms=result.duration_ms,
        error_detail=_truncate_detail(result.error),
    )


__all__ = [
    "LINUX_CASCADE",
    "MACOS_CASCADE",
    "WINDOWS_CASCADE",
    "WINDOWS_CASCADE_AGGRESSIVE",
    "ProbeCallable",
    "build_linux_cascade_for_device",
    "run_cascade",
    "run_cascade_for_candidates",
]
