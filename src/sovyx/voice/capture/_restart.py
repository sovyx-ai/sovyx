"""Restart-verdict types + result dataclasses + metric emitters.

Extracted from ``voice/_capture_task.py`` (lines 381-761 pre-split)
per master mission Phase 1 / T1.4 step 1. Pure data + pure functions
— no class state coupling, no awaits, no I/O on the hot path.

The four restart strategies (exclusive ↔ shared on Windows;
alsa_hw_direct ↔ session_manager on Linux) each carry:

* a ``StrEnum`` of possible verdicts (``EXCLUSIVE_ENGAGED``,
  ``DOWNGRADED_TO_SHARED``, ``OPEN_FAILED_NO_STREAM``, ...)
* a frozen-slots dataclass capturing the structured outcome
* a metric emitter that records the verdict + host_api + platform
  to the corresponding OTel counter.

The 4-strategy symmetry is intentional: every "engage exclusive /
direct" path has a "revert to shared / session_manager" twin so the
bypass coordinator can roll back cleanly when a strategy proves
ineffective.

Legacy import surface preserved: ``voice/_capture_task.py``
re-exports every name in ``__all__`` (via ``voice/capture``) so
existing imports like ``from sovyx.voice._capture_task import
ExclusiveRestartVerdict`` and the 13 timing-primitive test patches
keep working without an import-path migration.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


__all__ = [
    "_LINUX_ALSA_HOST_API",
    "_LINUX_SESSION_MANAGER_HOST_APIS",
    "AlsaHwDirectRestartResult",
    "AlsaHwDirectRestartVerdict",
    "ExclusiveRestartResult",
    "ExclusiveRestartVerdict",
    "HostApiRotateResult",
    "HostApiRotateVerdict",
    "SessionManagerRestartResult",
    "SessionManagerRestartVerdict",
    "SharedRestartResult",
    "SharedRestartVerdict",
    "_emit_alsa_hw_direct_restart_metric",
    "_emit_exclusive_restart_metric",
    "_emit_host_api_rotate_metric",
    "_emit_session_manager_restart_metric",
    "_emit_shared_restart_metric",
]


class ExclusiveRestartVerdict(StrEnum):
    """Verdict of :meth:`AudioCaptureTask.request_exclusive_restart`.

    Pre-v0.20.2 the method returned ``None`` and always logged
    ``audio_capture_exclusive_restart_ok`` when the reopen succeeded —
    even when WASAPI silently handed back a shared-mode stream (e.g.
    the device was held by another exclusive-mode app, or Windows
    policy denied exclusive access). Callers could not distinguish
    "APO bypassed" from "APO still active, we just reopened the same
    deaf pipe". This enum makes the outcome inspectable:

    Members:
        EXCLUSIVE_ENGAGED: Stream reopened and WASAPI confirmed
            exclusive engagement (``info.exclusive_used=True``). The
            APO chain is bypassed — the user's mic is now reaching
            PortAudio untouched.
        DOWNGRADED_TO_SHARED: Stream reopened successfully, but
            ``info.exclusive_used=False``. WASAPI returned shared
            mode (the only combos that survived fallback were shared
            variants) — the APO chain is still in the signal path,
            so the deaf condition that triggered the bypass remains.
        OPEN_FAILED_SHARED_FALLBACK: The exclusive reopen raised a
            :class:`StreamOpenError` and the secondary shared-mode
            :meth:`_reopen_stream_after_device_error` recovered. The
            pipeline is alive but deaf (same as before the request).
        OPEN_FAILED_NO_STREAM: Both the exclusive reopen and the
            shared-mode fallback raised. The stream is closed *and*
            the consumer task has been signalled to exit
            (``_running=False`` + consumer cancelled) — ``_consume_loop``
            cannot self-recover from this state (it would be parked on
            ``queue.get()`` with no callback feeding it), so upstream
            supervisors MUST detect the dead state via the returned
            verdict and rebuild the task.
        NOT_RUNNING: Called while the task is stopped — no-op.
    """

    EXCLUSIVE_ENGAGED = "exclusive_engaged"
    DOWNGRADED_TO_SHARED = "downgraded_to_shared"
    OPEN_FAILED_SHARED_FALLBACK = "open_failed_shared_fallback"
    OPEN_FAILED_NO_STREAM = "open_failed_no_stream"
    NOT_RUNNING = "not_running"


class SharedRestartVerdict(StrEnum):
    """Verdict of :meth:`AudioCaptureTask.request_shared_restart`.

    Symmetric revert of :class:`ExclusiveRestartVerdict` — re-opens the
    stream in shared mode when the APO-bypass experiment needs to be
    rolled back (e.g. a strategy proved ineffective and the coordinator
    wants the pipeline returned to its pre-bypass configuration before
    trying the next strategy, or the user explicitly unpins exclusive
    mode in the wizard).

    Members:
        SHARED_ENGAGED: Stream reopened successfully in shared mode; the
            platform APO chain is back in the signal path. Equivalent to
            the pre-bypass state.
        OPEN_FAILED_NO_STREAM: The shared-mode reopen raised and no
            stream is live. The consumer task has been signalled to
            exit (``_running=False`` + consumer cancelled) because
            ``_consume_loop`` cannot self-recover from this state —
            it would be parked on ``queue.get()`` with no callback
            feeding it. Upstream supervisors MUST detect the dead
            state via the returned verdict and rebuild the capture
            task (or issue an explicit
            :meth:`request_exclusive_restart`).
        NOT_RUNNING: Called while the task is stopped — no-op.
    """

    SHARED_ENGAGED = "shared_engaged"
    OPEN_FAILED_NO_STREAM = "open_failed_no_stream"
    NOT_RUNNING = "not_running"


@dataclass(frozen=True, slots=True)
class ExclusiveRestartResult:
    """Structured outcome of :meth:`AudioCaptureTask.request_exclusive_restart`.

    Attributes:
        verdict: The :class:`ExclusiveRestartVerdict` describing what
            happened. Callers should treat anything other than
            :attr:`ExclusiveRestartVerdict.EXCLUSIVE_ENGAGED` as an
            unsuccessful bypass — the APO chain is still in place.
        engaged: Convenience flag — ``True`` iff
            ``verdict == EXCLUSIVE_ENGAGED``.
        host_api: Host API of the resulting stream (or ``None`` when
            :attr:`OPEN_FAILED_NO_STREAM` / :attr:`NOT_RUNNING`).
        device: Resolved PortAudio device index of the resulting
            stream.
        sample_rate: Effective sample rate of the resulting stream.
        detail: Human-readable error / downgrade reason for logs and
            the dashboard UI. ``None`` on a successful engagement.
    """

    verdict: ExclusiveRestartVerdict
    engaged: bool
    host_api: str | None = None
    device: int | str | None = None
    sample_rate: int | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class SharedRestartResult:
    """Structured outcome of :meth:`AudioCaptureTask.request_shared_restart`.

    Attributes:
        verdict: The :class:`SharedRestartVerdict` describing what
            happened. ``SHARED_ENGAGED`` means the revert worked and the
            pipeline is now running on the default shared-mode pipe;
            anything else means the stream is down.
        engaged: Convenience flag — ``True`` iff
            ``verdict == SHARED_ENGAGED``.
        host_api: Host API of the resulting stream (or ``None`` when
            :attr:`OPEN_FAILED_NO_STREAM` / :attr:`NOT_RUNNING`).
        device: Resolved PortAudio device index of the resulting stream.
        sample_rate: Effective sample rate of the resulting stream.
        detail: Human-readable error / downgrade reason for logs and
            the dashboard UI. ``None`` on a successful engagement.
    """

    verdict: SharedRestartVerdict
    engaged: bool
    host_api: str | None = None
    device: int | str | None = None
    sample_rate: int | None = None
    detail: str | None = None


class AlsaHwDirectRestartVerdict(StrEnum):
    """Verdict of :meth:`AudioCaptureTask.request_alsa_hw_direct_restart`.

    Linux-specific twin of :class:`ExclusiveRestartVerdict`. The
    ``LinuxPipeWireDirectBypass`` strategy requests this restart when it
    wants to bypass a misbehaving PipeWire/PulseAudio filter chain and
    talk to the kernel ALSA device directly. PortAudio's ``ALSA`` host
    API opens the device without traversing the session manager.

    Members:
        ALSA_HW_ENGAGED: Stream reopened and PortAudio confirmed the
            winning attempt targets the ``ALSA`` host API. The session
            manager is no longer in the signal path.
        DOWNGRADED_TO_SESSION_MANAGER: Stream reopened but the opener
            fell back to a sibling device that routes through
            PipeWire/PulseAudio (no ALSA-direct sibling survived). The
            session-manager chain is still in the signal path.
        NO_ALSA_SIBLING: Enumeration yielded no ``ALSA``-host-API
            sibling for the currently active device — some distros ship
            PortAudio builds with ALSA compiled out, or the ALSA device
            is held by another exclusive client. Existing stream
            preserved; no mutation occurred.
        OPEN_FAILED_NO_STREAM: Both the ALSA-direct open and the
            session-manager fallback raised. The stream is closed and
            the consumer task has been signalled to exit — upstream
            supervisors MUST rebuild the capture task.
        NOT_LINUX: Called on a non-Linux host — no-op, preserves the
            existing stream. Strategies must gate on
            ``platform_key == "linux"`` but the method is defensive.
        NOT_RUNNING: Called while the task is stopped — no-op.
    """

    ALSA_HW_ENGAGED = "alsa_hw_engaged"
    DOWNGRADED_TO_SESSION_MANAGER = "downgraded_to_session_manager"
    NO_ALSA_SIBLING = "no_alsa_sibling"
    OPEN_FAILED_NO_STREAM = "open_failed_no_stream"
    NOT_LINUX = "not_linux"
    NOT_RUNNING = "not_running"


class SessionManagerRestartVerdict(StrEnum):
    """Verdict of :meth:`AudioCaptureTask.request_session_manager_restart`.

    Linux-specific twin of :class:`SharedRestartVerdict`. Called by the
    ``LinuxPipeWireDirectBypass`` strategy during ``revert`` to return
    the stream to PipeWire/PulseAudio after an ALSA-direct experiment.

    Members:
        SESSION_MANAGER_ENGAGED: Stream reopened against a sibling
            device served by PulseAudio or PipeWire; the session
            manager is back in the signal path.
        DOWNGRADED_TO_ALSA_HW: Enumeration yielded no
            PulseAudio/PipeWire sibling — the device is only reachable
            via ALSA direct. The stream is alive but still bypasses the
            session manager (same state as before the request).
        OPEN_FAILED_NO_STREAM: The session-manager reopen raised and no
            stream is live. Consumer task signalled to exit; supervisor
            must rebuild.
        NOT_LINUX: Called on a non-Linux host — no-op.
        NOT_RUNNING: Called while the task is stopped — no-op.
    """

    SESSION_MANAGER_ENGAGED = "session_manager_engaged"
    DOWNGRADED_TO_ALSA_HW = "downgraded_to_alsa_hw"
    OPEN_FAILED_NO_STREAM = "open_failed_no_stream"
    NOT_LINUX = "not_linux"
    NOT_RUNNING = "not_running"


@dataclass(frozen=True, slots=True)
class AlsaHwDirectRestartResult:
    """Structured outcome of :meth:`AudioCaptureTask.request_alsa_hw_direct_restart`.

    Attributes:
        verdict: The :class:`AlsaHwDirectRestartVerdict` describing what
            happened. Callers should treat anything other than
            :attr:`AlsaHwDirectRestartVerdict.ALSA_HW_ENGAGED` as an
            unsuccessful bypass — the session-manager chain is still in
            place or the stream is gone.
        engaged: Convenience flag — ``True`` iff
            ``verdict == ALSA_HW_ENGAGED``.
        host_api: Host API of the resulting stream. ``"ALSA"`` on
            successful engagement; ``None`` when the stream is down.
        device: Resolved PortAudio device index of the resulting stream.
        sample_rate: Effective sample rate of the resulting stream.
        detail: Human-readable error / downgrade reason for logs and
            the dashboard UI. ``None`` on successful engagement.
    """

    verdict: AlsaHwDirectRestartVerdict
    engaged: bool
    host_api: str | None = None
    device: int | str | None = None
    sample_rate: int | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class SessionManagerRestartResult:
    """Structured outcome of :meth:`AudioCaptureTask.request_session_manager_restart`.

    Attributes:
        verdict: The :class:`SessionManagerRestartVerdict` describing
            what happened. ``SESSION_MANAGER_ENGAGED`` means the revert
            worked and the pipeline is now running through PipeWire or
            PulseAudio again.
        engaged: Convenience flag — ``True`` iff
            ``verdict == SESSION_MANAGER_ENGAGED``.
        host_api: Host API of the resulting stream.
        device: Resolved PortAudio device index of the resulting stream.
        sample_rate: Effective sample rate of the resulting stream.
        detail: Human-readable error / downgrade reason for logs and
            the dashboard UI. ``None`` on successful engagement.
    """

    verdict: SessionManagerRestartVerdict
    engaged: bool
    host_api: str | None = None
    device: int | str | None = None
    sample_rate: int | None = None
    detail: str | None = None


_LINUX_ALSA_HOST_API = "ALSA"
"""PortAudio's label for the direct-to-kernel ALSA host API on Linux."""


_LINUX_SESSION_MANAGER_HOST_APIS: frozenset[str] = frozenset({"PulseAudio", "PipeWire", "JACK"})
"""Host APIs that route through a Linux session manager.

A device served by any of these is considered non-direct for the
purpose of :meth:`request_alsa_hw_direct_restart` — the strategy wants
to route *around* these layers, not through them.
"""


def _emit_exclusive_restart_metric(result: ExclusiveRestartResult) -> None:
    """Record a ``voice.capture.exclusive_restart.verdicts`` counter event.

    Lazy-imports :mod:`sovyx.observability.metrics` so module-load in
    unit suites that swap the metrics provider still works. Failures
    in the metrics pipeline never bubble up to the caller — instead we
    log at DEBUG and continue, so an OTel exporter hiccup cannot break
    the capture task's reopen path.
    """
    try:
        import sys

        from sovyx.observability.metrics import get_metrics

        registry = get_metrics()
        counter = getattr(registry, "voice_capture_exclusive_restart_verdicts", None)
        if counter is None:
            return
        counter.add(
            1,
            attributes={
                "verdict": result.verdict.value,
                "host_api": result.host_api or "none",
                "platform": sys.platform,
            },
        )
    except Exception:  # noqa: BLE001 — metrics must never break capture
        logger.debug("voice_capture_exclusive_restart_metric_failed", exc_info=True)


def _emit_shared_restart_metric(result: SharedRestartResult) -> None:
    """Record a ``voice.capture.shared_restart.verdicts`` counter event.

    Symmetric twin of :func:`_emit_exclusive_restart_metric`; separate
    counter name so dashboards can distinguish engagements from reverts
    without parsing labels.
    """
    try:
        import sys

        from sovyx.observability.metrics import get_metrics

        registry = get_metrics()
        counter = getattr(registry, "voice_capture_shared_restart_verdicts", None)
        if counter is None:
            return
        counter.add(
            1,
            attributes={
                "verdict": result.verdict.value,
                "host_api": result.host_api or "none",
                "platform": sys.platform,
            },
        )
    except Exception:  # noqa: BLE001 — metrics must never break capture
        logger.debug("voice_capture_shared_restart_metric_failed", exc_info=True)


def _emit_alsa_hw_direct_restart_metric(result: AlsaHwDirectRestartResult) -> None:
    """Record a ``voice.capture.alsa_hw_direct_restart.verdicts`` counter event.

    Linux-specific twin of :func:`_emit_exclusive_restart_metric`. The
    counter is emitted regardless of whether the strategy engaged so
    dashboards can tell "Linux direct bypass never got a chance"
    (``NO_ALSA_SIBLING`` / ``NOT_LINUX``) apart from "direct bypass was
    tried and the session manager was bypassed" (``ALSA_HW_ENGAGED``)
    without scraping logs.
    """
    try:
        import sys

        from sovyx.observability.metrics import get_metrics

        registry = get_metrics()
        counter = getattr(registry, "voice_capture_alsa_hw_direct_restart_verdicts", None)
        if counter is None:
            return
        counter.add(
            1,
            attributes={
                "verdict": result.verdict.value,
                "host_api": result.host_api or "none",
                "platform": sys.platform,
            },
        )
    except Exception:  # noqa: BLE001 — metrics must never break capture
        logger.debug("voice_capture_alsa_hw_direct_restart_metric_failed", exc_info=True)


def _emit_session_manager_restart_metric(
    result: SessionManagerRestartResult,
) -> None:
    """Record a ``voice.capture.session_manager_restart.verdicts`` counter event.

    Symmetric twin of :func:`_emit_alsa_hw_direct_restart_metric` for
    the revert side of the Linux PipeWire-direct strategy.
    """
    try:
        import sys

        from sovyx.observability.metrics import get_metrics

        registry = get_metrics()
        counter = getattr(registry, "voice_capture_session_manager_restart_verdicts", None)
        if counter is None:
            return
        counter.add(
            1,
            attributes={
                "verdict": result.verdict.value,
                "host_api": result.host_api or "none",
                "platform": sys.platform,
            },
        )
    except Exception:  # noqa: BLE001 — metrics must never break capture
        logger.debug("voice_capture_session_manager_restart_metric_failed", exc_info=True)


class HostApiRotateVerdict(StrEnum):
    """Verdict of :meth:`AudioCaptureTask.request_host_api_rotate`.

    T28 — drives the Tier 2 ``WindowsHostApiRotateThenExclusive``
    bypass strategy. The strategy applies in 2 phases: rotate the
    capture stream to a target host_api (typically ``Windows
    WASAPI``), then engage exclusive mode on the rotated stream.
    Each phase reports its own verdict; this enum covers Phase A
    (rotate). Phase B reuses :class:`ExclusiveRestartVerdict`.

    Members:
        ROTATED_SUCCESS: Stream reopened against a sibling device on
            the requested target host_api. ``self._host_api_name``
            now reflects the new host_api so subsequent reopens (via
            ``_reopen_stream_after_device_error``) honour the rotate.
            The cascade-alignment-enabled opener (Furo W-4 fix) is a
            prerequisite — without it, the next reopen drifts back.
        DOWNGRADED_TO_SOURCE: Opener fell back to the source
            host_api during the unified-opener fallback chain — the
            stream is alive but the rotation didn't take. The
            strategy SHOULD treat this as a not-engaged outcome and
            advance to the next tier.
        NO_TARGET_SIBLING: Enumeration yielded no DeviceEntry on the
            requested target host_api. Existing stream preserved; no
            mutation occurred. Common on systems where PortAudio's
            WASAPI build excludes the active endpoint (rare but
            documented for legacy hardware).
        OPEN_FAILED_NO_STREAM: Both the target-host_api open and the
            source-fallback open raised. The stream is closed and
            the consumer task has been signalled to exit — upstream
            supervisors MUST rebuild the capture task. Mirrors the
            ``OPEN_FAILED_NO_STREAM`` semantics of the existing
            restart methods.
        NOT_WIN32: Called on a non-Windows host. The Windows-only
            Tier 2 strategy gates eligibility on ``platform_key ==
            "win32"``, but this method is defensive — direct
            invocation on a non-Windows host returns this verdict
            and preserves the existing stream.
        NOT_RUNNING: Called while the task is stopped. No-op.
    """

    ROTATED_SUCCESS = "rotated_success"
    DOWNGRADED_TO_SOURCE = "downgraded_to_source"
    NO_TARGET_SIBLING = "no_target_sibling"
    OPEN_FAILED_NO_STREAM = "open_failed_no_stream"
    NOT_WIN32 = "not_win32"
    NOT_RUNNING = "not_running"


@dataclass(frozen=True, slots=True)
class HostApiRotateResult:
    """Structured outcome of :meth:`AudioCaptureTask.request_host_api_rotate`.

    Attributes:
        verdict: The :class:`HostApiRotateVerdict` describing what
            happened. Anything other than
            :attr:`HostApiRotateVerdict.ROTATED_SUCCESS` means the
            rotation didn't take and the strategy should advance.
        engaged: Convenience flag — ``True`` iff
            ``verdict == ROTATED_SUCCESS``.
        target_host_api: The requested target host_api (e.g.
            ``"Windows WASAPI"``). Echoed in the result for
            downstream telemetry symmetry.
        source_host_api: ``self._host_api_name`` BEFORE the rotate
            call. Strategy implementations capture this so the
            revert path can rotate back; Tier 2's ``revert`` reads
            it to restore the pre-bypass host_api.
        host_api: Effective host_api of the resulting stream. Equal
            to ``target_host_api`` on ``ROTATED_SUCCESS``; falls
            back to the source host_api on ``DOWNGRADED_TO_SOURCE``;
            ``None`` when the stream is down.
        device: Resolved PortAudio device index of the resulting
            stream.
        sample_rate: Effective sample rate of the resulting stream.
        detail: Human-readable error / downgrade reason. ``None`` on
            successful engagement.
    """

    verdict: HostApiRotateVerdict
    engaged: bool
    target_host_api: str = ""
    source_host_api: str | None = None
    host_api: str | None = None
    device: int | str | None = None
    sample_rate: int | None = None
    detail: str | None = None


def _emit_host_api_rotate_metric(result: HostApiRotateResult) -> None:
    """Record a ``voice.capture.host_api_rotate.verdicts`` counter event.

    T28 — symmetric twin of :func:`_emit_exclusive_restart_metric`
    for the Tier 2 rotate-then-exclusive strategy. The counter is
    emitted regardless of whether the rotation engaged so
    dashboards can split "rotation tried + engaged" vs "rotation
    tried + downgraded" vs "rotation never got a chance" without
    scraping logs.
    """
    try:
        import sys

        from sovyx.observability.metrics import get_metrics

        registry = get_metrics()
        counter = getattr(registry, "voice_capture_host_api_rotate_verdicts", None)
        if counter is None:
            return
        counter.add(
            1,
            attributes={
                "verdict": result.verdict.value,
                "target_host_api": result.target_host_api or "none",
                "host_api": result.host_api or "none",
                "platform": sys.platform,
            },
        )
    except Exception:  # noqa: BLE001 — metrics must never break capture
        logger.debug("voice_capture_host_api_rotate_metric_failed", exc_info=True)
