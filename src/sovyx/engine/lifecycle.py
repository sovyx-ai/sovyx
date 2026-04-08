"""Sovyx LifecycleManager — daemon startup, shutdown, signal handling."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from sovyx.engine.errors import EngineError
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.events import EventBus
    from sovyx.engine.registry import ServiceRegistry

logger = get_logger(__name__)


class PidLock:
    """PID file management with stale detection.

    Locations:
    - /run/sovyx/sovyx.pid (systemd — RuntimeDirectory=sovyx)
    - ~/.sovyx/sovyx.pid (user-space)
    """

    DEFAULT_SYSTEM_PATH: ClassVar[Path] = Path("/run/sovyx/sovyx.pid")
    DEFAULT_USER_PATH: ClassVar[Path] = Path.home() / ".sovyx" / "sovyx.pid"

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or self._default_path()

    def acquire(self) -> None:
        """Write PID file. Raises EngineError if another instance running.

        1. If PID file exists → read PID
        2. If process alive → raise "already running (PID=X)"
        3. If process dead (stale) → remove → proceed
        4. Write current PID
        """
        if self._path.exists():
            try:
                existing_pid = int(self._path.read_text().strip())
            except (ValueError, OSError):
                # Corrupt PID file, remove it
                self._path.unlink(missing_ok=True)
            else:
                if self._is_process_alive(existing_pid):
                    msg = f"Sovyx already running (PID={existing_pid})"
                    raise EngineError(msg)
                logger.warning(
                    "stale_pid_removed",
                    pid=existing_pid,
                    path=str(self._path),
                )
                self._path.unlink(missing_ok=True)

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(str(os.getpid()))
        logger.debug("pid_lock_acquired", path=str(self._path))

    def release(self) -> None:
        """Remove PID file."""
        self._path.unlink(missing_ok=True)
        logger.debug("pid_lock_released", path=str(self._path))

    @staticmethod
    def _is_process_alive(pid: int) -> bool:
        """Check if process exists via os.kill(pid, 0)."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:  # pragma: no cover
            # Process exists but we can't signal it
            return True
        return True

    @staticmethod
    def _default_path() -> Path:  # pragma: no cover
        """Choose system or user path based on permissions."""
        system = PidLock.DEFAULT_SYSTEM_PATH
        if system.parent.exists() and os.access(system.parent, os.W_OK):
            return system
        return PidLock.DEFAULT_USER_PATH


class LifecycleManager:
    """Manage daemon startup and shutdown.

    - PidLock: detect zombie / stale instances
    - Signal handlers: SIGTERM, SIGINT → graceful shutdown
    - Ordered drain: channels → cogloop → brain → persistence
    - Timeout: 10s max for shutdown
    - Emits EngineStarted / EngineStopping events
    """

    SHUTDOWN_TIMEOUT: ClassVar[float] = 10.0

    def __init__(
        self,
        registry: ServiceRegistry,
        event_bus: EventBus,
        pid_path: Path | None = None,
    ) -> None:
        self._registry = registry
        self._events = event_bus
        self._pid = PidLock(pid_path)
        self._shutdown_event = asyncio.Event()
        self._running = False

    async def start(self) -> None:
        """Full startup sequence.

        1. PidLock.acquire()
        2. Signal handlers (SIGTERM, SIGINT)
        3. Start services via registry
        4. Emit EngineStarted
        5. sd_notify(READY=1)
        """
        self._pid.acquire()
        self._install_signal_handlers()
        self._running = True

        # Start services in order
        await self._start_services()

        # Start dashboard server (if API enabled)
        await self._start_dashboard()

        # Print startup banner with dashboard URL + token
        self._print_startup_banner()

        # Emit engine started
        from sovyx.engine.events import EngineStarted

        await self._events.emit(EngineStarted())
        self._notify_systemd("READY=1")

        logger.info("engine_started", pid=os.getpid())

    async def stop(self, reason: str = "requested") -> None:
        """Graceful shutdown.

        Order: channels → cogloop → brain → LLM → persistence → event bus.
        """
        if not self._running:
            return

        self._running = False
        self._notify_systemd("STOPPING=1")

        from sovyx.engine.events import EngineStopping

        await self._events.emit(EngineStopping(reason=reason))

        logger.info("engine_stopping", reason=reason)

        try:
            await asyncio.wait_for(
                self._shutdown_services(),
                timeout=self.SHUTDOWN_TIMEOUT,
            )
        except TimeoutError:
            logger.error("shutdown_timeout", timeout=self.SHUTDOWN_TIMEOUT)

        self._pid.release()
        logger.info("engine_stopped")

    async def run_forever(self) -> None:
        """Block until SIGTERM/SIGINT."""
        await self._shutdown_event.wait()
        await self.stop(reason="signal")

    def _install_signal_handlers(self) -> None:
        """Install SIGTERM and SIGINT handlers."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._handle_signal, sig)

    def _print_startup_banner(self) -> None:
        """Print startup banner with dashboard URL and token.

        DASH-07: Shows users how to access the dashboard immediately
        after `sovyx start`.
        """
        from sovyx.dashboard.server import TOKEN_FILE

        host = "127.0.0.1"
        port = 7777

        try:
            from sovyx.engine.config import EngineConfig

            if self._registry.is_registered(EngineConfig):
                import asyncio

                async def _get_config() -> tuple[str, int]:
                    cfg = await self._registry.resolve(EngineConfig)
                    return cfg.api.host, cfg.api.port

                # Use existing loop if available
                loop = asyncio.get_running_loop()
                task = loop.create_task(_get_config())
                # Can't await in sync — use defaults if not resolved yet
                if task.done():
                    host, port = task.result()
        except Exception:  # noqa: BLE001 — banner is best-effort
            pass

        url = f"http://{host}:{port}"

        token_display = "[not generated]"
        if TOKEN_FILE.exists():
            token_value = TOKEN_FILE.read_text().strip()
            if token_value:
                token_display = token_value

        banner = (
            "\n"
            "╔══════════════════════════════════════════════╗\n"
            "║           🔮 Sovyx — Mind Engine             ║\n"
            "╠══════════════════════════════════════════════╣\n"
            f"║  Dashboard:  {url:<32} ║\n"
            f"║  Token:      {token_display:<32} ║\n"
            "╠══════════════════════════════════════════════╣\n"
            "║  Paste the token in the dashboard login.     ║\n"
            "║  Or run: sovyx token                         ║\n"
            "╚══════════════════════════════════════════════╝\n"
        )

        # Use print() instead of logger for console visibility
        print(banner)  # noqa: T201
        logger.info(
            "startup_banner",
            dashboard_url=url,
            token_available=TOKEN_FILE.exists(),
        )

    def _handle_signal(self, sig: signal.Signals) -> None:
        """Handle shutdown signal."""
        logger.info("signal_received", signal=sig.name)
        self._shutdown_event.set()

    async def _start_dashboard(self) -> None:
        """Start the dashboard server if API is enabled in config."""
        from sovyx.dashboard.server import DashboardServer
        from sovyx.engine.config import APIConfig, EngineConfig

        config: APIConfig | None = None
        if self._registry.is_registered(EngineConfig):
            engine_config = await self._registry.resolve(EngineConfig)
            if not engine_config.api.enabled:
                logger.info("dashboard_disabled")
                return
            config = engine_config.api

        server = DashboardServer(config=config, registry=self._registry)
        await server.start()
        self._registry.register_instance(DashboardServer, server)

        # Wire WebSocket event bridge
        if server.ws_manager is not None:
            from sovyx.dashboard.events import DashboardEventBridge

            bridge = DashboardEventBridge(server.ws_manager, self._events)
            bridge.subscribe_all()

    async def _stop_dashboard(self) -> None:
        """Stop the dashboard server if running."""
        from sovyx.dashboard.server import DashboardServer

        if self._registry.is_registered(DashboardServer):
            server = await self._registry.resolve(DashboardServer)
            await server.stop()

    async def _start_services(self) -> None:
        """Start services that have start() methods."""
        from sovyx.bridge.manager import BridgeManager
        from sovyx.cognitive.gate import CogLoopGate
        from sovyx.cognitive.loop import CognitiveLoop

        # Start cognitive loop
        if self._registry.is_registered(CognitiveLoop):
            loop = await self._registry.resolve(CognitiveLoop)
            await loop.start()

        # Start gate
        if self._registry.is_registered(CogLoopGate):
            gate = await self._registry.resolve(CogLoopGate)
            await gate.start()

        # Start bridge (channels connect last)
        if self._registry.is_registered(BridgeManager):
            bridge = await self._registry.resolve(BridgeManager)
            await bridge.start()

    async def _shutdown_services(self) -> None:
        """Stop services in reverse dependency order.

        Order: dashboard → acceptors → processors → writers → stores.
        ConsolidationScheduler must stop before DatabaseManager
        to prevent writes to a closed pool (P28).
        """
        # 0. Stop dashboard (stop accepting HTTP/WS)
        await self._stop_dashboard()

        from sovyx.brain.consolidation import ConsolidationScheduler
        from sovyx.bridge.manager import BridgeManager
        from sovyx.cognitive.gate import CogLoopGate
        from sovyx.cognitive.loop import CognitiveLoop
        from sovyx.llm.router import LLMRouter
        from sovyx.persistence.manager import DatabaseManager

        # 1. Stop channels (stop accepting new messages)
        if self._registry.is_registered(BridgeManager):
            bridge = await self._registry.resolve(BridgeManager)
            await bridge.stop()

        # 2. Drain gate (finish in-flight requests)
        if self._registry.is_registered(CogLoopGate):
            gate = await self._registry.resolve(CogLoopGate)
            await gate.stop()

        # 3. Stop cognitive loop
        if self._registry.is_registered(CognitiveLoop):
            loop = await self._registry.resolve(CognitiveLoop)
            await loop.stop()

        # 4. Stop consolidation scheduler (writes to brain DB)
        if self._registry.is_registered(ConsolidationScheduler):
            scheduler = await self._registry.resolve(ConsolidationScheduler)
            await scheduler.stop()

        # 5. Stop LLM router (close HTTP clients)
        if self._registry.is_registered(LLMRouter):
            router = await self._registry.resolve(LLMRouter)
            await router.stop()

        # 6. Stop database (last — everything else must be stopped)
        if self._registry.is_registered(DatabaseManager):
            db = await self._registry.resolve(DatabaseManager)
            await db.stop()

    @staticmethod
    def _notify_systemd(status: str) -> None:
        """Send sd_notify if NOTIFY_SOCKET is set.

        Silent if systemd not present (desktop/Docker).
        """
        notify_socket = os.environ.get("NOTIFY_SOCKET")
        if not notify_socket:
            return

        try:  # pragma: no cover
            addr = notify_socket
            if addr.startswith("@"):
                addr = "\0" + addr[1:]
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            try:
                sock.connect(addr)
                sock.sendall(status.encode())
            finally:
                sock.close()
        except OSError:  # pragma: no cover
            logger.debug("sd_notify_failed", status=status)
