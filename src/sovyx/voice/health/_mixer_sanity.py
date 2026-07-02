"""L2.5 mixer-sanity orchestrator — bidirectional mixer healing (F1.E).

The entry point :func:`check_and_maybe_heal` runs inside
:func:`~sovyx.voice.health.cascade.run_cascade`, after the pinned-
override + ComboStore fast-paths and before the platform cascade
walk. It probes the ALSA mixer, classifies the regime, detects user
customization, and — when appropriate — applies a KB-driven preset,
validates the result, and persists via ``alsactl store -f``. Full
rollback fires on any validation or apply failure.

This module is the behavioural heart of L2.5. It wires together:

* :mod:`~sovyx.voice.health._linux_mixer_probe` — mixer state read
* :mod:`~sovyx.voice.health._mixer_roles` — role resolution
* :mod:`~sovyx.voice.health._mixer_kb` — KB match + scoring
* :mod:`~sovyx.voice.health._linux_mixer_apply` — apply + rollback
* A caller-injected ``validation_probe_fn`` — post-apply signal check
* A caller-injected ``persist_fn`` — ``alsactl store`` wrapper

Every external side-effect is injected through a typed callable so
the state machine is fully mockable in unit tests. Default
implementations live at the bottom of the file and are used in
production.

See V2 Master Plan Part C.1 (placement), C.2 (state machine), E.1
(public API), E.5 (customization heuristic), E.6 (validation).
"""

from __future__ import annotations

import asyncio
import contextvars
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypeAlias

from sovyx.observability.logging import get_logger
from sovyx.voice.health._half_heal_recovery import (
    recover_if_present as _recover_half_heal_if_present,
)
from sovyx.voice.health._linux_mixer_apply import (
    restore_mixer_snapshot as _default_restore_mixer_snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice.health._mixer_kb import MixerKBLookup
    from sovyx.voice.health._mixer_roles import MixerControlRoleResolver
    from sovyx.voice.health.capture_overrides import CaptureOverrides
    from sovyx.voice.health.combo_store import ComboStore
    from sovyx.voice.health.contract import (
        CandidateEndpoint,
        HardwareContext,
        MixerApplySnapshot,
        MixerCardSnapshot,
        MixerControlRole,
        MixerControlSnapshot,
        MixerPresetSpec,
        MixerSanityResult,
        MixerValidationMetrics,
    )

logger = get_logger(__name__)


# ── DI type aliases ──────────────────────────────────────────────────


MixerProbeFn: TypeAlias = "Callable[[], Sequence[MixerCardSnapshot]]"
"""Reads current mixer state. Defaults to
:func:`~sovyx.voice.health._linux_mixer_probe.enumerate_alsa_mixer_snapshots`.
Synchronous because the default wraps ``amixer`` subprocess calls
with their own internal timeout and the caller runs this in a
thread-bounded context.
"""


class MixerApplyFn(Protocol):
    """Applies a KB preset. Signature matches
    :func:`~sovyx.voice.health._linux_mixer_apply.apply_mixer_preset`.

    Paranoid-QA R3 HIGH #10: expressed as a ``Protocol`` — not
    ``Callable[..., Awaitable[MixerApplySnapshot]]`` — because
    the ``...`` ellipsis erases arity. Tests that injected
    ``lambda: snapshot`` (zero args) type-checked fine under the
    alias form, then blew up at runtime with ``TypeError: missing 3
    positional args``. The Protocol declares the real signature
    with keyword-only ``tuning`` so mypy catches shape mismatches
    at compile time.
    """

    async def __call__(
        self,
        card_index: int,
        preset: MixerPresetSpec,
        role_mapping: Mapping[MixerControlRole, tuple[MixerControlSnapshot, ...]],
        *,
        tuning: VoiceTuningConfig,
    ) -> MixerApplySnapshot: ...


class MixerRestoreFn(Protocol):
    """Restores a mixer snapshot. Signature matches
    :func:`~sovyx.voice.health._linux_mixer_apply.restore_mixer_snapshot`.

    Expressed as a ``Protocol`` (not ``Callable[...]``) because
    ``tuning`` is a keyword-only parameter and ``Callable[[X, Y], ...]``
    types are always positional. A Protocol expresses the real
    keyword-only signature and lets mypy-strict (CI-Linux) verify
    callers pass a compatible function.
    """

    async def __call__(
        self,
        snapshot: MixerApplySnapshot,
        *,
        tuning: VoiceTuningConfig,
    ) -> None: ...


ValidationProbeFn: TypeAlias = (
    "Callable[[CandidateEndpoint, VoiceTuningConfig], Awaitable[MixerValidationMetrics]]"
)
"""Runs the post-apply audio probe + metric composition.

Callers with real ``capture_raw_fn`` + ``wake_word_probe_fn`` inject
a richer implementation; the state machine does not care about
acquisition strategy.
"""


PersistFn: TypeAlias = "Callable[[Sequence[int], VoiceTuningConfig], Awaitable[bool]]"
"""Persists a set of ALSA cards via ``alsactl store``. Returns
``True`` on success. Default: :func:`default_persist_via_alsactl`.
"""


@dataclass(frozen=True, slots=True)
class MixerSanitySetup:
    """L2.5 dependency bundle — opts cascade into mixer healing.

    Passed to :func:`~sovyx.voice.health.cascade.run_cascade` via the
    ``mixer_sanity`` kwarg. When set AND ``platform_key == "linux"``,
    the cascade invokes :func:`check_and_maybe_heal` between the
    ComboStore fast-path and the platform cascade walk. Default
    ``None`` keeps every existing caller at the pre-L2.5 behaviour —
    zero regression by construction.

    Why a dataclass instead of 5+ kwargs on ``run_cascade``: the
    cascade signature already carries 15+ parameters; threading
    individual L2.5 deps would push it past the reviewability
    threshold. One aggregate parameter keeps the surface area sane
    and makes future L2.5 extensions (e.g., user-contributed KB
    dirs, alternative validation strategies) additive.

    Args:
        hw: Detected hardware context (driver family + codec +
            system + kernel). Required — every scoring step keys
            off it.
        kb_lookup: Profile catalogue. Typically built at daemon
            startup via :meth:`MixerKBLookup.load_shipped`.
        role_resolver: Control-name → role resolver. A single
            shared instance is fine.
        validation_probe_fn: Post-apply metric acquisition — the
            caller decides whether to run a real audio probe or a
            stub. Production wires a warm probe + SNR + Silero +
            OpenWakeWord; tests inject a deterministic pass/fail
            callable.
        mixer_probe_fn, mixer_apply_fn, mixer_restore_fn,
        persist_fn: Optional overrides — ``None`` defaults resolve
            inside ``check_and_maybe_heal`` to the shipped Linux
            implementations.
        telemetry: Optional :class:`_TelemetryProto`. ``None``
            uses the internal no-op.
    """

    hw: HardwareContext
    kb_lookup: MixerKBLookup
    role_resolver: MixerControlRoleResolver
    validation_probe_fn: ValidationProbeFn
    mixer_probe_fn: MixerProbeFn | None = None
    mixer_apply_fn: MixerApplyFn | None = None
    mixer_restore_fn: MixerRestoreFn | None = None
    persist_fn: PersistFn | None = None
    telemetry: _TelemetryProto | None = None
    # Paranoid-QA R2 HIGH #3: path to the half-heal write-ahead
    # log. ``None`` disables WAL recovery entirely (tests + dev
    # environments without a persistent data_dir). Production wires
    # ``default_wal_path(config.database.data_dir)``. When set,
    # ``check_and_maybe_heal`` runs a recovery replay at the top of
    # every cascade — if a prior invocation died mid-apply, its WAL
    # is still on disk and gets replayed before the normal state
    # machine runs. Also enables per-apply WAL writes around
    # ``_step_apply`` so a crash DURING apply can be recovered on
    # the next boot.
    half_heal_wal_path: Path | None = None


class _TelemetryProto(Protocol):
    """Minimal surface L2.5 needs from
    :class:`~sovyx.voice.health._telemetry.VoiceHealthTelemetry`.

    Defined as a Protocol so tests can inject a no-op stub; production
    wires the real telemetry singleton via
    :func:`~sovyx.voice.health._telemetry.get_telemetry`.

    Paranoid-QA R3 HIGH #9: NOT ``@runtime_checkable``. R2 CRIT-4
    removed the only ``isinstance(x, _TelemetryProto)`` call site;
    leaving the decorator on would invite a future edit to re-add
    ``isinstance()``-based dispatch, which performs **nominal-only**
    signature matching (checks method presence + roughly arity, does
    NOT verify parameter names or types). Sticking to mypy-only
    verification catches signature-drift at compile time instead of
    blowing up at runtime with ``TypeError: unexpected kwarg``.
    """

    def record_mixer_sanity_outcome(
        self,
        *,
        decision: str,
        matched_profile: str | None,
        score: float,
        is_user_contributed: bool = False,
    ) -> None: ...


class _NoopTelemetry:
    """Fallback when the telemetry singleton is disabled or absent."""

    def record_mixer_sanity_outcome(
        self,
        *,
        decision: str,  # noqa: ARG002 — Protocol conformance
        matched_profile: str | None,  # noqa: ARG002
        score: float,  # noqa: ARG002
        is_user_contributed: bool = False,  # noqa: ARG002
    ) -> None:
        """No-op — L2.5 respects the user's ``telemetry.enabled=False``."""
        return


# Phase 5.F.11 god-file split: customization heuristic (V2 §E.5;
# 6 symbols, ~185 LOC) extracted to _mixer_sanity_customization.py.
# Re-exported here so the public consumer at voice/health/__init__.py
# and the dedicated test file continue to resolve via standard
# module-namespace lookup.
from sovyx.voice.health._mixer_sanity_customization import (  # noqa: E402  F401
    _ASOUND_STATE_RECENT_SECONDS,
    _SIGNAL_WEIGHTS,
    _directory_has_configs,
    _file_mtime_recent,
    _UserCustomizationReport,
    detect_user_customization,
)

# Phase 5.F.14 god-file split: state machine types + orchestrator class
# (~810 LOC) extracted to _mixer_sanity_orchestrator.py. Re-exported here
# so the in-parent call site at _check_and_maybe_heal_impl (which
# instantiates _OrchestratorContext + _SanityOrchestrator) resolves via
# standard module-namespace lookup. Anti-pattern #16 + #20.
from sovyx.voice.health._mixer_sanity_orchestrator import (  # noqa: E402  F401
    _OrchestratorContext,
    _SanityOrchestrator,
    _StepName,
    _StepResult,
)

# ── Public entry point ──────────────────────────────────────────────


# Paranoid-QA R2 HIGH #1: single global asyncio.Lock serialises every
# L2.5 invocation regardless of endpoint. The ALSA mixer is shared
# kernel state — two concurrent cascades (two endpoints coming online
# within the cascade-debounce window, or a watchdog-triggered
# recascade overlapping a boot-time cascade) could both probe the
# same state, both apply, and leave the post-apply rollback out of
# sync with the current committed preset.
#
# Contention budget: L2.5 fires at most on boot + on hot-plug events.
# A single global lock is boring, correct, and has no semantic holes.
# Per-card or per-endpoint locks would admit harder races because
# different cascades would probe overlapping card sets.
_L25_LOCK: asyncio.Lock | None = None
_L25_LOCK_LOOP: asyncio.AbstractEventLoop | None = None


def _get_l25_lock() -> asyncio.Lock:
    """Lazy lock factory — creates the Lock on first use within the
    current event loop.

    Paranoid-QA R3 HIGH #5: also re-initialises when the current
    running loop differs from the one the lock was bound to. The
    daemon's `sovyx stop` + `sovyx start` flow tears down Loop A
    and spins up Loop B within the SAME process (the CLI process).
    Without this guard, the second cascade would acquire a Lock
    that belongs to a torn-down loop → ``RuntimeError: Event loop
    is closed`` (or, worse, silent misbinding under CPython's
    lenient unbound-Future handling). Production has no test hook
    that resets the lock — this does.

    Paranoid-QA R4 HIGH-7: emit a WARN breadcrumb on every rebind
    so operators auditing multi-cascade behaviour can see the loop
    swap happened. Also assert the prior lock was not held — a
    held lock at rebind time signals a supervisor that cancelled
    a task holding the lock WITHOUT running the task's
    ``__aexit__`` — a latent leak worth surfacing even if the new
    lock keeps working.
    """
    global _L25_LOCK, _L25_LOCK_LOOP  # noqa: PLW0603 — single-instance lazy init
    current_loop = asyncio.get_running_loop()
    if _L25_LOCK is None:
        _L25_LOCK = asyncio.Lock()
        _L25_LOCK_LOOP = current_loop
        return _L25_LOCK
    if _L25_LOCK_LOOP is not current_loop:
        prior_was_locked = _L25_LOCK.locked()
        logger.warning(
            "mixer_sanity_lock_rebound_to_new_loop",
            prior_loop_id=id(_L25_LOCK_LOOP),
            current_loop_id=id(current_loop),
            prior_was_locked=prior_was_locked,
            note=(
                "event loop changed since last cascade (daemon "
                "stop+start cycle?). Creating a fresh Lock on the "
                "current loop — safe, but prior_was_locked=True "
                "indicates a task was cancelled without running "
                "its lock-release __aexit__"
            ),
        )
        _L25_LOCK = asyncio.Lock()
        _L25_LOCK_LOOP = current_loop
    return _L25_LOCK


# Paranoid-QA R2 HIGH #2: contextvar guard against recursive entry.
# The validation_probe_fn injected by production is wired through the
# voice stack (capture probe + wake-word probe). A misrouted
# event — e.g., validation probe triggering an audio-service restart
# that fires a watchdog recascade — would re-enter L2.5 from within
# the apply-validate window, racing the ALSA mutations we're in the
# middle of.
#
# A contextvar (rather than a plain thread-local or module flag) is
# the right tool because asyncio tasks inherit contextvars by
# default — so a child Task spawned by the orchestrator also sees
# the guard, blocking inadvertent self-dispatch through task groups.
_L25_INSIDE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "sovyx_mixer_sanity_inside",
    default=False,
)


async def check_and_maybe_heal(
    endpoint: CandidateEndpoint,
    hw: HardwareContext,
    *,
    kb_lookup: MixerKBLookup,
    role_resolver: MixerControlRoleResolver,
    validation_probe_fn: ValidationProbeFn,
    tuning: VoiceTuningConfig,
    mixer_probe_fn: MixerProbeFn | None = None,
    mixer_apply_fn: MixerApplyFn | None = None,
    mixer_restore_fn: MixerRestoreFn | None = None,
    persist_fn: PersistFn | None = None,
    telemetry: _TelemetryProto | None = None,
    combo_store: ComboStore | None = None,
    capture_overrides: CaptureOverrides | None = None,
    half_heal_wal_path: Path | None = None,
) -> MixerSanityResult:
    """Run the full L2.5 state machine for ``endpoint``.

    Returns a :class:`MixerSanityResult` whose
    :attr:`MixerSanityResult.decision` (``HEALED``, ``SKIPPED_*``,
    ``DEFERRED_*``, ``ROLLED_BACK``, ``ERROR``) the cascade
    integration layer logs + feeds to telemetry. The call is
    fire-and-forget from the cascade's perspective: the platform
    walk always proceeds afterwards — on ``HEALED`` it validates
    the now-corrected mixer state rather than being skipped (see
    ``cascade/_alignment.py::_run_mixer_sanity``).

    Timings: target ≤ 3 s typical; hard abort at
    :attr:`VoiceTuningConfig.linux_mixer_sanity_budget_s` (default
    5 s). On timeout, any in-flight apply is rolled back and the
    decision becomes ``ERROR`` with
    ``error=MIXER_SANITY_BUDGET_EXCEEDED``.

    Concurrency: a module-level ``asyncio.Lock`` serialises every
    call, regardless of endpoint. Two concurrent cascades contend
    for the same ALSA mixer — the second waits for the first's
    full apply-validate-persist chain to complete. Plus a
    contextvar-based recursive-entry guard: nested calls within the
    same asyncio Task short-circuit to ``ERROR`` with error token
    ``MIXER_SANITY_REENTRANT_GUARD`` (a validation probe triggering
    a recascade should never be able to race the in-flight apply).

    Crash safety: when ``half_heal_wal_path`` is supplied, every
    apply writes a write-ahead log to that path BEFORE the first
    ``amixer_set`` and clears it on every terminal transition
    (success, rollback, cancellation). If the process dies
    mid-apply, the next cascade detects the WAL at entry and
    replays it via ``mixer_restore_fn`` to restore the pre-apply
    state before probing. See :mod:`_half_heal_recovery`.

    Platform: Linux only in F1. Non-Linux callers receive
    ``DEFERRED_PLATFORM`` with no side-effects.
    """
    if sys.platform != "linux":
        logger.debug(
            "mixer_sanity_non_linux_defer",
            platform=sys.platform,
            endpoint_guid=endpoint.endpoint_guid,
        )
        return _defer_platform_result()

    # Reentrancy guard — checked BEFORE acquiring the lock so a
    # nested call can't deadlock on a lock its ancestor already
    # holds.
    if _L25_INSIDE.get():
        logger.warning(
            "mixer_sanity_reentrant_call_blocked",
            endpoint_guid=endpoint.endpoint_guid,
            note="L2.5 already running in this Task — returning ERROR",
        )
        return _reentrant_result()

    token = _L25_INSIDE.set(True)
    try:
        async with _get_l25_lock():
            # Paranoid-QA R2 HIGH #3: half-heal recovery — if a
            # previous L2.5 invocation died mid-apply, its WAL is
            # on disk. Replay it now (before the state machine
            # probes) so the mixer is back to pre-L2.5 state.
            # Runs INSIDE the lock so two cascades can't race
            # the WAL. Invoked once per cascade regardless of
            # whether we end up applying anything — the cost is
            # a single stat() when no WAL exists.
            if half_heal_wal_path is not None:
                effective_restore_fn = (
                    mixer_restore_fn
                    if mixer_restore_fn is not None
                    else _default_restore_mixer_snapshot
                )
                # Paranoid-QA R3 HIGH #1 + R4 HIGH-5: cap the WAL
                # replay with a wall-clock budget. The original R3
                # cap used ``linux_mixer_sanity_budget_s`` (5s) —
                # but a legitimate WAL with ``_WAL_MAX_ENTRIES`` ×
                # ``linux_mixer_subprocess_timeout_s`` can easily
                # exceed that, causing spurious timeouts on
                # honest-but-slow systems (high loadavg, slow
                # storage). R4 HIGH-5 sizes the timeout to
                # ``max_entries × subprocess_timeout × 1.25`` so
                # every realistic WAL has headroom, while an
                # attacker-capped WAL still has a bounded worst
                # case.
                from sovyx.voice.health._half_heal_recovery import (  # noqa: PLC0415 — local timeout sizing
                    _WAL_MAX_ENTRIES,
                )

                recovery_timeout = max(
                    _WAL_MAX_ENTRIES * tuning.linux_mixer_subprocess_timeout_s * 1.25,
                    tuning.linux_mixer_sanity_budget_s,
                )
                await _recover_half_heal_if_present(
                    path=half_heal_wal_path,
                    restore_fn=effective_restore_fn,
                    tuning=tuning,
                    timeout_s=recovery_timeout,
                )
            return await _check_and_maybe_heal_impl(
                endpoint,
                hw,
                kb_lookup=kb_lookup,
                role_resolver=role_resolver,
                validation_probe_fn=validation_probe_fn,
                tuning=tuning,
                mixer_probe_fn=mixer_probe_fn,
                mixer_apply_fn=mixer_apply_fn,
                mixer_restore_fn=mixer_restore_fn,
                persist_fn=persist_fn,
                telemetry=telemetry,
                combo_store=combo_store,
                capture_overrides=capture_overrides,
                half_heal_wal_path=half_heal_wal_path,
            )
    finally:
        _L25_INSIDE.reset(token)


# Phase 5.F.15 god-file split: _reentrant_result + _check_and_maybe_heal_impl
# (~205 LOC) extracted to _mixer_sanity_orchestrator.py (their natural home —
# they instantiate _OrchestratorContext + _SanityOrchestrator). Re-exported
# here so the public entry point check_and_maybe_heal continues to call
# _check_and_maybe_heal_impl via parent's module namespace.
# Phase 5.F.12 god-file split: 4 pure helpers (~85 LOC) extracted
# to _mixer_sanity_helpers.py. Re-exported here so the in-class
# call sites resolve via standard module-namespace lookup.
# Phase 5.F.13 god-file split: validation-probe + setup builder
# (~250 LOC) extracted to _mixer_sanity_factory.py. Re-exported
# here so the public consumer at voice/health/__init__.py and the
# lazy-import call site at voice/health/_factory_integration.py:442
# continue to resolve.
from sovyx.voice.health._mixer_sanity_factory import (  # noqa: E402  F401
    build_mixer_sanity_setup,
    make_default_validation_probe_fn,
)
from sovyx.voice.health._mixer_sanity_helpers import (  # noqa: E402  F401
    _check_validation_gates,
    _classify_regime_heuristically,
    _defer_platform_result,
    _diagnosis_for_regime,
)
from sovyx.voice.health._mixer_sanity_orchestrator import (  # noqa: E402  F401
    _check_and_maybe_heal_impl,
    _reentrant_result,
)

# Phase 5.F.8 god-file split: default-persist (alsactl + systemd unit
# delegate, ~230 LOC) extracted to _mixer_sanity_persist.py.
# Re-exported here so existing call sites + tests at
# sovyx.voice.health._mixer_sanity.<name> continue to resolve.
from sovyx.voice.health._mixer_sanity_persist import (  # noqa: E402  F401
    _ALSACTL_PATHS,
    _SYSTEMCTL_PATHS,
    _SYSTEMD_PERSIST_UNIT,
    _find_trusted_binary,
    default_persist_via_alsactl,
)

__all__ = [
    "MixerApplyFn",
    "MixerProbeFn",
    "MixerRestoreFn",
    "MixerSanitySetup",
    "PersistFn",
    "ValidationProbeFn",
    "build_mixer_sanity_setup",
    "check_and_maybe_heal",
    "default_persist_via_alsactl",
    "detect_user_customization",
    "make_default_validation_probe_fn",
]
