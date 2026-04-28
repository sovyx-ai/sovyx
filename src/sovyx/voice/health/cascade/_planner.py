"""Platform cascade tables.

Split from the legacy ``cascade.py`` (CLAUDE.md anti-pattern #16
hygiene) — see ``MISSION-voice-godfile-splits-v0.24.1.md`` Part 3 / T02.

Pure data builders + the four public cascade tuples
(:data:`WINDOWS_CASCADE`, :data:`WINDOWS_CASCADE_AGGRESSIVE`,
:data:`LINUX_CASCADE`, :data:`MACOS_CASCADE`) plus the
:func:`build_linux_cascade_for_device` per-device tailoring helper.

All public names re-exported from :mod:`sovyx.voice.health.cascade`.
"""

from __future__ import annotations

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.voice.health.contract import Combo

__all__ = [
    "LINUX_CASCADE",
    "MACOS_CASCADE",
    "WINDOWS_CASCADE",
    "WINDOWS_CASCADE_AGGRESSIVE",
    "build_linux_cascade_for_device",
]


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


def _platform_cascade(platform_key: str) -> tuple[Combo, ...]:
    return _PLATFORM_CASCADES.get(platform_key, ())
