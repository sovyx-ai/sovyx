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
  cascade (8 attempts × ~3 s each); per-attempt 5 s via the probe's
  hard timeout. On total-budget exhaustion the cascade returns with
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
from sovyx.voice.health.contract import (
    CascadeResult,
    Combo,
    Diagnosis,
    ProbeMode,
    ProbeResult,
)
from sovyx.voice.health.probe import probe as _default_probe

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

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

_VOICE_CLARITY_AUTOFIX_FIRST_ATTEMPT = 5
"""When ``voice_clarity_autofix=False``, skip indices 0..4 (exclusive + WDM-KS)
and start at attempt 5 (shared best-effort). ADR §5.11/§5.12.

This is a cascade-table index, not a tuning knob — changing it requires
re-ordering the :data:`WINDOWS_CASCADE` tuple. It belongs here, not in
:class:`VoiceTuningConfig`.
"""


# ── Platform cascade tables ─────────────────────────────────────────────


def _windows_cascade() -> tuple[Combo, ...]:
    """Build the Windows cascade per ADR §4.2.

    ``sample_rate`` is nominal here — callers that need a device's
    actual "native" rate (attempt 2) override the tuple entry at the
    call site via ``cascade_override``. 48 kHz is the overwhelming
    default on modern Windows hardware, so it doubles as attempt 2's
    nominal native rate for the default cascade.
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


WINDOWS_CASCADE: tuple[Combo, ...] = _windows_cascade()
"""Windows 8-attempt cascade. Exclusive WASAPI → WDM-KS → shared → legacy.

Ordering rationale (ADR §4.2):

* Attempts 0-2: exclusive WASAPI bypasses the entire capture APO chain
  (Voice Clarity, OEM DSPs). Most hostile environments resolve here.
* Attempts 3-4: WDM-KS — kernel streaming, also APO-free, available on
  more legacy drivers.
* Attempt 5: shared WASAPI with ``auto_convert`` — last resort before
  giving up on APO bypass; used when ``voice_clarity_autofix=False``.
* Attempts 6-7: DirectSound + MME — legacy fallbacks for ancient
  hardware. Signal still flows but resampler-rich and lossy.
"""

LINUX_CASCADE: tuple[Combo, ...] = ()
"""Linux cascade — populated in Task #27 (S4.1). Empty on Sprint 1."""

MACOS_CASCADE: tuple[Combo, ...] = ()
"""macOS cascade — populated in Task #28 (S4.2). Empty on Sprint 1."""


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
    combo_store: ComboStore | None = None,
    capture_overrides: CaptureOverrides | None = None,
    probe_fn: ProbeCallable | None = None,
    lifecycle_locks: LRULockDict[str] | None = None,
    total_budget_s: float = _DEFAULT_TOTAL_BUDGET_S,
    attempt_budget_s: float = _DEFAULT_ATTEMPT_BUDGET_S,
    voice_clarity_autofix: bool = True,
    cascade_override: Sequence[Combo] | None = None,
    clock: Callable[[], float] = time.monotonic,
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
    """
    locks = lifecycle_locks or _default_locks()
    lock = locks[endpoint_guid]

    async with lock:
        return await _run_cascade_locked(
            endpoint_guid=endpoint_guid,
            device_index=device_index,
            mode=mode,
            platform_key=platform_key,
            device_friendly_name=device_friendly_name,
            device_interface_name=device_interface_name,
            device_class=device_class,
            endpoint_fxproperties_sha=endpoint_fxproperties_sha,
            detected_apos=detected_apos,
            combo_store=combo_store,
            capture_overrides=capture_overrides,
            probe_fn=probe_fn or _default_probe,
            total_budget_s=total_budget_s,
            attempt_budget_s=attempt_budget_s,
            voice_clarity_autofix=voice_clarity_autofix,
            cascade_override=cascade_override,
            clock=clock,
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
    combo_store: ComboStore | None,
    capture_overrides: CaptureOverrides | None,
    probe_fn: ProbeCallable,
    total_budget_s: float,
    attempt_budget_s: float,
    voice_clarity_autofix: bool,
    cascade_override: Sequence[Combo] | None,
    clock: Callable[[], float],
) -> CascadeResult:
    deadline = clock() + total_budget_s
    attempts: list[ProbeResult] = []
    attempts_count = 0

    # 1. Pinned override.
    pinned = _lookup_override(capture_overrides, endpoint_guid, platform_key)
    if pinned is not None:
        logger.info(
            "voice_cascade_pinned_lookup",
            endpoint=endpoint_guid,
            combo=_combo_tag(pinned),
        )
        result = await _try_combo(
            probe_fn=probe_fn,
            combo=pinned,
            mode=mode,
            device_index=device_index,
            attempt_budget_s=attempt_budget_s,
        )
        attempts.append(result)
        attempts_count += 1
        if result.diagnosis is Diagnosis.HEALTHY:
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=pinned,
                winning_probe=result,
                attempts=attempts,
                attempts_count=0,
                budget_exhausted=False,
                source="pinned",
            )
        logger.warning(
            "voice_cascade_pinned_failed",
            endpoint=endpoint_guid,
            diagnosis=str(result.diagnosis),
        )

    # 2. ComboStore fast path.
    store_combo = _lookup_store(combo_store, endpoint_guid)
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
        result = await _try_combo(
            probe_fn=probe_fn,
            combo=store_combo,
            mode=mode,
            device_index=device_index,
            attempt_budget_s=attempt_budget_s,
        )
        attempts.append(result)
        if result.diagnosis is Diagnosis.HEALTHY:
            # Fast-path hit: do NOT re-record (combo already in store).
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=store_combo,
                winning_probe=result,
                attempts=attempts,
                attempts_count=0,
                budget_exhausted=False,
                source="store",
            )
        # Invalidate the stale store entry so the next boot runs the
        # full cascade fresh rather than re-probing the known-bad combo.
        if combo_store is not None:
            combo_store.invalidate(endpoint_guid, reason="fast_path_probe_failed")
            logger.warning(
                "voice_cascade_store_invalidated",
                endpoint=endpoint_guid,
                diagnosis=str(result.diagnosis),
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
        result = await _try_combo(
            probe_fn=probe_fn,
            combo=combo,
            mode=mode,
            device_index=device_index,
            attempt_budget_s=attempt_budget_s,
        )
        attempts.append(result)
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
        logger.error(
            "voice_cascade_probe_raised",
            combo=_combo_tag(combo),
            error=repr(exc),
            exc_info=True,
        )
        return ProbeResult(
            diagnosis=Diagnosis.DRIVER_ERROR,
            mode=mode,
            combo=combo,
            vad_max_prob=None,
            vad_mean_prob=None,
            rms_db=float("-inf"),
            callbacks_fired=0,
            duration_ms=0,
            error=f"probe raised: {exc!r}",
        )


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


__all__ = [
    "LINUX_CASCADE",
    "MACOS_CASCADE",
    "WINDOWS_CASCADE",
    "ProbeCallable",
    "run_cascade",
]
