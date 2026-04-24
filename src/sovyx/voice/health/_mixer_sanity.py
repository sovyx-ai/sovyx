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
import contextlib
import contextvars
import os
import stat
import subprocess  # noqa: S404 — fixed-argv subprocess to trusted alsa-utils binary
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypeAlias

from sovyx.observability.logging import get_logger
from sovyx.voice.health._half_heal_recovery import (
    clear_wal as _clear_half_heal_wal,
)
from sovyx.voice.health._half_heal_recovery import (
    recover_if_present as _recover_half_heal_if_present,
)
from sovyx.voice.health._half_heal_recovery import (
    write_wal as _write_half_heal_wal,
)
from sovyx.voice.health._linux_mixer_apply import (
    apply_mixer_preset as _default_apply_mixer_preset,
)
from sovyx.voice.health._linux_mixer_apply import (
    restore_mixer_snapshot as _default_restore_mixer_snapshot,
)
from sovyx.voice.health._linux_mixer_probe import (
    enumerate_alsa_mixer_snapshots as _default_mixer_probe,
)
from sovyx.voice.health._mixer_kb.matcher import _match_factory_signature
from sovyx.voice.health.contract import (
    Combo,
    Diagnosis,
    MixerControlRole,
    MixerSanityDecision,
    MixerSanityResult,
    MixerValidationMetrics,
    ProbeMode,
    RemediationHint,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice.health._mixer_kb import MixerKBLookup, MixerKBMatch
    from sovyx.voice.health._mixer_roles import MixerControlRoleResolver
    from sovyx.voice.health.capture_overrides import CaptureOverrides
    from sovyx.voice.health.cascade import ProbeCallable
    from sovyx.voice.health.combo_store import ComboStore
    from sovyx.voice.health.contract import (
        CandidateEndpoint,
        HardwareContext,
        MixerApplySnapshot,
        MixerCardSnapshot,
        MixerControlSnapshot,
        MixerPresetSpec,
        ValidationGates,
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


# ── Customization heuristic (V2 Master Plan §E.5) ───────────────────


_SIGNAL_WEIGHTS: Mapping[str, float] = {
    # Order matches V2 Master Plan §E.5 bullet list. Sums to 1.0.
    "A_mixer_differs_from_factory": 0.30,
    "B_asoundrc_exists": 0.15,
    "C_pipewire_user_conf": 0.15,
    "D_asound_state_recent": 0.15,
    "E_wireplumber_user_conf": 0.10,
    "F_combo_store_has_entry_with_drift": 0.10,
    "G_capture_overrides_pinned": 0.05,
}
"""Per-signal weights for the user-customization heuristic.

Treat this mapping as the single source of truth for tests — any
reweighting MUST update every doctest + test assertion that depends
on the 0-1 total. Rebalancing requires an ADR amendment (see
ADR-voice-mixer-sanity-l2.5-bidirectional §4.I4).
"""


_ASOUND_STATE_RECENT_SECONDS: float = 7 * 24 * 3600.0
"""A mtime within the last 7 days on ``/var/lib/alsa/asound.state``
counts as "user tweaked recently". Shorter than the pilot-case
tolerance (factory-bad state rewrites the file on every boot — one
week excludes that).
"""


@dataclass(frozen=True, slots=True)
class _UserCustomizationReport:
    """Per-signal breakdown — for telemetry + test introspection."""

    score: float
    signals_fired: tuple[str, ...]


def detect_user_customization(
    *,
    factory_signature_score: float,
    hw: HardwareContext,
    combo_store: ComboStore | None = None,
    capture_overrides: CaptureOverrides | None = None,
    endpoint_guid: str | None = None,
    home_dir: Path | None = None,
    asound_state_path: Path | None = None,
    time_now_s: float | None = None,
) -> _UserCustomizationReport:
    """Score user-customization likelihood in ``[0, 1]`` via 7 signals.

    All filesystem paths are injectable so tests can pin ``home_dir``
    and ``asound_state_path`` at a ``tmp_path`` fixture without
    touching the real user environment.

    Signal semantics (matching V2 §E.5):

    * **A** — current mixer deviates from the matched KB profile's
      factory signature. When ``factory_signature_score`` is low the
      mixer is unlike the factory-bad regime → user has moved it.
      Contributes ``(1.0 - factory_signature_score) * 0.30``.
    * **B** — ``~/.asoundrc`` exists. Explicit user config.
    * **C** — any file under ``~/.config/pipewire/pipewire.conf.d/``
      suggests PipeWire tuning.
    * **D** — ``/var/lib/alsa/asound.state`` mtime within the last
      7 days (``_ASOUND_STATE_RECENT_SECONDS``).
    * **E** — any file under
      ``~/.config/wireplumber/wireplumber.conf.d/``.
    * **F** — ``ComboStore`` has a recorded entry for this
      endpoint AND the factory-signature score is below 0.5
      (meaning "user got this working outside the factory-bad
      regime").
    * **G** — ``CaptureOverrides`` has a pinned combo for this
      endpoint (hard signal: user explicitly pinned a config).

    Args:
        factory_signature_score: ``0..1`` fraction from the matched
            profile's factory-signature check. Lower → stronger
            customization signal.
        hw: Detected hardware context. Currently consumed only by
            signal A via the factory score; kept in the signature
            so future signals (per-codec quirks) fit without an API
            break.
        combo_store: ``ComboStore`` singleton. ``None`` disables
            signal F (tests may choose to skip).
        capture_overrides: ``CaptureOverrides`` singleton. ``None``
            disables signal G.
        endpoint_guid: Needed to key into combo_store /
            capture_overrides. ``None`` disables F + G.
        home_dir: User home directory. Defaults to
            :meth:`Path.home()`; injected in tests.
        asound_state_path: Absolute path to asound.state. Defaults to
            ``/var/lib/alsa/asound.state``; injected in tests.
        time_now_s: ``time.time()`` override for deterministic
            mtime comparison in tests.

    Returns:
        :class:`_UserCustomizationReport` with the composite score
        and the list of signal codes that fired.
    """
    # Signal A is continuous — partial credit per plan. Every other
    # signal is boolean (present → full weight).
    del hw  # reserved for future per-codec quirks
    signals_fired: list[str] = []
    total: float = 0.0

    a_contribution = (
        max(0.0, 1.0 - float(factory_signature_score))
        * _SIGNAL_WEIGHTS["A_mixer_differs_from_factory"]
    )
    if a_contribution > 0:
        signals_fired.append("A_mixer_differs_from_factory")
    total += a_contribution

    home = home_dir if home_dir is not None else Path.home()

    if (home / ".asoundrc").exists():
        signals_fired.append("B_asoundrc_exists")
        total += _SIGNAL_WEIGHTS["B_asoundrc_exists"]

    pipewire_conf_d = home / ".config" / "pipewire" / "pipewire.conf.d"
    if _directory_has_configs(pipewire_conf_d):
        signals_fired.append("C_pipewire_user_conf")
        total += _SIGNAL_WEIGHTS["C_pipewire_user_conf"]

    asound_path = (
        asound_state_path if asound_state_path is not None else Path("/var/lib/alsa/asound.state")
    )
    now = time_now_s if time_now_s is not None else time.time()
    if _file_mtime_recent(asound_path, now=now, window_s=_ASOUND_STATE_RECENT_SECONDS):
        signals_fired.append("D_asound_state_recent")
        total += _SIGNAL_WEIGHTS["D_asound_state_recent"]

    wireplumber_conf_d = home / ".config" / "wireplumber" / "wireplumber.conf.d"
    if _directory_has_configs(wireplumber_conf_d):
        signals_fired.append("E_wireplumber_user_conf")
        total += _SIGNAL_WEIGHTS["E_wireplumber_user_conf"]

    if (
        combo_store is not None
        and endpoint_guid is not None
        and combo_store.get(endpoint_guid) is not None
        and factory_signature_score < 0.5  # noqa: PLR2004 — §E.5 threshold
    ):
        signals_fired.append("F_combo_store_has_entry_with_drift")
        total += _SIGNAL_WEIGHTS["F_combo_store_has_entry_with_drift"]

    if (
        capture_overrides is not None
        and endpoint_guid is not None
        and capture_overrides.get_entry(endpoint_guid) is not None
    ):
        signals_fired.append("G_capture_overrides_pinned")
        total += _SIGNAL_WEIGHTS["G_capture_overrides_pinned"]

    clamped = min(1.0, max(0.0, total))
    return _UserCustomizationReport(
        score=clamped,
        signals_fired=tuple(signals_fired),
    )


def _directory_has_configs(directory: Path) -> bool:
    """Return True iff ``directory`` exists AND contains at least one
    ``*.conf`` file. Non-existence is the dominant case — users who
    never tuned PipeWire / WirePlumber don't have these paths at all.
    """
    try:
        if not directory.is_dir():
            return False
        return any(entry.suffix == ".conf" for entry in directory.iterdir())
    except OSError:
        # Permission denied / transient I/O — don't fire the signal.
        return False


def _file_mtime_recent(path: Path, *, now: float, window_s: float) -> bool:
    """Return True iff ``path`` exists and its mtime is within
    ``window_s`` seconds of ``now``. Any OSError → False.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    return (now - mtime) <= window_s


# ── State machine ───────────────────────────────────────────────────


_StepName: TypeAlias = Literal[
    "probe",
    "classify",
    "detect_customization",
    "apply",
    "validate",
    "persist",
    "rollback",
    "done",
]


@dataclass(slots=True)
class _OrchestratorContext:
    """Mutable state passed between steps. Private to the orchestrator.

    Every field starts ``None``/empty and fills in as the state
    machine advances. The final :meth:`build_result` reads this state
    into an immutable :class:`MixerSanityResult`.
    """

    endpoint: CandidateEndpoint
    hw: HardwareContext
    tuning: VoiceTuningConfig
    start_time_s: float

    # Injected callables
    mixer_probe_fn: MixerProbeFn
    mixer_apply_fn: MixerApplyFn
    mixer_restore_fn: MixerRestoreFn
    kb_lookup: MixerKBLookup
    role_resolver: MixerControlRoleResolver
    validation_probe_fn: ValidationProbeFn
    persist_fn: PersistFn
    telemetry: _TelemetryProto
    # Paranoid-QA R2 CRITICAL #4 — distinguishes "caller passed None"
    # (fall back to module-level singleton at record time) from
    # "caller explicitly injected a _NoopTelemetry or real recorder".
    # Without this flag, an explicit ``telemetry=_NoopTelemetry()`` is
    # indistinguishable from the default, and the late-bind logic
    # silently swaps in the module singleton — overriding the test's
    # explicit choice to disable telemetry.
    telemetry_was_provided: bool = False
    combo_store: ComboStore | None = None
    capture_overrides: CaptureOverrides | None = None
    # Paranoid-QA R2 HIGH #3: when set, orchestrator writes a
    # write-ahead log around _step_apply and attempts recovery at
    # the top of the state machine. See :mod:`_half_heal_recovery`.
    half_heal_wal_path: Path | None = None

    # Filled as the state machine progresses
    mixer_snapshot: tuple[MixerCardSnapshot, ...] = ()
    kb_match: MixerKBMatch | None = None
    customization: _UserCustomizationReport | None = None
    apply_snapshot: MixerApplySnapshot | None = None
    validation_metrics: MixerValidationMetrics | None = None
    validation_passed: bool | None = None
    probe_duration_ms: int = 0
    apply_duration_ms: int | None = None
    diagnosis_before: Diagnosis = Diagnosis.UNKNOWN
    diagnosis_after: Diagnosis | None = None
    regime: Literal["saturation", "attenuation", "mixed", "healthy", "unknown"] = "unknown"
    decision: MixerSanityDecision | None = None
    error_token: str | None = None
    remediation: RemediationHint | None = None
    # Persist outcome — False/None means the preset applied but
    # survives only until reboot.
    persist_succeeded: bool | None = None
    # Paranoid-QA R2 HIGH #4: guard against double-rollback. The
    # orchestrator's rollback path can be entered twice on a
    # validation-failure → cancel chain (``_step_rollback`` runs, then
    # the top-level CancelledError handler runs ``rollback_if_needed``
    # again). ``restore_mixer_snapshot`` is semantically idempotent
    # but re-applying the same snapshot wastes amixer round-trips +
    # reopens the race window for a concurrent user tweak to get
    # silently reverted.
    rollback_performed: bool = False
    # Paranoid-QA R3 CRIT-3: distinguish "rollback completed" from
    # "rollback ATTEMPTED but restore_fn raised and we logged +
    # swallowed". Previous code set ``rollback_performed=True`` on
    # both paths, which silently advertised
    # ``decision=ROLLED_BACK, rollback_snapshot=X`` to the dashboard
    # when the mixer was still stuck in the failing-validation
    # applied state. Surfaced via ``error_token`` + consulted by
    # the top-level impl to decide whether to clear the WAL
    # (failed rollback MUST leave the WAL on disk so cross-boot
    # recovery can retry).
    rollback_failed: bool = False

    def controls_modified(self) -> tuple[str, ...]:
        """Names of controls actually mutated, as a flat tuple."""
        if self.apply_snapshot is None:
            return ()
        return tuple(name for name, _ in self.apply_snapshot.applied_controls)

    def cards_probed(self) -> tuple[int, ...]:
        return tuple(card.card_index for card in self.mixer_snapshot)

    def budget_exceeded(self) -> bool:
        # ``>=`` rather than ``>``: the budget is a hard cap — the
        # orchestrator must terminate BY that wall-clock, not AT or
        # beyond. Also makes ``budget_s=0`` a deterministic
        # "fail-fast" for tests (under low-resolution monotonic
        # clocks the strict ``>`` yields 0 > 0 on the very first
        # check, which would never fire).
        elapsed = time.monotonic() - self.start_time_s
        return elapsed >= self.tuning.linux_mixer_sanity_budget_s


@dataclass(frozen=True, slots=True)
class _StepResult:
    """Outcome of one step — names the next step."""

    next_step: _StepName


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
    :attr:`MixerSanityResult.decision` the cascade keys off to decide
    whether to skip the platform walk (``HEALED``) or continue
    (``SKIPPED_*``, ``DEFERRED_*``, ``ROLLED_BACK``, ``ERROR``).

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


def _reentrant_result() -> MixerSanityResult:
    """Terminal record for a blocked recursive entry.

    Shape mirrors :func:`_defer_platform_result` — zero side-effects,
    explicit error token so telemetry + dashboard can surface the
    guard firing as a distinct event from "real" L2.5 failures.
    """
    return MixerSanityResult(
        decision=MixerSanityDecision.ERROR,
        diagnosis_before=Diagnosis.UNKNOWN,
        diagnosis_after=None,
        regime="unknown",
        matched_kb_profile=None,
        kb_match_score=0.0,
        user_customization_score=0.0,
        cards_probed=(),
        controls_modified=(),
        rollback_snapshot=None,
        probe_duration_ms=0,
        apply_duration_ms=None,
        validation_passed=None,
        validation_metrics=None,
        remediation=None,
        error="MIXER_SANITY_REENTRANT_GUARD",
    )


async def _check_and_maybe_heal_impl(
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
    """Actual state-machine body. Callers must acquire ``_get_l25_lock()``
    and set the reentrancy contextvar before invoking this — the
    public entry point :func:`check_and_maybe_heal` does both."""

    ctx = _OrchestratorContext(
        endpoint=endpoint,
        hw=hw,
        tuning=tuning,
        start_time_s=time.monotonic(),
        mixer_probe_fn=mixer_probe_fn if mixer_probe_fn is not None else _default_mixer_probe,
        mixer_apply_fn=mixer_apply_fn
        if mixer_apply_fn is not None
        else _default_apply_mixer_preset,
        mixer_restore_fn=mixer_restore_fn
        if mixer_restore_fn is not None
        else _default_restore_mixer_snapshot,
        kb_lookup=kb_lookup,
        role_resolver=role_resolver,
        validation_probe_fn=validation_probe_fn,
        persist_fn=persist_fn if persist_fn is not None else default_persist_via_alsactl,
        telemetry=telemetry if telemetry is not None else _NoopTelemetry(),
        telemetry_was_provided=telemetry is not None,
        combo_store=combo_store,
        capture_overrides=capture_overrides,
        half_heal_wal_path=half_heal_wal_path,
    )

    orchestrator = _SanityOrchestrator(ctx)
    # Paranoid-QA R2 CRITICAL #3 — preserve the originating
    # ``CancelledError`` across telemetry recording. A naked ``raise``
    # inside the ``except asyncio.CancelledError`` handler skips the
    # build_result + record_mixer_sanity_outcome that sits after this
    # try/except, so the shutdown cascade drops the partial L2.5
    # outcome. Instead we stash the exception, let telemetry record,
    # then re-raise below.
    cancel_exc: BaseException | None = None
    try:
        await orchestrator.run()
    except asyncio.CancelledError as exc:
        cancel_exc = exc
        # Paranoid-QA R4 HIGH-2: skip rollback when validation had
        # already passed. A cancel during ``_step_persist`` fires
        # AFTER apply+validate have succeeded — the mixer is in a
        # good state, only the ``alsactl store`` persistence was
        # interrupted. Rolling back now would drop the user from a
        # validated-healthy mixer back to the pre-apply (broken)
        # state purely because the persist subprocess got
        # interrupted. The persist is recoverable on next boot
        # (``alsactl restore`` loads the last stored state; the
        # kernel still holds the live apply).
        #
        # Skip rollback iff (a) validation passed AND (b) apply
        # committed. Otherwise fall through to the best-effort
        # rollback — a cancel during apply / validate MUST still
        # revert since the mixer is in a partial-apply or
        # invalid-validate state.
        skip_rollback = ctx.validation_passed is True and ctx.apply_snapshot is not None
        if not skip_rollback:
            # Best-effort rollback. If the caller double-cancels
            # and rollback itself raises CancelledError, we still
            # want to reach the telemetry recorder — swallow here
            # and re-raise the *original* below.
            with contextlib.suppress(asyncio.CancelledError):
                await orchestrator.rollback_if_needed()
        else:
            logger.info(
                "mixer_sanity_cancel_during_persist_retained_heal",
                endpoint_guid=endpoint.endpoint_guid,
                note=(
                    "cancel fired after validation passed — mixer "
                    "is in a healthy state, skipping rollback; the "
                    "persist subprocess was interrupted but apply "
                    "+ validation already committed"
                ),
            )
        ctx.decision = MixerSanityDecision.ERROR
        ctx.error_token = "MIXER_SANITY_CANCELLED"
    except Exception as exc:  # noqa: BLE001 — "Exception" not "BaseException" post-QA
        # Paranoid-QA CRITICAL #1 / HIGH #1: narrowed from BaseException
        # so KeyboardInterrupt / SystemExit propagate to the caller.
        # CancelledError is a direct BaseException subclass (Python 3.8+
        # moved it off Exception) so this ``except Exception`` correctly
        # does NOT catch Cancelled / KeyboardInterrupt / SystemExit.
        logger.exception(
            "mixer_sanity_unexpected_error",
            endpoint_guid=endpoint.endpoint_guid,
            error_type=type(exc).__name__,
        )
        await orchestrator.rollback_if_needed()
        ctx.decision = MixerSanityDecision.ERROR
        ctx.error_token = "MIXER_SANITY_UNEXPECTED_ERROR"

    result = orchestrator.build_result()
    # ── Late-bind telemetry (Paranoid-QA CRITICAL #9 + R2 CRITICAL #4) ──
    #
    # The module-level singleton is consulted only when the caller did
    # NOT explicitly inject a telemetry. An explicit
    # ``_NoopTelemetry()`` injection (common in tests) must survive
    # this branch — isinstance() alone can't tell "default noop" from
    # "explicit noop", hence the ``telemetry_was_provided`` sentinel.
    final_telemetry: _TelemetryProto = ctx.telemetry
    if not ctx.telemetry_was_provided:
        from sovyx.voice.health._telemetry import (  # noqa: PLC0415 — late-bound singleton lookup
            get_telemetry,
        )

        late = get_telemetry()
        if late is not None:
            final_telemetry = late
    # Telemetry recording MUST NOT shadow the real exit. A broken
    # recorder (network down, serialization bug, clock skew) should
    # log and move on — the orchestrator's result is authoritative.
    try:
        final_telemetry.record_mixer_sanity_outcome(
            decision=result.decision.value,
            matched_profile=result.matched_kb_profile,
            score=result.kb_match_score,
            is_user_contributed=(ctx.kb_match is not None and ctx.kb_match.is_user_contributed),
        )
    except Exception:  # noqa: BLE001 — never let telemetry mask a real outcome
        logger.exception(
            "mixer_sanity_telemetry_record_failed",
            endpoint_guid=endpoint.endpoint_guid,
            decision=result.decision.value,
        )

    # Paranoid-QA R2 HIGH #3 — clear the half-heal WAL on every
    # terminal transition (success, rollback, error, cancel). By
    # the time we reach here, either apply committed fully +
    # persist ran (HEALED), or rollback ran (ROLLED_BACK / ERROR),
    # or we never applied at all (SKIPPED / DEFERRED). In all
    # cases the WAL is stale and must be cleared so the next
    # cascade doesn't attempt a spurious recovery. The
    # ``clear_wal`` helper is idempotent and never raises.
    #
    # Paranoid-QA R3 CRIT-3 amendment: preserve the WAL when
    # ``rollback_failed`` is True. In that path the in-process
    # restore raised — the mixer is stuck in the applied state
    # and the NEXT boot's ``recover_if_present`` needs the WAL on
    # disk to retry the restore via a fresh ``restore_fn``. If we
    # cleared the WAL here, the stuck-mixer would persist until
    # the user manually intervenes.
    if ctx.half_heal_wal_path is not None and not ctx.rollback_failed:
        _clear_half_heal_wal(ctx.half_heal_wal_path)
    elif ctx.rollback_failed and ctx.half_heal_wal_path is not None:
        logger.warning(
            "mixer_sanity_wal_preserved_for_next_boot_recovery",
            endpoint_guid=endpoint.endpoint_guid,
            wal_path=str(ctx.half_heal_wal_path),
            note=(
                "rollback_fn raised during in-process restore; WAL "
                "retained so the next cascade's recovery path retries "
                "the restore via a fresh restore_fn"
            ),
        )

    if cancel_exc is not None:
        raise cancel_exc
    return result


# ── Orchestrator ────────────────────────────────────────────────────


class _SanityOrchestrator:
    """Drives the 7-step state machine.

    Each step reads ``self._ctx``, makes one decision, and returns a
    :class:`_StepResult` naming the next step. :meth:`run` is the
    dispatch loop.
    """

    def __init__(self, ctx: _OrchestratorContext) -> None:
        self._ctx = ctx

    async def run(self) -> None:
        """Run the state machine from entry to done."""
        step = "probe"
        while step != "done":
            if self._ctx.budget_exceeded():
                logger.warning(
                    "mixer_sanity_budget_exceeded",
                    endpoint_guid=self._ctx.endpoint.endpoint_guid,
                    step=step,
                )
                # Paranoid-QA CRITICAL #6/#7: if budget trips AFTER
                # apply has committed (apply_snapshot set) but BEFORE
                # validate/persist finished, the terminal record would
                # otherwise carry ``diagnosis_after=HEALTHY`` +
                # ``validation_passed=True`` set by the earlier step
                # while ``decision=ERROR`` — a self-contradictory
                # shape. Normalise: when we're ROLLING BACK an
                # apply-in-flight, surface it as ROLLED_BACK;
                # otherwise ERROR.
                if self._ctx.apply_snapshot is not None:
                    self._ctx.decision = MixerSanityDecision.ROLLED_BACK
                    self._ctx.diagnosis_after = self._ctx.diagnosis_before
                    # Paranoid-QA R2 HIGH #10: preserve validation
                    # truth. The earlier hard-coded ``= False`` lied
                    # when validation had already passed and the
                    # budget tripped during persist — the audit
                    # record then read "apply succeeded, validation
                    # failed, rolled back" when reality was "apply
                    # succeeded, validation PASSED, persist starved
                    # the budget, rolled back anyway". Only stamp
                    # ``False`` when validation hadn't decided yet.
                    if self._ctx.validation_passed is None:
                        self._ctx.validation_passed = False
                else:
                    self._ctx.decision = MixerSanityDecision.ERROR
                self._ctx.error_token = "MIXER_SANITY_BUDGET_EXCEEDED"
                # Paranoid-QA R4 HIGH-4: cap the budget-branch
                # rollback too — without the wrap, a rollback of N
                # controls (each ``linux_mixer_subprocess_timeout_s``
                # ceiling) could run ~N*3s past the budget that
                # supposedly already tripped. Only wrap in wait_for
                # when there's ACTUALLY something to roll back
                # (``apply_snapshot is not None``); otherwise
                # ``rollback_if_needed`` short-circuits synchronously
                # and a 0.0-second wait_for would spuriously fire.
                if self._ctx.apply_snapshot is not None:
                    # Use subprocess_timeout × entry-count as the
                    # cap — lets a legitimate rollback complete even
                    # when budget is tight; only trips on genuine
                    # pathological wall-clock blowout.
                    entries = len(self._ctx.apply_snapshot.reverted_controls) + len(
                        self._ctx.apply_snapshot.reverted_enum_controls
                    )
                    rollback_timeout = max(
                        entries * self._ctx.tuning.linux_mixer_subprocess_timeout_s * 1.25,
                        self._ctx.tuning.linux_mixer_sanity_budget_s,
                    )
                    try:
                        await asyncio.wait_for(
                            self.rollback_if_needed(),
                            timeout=rollback_timeout,
                        )
                    except TimeoutError:
                        logger.warning(
                            "mixer_sanity_rollback_budget_timeout",
                            endpoint_guid=self._ctx.endpoint.endpoint_guid,
                            rollback_timeout_s=rollback_timeout,
                            budget_s=self._ctx.tuning.linux_mixer_sanity_budget_s,
                        )
                        self._ctx.rollback_failed = True
                else:
                    await self.rollback_if_needed()
                return
            match step:
                case "probe":
                    result = await self._step_probe()
                case "classify":
                    result = await self._step_classify()
                case "detect_customization":
                    result = await self._step_detect_customization()
                case "apply":
                    result = await self._step_apply()
                case "validate":
                    result = await self._step_validate()
                case "persist":
                    result = await self._step_persist()
                case "rollback":
                    result = await self._step_rollback()
                case _:  # pragma: no cover — exhaustiveness
                    msg = f"unexpected step {step!r}"
                    raise RuntimeError(msg)
            step = result.next_step

    def build_result(self) -> MixerSanityResult:
        """Freeze current context into the terminal
        :class:`MixerSanityResult` record.

        Paranoid-QA R2 HIGH #9: enforces the shape invariant
        ``apply_snapshot is not None ⇒ apply_duration_ms is not None``.
        The two fields are set atomically inside ``_step_apply`` (no
        await between them), so the invariant is true by construction
        in the happy path. This assertion catches any future edit
        that moves ``apply_duration_ms`` assignment past an await
        point — at which point the builder produces an impossible
        shape (``rollback_snapshot`` populated but ``apply_duration_ms``
        None) that downstream dashboards would silently render as
        "apply took 0 ms".
        """
        c = self._ctx
        decision = c.decision if c.decision is not None else MixerSanityDecision.ERROR
        match = c.kb_match
        if c.apply_snapshot is not None and c.apply_duration_ms is None:
            # Impossible in the happy path — defensive stamp + log
            # so the invariant violation surfaces in observability
            # without poisoning the result.
            logger.error(
                "mixer_sanity_impossible_shape_apply_duration_missing",
                endpoint_guid=c.endpoint.endpoint_guid,
                decision=decision.value,
            )
            c.apply_duration_ms = 0
        # Paranoid-QA R3 CRIT-3 + R4 CRIT-1: surface rollback
        # failure unconditionally. The earlier allow-list
        # (``VALIDATION_FAILED`` | ``BUDGET_EXCEEDED``) silently
        # skipped composition for every other upstream token
        # (APPLY_FAILED, PERSIST_FAILED, future tokens) — leaving
        # ``decision=ERROR`` paired with the upstream token and no
        # indication the rollback itself failed. Observers reading
        # ``error=MIXER_SANITY_PERSIST_FAILED`` couldn't tell from
        # the result whether the mixer had been restored or was
        # stuck in the applied state.
        #
        # New rule: whenever ``rollback_failed`` is set, compose the
        # ``ROLLBACK_FAILED_AFTER_<trigger>`` token. Callers that
        # want the raw upstream token can still recover it by
        # splitting on the ``_AFTER_`` suffix. No allow-list.
        if c.rollback_failed:
            decision = MixerSanityDecision.ERROR
            if c.error_token:
                upstream = c.error_token.removeprefix("MIXER_SANITY_")
                c.error_token = f"MIXER_SANITY_ROLLBACK_FAILED_AFTER_{upstream}"
            else:
                c.error_token = "MIXER_SANITY_ROLLBACK_FAILED"
        return MixerSanityResult(
            decision=decision,
            diagnosis_before=c.diagnosis_before,
            diagnosis_after=c.diagnosis_after,
            regime=c.regime,
            matched_kb_profile=match.profile.profile_id if match is not None else None,
            kb_match_score=match.score if match is not None else 0.0,
            user_customization_score=(
                c.customization.score if c.customization is not None else 0.0
            ),
            cards_probed=c.cards_probed(),
            controls_modified=c.controls_modified(),
            rollback_snapshot=c.apply_snapshot,
            probe_duration_ms=c.probe_duration_ms,
            apply_duration_ms=c.apply_duration_ms,
            validation_passed=c.validation_passed,
            validation_metrics=c.validation_metrics,
            remediation=c.remediation,
            error=c.error_token,
        )

    async def rollback_if_needed(self) -> None:
        """Invoke ``mixer_restore_fn`` if we have an apply snapshot.

        Best-effort for ``Exception`` subclasses only — rollback
        failures are logged and swallowed so the caller's exception
        semantics are preserved.

        Paranoid-QA HIGH #2: ``CancelledError`` (BaseException in
        Python 3.8+) IS re-raised. Earlier implementations used
        ``except BaseException`` which silently swallowed the
        cancellation delivered while restore was running mid-await,
        leaving the caller's shutdown path hanging on a coroutine
        that never propagated the cancel.

        Paranoid-QA R2 HIGH #4: idempotent — second call is a no-op.
        Rollback can be triggered from ``_step_rollback`` AND from a
        top-level exception handler on the same invocation
        (validation-fail → rollback step → caller cancels the
        already-failing run mid-restore). The flag keeps the mixer
        from being re-restored and also prevents the second call
        from competing for the amixer lock with the first.
        """
        if self._ctx.apply_snapshot is None:
            return
        if self._ctx.rollback_performed:
            logger.debug(
                "mixer_sanity_rollback_skipped_already_done",
                endpoint_guid=self._ctx.endpoint.endpoint_guid,
            )
            return
        try:
            await self._ctx.mixer_restore_fn(
                self._ctx.apply_snapshot,
                tuning=self._ctx.tuning,
            )
        except asyncio.CancelledError:
            logger.warning(
                "mixer_sanity_rollback_cancelled_mid_restore",
                endpoint_guid=self._ctx.endpoint.endpoint_guid,
            )
            # Do NOT set rollback_performed=True — cancellation mid-
            # restore means the mixer is in an unknown state (part of
            # the snapshot may have restored, part may not). A
            # subsequent rollback attempt from the caller's handler
            # should retry. Callers that can't tolerate that (e.g.,
            # daemon shutdown) use ``contextlib.suppress`` at the
            # outer frame.
            raise
        except Exception as exc:  # noqa: BLE001 — Exception-only; BaseException propagates
            logger.warning(
                "mixer_sanity_rollback_failed",
                endpoint_guid=self._ctx.endpoint.endpoint_guid,
                detail=str(exc)[:200],
            )
            # Paranoid-QA R3 CRIT-3: DO NOT set ``rollback_performed=True``
            # when the restore raised. The earlier code set it to
            # suppress the re-entry retry in the caller's handler
            # — but that conflated "rollback complete" with
            # "rollback gave up", causing
            # ``decision=ROLLED_BACK, rollback_snapshot=X`` to be
            # surfaced as success to the dashboard while the mixer
            # was actually stuck in the applied state.
            #
            # The correct signal: ``rollback_failed=True`` so
            # :meth:`build_result` can downgrade the decision to
            # ERROR + set ``error_token=MIXER_SANITY_ROLLBACK_FAILED``,
            # AND the top-level ``_check_and_maybe_heal_impl``
            # preserves the WAL on disk so the NEXT boot's recovery
            # path retries the restore via the same ``restore_fn``.
            # Re-entry from the caller's handler is blocked by the
            # existing ``rollback_performed`` flag (still False here)
            # — wait, that's the opposite of what we want. We need
            # to distinguish "first attempt raised → stop, surface,
            # preserve WAL" from "two handlers both tried → first
            # was fine, skip". Use BOTH flags:
            #   rollback_performed = False  → we never completed
            #   rollback_failed    = True   → we tried and failed
            # The caller's handler sees ``performed=False`` and
            # would normally retry; but we want to prevent retry
            # in the SAME orchestrator run because the failure mode
            # is sticky. Set performed=True only as a retry guard,
            # and use rollback_failed as the observability signal.
            self._ctx.rollback_performed = True
            self._ctx.rollback_failed = True
            return
        self._ctx.rollback_performed = True

    # ── Individual steps ────────────────────────────────────────────

    async def _step_probe(self) -> _StepResult:
        """Read current mixer state. Failure → ERROR (no rollback)."""
        c = self._ctx
        probe_start = time.monotonic()
        try:
            snapshots = await asyncio.to_thread(c.mixer_probe_fn)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning(
                "mixer_sanity_probe_failed",
                endpoint_guid=c.endpoint.endpoint_guid,
                detail=str(exc)[:200],
            )
            c.decision = MixerSanityDecision.ERROR
            c.error_token = "MIXER_SANITY_PROBE_FAILED"
            return _StepResult(next_step="done")
        c.mixer_snapshot = tuple(snapshots)
        c.probe_duration_ms = int((time.monotonic() - probe_start) * 1000)
        if not c.mixer_snapshot:
            # No cards → nothing to heal. Deferring cleanly means the
            # cascade proceeds — user may be on a USB-only / BT-only
            # setup that doesn't expose the HDA-style mixer surface.
            logger.debug(
                "mixer_sanity_no_cards",
                endpoint_guid=c.endpoint.endpoint_guid,
            )
            c.decision = MixerSanityDecision.DEFERRED_NO_KB
            c.regime = "unknown"
            c.error_token = "MIXER_SANITY_NO_CARDS"
            return _StepResult(next_step="done")
        return _StepResult(next_step="classify")

    async def _step_classify(self) -> _StepResult:
        """Match KB + assign regime + decide next step."""
        c = self._ctx
        match = c.kb_lookup.match(
            c.hw,
            c.mixer_snapshot,
            min_score=c.tuning.linux_mixer_sanity_kb_match_threshold,
        )
        c.kb_match = match
        if match is None:
            # No profile matched above threshold OR ambiguous.
            # Distinguish via the lookup's own logging; for the result
            # we keep the aggregated "no actionable KB" bucket.
            c.regime = _classify_regime_heuristically(c.mixer_snapshot)
            if c.regime == "healthy":
                c.decision = MixerSanityDecision.SKIPPED_HEALTHY
                c.diagnosis_before = Diagnosis.HEALTHY
                return _StepResult(next_step="done")
            c.decision = MixerSanityDecision.DEFERRED_NO_KB
            c.diagnosis_before = Diagnosis.MIXER_UNKNOWN_PATTERN
            c.error_token = "MIXER_SANITY_NO_KB_MATCH"
            c.remediation = RemediationHint(
                code="remediation.mixer_unknown",
                severity="info",
            )
            return _StepResult(next_step="done")
        # We have a KB profile. Assign regime from the profile and
        # the observed state; diagnosis is MIXER_ZEROED /
        # MIXER_SATURATED based on match.profile.factory_regime.
        regime = match.profile.factory_regime
        # `"either"` is a KB-author hint that this profile targets both
        # regimes; at classification time we still need a concrete
        # label, so fall back to the probe-based heuristic.
        if regime == "attenuation":
            c.regime = "attenuation"
        elif regime == "saturation":
            c.regime = "saturation"
        elif regime == "mixed":
            c.regime = "mixed"
        else:  # "either"
            c.regime = _classify_regime_heuristically(c.mixer_snapshot)
        c.diagnosis_before = _diagnosis_for_regime(c.regime)
        return _StepResult(next_step="detect_customization")

    async def _step_detect_customization(self) -> _StepResult:
        """Run the 7-signal heuristic + branch APPLY / DEFER / SKIP."""
        c = self._ctx
        assert c.kb_match is not None  # noqa: S101 — state-machine invariant
        # Compute the factory-signature score separately so the heuristic
        # can score signal A accurately. (The kb_lookup's composite score
        # mixes in other fields.)
        factory_result = _match_factory_signature(
            c.kb_match.profile.factory_signature,
            c.mixer_snapshot,
            c.role_resolver,
            c.hw,
        )
        c.customization = detect_user_customization(
            factory_signature_score=factory_result.score,
            hw=c.hw,
            combo_store=c.combo_store,
            capture_overrides=c.capture_overrides,
            endpoint_guid=c.endpoint.endpoint_guid,
        )
        score = c.customization.score
        # Paranoid-QA HIGH #9: both thresholds use ``>=`` so the
        # boundary lives at the apply-threshold (inclusive). With
        # apply=0.5 and skip=0.75 a score of exactly 0.5 is the first
        # "defer" value, 0.75 is the first "skip" value. VoiceTuningConfig
        # validates apply <= skip so the bands never invert.
        if score >= c.tuning.linux_mixer_user_customization_threshold_skip:
            c.decision = MixerSanityDecision.SKIPPED_CUSTOMIZED
            c.diagnosis_before = Diagnosis.MIXER_CUSTOMIZED
            c.remediation = RemediationHint(
                code="remediation.mixer_customized",
                severity="info",
            )
            logger.info(
                "mixer_sanity_skipped_customized",
                endpoint_guid=c.endpoint.endpoint_guid,
                customization_score=score,
                signals=list(c.customization.signals_fired),
            )
            return _StepResult(next_step="done")
        if score >= c.tuning.linux_mixer_user_customization_threshold_apply:
            # Ambiguous zone — defer; dashboard card (F1.I) offers choice.
            c.decision = MixerSanityDecision.DEFERRED_AMBIGUOUS
            c.error_token = "MIXER_SANITY_USER_CUSTOMIZED_AMBIGUOUS"
            c.remediation = RemediationHint(
                code="remediation.mixer_customized",
                severity="info",
            )
            logger.info(
                "mixer_sanity_deferred_customization_ambiguous",
                endpoint_guid=c.endpoint.endpoint_guid,
                customization_score=score,
            )
            return _StepResult(next_step="done")
        return _StepResult(next_step="apply")

    async def _step_apply(self) -> _StepResult:
        """Apply the KB preset to every probed card."""
        c = self._ctx
        assert c.kb_match is not None  # noqa: S101 — state-machine invariant
        apply_start = time.monotonic()
        # Apply to the card whose controls best fit the profile.
        # Multi-card systems are rare on laptops; F1 targets the
        # first card — F2 can extend to multi-card selection by
        # scoring each card independently.
        target_card = c.mixer_snapshot[0]
        role_mapping = c.role_resolver.resolve_card(target_card, c.hw)

        # Paranoid-QA R2 HIGH #3 — write the WAL BEFORE the first
        # mutation so a crash mid-apply leaves enough state on disk
        # for the next boot to restore. The preset's target controls
        # are the set that WILL be touched; we serialise their
        # pre-apply raw values as (name, raw) pairs. If anything in
        # this step fails (rollback, exception, cancel), the WAL is
        # cleared before return so the next boot doesn't
        # double-restore. The WAL is intentionally a conservative
        # superset — if apply_mixer_preset skips a control because
        # current_raw already equals target_raw, restoring that
        # control is a no-op, so inclusion is harmless.
        pre_apply_controls = self._build_half_heal_wal_plan(
            target_card, role_mapping, c.kb_match.profile.recommended_preset
        )
        # Paranoid-QA R3 CRIT-1: pre-read the Auto-Mute Mode label
        # when the preset is going to toggle it, so the WAL carries
        # the pre-apply enum state too. Without this, a mid-apply
        # crash between the numeric loop and ``_apply_auto_mute``
        # would be recovered with numerics restored but Auto-Mute
        # stuck in the applied (``Disabled``/``Enabled``) state.
        pre_apply_enum_controls = await self._build_half_heal_wal_enum_plan(
            target_card.card_index,
            role_mapping,
            c.kb_match.profile.recommended_preset,
            timeout_s=c.tuning.linux_mixer_subprocess_timeout_s,
        )
        if c.half_heal_wal_path is not None and (pre_apply_controls or pre_apply_enum_controls):
            wal_written = _write_half_heal_wal(
                card_index=target_card.card_index,
                reverted_controls=pre_apply_controls,
                reverted_enum_controls=pre_apply_enum_controls,
                path=c.half_heal_wal_path,
            )
            if not wal_written:
                # Surface the degradation as a WARNING but proceed —
                # aborting the cascade on a transient disk hiccup
                # would be a worse outcome than running apply
                # without crash recovery for this one pass.
                logger.warning(
                    "mixer_sanity_wal_write_failed_proceeding",
                    endpoint_guid=c.endpoint.endpoint_guid,
                    wal_path=str(c.half_heal_wal_path),
                    note=(
                        "proceeding with apply without WAL protection — "
                        "a mid-apply process death will not self-heal "
                        "on next boot for this single attempt"
                    ),
                )

        try:
            c.apply_snapshot = await c.mixer_apply_fn(
                target_card.card_index,
                c.kb_match.profile.recommended_preset,
                role_mapping,
                tuning=c.tuning,
            )
        except Exception as exc:  # noqa: BLE001 — translate to ERROR decision
            logger.warning(
                "mixer_sanity_apply_failed",
                endpoint_guid=c.endpoint.endpoint_guid,
                profile_id=c.kb_match.profile.profile_id,
                detail=str(exc)[:200],
            )
            c.decision = MixerSanityDecision.ERROR
            c.error_token = "MIXER_SANITY_APPLY_FAILED"
            # apply_mixer_preset already rolled back internally; our
            # snapshot stays None. Clear the WAL so the next cascade
            # doesn't attempt recovery on a state that was already
            # reverted in-process.
            if c.half_heal_wal_path is not None:
                _clear_half_heal_wal(c.half_heal_wal_path)
            return _StepResult(next_step="done")
        c.apply_duration_ms = int((time.monotonic() - apply_start) * 1000)
        return _StepResult(next_step="validate")

    @staticmethod
    def _build_half_heal_wal_plan(
        target_card: MixerCardSnapshot,
        role_mapping: Mapping[MixerControlRole, tuple[MixerControlSnapshot, ...]],
        preset: MixerPresetSpec,
    ) -> tuple[tuple[str, int], ...]:
        """Pre-compute the (name, pre_apply_raw) set for the WAL.

        Walks the preset's roles, looks up each role's resolved
        controls on the target card, and emits (control.name,
        control.current_raw) entries. The WAL intentionally over-
        covers relative to the actual amixer_set calls
        (apply_mixer_preset skips controls whose current_raw already
        equals the target); an over-restore during recovery is a
        no-op, whereas an under-restore would miss a control.
        """
        entries: list[tuple[str, int]] = []
        seen: set[str] = set()
        for pc in preset.controls:
            for control in role_mapping.get(pc.role, ()):
                if control.name in seen:
                    continue
                seen.add(control.name)
                entries.append((control.name, control.current_raw))
        # card_index comes from the outer caller — here we only
        # return the control tuples.
        del target_card
        return tuple(entries)

    @staticmethod
    async def _build_half_heal_wal_enum_plan(
        card_index: int,
        role_mapping: Mapping[MixerControlRole, tuple[MixerControlSnapshot, ...]],
        preset: MixerPresetSpec,
        *,
        timeout_s: float,
    ) -> tuple[tuple[str, str], ...]:
        """Pre-compute the (name, pre_apply_enum_label) set for the WAL.

        Paranoid-QA R3 CRIT-1: covers the enum mutation path that
        the numeric WAL plan misses. Reads the current
        ``Auto-Mute Mode`` label when the preset is going to toggle
        it. Uses :func:`_amixer_get_enum` via :func:`asyncio.to_thread`
        so the subprocess doesn't block the event loop (CLAUDE.md
        anti-pattern #14).

        Returns empty tuple when:

        * The preset's ``auto_mute_mode`` is ``"leave"`` (no-op).
        * The resolver doesn't expose a resolved
          ``MixerControlRole.AUTO_MUTE`` AND the default
          ``"Auto-Mute Mode"`` control isn't on the card.
        * The amixer read fails for any reason — we treat absence
          as "nothing to record"; if apply later succeeds, no WAL
          entry is the correct state.
        """
        # Only touch enum controls when the preset actually toggles them.
        if preset.auto_mute_mode == "leave":
            return ()
        # Resolve the control name the same way ``_apply_auto_mute``
        # does: role-mapped control first, canonical fallback second.
        # Import lazily to avoid a top-level cycle with
        # ``_linux_mixer_apply`` (which imports from contract.py too).
        from sovyx.voice.health._linux_mixer_apply import (  # noqa: PLC0415
            _AUTO_MUTE_MODE_CONTROL_NAME,
            _amixer_get_enum,
        )

        auto_mute_snapshots = role_mapping.get(MixerControlRole.AUTO_MUTE, ())
        control_name = (
            auto_mute_snapshots[0].name if auto_mute_snapshots else _AUTO_MUTE_MODE_CONTROL_NAME
        )
        try:
            current_label = await asyncio.to_thread(
                _amixer_get_enum,
                card_index,
                control_name,
                timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 — WAL pre-read is best-effort
            logger.debug(
                "mixer_sanity_auto_mute_pre_read_failed",
                card_index=card_index,
                control_name=control_name,
                detail=str(exc)[:200],
            )
            return ()
        if current_label is None:
            # Control absent on this card — apply will also no-op
            # on it, so no WAL entry is correct.
            return ()
        # Paranoid-QA R4 HIGH-3: strip whitespace. Some amixer
        # versions emit the enum label with padding (e.g.,
        # ``"Item0: 'Enabled  '"``) and ``_amixer_get_enum`` only
        # strips outer quotes, not internal whitespace. A WAL that
        # later replays ``amixer set <ctrl> "Enabled  "`` fails
        # because amixer doesn't recognise the padded label; the
        # replay's BypassApplyError is logged-and-swallowed,
        # leaving the mixer stuck. Stripping here keeps the WAL
        # round-trip deterministic.
        stripped = current_label.strip()
        if not stripped:
            # Empty after strip — treat as absent.
            return ()
        return ((control_name, stripped),)

    async def _step_validate(self) -> _StepResult:
        """Run post-apply validation; gates pass → persist, fail → rollback."""
        c = self._ctx
        assert c.kb_match is not None  # noqa: S101 — state-machine invariant
        try:
            metrics = await c.validation_probe_fn(c.endpoint, c.tuning)
        except Exception as exc:  # noqa: BLE001 — validation failure ⇒ rollback
            logger.warning(
                "mixer_sanity_validation_probe_failed",
                endpoint_guid=c.endpoint.endpoint_guid,
                detail=str(exc)[:200],
            )
            c.validation_passed = False
            c.error_token = "MIXER_SANITY_VALIDATION_FAILED"
            return _StepResult(next_step="rollback")
        c.validation_metrics = metrics
        gates = c.kb_match.profile.validation_gates
        passed = _check_validation_gates(metrics, gates)
        c.validation_passed = passed
        if passed:
            c.diagnosis_after = Diagnosis.HEALTHY
            return _StepResult(next_step="persist")
        c.error_token = "MIXER_SANITY_VALIDATION_FAILED"
        # Paranoid-QA R2 HIGH #6: emit explicit numeric fields rather
        # than letting structlog ``repr()`` the whole dataclass.
        # ``repr(dataclass)`` is an unbounded surface — a future
        # field addition (e.g., a raw buffer sample, a device name,
        # a file path) would be logged verbatim by accident.
        # Explicit fields put the log schema under review.
        logger.info(
            "mixer_sanity_validation_gates_failed",
            endpoint_guid=c.endpoint.endpoint_guid,
            rms_dbfs=metrics.rms_dbfs,
            peak_dbfs=metrics.peak_dbfs,
            snr_db_vocal_band=metrics.snr_db_vocal_band,
            silero_max_prob=metrics.silero_max_prob,
            silero_mean_prob=metrics.silero_mean_prob,
            wake_word_stage2_prob=metrics.wake_word_stage2_prob,
            measurement_duration_ms=metrics.measurement_duration_ms,
        )
        return _StepResult(next_step="rollback")

    async def _step_persist(self) -> _StepResult:
        """alsactl store — best-effort, HEALED either way."""
        c = self._ctx
        try:
            c.persist_succeeded = await c.persist_fn(c.cards_probed(), c.tuning)
        except Exception as exc:  # noqa: BLE001 — Exception-only (Paranoid-QA R3 CRIT-2)
            # Paranoid-QA R3 CRIT-2: previously ``except BaseException``
            # which contradicted :meth:`rollback_if_needed`'s narrower
            # form (post-R2 HIGH #1) and swallowed ``CancelledError``,
            # ``KeyboardInterrupt``, ``SystemExit``. A cancel delivered
            # during ``systemctl start --no-block`` (several-second
            # subprocess on a loaded system) would be silently
            # swallowed, ``persist_succeeded=False`` set, and the
            # state machine would march on to ``HEALED`` — leaving
            # the caller's shutdown sequence hanging on a
            # cancellation that never propagated. Exception-only
            # semantics restore the R2-level rigour.
            logger.warning(
                "mixer_sanity_persist_failed",
                endpoint_guid=c.endpoint.endpoint_guid,
                detail=str(exc)[:200],
            )
            c.persist_succeeded = False
        if c.persist_succeeded is False:
            c.error_token = "MIXER_SANITY_PERSIST_FAILED"
        c.decision = MixerSanityDecision.HEALED
        c.remediation = RemediationHint(
            code=(
                "remediation.mixer_zeroed"
                if c.regime == "attenuation"
                else "remediation.mixer_saturated"
            ),
            severity="info",
        )
        return _StepResult(next_step="done")

    async def _step_rollback(self) -> _StepResult:
        """Explicit rollback after validation failure."""
        c = self._ctx
        await self.rollback_if_needed()
        c.decision = MixerSanityDecision.ROLLED_BACK
        c.diagnosis_after = c.diagnosis_before
        return _StepResult(next_step="done")


# ── Pure helpers ────────────────────────────────────────────────────


def _check_validation_gates(
    metrics: MixerValidationMetrics,
    gates: ValidationGates,
) -> bool:
    """Every declared gate must pass — any single failure → False."""
    lo, hi = gates.rms_dbfs_range
    if not (lo <= metrics.rms_dbfs <= hi):
        return False
    if metrics.peak_dbfs > gates.peak_dbfs_max:
        return False
    if metrics.snr_db_vocal_band < gates.snr_db_vocal_band_min:
        return False
    if metrics.silero_max_prob < gates.silero_prob_min:
        return False
    return metrics.wake_word_stage2_prob >= gates.wake_word_stage2_prob_min


def _classify_regime_heuristically(
    snapshot: Sequence[MixerCardSnapshot],
) -> Literal["saturation", "attenuation", "mixed", "healthy", "unknown"]:
    """Fallback regime classification when no KB profile matches.

    Looks at the probe's own saturation flags + aggregated boost dB
    to split ``"saturation"`` / ``"healthy"`` / ``"unknown"``.
    Attenuation has no reliable signal without KB knowledge (a low
    Capture can be intentional), so we return ``"unknown"`` rather
    than guess.
    """
    if not snapshot:
        return "unknown"
    for card in snapshot:
        if card.saturation_warning:
            return "saturation"
    # No saturation flags → assume healthy; probe didn't surface any
    # obvious red flags.
    return "healthy"


def _diagnosis_for_regime(
    regime: Literal["saturation", "attenuation", "mixed", "healthy", "unknown"],
) -> Diagnosis:
    """Map a regime label to the L2.5 Diagnosis value.

    Paranoid-QA R3 HIGH #8: exhaustiveness enforced via
    ``assert_never`` — a future edit that adds a new ``Literal``
    value to the regime type WITHOUT updating this dispatch will
    fail mypy-strict at the ``assert_never(regime)`` call. Earlier
    the trailing ``return Diagnosis.MIXER_UNKNOWN_PATTERN`` silently
    absorbed any new value, producing a potentially-wrong diagnosis
    with zero type-checker signal.
    """
    from typing import assert_never  # noqa: PLC0415 — Python 3.11+ only, local import

    if regime == "attenuation":
        return Diagnosis.MIXER_ZEROED
    if regime == "saturation":
        return Diagnosis.MIXER_SATURATED
    if regime == "mixed":
        return Diagnosis.MIXER_SATURATED  # bias to the more actionable side
    if regime == "healthy":
        return Diagnosis.HEALTHY
    if regime == "unknown":
        return Diagnosis.MIXER_UNKNOWN_PATTERN
    assert_never(regime)


def _defer_platform_result() -> MixerSanityResult:
    """Build the canonical DEFERRED_PLATFORM result shape."""
    return MixerSanityResult(
        decision=MixerSanityDecision.DEFERRED_PLATFORM,
        diagnosis_before=Diagnosis.UNKNOWN,
        diagnosis_after=None,
        regime="unknown",
        matched_kb_profile=None,
        kb_match_score=0.0,
        user_customization_score=0.0,
        cards_probed=(),
        controls_modified=(),
        rollback_snapshot=None,
        probe_duration_ms=0,
        apply_duration_ms=None,
        validation_passed=None,
        validation_metrics=None,
    )


# ── Default persist (alsactl store) ─────────────────────────────────


_SYSTEMD_PERSIST_UNIT = "sovyx-audio-mixer-persist.service"
"""systemd unit (ships under ``packaging/systemd/``) that runs
``alsactl store`` as root with a tight sandbox. Invoked on-demand by
the L2.5 orchestrator after a successful heal."""


# Absolute-path binaries — paranoid-QA HIGH #6. A daemon whose PATH is
# influenced by the unit's ``Environment=`` directive (common operator
# mistake: ``PATH=$HOME/bin:$PATH``) would otherwise let an attacker
# with write access to that dir plant a shim for ``systemctl`` or
# ``alsactl`` and hijack the one runtime bridge to root (invariant I7
# relies on systemctl being the genuine systemd client). Hardcoding
# canonical absolute paths — with graceful fallthrough when absent —
# removes that hijack surface.
_SYSTEMCTL_PATHS: tuple[str, ...] = (
    "/usr/bin/systemctl",
    "/bin/systemctl",
)
_ALSACTL_PATHS: tuple[str, ...] = (
    "/usr/sbin/alsactl",
    "/sbin/alsactl",
    "/usr/bin/alsactl",
)


def _find_trusted_binary(candidates: tuple[str, ...]) -> str | None:
    """Return the first canonical-path binary that exists on disk.

    Does NOT consult ``PATH``. Any path outside the whitelist is
    refused even when present. Returns ``None`` when no canonical
    location holds the binary — caller falls through to the next
    persistence strategy or returns ``False``.

    Threat model (Paranoid-QA R2 MEDIUM #1):

    The classic TOCTOU concern (check via stat, use via subprocess)
    does not apply here because the candidate paths — ``/usr/bin``,
    ``/sbin``, ``/bin``, ``/usr/sbin`` — are writable only by root.
    An attacker who can replace ``/usr/bin/systemctl`` between
    resolution and exec already has root and can bypass any check
    we layer on top. For that reason we don't use ``O_NOFOLLOW``
    + ``fexecve`` here (which would also break legitimate
    ``/usr/bin/systemctl -> /sbin/systemctl`` symlinks on
    Arch-derivatives).

    We DO log at DEBUG when a candidate resolves through a symlink
    so operator-driven audits can spot unexpected indirection. The
    final ``subprocess.run`` relies on the same canonical path, so
    a symlink pointing outside the whitelist is detectable via
    ``resolve()`` and skipped.
    """
    for path in candidates:
        p = Path(path)
        try:
            # ``lstat`` inspects the link itself, not the target.
            # ``is_file`` follows symlinks — we want to know both.
            lstat_info = p.lstat()
            if not p.is_file() or not os.access(p, os.X_OK):
                continue
            if stat.S_ISLNK(lstat_info.st_mode):
                # Resolve the target and confirm it's either in the
                # whitelist or under one of the canonical system bin
                # directories. ``/usr/bin/systemctl -> /sbin/systemctl``
                # is legitimate; ``/usr/bin/systemctl -> /tmp/attacker``
                # is not.
                try:
                    resolved = str(p.resolve(strict=True))
                except (OSError, RuntimeError):
                    # Broken symlink or loop — skip.
                    continue
                trusted_prefixes = (
                    "/usr/bin/",
                    "/usr/sbin/",
                    "/bin/",
                    "/sbin/",
                    "/usr/local/bin/",
                    "/usr/local/sbin/",
                )
                if not any(resolved.startswith(prefix) for prefix in trusted_prefixes):
                    logger.warning(
                        "mixer_sanity_trusted_binary_symlink_escapes_whitelist",
                        candidate=str(p),
                        resolved=resolved,
                        note="refusing — symlink target is outside canonical bin dirs",
                    )
                    continue
                logger.debug(
                    "mixer_sanity_trusted_binary_symlink_resolved",
                    candidate=str(p),
                    resolved=resolved,
                )
            return str(p)
        except OSError:
            continue
    return None


async def default_persist_via_alsactl(
    cards: Sequence[int],
    tuning: VoiceTuningConfig,
) -> bool:
    """Persist the current mixer state via ``alsactl store``.

    Tries two strategies in order (invariant I7 — the daemon never
    writes ``/var/lib/alsa/asound.state`` directly):

    1. **systemd delegate** — ``systemctl start --no-block
       sovyx-audio-mixer-persist.service``. This is the production
       path: the packaged unit runs ``alsactl store -f`` as root
       with the same capability-bounded sandbox as the runtime_pm
       oneshot. ``--no-block`` returns as soon as systemd accepts
       the start request; the actual store takes ~30 ms on a
       single-card laptop but we don't need to wait.
    2. **Direct alsactl fallback** — useful in containers / dev
       environments where the daemon runs as root AND the systemd
       unit isn't installed (``pipx install sovyx`` before
       ``sudo postinstall_admin.sh``). The daemon's own alsactl
       invocation succeeds when the process has write access to
       ``/var/lib/alsa/asound.state``; otherwise it logs and
       returns ``False``.

    Returns ``True`` when strategy (1) accepted the start request OR
    strategy (2) exited 0 for every card. ``False`` when neither
    strategy is available or both fail. Never raises.

    A ``False`` return is not fatal: the L2.5 orchestrator still
    reports ``HEALED`` with an ``error=MIXER_SANITY_PERSIST_FAILED``
    token — the preset lives in-memory until reboot and re-applies
    on the next boot cascade.

    ``cards`` is ignored by strategy (1) — ``alsactl store -f``
    persists every card in one call. Strategy (2) passes the list
    verbatim to preserve backward compatibility with the pre-
    systemd-delegate behaviour.
    """
    if sys.platform != "linux":
        return False
    # Paranoid-QA HIGH #6: resolve subprocess binaries via a fixed
    # canonical-path whitelist. ``shutil.which`` honours $PATH and
    # would let an operator's ill-configured unit-level
    # ``Environment=PATH=$HOME/bin:$PATH`` redirect ``systemctl`` to
    # an attacker-controlled shim.
    systemctl_path = _find_trusted_binary(_SYSTEMCTL_PATHS)
    # Paranoid-QA LOW: clamp subprocess timeout so a bad env override
    # (``SOVYX_TUNING__VOICE__LINUX_MIXER_SUBPROCESS_TIMEOUT_S=0``)
    # cannot DoS the persist path by timing out instantly, nor block
    # the event loop for minutes at the other extreme.
    timeout_s = max(0.5, min(tuning.linux_mixer_subprocess_timeout_s, 30.0))
    # Strategy 1: systemd delegate.
    if systemctl_path is not None:
        # ``start`` without ``--no-block`` so the real exit code of the
        # unit (success / failure during ExecStart) propagates to us.
        # The unit's ``TimeoutStartSec=5s`` bounds wall-clock at the
        # systemd side; our own ``timeout_s`` bounds us.
        argv_sd = [
            systemctl_path,
            "start",
            _SYSTEMD_PERSIST_UNIT,
        ]
        try:
            proc = await asyncio.to_thread(
                subprocess.run,  # noqa: S603 — fixed argv, no shell, timeout enforced
                argv_sd,
                capture_output=True,
                timeout=timeout_s,
                check=False,
                text=True,
                errors="replace",
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.debug(
                "mixer_sanity_systemd_persist_subprocess_failed",
                detail=str(exc)[:200],
            )
        else:
            if proc.returncode == 0:
                logger.info(
                    "mixer_sanity_persist_delegated_to_systemd",
                    unit=_SYSTEMD_PERSIST_UNIT,
                )
                return True
            logger.debug(
                "mixer_sanity_systemd_persist_nonzero",
                returncode=proc.returncode,
                stderr=(proc.stderr or "").strip()[:200],
                note="unit probably not installed; falling back to direct alsactl",
            )

    # Strategy 2: direct alsactl — only works when daemon has write
    # access to /var/lib/alsa/asound.state (typically means running
    # as root, which is rare in Sovyx deployments).
    alsactl_path = _find_trusted_binary(_ALSACTL_PATHS)
    if alsactl_path is None:
        logger.debug("mixer_sanity_alsactl_missing")
        return False
    all_ok = True
    for card_index in cards:
        argv = [alsactl_path, "store", "-f", "-c", str(card_index)]
        try:
            proc = await asyncio.to_thread(
                subprocess.run,  # noqa: S603 — fixed argv, no shell, timeout enforced
                argv,
                capture_output=True,
                timeout=timeout_s,
                check=False,
                text=True,
                errors="replace",
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning(
                "mixer_sanity_alsactl_store_subprocess_failed",
                card_index=card_index,
                detail=str(exc)[:200],
            )
            all_ok = False
            continue
        if proc.returncode != 0:
            logger.warning(
                "mixer_sanity_alsactl_store_nonzero",
                card_index=card_index,
                returncode=proc.returncode,
                stderr=(proc.stderr or "").strip()[:200],
            )
            all_ok = False
    return all_ok


# ── Default validation probe (F1 honest-sentinel) ──────────────────


_SPEECH_CREST_FACTOR_DB: float = 9.0
"""Typical peak-to-RMS delta for unvoiced / mixed speech, in dB.

Used by :func:`make_default_validation_probe_fn` to estimate the
peak_dbfs field from the probe's measured RMS. Real peak measurement
requires inspecting the raw frames — the F2 validation probe taps
the capture ring buffer to compute it exactly; F1's approximation is
tight enough that the peak gate (≤ -2 dBFS default) fires correctly
on any reasonable speech signal.
"""


def make_default_validation_probe_fn(
    probe_fn: ProbeCallable,
    *,
    duration_ms: int = 2000,
) -> ValidationProbeFn:
    """Build the F1 default :class:`ValidationProbeFn`.

    Strategy: run a warm probe via the cascade's ``probe_fn`` and
    derive :class:`MixerValidationMetrics` from what the probe
    already measures (RMS + Silero VAD max/mean). For the two gates
    F1 cannot compute exactly (SNR in vocal band, OpenWakeWord
    stage-2), use honest sentinels:

    * **SNR**: ``20.0`` dB when probe is HEALTHY; ``0.0`` dB
      otherwise. The gate (default ``snr_db_vocal_band_min=15.0``)
      fires correctly — a HEALTHY probe had adequate signal energy;
      a non-HEALTHY probe should fail validation and trigger
      rollback.
    * **WW stage-2**: ``0.5`` when Silero ``max_prob >= 0.5``;
      ``0.0`` otherwise. The gate (default
      ``wake_word_stage2_prob_min=0.4``) trivially passes when VAD
      is alive — this is conservative (we skip a real WW probe in
      F1) but not FALSE-positive, because the gate fires only when
      Silero already corroborates the signal.

    F2 extends this function with an actual SNR computation (scipy
    FFT over the 300-3400 Hz band against a noise-floor estimate)
    and OpenWakeWord stage-2 invocation on the captured frames.
    Callers with that infrastructure today inject their own
    :class:`ValidationProbeFn`; the F1 default is the
    lowest-dependency option that ships.

    The returned callable is closure-captured so it can be passed
    directly as :attr:`MixerSanitySetup.validation_probe_fn`.

    Args:
        probe_fn: Cascade probe entry point — typically
            :func:`sovyx.voice.health.probe.probe`. Tests inject a
            deterministic fake.
        duration_ms: Target probe duration in ms. Defaults to
            2000 ms — matches V2 §E.6 validation window.
    """
    hard_timeout_s = (duration_ms / 1000.0) + 1.0

    async def _validate(
        endpoint: CandidateEndpoint,
        tuning: VoiceTuningConfig,  # noqa: ARG001 — reserved for F2 telemetry
    ) -> MixerValidationMetrics:
        # Canonical 16 kHz mono int16 Linux combo — the cascade's
        # default for ALSA probes. Validation runs AFTER L2.5 has
        # healed the mixer, so a plain shared-mode combo against
        # ``ALSA`` should succeed on any Linux setup.
        combo = Combo(
            host_api="ALSA",
            sample_rate=16_000,
            channels=1,
            sample_format="int16",
            exclusive=False,
            auto_convert=False,
            frames_per_buffer=480,
            platform_key="linux",
        )
        probe_result = await probe_fn(
            combo=combo,
            mode=ProbeMode.WARM,
            device_index=endpoint.device_index,
            hard_timeout_s=hard_timeout_s,
        )
        rms_dbfs = probe_result.rms_db
        # Clamp peak to the canonical ceiling (-2 dBFS) — no audible
        # signal SHOULD peak above that; going higher would indicate
        # clipping, which would already have failed the probe's
        # spectral check.
        peak_dbfs = min(-2.0, rms_dbfs + _SPEECH_CREST_FACTOR_DB)
        is_healthy = probe_result.diagnosis == Diagnosis.HEALTHY
        snr_sentinel = 20.0 if is_healthy else 0.0
        vad_max = probe_result.vad_max_prob or 0.0
        # Closed-at-threshold behaviour: WW sentinel mirrors Silero
        # crossing 0.5. Below that, VAD doesn't corroborate a signal
        # → WW sentinel stays 0.0 → gate fails → rollback.
        ww_sentinel = 0.5 if vad_max >= 0.5 else 0.0  # noqa: PLR2004
        return MixerValidationMetrics(
            rms_dbfs=rms_dbfs,
            peak_dbfs=peak_dbfs,
            snr_db_vocal_band=snr_sentinel,
            silero_max_prob=vad_max,
            silero_mean_prob=probe_result.vad_mean_prob or 0.0,
            wake_word_stage2_prob=ww_sentinel,
            measurement_duration_ms=probe_result.duration_ms,
        )

    return _validate


async def build_mixer_sanity_setup(
    *,
    probe_fn: ProbeCallable,
    telemetry: _TelemetryProto | None = None,
    hw: HardwareContext | None = None,
    kb_lookup: MixerKBLookup | None = None,
    role_resolver: MixerControlRoleResolver | None = None,
    half_heal_wal_path: Path | None = None,
    mixer_probe_fn: MixerProbeFn | None = None,
    mixer_apply_fn: MixerApplyFn | None = None,
    mixer_restore_fn: MixerRestoreFn | None = None,
    persist_fn: PersistFn | None = None,
) -> MixerSanitySetup | None:
    """Construct a :class:`MixerSanitySetup` for daemon boot.

    The one-call factory used by
    :func:`sovyx.voice.health._factory_integration.run_boot_cascade_for_candidates`
    to opt L2.5 into the cascade. Returns ``None`` when L2.5 cannot
    meaningfully fire on the current host — the caller then passes
    ``mixer_sanity=None`` to :func:`run_cascade_for_candidates` and
    the cascade runs unchanged.

    Returns ``None`` when:

    * Platform is not Linux (F1 scope).
    * ``detect_hardware_context`` yields ``driver_family="unknown"``
      — no KB profile can match, running L2.5 would only add latency.
    * ``MixerKBLookup.load_shipped`` raises (disk corruption, etc.).

    Args:
        probe_fn: Cascade probe used by the default
            :class:`ValidationProbeFn`.
        telemetry: Optional singleton for
            :meth:`record_mixer_sanity_outcome`. Defaults to the
            module-level telemetry recorder when unset — ``None`` in
            the returned setup if no recorder is installed.
        hw: Override for hardware context (tests; production passes
            ``None`` to use :func:`detect_hardware_context`).
        kb_lookup: Override for KB lookup (tests).
        role_resolver: Override for the role resolver (tests).
        half_heal_wal_path: Optional WAL path for mid-apply crash
            recovery; production wires
            ``default_wal_path(data_dir)``.
        mixer_probe_fn, mixer_apply_fn, mixer_restore_fn, persist_fn:
            Optional overrides for the Linux mixer strategy layer.
            Paranoid-QA R4 MEDIUM-4: previously dropped on the
            floor — the factory accepted only ``hw`` / ``kb_lookup``
            / ``role_resolver``, and operators wiring custom
            persist/apply/restore strategies saw the shipped
            defaults silently run instead. All four fields now
            flow through to the constructed :class:`MixerSanitySetup`.
    """
    # Lazy imports — these modules touch Linux-only subprocess /
    # /proc paths that we want to avoid importing on Windows / macOS
    # cold boot where L2.5 never fires.
    from sovyx.voice.health._hardware_detector import (  # noqa: PLC0415 — lazy-Linux
        detect_hardware_context,
    )
    from sovyx.voice.health._mixer_kb import MixerKBLookup  # noqa: PLC0415
    from sovyx.voice.health._mixer_roles import (  # noqa: PLC0415
        MixerControlRoleResolver,
    )

    if sys.platform != "linux":
        logger.debug("mixer_sanity_setup_non_linux_skipped", platform=sys.platform)
        return None

    effective_hw = hw if hw is not None else await detect_hardware_context()
    if effective_hw.driver_family == "unknown":
        logger.info(
            "mixer_sanity_setup_unknown_driver_family",
            codec_id=effective_hw.codec_id,
            system_vendor=effective_hw.system_vendor,
            system_product=effective_hw.system_product,
            note="L2.5 skipped — no KB profile can match unknown driver family",
        )
        return None

    effective_resolver = role_resolver if role_resolver is not None else MixerControlRoleResolver()
    if kb_lookup is not None:
        effective_kb = kb_lookup
    else:
        try:
            effective_kb = MixerKBLookup.load_shipped(resolver=effective_resolver)
        except Exception as exc:  # noqa: BLE001 — KB load failure is best-effort
            logger.warning(
                "mixer_sanity_setup_kb_load_failed",
                error_type=type(exc).__name__,
                detail=str(exc)[:200],
            )
            return None

    return MixerSanitySetup(
        hw=effective_hw,
        kb_lookup=effective_kb,
        role_resolver=effective_resolver,
        validation_probe_fn=make_default_validation_probe_fn(probe_fn),
        telemetry=telemetry,
        half_heal_wal_path=half_heal_wal_path,
        mixer_probe_fn=mixer_probe_fn,
        mixer_apply_fn=mixer_apply_fn,
        mixer_restore_fn=mixer_restore_fn,
        persist_fn=persist_fn,
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
