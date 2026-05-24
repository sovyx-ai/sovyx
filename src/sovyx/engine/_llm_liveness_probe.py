"""Periodic LLM provider liveness probe (Mission C6 §T2.5).

Single asyncio task that re-runs :func:`scan_llm_provider_health` at the
cadence configured by ``tuning.llm.liveness_check_interval_sec``. On every
tick the probe:

1. Re-pings Ollama (always-on).
2. Re-scans against the live ``os.environ`` to detect mid-session changes.
3. Computes the discovery verdict.
4. If the verdict transitions from the previous tick, dispatches the new
   verdict through :func:`dispatch_llm_discovery_verdict` to update the
   composite store + emit a structured ``llm.liveness_probe.transition``
   event.

Anti-pattern compliance:

* #14 — ``OllamaProvider.ping()`` is the only async I/O; the scan itself
  is pure-sync and runs in-place (no ``asyncio.to_thread`` needed because
  there's no blocking I/O — ``os.environ.get`` is dict-lookup speed).
* #15 — ONE asyncio task per process (not per-provider). Cardinality
  bounded by design — ``LLMProviderKey`` membership is fixed at 10.
* #24 — uses ``time.monotonic()`` for the unhealthy-grace window; the
  comparison is ``>=`` (inclusive on coarse clocks).
* #30 — no ``os.stat`` or psutil iteration; teardown is bounded
  ``asyncio.CancelledError`` await.
* #34 — kill-switch ``tuning.llm.liveness_check_enabled`` defaults TRUE
  (anti-pattern #34 inverse — observability is always-on; operators must
  opt OUT explicitly).
* #42 — composite-store updates flow through :func:`dispatch_llm_discovery_
  verdict` (the producer side); the probe never directly writes to the
  store.
* #44 — the probe IS the liveness-probe pairing required by anti-pattern
  #44 for the LLM-axis dependency-gated workers (CognitiveLoop, scheduler
  jobs that consume LLM completions).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import TYPE_CHECKING

from sovyx.engine._llm_dispatch import dispatch_llm_discovery_verdict
from sovyx.llm._provider_health import (
    DiscoveryVerdict,
    scan_llm_provider_health,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sovyx.engine.config import LLMTuningConfig
    from sovyx.llm._provider_health import LLMRouterDiscoveryReport
    from sovyx.llm.providers.ollama import OllamaProvider
    from sovyx.llm.router import LLMRouter
    from sovyx.mind.config import MindConfig


logger = logging.getLogger(__name__)


class LLMLivenessProbe:
    """Periodic re-scan of LLM provider liveness.

    Lifecycle:

    * :meth:`start` — spawns the background task ``llm-liveness-probe``.
      No-op when ``tuning.llm.liveness_check_enabled`` is False; emits a
      single ``llm.liveness_probe.disabled`` INFO event for telemetry.
    * :meth:`stop` — cancels the task; awaits ``asyncio.CancelledError``
      cleanly. Idempotent.

    Transition discipline:

    * The probe maintains ``self._last_verdict`` across ticks.
    * On verdict change, emits ``llm.liveness_probe.transition``
      ``{from_verdict, to_verdict}`` AND dispatches through the composite-
      store wire. Same-verdict ticks are silent (no log spam, no store
      thrash).
    * The grace-period filter (``tuning.llm.provider_unhealthy_grace_
      period_sec``) suppresses transitions where the unhealthy state has
      NOT persisted for at least the grace window — protects against
      transient blips that would otherwise flap the banner.
    """

    def __init__(
        self,
        router: LLMRouter,
        ollama_provider: OllamaProvider,
        config: LLMTuningConfig,
        mind_config: MindConfig,
        boot_verdict: DiscoveryVerdict | None = None,
    ) -> None:
        self._router = router
        self._ollama = ollama_provider
        self._config = config
        self._mind_config = mind_config
        self._task: asyncio.Task[None] | None = None
        # LIVE-1 Bug A — first-tick reconciliation. Seed ``_last_verdict``
        # with the verdict the boot-time dispatch already recorded so the
        # FIRST probe tick is compared against it (a real transition check)
        # instead of silently baselining. Without this, a recovery that
        # lands in the boot→first-tick window (e.g. the operator configures
        # a provider via onboarding within the liveness interval) is masked:
        # the probe's first observation is already healthy, ``previous is
        # None`` returns without dispatch, and ``clear_axis("llm")`` never
        # fires — leaving a stale "no provider configured" banner forever.
        self._last_verdict: DiscoveryVerdict | None = boot_verdict
        # Monotonic timestamp when the CURRENT non-FULLY_AVAILABLE verdict
        # was first observed. Used by the grace-period filter to suppress
        # transient blips. Armed at construction when the boot verdict is
        # already unhealthy so the grace window measures from boot.
        self._unhealthy_first_observed: float | None = (
            time.monotonic()
            if boot_verdict is not None and boot_verdict is not DiscoveryVerdict.FULLY_AVAILABLE
            else None
        )
        self._running = False
        # Mission C6 §T4.2 — optional callback wired by bootstrap.py to
        # propagate verdict transitions to the CogLoopGate's
        # ``dependency_ready_event``. The probe is constructed BEFORE the
        # gate in bootstrap; the callback is wired via
        # :meth:`set_dependency_state_callback` after the gate exists.
        self._dependency_state_callback: Callable[[bool], None] | None = None

    def set_dependency_state_callback(
        self,
        callback: Callable[[bool], None] | None,
    ) -> None:
        """Wire the gate's ``set_dependency_ready`` (or any observer).

        Called by bootstrap.py once the ``CogLoopGate`` is constructed so
        the probe can propagate verdict transitions. Idempotent — passing
        ``None`` un-wires the callback.
        """
        self._dependency_state_callback = callback

    async def start(self) -> None:
        """Start the periodic-rescan task.

        No-op + INFO event when ``liveness_check_enabled`` is False.
        Idempotent — calling ``start`` twice does not spawn a second task.
        """
        if not self._config.liveness_check_enabled:
            logger.info(
                "llm.liveness_probe.disabled",
                extra={"reason": "tuning_disabled"},
            )
            return
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="llm-liveness-probe")
        logger.info(
            "llm.liveness_probe.started",
            extra={
                "interval_sec": self._config.liveness_check_interval_sec,
                "grace_period_sec": self._config.provider_unhealthy_grace_period_sec,
            },
        )

    async def stop(self) -> None:
        """Cancel the periodic-rescan task; await graceful exit."""
        self._running = False
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("llm.liveness_probe.stopped")

    async def _loop(self) -> None:
        """Internal sleep → tick → repeat loop. Bounded latency per #14."""
        while self._running:
            try:
                await asyncio.sleep(self._config.liveness_check_interval_sec)
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 — observability-only
                # Probe failures are surface-level only; the next tick retries.
                # Anti-pattern #27: structured ignore with logged reason.
                logger.warning(
                    "llm.liveness_probe.tick_failed",
                    extra={"error": str(exc), "error_type": type(exc).__name__},
                )

    async def _scan(self) -> LLMRouterDiscoveryReport:
        """Ping Ollama, compute a fresh discovery report, refresh the cache.

        Shared by :meth:`_tick` (transition-gated dispatch) and
        :meth:`refresh_now` (unconditional dispatch). Pure observation —
        does NOT dispatch to the composite store; the caller decides the
        dispatch discipline.

        Tick atomicity: an Ollama ping that takes > liveness_check_interval_sec
        would naturally cause the next tick to start late — that's acceptable;
        bounded by the ``OllamaProvider.ping()`` 2-second timeout (anti-pattern
        #14 — bounded I/O).
        """
        await self._ollama.ping()
        ollama_models: tuple[str, ...] = ()
        if self._ollama.is_available:
            with contextlib.suppress(Exception):
                ollama_models = tuple(await self._ollama.list_models())

        report = scan_llm_provider_health(
            env=os.environ,
            ollama_ping_result=self._ollama.is_available,
            ollama_models=ollama_models if self._ollama.is_available else None,
            default_provider=self._mind_config.llm.default_provider,
            default_model=self._mind_config.llm.default_model,
            cloud_key_validation_results=None,
        )
        self._router.update_discovery_report(report)
        return report

    async def _tick(self) -> None:
        """Single probe tick — refreshes the discovery report + maybe dispatches."""
        report = await self._scan()
        self._maybe_dispatch_transition(report)

    async def refresh_now(self) -> None:
        """Re-scan + UNCONDITIONALLY dispatch the current verdict.

        LIVE-1 Bug A synchronous clear-edge. Called immediately after a
        provider is hot-registered (e.g. via the dashboard onboarding flow)
        so the composite-store ``llm`` axis reflects the new state within the
        same request instead of waiting up to one liveness interval. Routes
        through the shared :func:`dispatch_llm_discovery_verdict` SSoT, so a
        recovered verdict (``FULLY_AVAILABLE``) clears ``axis="llm"`` via the
        same path the boot dispatch used (anti-pattern #54 record/clear
        pairing). The grace-period filter is intentionally bypassed: an
        explicit operator configuration action is not a transient blip.
        Observability-only — never raises into the caller.
        """
        report = await self._scan()
        dispatch_llm_discovery_verdict(report)
        self._last_verdict = report.verdict
        self._unhealthy_first_observed = (
            None if report.verdict is DiscoveryVerdict.FULLY_AVAILABLE else time.monotonic()
        )
        logger.info(
            "llm.liveness_probe.refreshed",
            extra={"verdict": report.verdict.value},
        )
        self._propagate_dependency_state(report.verdict)

    def _maybe_dispatch_transition(self, report: LLMRouterDiscoveryReport) -> None:
        """Compare against ``_last_verdict``; dispatch on transition with grace filter."""
        new_verdict = report.verdict
        previous = self._last_verdict

        # First tick — set the baseline AND dispatch unconditionally (the
        # boot-time dispatch already ran, but the first probe tick is a
        # natural "current state" anchor for the transition log).
        if previous is None:
            self._last_verdict = new_verdict
            if new_verdict is not DiscoveryVerdict.FULLY_AVAILABLE:
                self._unhealthy_first_observed = time.monotonic()
            return

        if new_verdict == previous:
            # No transition — extend the unhealthy window (or keep cleared).
            if new_verdict is DiscoveryVerdict.FULLY_AVAILABLE:
                self._unhealthy_first_observed = None
            return

        # Transition detected. Apply grace filter on the unhealthy direction:
        # if we're going healthy → unhealthy, require the unhealthy state to
        # have persisted at least grace_period_sec before promoting. Going
        # unhealthy → healthy is always promoted immediately (no penalty for
        # recovery). When grace_period_sec is 0 the filter is bypassed and
        # the transition promotes on first detection.
        if (
            previous is DiscoveryVerdict.FULLY_AVAILABLE
            and new_verdict is not DiscoveryVerdict.FULLY_AVAILABLE
            and self._config.provider_unhealthy_grace_period_sec > 0.0
        ):
            if self._unhealthy_first_observed is None:
                # Arm the grace clock; don't dispatch yet.
                self._unhealthy_first_observed = time.monotonic()
                logger.info(
                    "llm.liveness_probe.unhealthy_grace_armed",
                    extra={
                        "verdict": new_verdict.value,
                        "grace_period_sec": self._config.provider_unhealthy_grace_period_sec,
                    },
                )
                return
            elapsed = time.monotonic() - self._unhealthy_first_observed
            if elapsed < self._config.provider_unhealthy_grace_period_sec:
                return

        # Promote transition.
        logger.info(
            "llm.liveness_probe.transition",
            extra={
                "from_verdict": previous.value,
                "to_verdict": new_verdict.value,
            },
        )
        dispatch_llm_discovery_verdict(report)
        self._last_verdict = new_verdict
        if new_verdict is DiscoveryVerdict.FULLY_AVAILABLE:
            self._unhealthy_first_observed = None
        else:
            self._unhealthy_first_observed = time.monotonic()

        self._propagate_dependency_state(new_verdict)

    def _propagate_dependency_state(self, verdict: DiscoveryVerdict) -> None:
        """Notify the cognitive-loop gate of the current dependency state.

        Mission C6 §T4.2 — propagate to the gate's ``dependency_ready_event``
        so the cognitive-loop worker pauses on degraded states + resumes on
        recovery. "Ready" means the router has at least one available
        provider; treat PARTIAL_HEALTH as ready (routing continues). Shared by
        :meth:`_maybe_dispatch_transition` and :meth:`refresh_now`.
        """
        if self._dependency_state_callback is None:
            return
        ready = verdict in (
            DiscoveryVerdict.FULLY_AVAILABLE,
            DiscoveryVerdict.PARTIAL_HEALTH,
        )
        try:
            self._dependency_state_callback(ready)
        except Exception as exc:  # noqa: BLE001 — callback failure must not break the probe
            logger.warning(
                "llm.liveness_probe.callback_failed",
                extra={"error": str(exc), "ready": ready},
            )


__all__ = ["LLMLivenessProbe"]
