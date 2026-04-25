"""Platform-bypass strategy Protocol + the coordinator's exception surface.

A :class:`PlatformBypassStrategy` represents one concrete way to route
around a capture-side DSP layer (Windows Voice Clarity,
PulseAudio ``module-echo-cancel``, CoreAudio VPIO, …). The
:class:`~sovyx.voice.health.capture_integrity.CaptureIntegrityCoordinator`
iterates a platform-filtered list of strategies in deterministic order
until one returns :class:`~sovyx.voice.health.contract.BypassVerdict.APPLIED_HEALTHY`
or the list is exhausted (in which case the endpoint is quarantined).

Keeping this Protocol in its own module — rather than in
:mod:`sovyx.voice.health.contract` — lets concrete strategies import
the Protocol without dragging the full contract surface into their
dependency graph.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from sovyx.voice.health.contract import BypassContext, Eligibility


class BypassApplyError(RuntimeError):
    """Raised by :meth:`PlatformBypassStrategy.apply` on a platform failure.

    The coordinator catches this, emits a :class:`BypassVerdict.FAILED_TO_APPLY`
    outcome, and advances to the next strategy. Attach the underlying
    exception via ``raise BypassApplyError(msg) from exc`` so the
    classified PortAudio / OS error chain survives in tracebacks.

    Attributes:
        reason: Machine-readable token classifying the failure
            (``"exclusive_downgraded_to_shared"``,
            ``"exclusive_open_failed"``, ``"policy_gate_denied"`` …).
            Stable across minor versions so dashboards can key on it.
    """

    def __init__(self, message: str, *, reason: str = "unspecified") -> None:
        super().__init__(message)
        self.reason = reason


class BypassRevertError(RuntimeError):
    """Raised by :meth:`PlatformBypassStrategy.revert` on a teardown failure (B3).

    The pre-B3 contract was to log a WARNING and silently return — a
    band-aid that left the capture pipeline in a half-applied state
    while obscuring the failure from the coordinator's telemetry. The
    coordinator's exception path could not distinguish a strategy bug
    (raised RuntimeError) from a graceful revert that just couldn't
    restore the original state (a recoverable event). With B3:

    * Strategies raise ``BypassRevertError`` with a stable ``reason``
      token whenever the platform-specific revert fails to restore the
      pre-apply state. The coordinator catches this exception class
      explicitly and emits ``voice.bypass.revert_failed`` so dashboards
      can attribute it to the strategy + reason.
    * Generic ``Exception`` from ``revert`` still falls through the
      coordinator's defensive ``except`` (a strategy bug, not a
      legitimate revert failure), but is now distinguishable from the
      structured ``BypassRevertError`` path.

    Attributes:
        reason: Machine-readable token classifying the failure (e.g.
            ``"shared_restart_downgraded"``,
            ``"session_manager_restart_failed"``,
            ``"capture_task_not_running"``). Stable across minor
            versions so dashboards can key on it.
    """

    def __init__(self, message: str, *, reason: str = "unspecified") -> None:
        super().__init__(message)
        self.reason = reason


@runtime_checkable
class PlatformBypassStrategy(Protocol):
    """Pluggable APO-bypass implementation for one platform path.

    Contract invariants (enforced by the coordinator, not the Protocol):

    * :attr:`name` is a stable telemetry identifier (e.g.
      ``"win.wasapi_exclusive"``, ``"win.disable_sysfx"``,
      ``"linux.alsa_hw_direct"``, ``"macos.coreaudio_vpio_off"``).
      Treat it as external API.
    * :meth:`probe_eligibility` must be cheap and side-effect-free. The
      coordinator calls it on every attempt; it cannot open streams,
      mutate state, or block for platform I/O beyond reading static
      host-API metadata.
    * :meth:`apply` is at-most-once per coordinator session. If called
      a second time on the same instance the behaviour is undefined —
      the coordinator never does this.
    * :meth:`revert` undoes whatever :meth:`apply` did. If ``apply``
      never succeeded (NOT_APPLICABLE or FAILED_TO_APPLY) then
      ``revert`` is a no-op. Must be idempotent in case the coordinator
      calls it during teardown.
    * All three methods must be cancellation-safe: if the caller
      cancels the awaited task, the strategy must leave the capture
      pipeline in a consistent state (either fully applied or fully
      reverted, never half-applied).
    """

    name: str

    async def probe_eligibility(
        self,
        context: BypassContext,
    ) -> Eligibility:
        """Return whether this strategy can legitimately run.

        A ``False`` verdict must include a stable ``reason`` token so
        the coordinator can surface it in telemetry without string
        parsing.
        """
        ...

    async def apply(
        self,
        context: BypassContext,
    ) -> str:
        """Execute the platform-specific bypass mutation.

        Returns a short machine-readable tag describing the mutation
        path actually taken (e.g. ``"exclusive_engaged"``,
        ``"sysfx_disabled_via_registry"``) so multi-branch strategies
        stay self-reporting. Raises :class:`BypassApplyError` on any
        platform failure — the coordinator translates the raise into a
        :class:`BypassVerdict.FAILED_TO_APPLY` outcome.

        The method must NOT itself run an integrity probe or sleep for
        the settle window — the coordinator owns that timing surface so
        thresholds live in one place.
        """
        ...

    async def revert(
        self,
        context: BypassContext,
    ) -> None:
        """Reverse whatever :meth:`apply` did. Idempotent.

        On a teardown failure that leaves the capture pipeline in a
        non-restored state, raise :class:`BypassRevertError` with a
        stable ``reason`` token. The coordinator translates the raise
        into a structured ``voice.bypass.revert_failed`` event so the
        failure is observable from the dashboard rather than buried in
        a WARN log (the pre-B3 band-aid). When ``apply`` never
        succeeded (NOT_APPLICABLE / FAILED_TO_APPLY) ``revert`` is a
        no-op and must NOT raise.
        """
        ...


__all__ = [
    "BypassApplyError",
    "BypassRevertError",
    "PlatformBypassStrategy",
]
