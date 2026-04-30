"""Cascade-combo + persistence dataclasses.

Split from the legacy ``contract.py`` (CLAUDE.md anti-pattern #16
hygiene) — see ``MISSION-voice-godfile-splits-v0.24.1.md`` Part 3 / T01.

This sub-module owns the audio-configuration tuple (:class:`Combo`)
plus the cascade-side metadata objects (:class:`CandidateEndpoint`,
:class:`CascadeResult`, :class:`ComboEntry`, :class:`OverrideEntry`,
:class:`LoadReport`, :class:`ComboStoreStats`) and the platform
validation tables (``ALLOWED_*`` constants + ``_platform_key``).

All public names re-exported from :mod:`sovyx.voice.health.contract`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from sovyx.voice.device_enum import DeviceKind
    from sovyx.voice.health.contract._diagnosis import Diagnosis
    from sovyx.voice.health.contract._probe_result import (
        ProbeHistoryEntry,
        ProbeResult,
    )


__all__ = [
    "ALLOWED_FORMATS",
    "ALLOWED_HOST_APIS_BY_PLATFORM",
    "ALLOWED_SAMPLE_RATES",
    "CandidateEndpoint",
    "CandidateSource",
    "CascadeResult",
    "Combo",
    "ComboEntry",
    "ComboStoreStats",
    "LoadReport",
    "OverrideEntry",
    "ProbeMode",
    "_allowed_host_apis_for",
    "_platform_key",
]


# ── Enums ────────────────────────────────────────────────────────────────


class CandidateSource(StrEnum):
    """Reason a :class:`CandidateEndpoint` is in the cascade's candidate set.

    Populated by :func:`~sovyx.voice.health._candidate_builder.build_capture_candidates`.
    Used for telemetry (``voice_cascade_probe_call.candidate_source``) and
    for the UI banner that explains why the cascade picked a fallback.

    Members:
        USER_PREFERRED: The device the user pinned via ``mind.yaml``
            (``input_device_name``) or the request body. Always rank 0
            when present.
        CANONICAL_SIBLING: Same :attr:`~sovyx.voice.device_enum.DeviceEntry.canonical_name`
            as USER_PREFERRED but different ``host_api_name`` — legacy
            "same physical device exposed by multiple host APIs" path.
            Dominant on Windows (WASAPI / DirectSound / MME of one mic);
            usually empty on Linux where PortAudio exposes only ALSA.
        SESSION_MANAGER_VIRTUAL: PipeWire / PulseAudio / JACK virtual
            PCM. Added on Linux only, and only when
            ``USER_PREFERRED.kind == HARDWARE`` (we don't duplicate).
        OS_DEFAULT: The ``default`` / ``sysdefault`` ALSA alias. Added
            on Linux only, and only when not already present via
            ``USER_PREFERRED``.
        FALLBACK: Any remaining enumerated input device not already
            claimed by one of the above sources. Tail of the list —
            rarely reached; exists so a truly exotic setup (vendor
            virtual PCM) still gets a shot before ``CaptureInoperativeError``.
    """

    USER_PREFERRED = "user_preferred"
    CANONICAL_SIBLING = "canonical_sibling"
    SESSION_MANAGER_VIRTUAL = "session_manager_virtual"
    OS_DEFAULT = "os_default"
    FALLBACK = "fallback"


class ProbeMode(StrEnum):
    """How a probe should validate the device.

    * :attr:`COLD` — boot-time, no user assumed to be speaking. Validates
      that the stream opens and PortAudio callbacks fire. Cannot detect
      :attr:`Diagnosis.APO_DEGRADED` because a silent room produces the
      same RMS as a destroyed signal.
    * :attr:`WARM` — wizard / first-interaction / fix mode, user is
      asked to speak. Runs the audio through SileroVAD to derive the
      full diagnosis surface.
    """

    COLD = "cold"
    WARM = "warm"


# ── Validation tables (immutable) ───────────────────────────────────────


ALLOWED_SAMPLE_RATES: frozenset[int] = frozenset(
    {8_000, 16_000, 22_050, 24_000, 32_000, 44_100, 48_000, 88_200, 96_000, 192_000},
)
"""Sample rates we accept in a :class:`Combo`. Wider than what we use in
practice so cascade overrides can target unusual hardware."""


ALLOWED_FORMATS: frozenset[str] = frozenset({"int16", "int24", "float32"})
"""PortAudio sample formats we know how to drive end-to-end through the
:class:`~sovyx.voice.FrameNormalizer` resampler."""


_ALLOWED_CHANNELS_MIN = 1
_ALLOWED_CHANNELS_MAX = 8
_ALLOWED_FRAMES_PER_BUFFER_MIN = 64
_ALLOWED_FRAMES_PER_BUFFER_MAX = 8_192


ALLOWED_HOST_APIS_BY_PLATFORM: Mapping[str, frozenset[str]] = {
    "win32": frozenset(
        {
            "WASAPI",
            "Windows WASAPI",
            "WDM-KS",
            "Windows WDM-KS",
            "DirectSound",
            "Windows DirectSound",
            "MME",
        },
    ),
    "linux": frozenset({"ALSA", "PulseAudio", "PipeWire", "JACK"}),
    "darwin": frozenset({"CoreAudio", "Core Audio"}),
}
"""Host APIs we accept on each platform.

Both bare (``"WASAPI"``) and PortAudio-formatted (``"Windows WASAPI"``)
labels are accepted so callers can use whichever the upstream API gave
them without an extra normalization step.
"""


def _platform_key() -> str:
    """Return the current platform key for :data:`ALLOWED_HOST_APIS_BY_PLATFORM`.

    Splits the hairsplitting of ``sys.platform`` ("linux", "linux2",
    "win32", "darwin", …) into the three buckets we care about and
    falls back to ``"linux"`` for unknown POSIX-likes.
    """
    plat = sys.platform
    if plat.startswith("win"):
        return "win32"
    if plat == "darwin":
        return "darwin"
    return "linux"


def _allowed_host_apis_for(platform_key: str) -> frozenset[str]:
    return ALLOWED_HOST_APIS_BY_PLATFORM.get(platform_key, frozenset())


# ── Dataclasses ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Combo:
    """The audio configuration tuple that opens a capture stream.

    Validated at construction so an invalid tuple (out-of-range rate,
    unknown host API for the current platform, channels = 0) raises
    immediately instead of propagating into the cascade.

    Args:
        host_api: Host-API label (``"WASAPI"`` or PortAudio's
            ``"Windows WASAPI"`` — both accepted). Validated against
            :data:`ALLOWED_HOST_APIS_BY_PLATFORM`.
        sample_rate: Device-side sample rate in Hz. Pipeline downstream
            resamples to 16 kHz mono int16 regardless.
        channels: Channel count requested from PortAudio. The
            normalizer mixes down to mono.
        sample_format: PortAudio sample format (``"int16"`` / ``"int24"``
            / ``"float32"``).
        exclusive: WASAPI exclusive mode flag (Windows only meaningful;
            ignored on other platforms).
        auto_convert: Let WASAPI resample / rechannel transparently
            (Windows only meaningful).
        frames_per_buffer: PortAudio buffer size in samples. ``480`` is
            30 ms at 16 kHz and matches the Silero VAD window cadence.
        platform_key: Optional override for the platform key used during
            validation. Tests pass ``"win32"`` to validate Windows
            cascades on a non-Windows host without monkey-patching
            :mod:`sys`.

    Raises:
        ValueError: On any out-of-range field or platform-host_api
            mismatch.
    """

    host_api: str
    sample_rate: int
    channels: int
    sample_format: str
    exclusive: bool
    auto_convert: bool
    frames_per_buffer: int
    platform_key: str = ""  # empty = use current platform; set in tests

    def __post_init__(self) -> None:
        if not self.host_api:
            msg = "host_api must be a non-empty string"
            raise ValueError(msg)
        plat = self.platform_key or _platform_key()
        allowed = _allowed_host_apis_for(plat)
        if allowed and self.host_api not in allowed:
            msg = (
                f"host_api={self.host_api!r} is not allowed on platform "
                f"{plat!r}; expected one of {sorted(allowed)}"
            )
            raise ValueError(msg)
        if self.sample_rate not in ALLOWED_SAMPLE_RATES:
            msg = (
                f"sample_rate={self.sample_rate} is not allowed; "
                f"expected one of {sorted(ALLOWED_SAMPLE_RATES)}"
            )
            raise ValueError(msg)
        if not _ALLOWED_CHANNELS_MIN <= self.channels <= _ALLOWED_CHANNELS_MAX:
            msg = (
                f"channels={self.channels} out of range "
                f"[{_ALLOWED_CHANNELS_MIN}, {_ALLOWED_CHANNELS_MAX}]"
            )
            raise ValueError(msg)
        if self.sample_format not in ALLOWED_FORMATS:
            msg = (
                f"sample_format={self.sample_format!r} not allowed; "
                f"expected one of {sorted(ALLOWED_FORMATS)}"
            )
            raise ValueError(msg)
        if not (
            _ALLOWED_FRAMES_PER_BUFFER_MIN
            <= self.frames_per_buffer
            <= _ALLOWED_FRAMES_PER_BUFFER_MAX
        ):
            msg = (
                f"frames_per_buffer={self.frames_per_buffer} out of range "
                f"[{_ALLOWED_FRAMES_PER_BUFFER_MIN}, {_ALLOWED_FRAMES_PER_BUFFER_MAX}]"
            )
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class CandidateEndpoint:
    """One endpoint the cascade should consider when opening capture.

    A :class:`CandidateEndpoint` is the cascade-land analogue of a
    :class:`~sovyx.voice.device_enum.DeviceEntry`: it carries the
    device identity *plus* the metadata the cascade needs to (a) order
    attempts, (b) key the ComboStore, (c) drive quarantine lifecycle,
    and (d) populate user-visible telemetry. Built by
    :func:`~sovyx.voice.health._candidate_builder.build_capture_candidates`.

    The distinction between :attr:`device_index` and
    :attr:`~sovyx.voice.device_enum.DeviceEntry.index` is deliberate:
    the cascade may produce multiple ``CandidateEndpoint``\\ s with
    different ``device_index`` values derived from the same physical
    device (e.g. ``hw:1,0`` + ``plughw:1,0`` on Linux), and each gets
    its own :attr:`endpoint_guid` for independent fast-path / quarantine
    state in :class:`~sovyx.voice.health.combo_store.ComboStore`.

    Attributes:
        device_index: PortAudio device index — ephemeral across boots,
            but always valid for the lifetime of one enumeration pass.
        host_api_name: PortAudio host-API name (``"ALSA"``, ``"Windows
            WASAPI"``, ``"Core Audio"``, …). Used for telemetry and to
            cross-check with the device's actual host API in the probe.
        kind: Semantic classification
            (:class:`~sovyx.voice.device_enum.DeviceKind`). Drives
            Linux-specific candidate-builder ordering heuristics.
        canonical_name: The normalised, host-API-agnostic device name —
            equal to :attr:`DeviceEntry.canonical_name`. Used by the
            cascade-side logging and ComboStore keys.
        friendly_name: Human-readable device name, for UI banners and
            doctor-subcommand output.
        source: Why this candidate is in the set — see
            :class:`CandidateSource`.
        preference_rank: 0 = try first. Stable deterministic ordering
            within one boot; **not** a persistent score. The
            :class:`CascadeResult.winning_candidate`'s rank is what the
            ComboStore fast-path hydrates on the next boot.
        endpoint_guid: Stable identifier from
            :func:`~sovyx.voice.health._factory_integration.derive_endpoint_guid`.
            Each ``CandidateEndpoint`` gets its own — two candidates
            that differ only by ``device_index`` but share
            ``(canonical_name, host_api_name, platform)`` produce the
            same guid by design (deterministic hash). Two candidates
            with different ``canonical_name`` produce different guids
            — which is exactly the desired ComboStore independence.
    """

    device_index: int
    host_api_name: str
    kind: DeviceKind
    canonical_name: str
    friendly_name: str
    source: CandidateSource
    preference_rank: int
    endpoint_guid: str
    default_samplerate: int = 0

    def __post_init__(self) -> None:
        if self.device_index < 0:
            msg = f"device_index must be >= 0, got {self.device_index}"
            raise ValueError(msg)
        if not self.host_api_name:
            msg = "host_api_name must be a non-empty string"
            raise ValueError(msg)
        if not self.endpoint_guid:
            msg = "endpoint_guid must be a non-empty string"
            raise ValueError(msg)
        if self.preference_rank < 0:
            msg = f"preference_rank must be >= 0, got {self.preference_rank}"
            raise ValueError(msg)
        if self.default_samplerate < 0:
            msg = f"default_samplerate must be >= 0, got {self.default_samplerate}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ComboEntry:
    """One row of :class:`~sovyx.voice.health.combo_store.ComboStore`.

    Pure data; persistence rules + invalidation gates live in the store.
    """

    endpoint_guid: str
    device_friendly_name: str
    device_interface_name: str
    device_class: str
    endpoint_fxproperties_sha: str
    winning_combo: Combo
    validated_at: str
    validation_mode: ProbeMode
    vad_max_prob_at_validation: float | None
    vad_mean_prob_at_validation: float | None
    rms_db_at_validation: float
    probe_duration_ms: int
    detected_apos_at_validation: tuple[str, ...]
    cascade_attempts_before_success: int
    boots_validated: int
    last_boot_validated: str
    last_boot_diagnosis: Diagnosis
    probe_history: tuple[ProbeHistoryEntry, ...] = ()
    pinned: bool = False
    needs_revalidation: bool = False  # set by R6/R7/R8/R9/R10/R11 on load
    # voice-linux-cascade-root-fix T11 / schema v3.
    # :class:`~sovyx.voice.device_enum.DeviceKind` value as string.
    # Back-compat default ``"unknown"`` preserves v2 entries as-is.
    candidate_kind: str = "unknown"
    # T5.43 + T5.51 wire-up — stable USB fingerprint
    # ``"usb-VVVV:PPPP[-SERIAL]"`` for cross-port / cross-firmware-update
    # combo recovery. ``None`` for non-USB endpoints (PCI codecs,
    # virtual loopback, Bluetooth A2DP) and for legacy entries written
    # before the resolver was wired in. Additive — no schema bump.
    usb_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class OverrideEntry:
    """One row of :class:`~sovyx.voice.health.capture_overrides.CaptureOverrides`."""

    endpoint_guid: str
    device_friendly_name: str
    pinned_combo: Combo
    pinned_at: str
    pinned_by: str  # "user" | "wizard" | "cli"
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CascadeResult:
    """Outcome of one :func:`~sovyx.voice.health.cascade.run_cascade` call.

    Attributes:
        winning_combo: The combo that produced :attr:`Diagnosis.HEALTHY`
            (warm) or callbacks-OK (cold). ``None`` when the cascade
            exhausted without a winner.
        winning_candidate: The :class:`CandidateEndpoint` that yielded
            the winner, with its original ``source`` and
            ``preference_rank`` preserved. ``None`` on exhaustion,
            quarantine, or when the cascade ran against a legacy
            single-endpoint call-site (pre-T3 of voice-linux-cascade-
            root-fix — kept for a window during the migration).
        winning_probe: The :class:`ProbeResult` that earned the winning
            combo. ``None`` on exhaustion.
        attempts: Every probe that ran during the cascade, in order.
            Always non-empty after a successful run; ``None`` when the
            cascade short-circuited on a fast-path lookup that produced
            no fresh probe.
        attempts_count: Number of attempts before success (0 = ComboStore
            fast path or pinned override hit). Independent of
            :attr:`attempts` length so callers don't have to subtract 1
            for the winning attempt.
        budget_exhausted: ``True`` when the total wall-clock budget ran
            out before a winner emerged.
        source: Where the winning combo came from —
            ``"pinned"`` / ``"store"`` / ``"cascade"`` / ``"none"`` /
            ``"quarantined"``. ``"quarantined"`` means every candidate
            was in the §4.4.7 kernel-invalidated quarantine and no probe
            ran; callers should fail-over to the next viable endpoint.
        endpoint_guid: GUID of the endpoint the cascade ran on (echoed
            for caller convenience). With candidate-set cascade this
            is the **winning** candidate's guid, or the first
            candidate's guid on exhaustion (for log correlation).
    """

    endpoint_guid: str
    winning_combo: Combo | None
    winning_probe: ProbeResult | None
    attempts: tuple[ProbeResult, ...]
    attempts_count: int
    budget_exhausted: bool
    source: str  # "pinned" | "store" | "cascade" | "none" | "quarantined"
    winning_candidate: CandidateEndpoint | None = None


@dataclass(frozen=True, slots=True)
class LoadReport:
    """Result of :meth:`~sovyx.voice.health.combo_store.ComboStore.load`.

    Attributes:
        rules_applied: Pairs of ``(rule_code, scope)`` where ``scope`` is
            either an endpoint GUID or ``"<global>"``.
        entries_loaded: Number of valid entries available after load.
        entries_dropped: Number of entries discarded by sanity rules
            (R12) or platform mismatch.
        backup_used: ``True`` when the main file was unreadable and the
            ``.bak`` file rescued the load.
        archived_to: Optional path the corrupt file was moved to.
    """

    rules_applied: tuple[tuple[str, str], ...]
    entries_loaded: int
    entries_dropped: int
    backup_used: bool
    archived_to: Path | None


@dataclass(slots=True)
class ComboStoreStats:
    """In-memory hit/miss counters since the last :meth:`load`.

    Mutable on purpose so the store can update counters in place without
    rebuilding a frozen object on every probe. Read-only from the
    outside.
    """

    fast_path_hits: int = 0
    fast_path_misses: int = 0
    invalidations_by_reason: dict[str, int] = field(default_factory=dict)
