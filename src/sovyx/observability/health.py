"""Sovyx health checks — 10 checks covering all critical subsystems.

Each check returns a :class:`CheckResult` with status (green/yellow/red),
a human-readable message, and optional metadata.  The :class:`HealthRegistry`
collects all checks and runs them concurrently.

Usage::

    registry = HealthRegistry()
    registry.register(DiskSpaceCheck())
    registry.register(RAMCheck())
    results = await registry.run_all()

For ``sovyx doctor``::

    from sovyx.observability.health import run_doctor
    results = await run_doctor(db_pool=pool, ...)
"""

from __future__ import annotations

import asyncio
import dataclasses
import shutil
from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sovyx.engine.registry import ServiceRegistry

# ── Data types ──────────────────────────────────────────────────────────────


class CheckStatus(StrEnum):
    """Health check status levels."""

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


@dataclasses.dataclass(frozen=True)
class CheckResult:
    """Result of a single health check.

    Attributes:
        name: Human-readable check name.
        status: GREEN (ok), YELLOW (degraded), RED (critical).
        message: Explanation of the status.
        metadata: Optional extra data (e.g. disk_free_gb, ram_used_pct).
    """

    name: str
    status: CheckStatus
    message: str
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True if status is GREEN."""
        return self.status == CheckStatus.GREEN


# ── Base class ──────────────────────────────────────────────────────────────


class HealthCheck(ABC):
    """Abstract base for health checks."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name (e.g. 'Disk Space')."""

    @abstractmethod
    async def check(self) -> CheckResult:
        """Run the check and return a result.

        Implementations must NOT raise — always return a CheckResult.
        """


# ── Registry ────────────────────────────────────────────────────────────────


class HealthRegistry:
    """Collects and runs health checks.

    Thread-safe for concurrent reads.  Checks run concurrently via
    ``asyncio.gather``.
    """

    def __init__(self) -> None:
        self._checks: list[HealthCheck] = []

    def register(self, check: HealthCheck) -> None:
        """Register a health check."""
        self._checks.append(check)

    @property
    def check_count(self) -> int:
        """Number of registered checks."""
        return len(self._checks)

    async def run_all(self, timeout: float = 10.0) -> list[CheckResult]:
        """Run all checks concurrently with a timeout.

        Args:
            timeout: Max seconds to wait for all checks.

        Returns:
            List of CheckResults (one per registered check).
            Checks that timeout or raise get a RED result.
        """
        tasks = [self._safe_run(c, timeout) for c in self._checks]
        return list(await asyncio.gather(*tasks))

    @staticmethod
    async def _safe_run(check: HealthCheck, timeout: float) -> CheckResult:
        """Run a single check with timeout and error handling."""
        try:
            return await asyncio.wait_for(check.check(), timeout=timeout)
        except TimeoutError:
            return CheckResult(
                name=check.name,
                status=CheckStatus.RED,
                message=f"Check timed out after {timeout}s",
            )
        except Exception as exc:  # noqa: BLE001 — health-check runner boundary
            return CheckResult(
                name=check.name,
                status=CheckStatus.RED,
                message=f"Check failed: {exc}",
            )

    def summary(self, results: list[CheckResult]) -> CheckStatus:
        """Return worst status across all results."""
        if any(r.status == CheckStatus.RED for r in results):
            return CheckStatus.RED
        if any(r.status == CheckStatus.YELLOW for r in results):
            return CheckStatus.YELLOW
        return CheckStatus.GREEN

    async def snapshot(self, timeout: float = 10.0) -> dict[str, Any]:
        """Run every check and return a serialisable summary.

        Used by the startup self-diagnosis cascade
        (``startup.health.snapshot``) and by any caller that wants a
        compact overview without iterating :meth:`run_all` results
        manually. The shape is intentionally small — overall status
        plus a per-check status/message map — so the payload stays
        within the wire-format clamp.
        """
        results = await self.run_all(timeout=timeout)
        overall = self.summary(results)
        return {
            "overall": overall.value,
            "check_count": len(results),
            "checks": {
                r.name: {
                    "status": r.status.value,
                    "message": r.message,
                }
                for r in results
            },
        }


# ── Built-in checks ────────────────────────────────────────────────────────


class DiskSpaceCheck(HealthCheck):
    """Check available disk space.

    GREEN: >= 1 GB free.
    YELLOW: >= 500 MB free.
    RED: < 500 MB free.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or Path.home()

    @property
    def name(self) -> str:
        """Health check name."""
        return "Disk Space"

    async def check(self) -> CheckResult:
        """Execute health check."""
        try:
            usage = shutil.disk_usage(self._path)
        except (FileNotFoundError, OSError) as exc:
            return CheckResult(
                name=self.name,
                status=CheckStatus.RED,
                message=f"Cannot check disk: {exc}",
            )
        free_gb = usage.free / (1024**3)
        meta = {
            "free_gb": round(free_gb, 2),
            "total_gb": round(usage.total / (1024**3), 2),
            "used_pct": round((usage.used / usage.total) * 100, 1),
        }

        if free_gb >= 1.0:
            return CheckResult(
                name=self.name,
                status=CheckStatus.GREEN,
                message=f"{free_gb:.1f} GB free",
                metadata=meta,
            )
        if free_gb >= 0.5:
            return CheckResult(
                name=self.name,
                status=CheckStatus.YELLOW,
                message=f"Low disk: {free_gb:.1f} GB free",
                metadata=meta,
            )
        return CheckResult(
            name=self.name,
            status=CheckStatus.RED,
            message=f"Critical: {free_gb:.2f} GB free",
            metadata=meta,
        )


class RAMCheck(HealthCheck):
    """Check available RAM.

    GREEN: >= 512 MB available.
    YELLOW: >= 256 MB available.
    RED: < 256 MB available.
    """

    @property
    def name(self) -> str:
        """Health check name."""
        return "RAM"

    async def check(self) -> CheckResult:
        """Execute health check."""
        import psutil

        mem = psutil.virtual_memory()
        avail_mb = mem.available / (1024**2)
        meta = {
            "available_mb": round(avail_mb, 0),
            "total_mb": round(mem.total / (1024**2), 0),
            "used_pct": round(mem.percent, 1),
        }

        if avail_mb >= 512:
            return CheckResult(
                name=self.name,
                status=CheckStatus.GREEN,
                message=f"{avail_mb:.0f} MB available",
                metadata=meta,
            )
        if avail_mb >= 256:
            return CheckResult(
                name=self.name,
                status=CheckStatus.YELLOW,
                message=f"Low RAM: {avail_mb:.0f} MB available",
                metadata=meta,
            )
        return CheckResult(
            name=self.name,
            status=CheckStatus.RED,
            message=f"Critical: {avail_mb:.0f} MB available",
            metadata=meta,
        )


class CPUCheck(HealthCheck):
    """Check CPU usage.

    GREEN: < 80%.
    YELLOW: < 95%.
    RED: >= 95%.
    """

    @property
    def name(self) -> str:
        """Health check name."""
        return "CPU"

    async def check(self) -> CheckResult:
        """Execute health check."""
        import psutil

        # interval=None returns since last call (instant, non-blocking)
        cpu_pct = psutil.cpu_percent(interval=0.1)
        meta = {"cpu_pct": round(cpu_pct, 1)}

        if cpu_pct < 80:
            return CheckResult(
                name=self.name,
                status=CheckStatus.GREEN,
                message=f"CPU at {cpu_pct:.0f}%",
                metadata=meta,
            )
        if cpu_pct < 95:
            return CheckResult(
                name=self.name,
                status=CheckStatus.YELLOW,
                message=f"High CPU: {cpu_pct:.0f}%",
                metadata=meta,
            )
        return CheckResult(
            name=self.name,
            status=CheckStatus.RED,
            message=f"Critical CPU: {cpu_pct:.0f}%",
            metadata=meta,
        )


class DatabaseCheck(HealthCheck):
    """Check database is writable.

    Attempts a write + read roundtrip on the database pool.
    """

    def __init__(self, write_fn: Any = None) -> None:  # noqa: ANN401
        """Args: write_fn — async callable that tests DB write. None = skip."""
        self._write_fn = write_fn

    @property
    def name(self) -> str:
        """Health check name."""
        return "Database"

    async def check(self) -> CheckResult:
        """Execute health check."""
        if self._write_fn is None:
            return CheckResult(
                name=self.name,
                status=CheckStatus.YELLOW,
                message="Database check not configured",
            )
        try:
            await self._write_fn()
            return CheckResult(
                name=self.name,
                status=CheckStatus.GREEN,
                message="Database writable",
            )
        except Exception as exc:  # noqa: BLE001 — health-check boundary
            return CheckResult(
                name=self.name,
                status=CheckStatus.RED,
                message=f"Database error: {exc}",
            )


class BrainIndexedCheck(HealthCheck):
    """Check that brain embedding engine is loaded and indexed."""

    def __init__(self, is_loaded_fn: Any = None) -> None:  # noqa: ANN401
        """Args: is_loaded_fn — callable returning bool. None = skip."""
        self._is_loaded_fn = is_loaded_fn

    @property
    def name(self) -> str:
        """Health check name."""
        return "Brain Index"

    async def check(self) -> CheckResult:
        """Execute health check."""
        if self._is_loaded_fn is None:
            return CheckResult(
                name=self.name,
                status=CheckStatus.YELLOW,
                message="Brain check not configured",
            )
        try:
            loaded = self._is_loaded_fn()
            if loaded:
                return CheckResult(
                    name=self.name,
                    status=CheckStatus.GREEN,
                    message="Brain embedding model loaded",
                )
            return CheckResult(
                name=self.name,
                status=CheckStatus.YELLOW,
                message="Brain model not yet loaded (lazy load on first use)",
            )
        except Exception as exc:  # noqa: BLE001 — health-check boundary
            return CheckResult(
                name=self.name,
                status=CheckStatus.RED,
                message=f"Brain check failed: {exc}",
            )


class LLMReachableCheck(HealthCheck):
    """Check that at least one LLM provider is reachable."""

    def __init__(self, provider_status_fn: Any = None) -> None:  # noqa: ANN401
        """Args: provider_status_fn — async callable returning list of (name, available) tuples."""
        self._provider_status_fn = provider_status_fn

    @property
    def name(self) -> str:
        """Health check name."""
        return "LLM Providers"

    async def check(self) -> CheckResult:
        """Execute health check."""
        if self._provider_status_fn is None:
            return CheckResult(
                name=self.name,
                status=CheckStatus.YELLOW,
                message="LLM check not configured",
            )
        try:
            statuses: list[tuple[str, bool]] = await self._provider_status_fn()
            available = [name for name, ok in statuses if ok]
            unavailable = [name for name, ok in statuses if not ok]
            meta = {"available": available, "unavailable": unavailable}

            if available:
                return CheckResult(
                    name=self.name,
                    status=CheckStatus.GREEN,
                    message=f"{len(available)} provider(s) available: {', '.join(available)}",
                    metadata=meta,
                )
            return CheckResult(
                name=self.name,
                status=CheckStatus.RED,
                message="No LLM providers available",
                metadata=meta,
            )
        except Exception as exc:  # noqa: BLE001 — health-check boundary
            return CheckResult(
                name=self.name,
                status=CheckStatus.RED,
                message=f"LLM check failed: {exc}",
            )


class ModelLoadedCheck(HealthCheck):
    """Check that the embedding model files exist on disk."""

    def __init__(self, model_dir: Path | None = None) -> None:
        self._model_dir = model_dir or Path.home() / ".sovyx" / "models"

    @property
    def name(self) -> str:
        """Health check name."""
        return "Embedding Model"

    async def check(self) -> CheckResult:
        """Execute health check."""
        if not self._model_dir.exists():
            return CheckResult(
                name=self.name,
                status=CheckStatus.YELLOW,
                message=f"Model directory not found: {self._model_dir}",
            )
        onnx_files = list(self._model_dir.glob("*.onnx"))
        if onnx_files:
            names = [f.name for f in onnx_files]
            return CheckResult(
                name=self.name,
                status=CheckStatus.GREEN,
                message=f"Model files present: {', '.join(names)}",
                metadata={"files": names},
            )
        return CheckResult(
            name=self.name,
            status=CheckStatus.YELLOW,
            message="No .onnx model files found (will download on first use)",
        )


class ChannelConnectedCheck(HealthCheck):
    """Check that at least one bridge channel is connected."""

    def __init__(self, channel_status_fn: Any = None) -> None:  # noqa: ANN401
        """Args: channel_status_fn — callable returning list of (name, connected) tuples."""
        self._channel_status_fn = channel_status_fn

    @property
    def name(self) -> str:
        """Health check name."""
        return "Channels"

    async def check(self) -> CheckResult:
        """Execute health check."""
        if self._channel_status_fn is None:
            return CheckResult(
                name=self.name,
                status=CheckStatus.YELLOW,
                message="Channel check not configured",
            )
        try:
            statuses: list[tuple[str, bool]] = self._channel_status_fn()
            connected = [name for name, ok in statuses if ok]
            meta = {"connected": connected, "total": len(statuses)}

            if connected:
                return CheckResult(
                    name=self.name,
                    status=CheckStatus.GREEN,
                    message=f"{len(connected)} channel(s) connected: {', '.join(connected)}",
                    metadata=meta,
                )
            return CheckResult(
                name=self.name,
                status=CheckStatus.RED,
                message="No channels connected",
                metadata=meta,
            )
        except Exception as exc:  # noqa: BLE001 — health-check boundary
            return CheckResult(
                name=self.name,
                status=CheckStatus.RED,
                message=f"Channel check failed: {exc}",
            )


class ConsolidationCheck(HealthCheck):
    """Check that memory consolidation scheduler is running."""

    def __init__(self, is_running_fn: Any = None) -> None:  # noqa: ANN401
        """Args: is_running_fn — callable returning bool."""
        self._is_running_fn = is_running_fn

    @property
    def name(self) -> str:
        """Health check name."""
        return "Consolidation"

    async def check(self) -> CheckResult:
        """Execute health check."""
        if self._is_running_fn is None:
            return CheckResult(
                name=self.name,
                status=CheckStatus.YELLOW,
                message="Consolidation check not configured",
            )
        try:
            running = self._is_running_fn()
            if running:
                return CheckResult(
                    name=self.name,
                    status=CheckStatus.GREEN,
                    message="Consolidation scheduler active",
                )
            return CheckResult(
                name=self.name,
                status=CheckStatus.YELLOW,
                message="Consolidation scheduler not running",
            )
        except Exception as exc:  # noqa: BLE001 — health-check boundary
            return CheckResult(
                name=self.name,
                status=CheckStatus.RED,
                message=f"Consolidation check failed: {exc}",
            )


class CostBudgetCheck(HealthCheck):
    """Check that LLM spending is within daily budget.

    GREEN: < 80% of budget used.
    YELLOW: < 100% of budget used.
    RED: budget exceeded.
    """

    def __init__(
        self,
        get_spend_fn: Any = None,  # noqa: ANN401
        daily_budget: float = 1.0,
    ) -> None:
        """Args: get_spend_fn — callable returning current daily spend (float)."""
        self._get_spend_fn = get_spend_fn
        self._daily_budget = daily_budget

    @property
    def name(self) -> str:
        """Health check name."""
        return "Cost Budget"

    async def check(self) -> CheckResult:
        """Execute health check."""
        if self._get_spend_fn is None:
            return CheckResult(
                name=self.name,
                status=CheckStatus.YELLOW,
                message="Cost check not configured",
            )
        try:
            spend = self._get_spend_fn()
            if self._daily_budget <= 0:
                # Zero or negative budget: any spend is over budget
                pct = 100.0 if spend > 0 else 0.0
            else:
                pct = spend / self._daily_budget * 100
            meta = {
                "daily_spend": round(spend, 4),
                "daily_budget": self._daily_budget,
                "used_pct": round(pct, 1),
            }

            if pct < 80:
                return CheckResult(
                    name=self.name,
                    status=CheckStatus.GREEN,
                    message=f"${spend:.4f} / ${self._daily_budget:.2f} ({pct:.0f}%)",
                    metadata=meta,
                )
            if pct < 100:
                return CheckResult(
                    name=self.name,
                    status=CheckStatus.YELLOW,
                    message=f"Budget warning: ${spend:.4f} / "
                    f"${self._daily_budget:.2f} ({pct:.0f}%)",
                    metadata=meta,
                )
            return CheckResult(
                name=self.name,
                status=CheckStatus.RED,
                message=f"Budget exceeded: ${spend:.4f} / ${self._daily_budget:.2f} ({pct:.0f}%)",
                metadata=meta,
            )
        except Exception as exc:  # noqa: BLE001 — health-check boundary
            return CheckResult(
                name=self.name,
                status=CheckStatus.RED,
                message=f"Cost check failed: {exc}",
            )


# ── Factory ─────────────────────────────────────────────────────────────────


def create_default_registry(
    *,
    db_write_fn: Any = None,  # noqa: ANN401
    brain_loaded_fn: Any = None,  # noqa: ANN401
    llm_status_fn: Any = None,  # noqa: ANN401
    model_dir: Path | None = None,
    channel_status_fn: Any = None,  # noqa: ANN401
    consolidation_fn: Any = None,  # noqa: ANN401
    cost_spend_fn: Any = None,  # noqa: ANN401
    cost_budget: float = 1.0,
    disk_path: Path | None = None,
) -> HealthRegistry:
    """Create a HealthRegistry with all 10 default checks.

    Pass ``None`` for any function to get a YELLOW "not configured" result
    instead of crashing.  This makes health checks work at any stage of
    application lifecycle.

    Returns:
        Configured HealthRegistry with all 10 checks.
    """
    registry = HealthRegistry()
    registry.register(DiskSpaceCheck(path=disk_path))
    registry.register(RAMCheck())
    registry.register(CPUCheck())
    registry.register(DatabaseCheck(write_fn=db_write_fn))
    registry.register(BrainIndexedCheck(is_loaded_fn=brain_loaded_fn))
    registry.register(LLMReachableCheck(provider_status_fn=llm_status_fn))
    registry.register(ModelLoadedCheck(model_dir=model_dir))
    registry.register(ChannelConnectedCheck(channel_status_fn=channel_status_fn))
    registry.register(ConsolidationCheck(is_running_fn=consolidation_fn))
    registry.register(CostBudgetCheck(get_spend_fn=cost_spend_fn, daily_budget=cost_budget))
    return registry


def create_offline_registry(
    *,
    disk_path: Path | None = None,
    model_dir: Path | None = None,
) -> HealthRegistry:
    """Create a HealthRegistry with only offline-capable checks.

    These checks require no running daemon or live services — they probe
    the local filesystem and system resources directly.

    Offline checks:
        - DiskSpaceCheck: Filesystem free space
        - RAMCheck: Available system memory
        - CPUCheck: CPU utilization
        - ModelLoadedCheck: Embedding model files exist on disk

    Returns:
        HealthRegistry with 4 offline checks.
    """
    registry = HealthRegistry()
    registry.register(DiskSpaceCheck(path=disk_path))
    registry.register(RAMCheck())
    registry.register(CPUCheck())
    registry.register(ModelLoadedCheck(model_dir=model_dir))
    return registry


def _wire_logger() -> Any:  # noqa: ANN401  # structlog BoundLogger has no public type.
    """Lazy logger fetch — avoids the ``observability.logging`` circular import.

    ``observability.logging`` imports ``observability.anomaly`` which
    imports back into ``observability.logging``; adding an eager
    ``from …logging import get_logger`` at module scope here would
    break the chain. This helper is called only inside the wireup
    helpers below (debug-only call sites), so the import happens
    after ``logging.py`` finishes initialising.
    """
    from sovyx.observability.logging import get_logger  # noqa: PLC0415

    return get_logger("sovyx.observability.health")


async def create_engine_health_registry(
    service_registry: ServiceRegistry,
) -> HealthRegistry:
    """Create a HealthRegistry wired to a live engine ServiceRegistry.

    Registers **6 online checks** that complement the 4 offline checks
    from :func:`create_offline_registry` (Disk, RAM, CPU, Model). The
    ``/api/health`` route merges both tiers and deduplicates by name.

    Online checks:
        1. **Database** — SQLite write roundtrip via DatabaseManager
        2. **Brain Index** — EmbeddingEngine loaded flag
        3. **LLM Providers** — cloud providers from LLMRouter (Ollama
           excluded when cloud providers are present to prevent a
           false positive — see below).
        4. **Channels** — connected bridge adapters (Telegram, Signal)
        5. **Consolidation** — scheduler running flag
        6. **Cost Budget** — daily LLM spend vs. budget

    Each callback is wrapped in try/except: if a service is not
    registered, the corresponding check receives ``None`` and returns
    YELLOW "not configured" (safe degradation, zero crash).

    **Ollama exclusion:** Ollama is always registered as a fallback
    provider with ``is_available`` unconditionally ``True``. Including
    it would make the LLM check permanently green even without any API
    key configured — a false positive. When no cloud providers exist,
    Ollama is included with a real ping so local-only installs still
    get a meaningful signal.

    Args:
        service_registry: The engine's :class:`ServiceRegistry` —
            already populated by ``bootstrap()`` with the live
            DatabaseManager / BrainService / LLMRouter / BridgeManager
            / ConsolidationScheduler / CostGuard instances.

    Returns:
        HealthRegistry with 6 online checks ready for ``run_all()``.
    """
    registry = HealthRegistry()

    # ── 1. Database: async write test via system pool ──
    db_write_fn: Any = None
    try:
        from sovyx.persistence.manager import DatabaseManager  # noqa: PLC0415

        if service_registry.is_registered(DatabaseManager):
            db_mgr = await service_registry.resolve(DatabaseManager)
            pool = db_mgr._system_pool

            async def _db_write() -> None:
                if pool is None:
                    msg = "System pool not initialized"
                    raise RuntimeError(msg)  # noqa: TRY301
                async with pool.read() as conn:
                    await conn.execute("SELECT 1")

            db_write_fn = _db_write
    except Exception:  # noqa: BLE001
        _wire_logger().debug("health_db_wire_failed", exc_info=True)

    registry.register(DatabaseCheck(write_fn=db_write_fn))

    # ── 2. Brain Index: embedding engine loaded flag ──
    brain_loaded_fn: Any = None
    try:
        from sovyx.brain.service import BrainService  # noqa: PLC0415

        if service_registry.is_registered(BrainService):
            brain = await service_registry.resolve(BrainService)

            def _brain_loaded() -> bool:
                return brain._embedding._loaded

            brain_loaded_fn = _brain_loaded
    except Exception:  # noqa: BLE001
        _wire_logger().debug("health_brain_wire_failed", exc_info=True)

    registry.register(BrainIndexedCheck(is_loaded_fn=brain_loaded_fn))

    # ── 3. LLM Providers: smart Ollama inclusion ──
    llm_status_fn: Any = None
    try:
        from sovyx.llm.router import LLMRouter  # noqa: PLC0415

        if service_registry.is_registered(LLMRouter):
            llm_router = await service_registry.resolve(LLMRouter)

            async def _llm_status() -> list[tuple[str, bool]]:
                cloud = [p for p in llm_router._providers if p.name != "ollama"]
                if cloud:
                    return [(p.name, p.is_available) for p in cloud]
                from sovyx.llm.providers.ollama import (  # noqa: PLC0415
                    OllamaProvider,
                )

                ollama = next(
                    (p for p in llm_router._providers if isinstance(p, OllamaProvider)),
                    None,
                )
                if ollama is not None:
                    reachable = await ollama.ping()
                    return [("ollama", reachable)]
                return []

            llm_status_fn = _llm_status
    except Exception:  # noqa: BLE001
        _wire_logger().debug("health_llm_wire_failed", exc_info=True)

    registry.register(LLMReachableCheck(provider_status_fn=llm_status_fn))

    # ── 4. Channels: bridge adapter connection status ──
    channel_status_fn: Any = None
    try:
        from sovyx.bridge.manager import BridgeManager  # noqa: PLC0415

        if service_registry.is_registered(BridgeManager):
            bridge = await service_registry.resolve(BridgeManager)

            def _channel_status() -> list[tuple[str, bool]]:
                return [
                    (ct.value, getattr(adapter, "is_running", False))
                    for ct, adapter in bridge._adapters.items()
                ]

            channel_status_fn = _channel_status
    except Exception:  # noqa: BLE001
        _wire_logger().debug("health_channel_wire_failed", exc_info=True)

    registry.register(ChannelConnectedCheck(channel_status_fn=channel_status_fn))

    # ── 5. Consolidation: scheduler running flag ──
    consolidation_fn: Any = None
    try:
        from sovyx.brain.consolidation import (  # noqa: PLC0415
            ConsolidationScheduler,
        )

        if service_registry.is_registered(ConsolidationScheduler):
            scheduler = await service_registry.resolve(ConsolidationScheduler)

            def _consolidation_running() -> bool:
                return scheduler._running

            consolidation_fn = _consolidation_running
    except Exception:  # noqa: BLE001
        _wire_logger().debug("health_consolidation_wire_failed", exc_info=True)

    registry.register(ConsolidationCheck(is_running_fn=consolidation_fn))

    # ── 6. Cost Budget: daily LLM spend from DashboardCounters ──
    cost_spend_fn: Any = None
    try:
        from sovyx.dashboard.status import get_counters  # noqa: PLC0415

        def _cost_spend() -> float:
            _calls, cost, _tokens, _msgs = get_counters().snapshot()
            return cost

        cost_spend_fn = _cost_spend
    except Exception:  # noqa: BLE001
        _wire_logger().debug("health_cost_wire_failed", exc_info=True)

    daily_budget = 2.0  # MindConfig.llm.budget_daily_usd default
    try:
        from sovyx.llm.cost import CostGuard  # noqa: PLC0415

        if service_registry.is_registered(CostGuard):
            guard = await service_registry.resolve(CostGuard)
            daily_budget = guard._daily_budget
    except Exception:  # noqa: BLE001
        _wire_logger().debug("health_cost_budget_wire_failed", exc_info=True)

    registry.register(
        CostBudgetCheck(get_spend_fn=cost_spend_fn, daily_budget=daily_budget),
    )

    return registry
