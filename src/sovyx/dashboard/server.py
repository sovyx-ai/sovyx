"""Sovyx Dashboard Server — FastAPI application with WebSocket support.

Serves:
- /api/* REST endpoints (status, health, conversations, brain, logs, settings)
- /ws WebSocket for real-time events
- /* Static files (Vite build) with SPA fallback

Integrated into Engine lifecycle via start()/stop().
"""

from __future__ import annotations

import asyncio
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_503_SERVICE_UNAVAILABLE

from sovyx import __version__
from sovyx.dashboard import STATIC_DIR
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.config import APIConfig
    from sovyx.engine.registry import ServiceRegistry
    from sovyx.observability.health import HealthRegistry

logger = get_logger(__name__)

# ── Token Management ──

TOKEN_FILE = Path.home() / ".sovyx" / "token"

# ── Upload Limits ──

MAX_IMPORT_BYTES = 100 * 1024 * 1024  # 100 MiB — hard cap on /api/import uploads.
_IMPORT_CHUNK_BYTES = 1 * 1024 * 1024  # 1 MiB streaming read chunk size.


def _ensure_token() -> str:
    """Read or generate the dashboard auth token."""
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        if token:
            return token
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token)
    TOKEN_FILE.chmod(0o600)
    logger.info("dashboard_token_generated", path=str(TOKEN_FILE))
    return token


# Will be set during create_app()
_server_token: str = ""


# ── WebSocket Connection Manager ──


class ConnectionManager:
    """Manage WebSocket connections for real-time event broadcasting."""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        """Accept and register a WebSocket connection."""
        await websocket.accept()
        async with self._lock:
            self._connections.append(websocket)
        logger.debug("ws_connected", count=len(self._connections))

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket connection."""
        async with self._lock:
            if websocket in self._connections:
                self._connections.remove(websocket)
        logger.debug("ws_disconnected", count=len(self._connections))

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Send JSON message to all connected clients.

        Copies the connection list and releases the lock before sending,
        so a slow client doesn't block other sends or connect/disconnect.
        """
        async with self._lock:
            snapshot = list(self._connections)

        if not snapshot:
            return

        stale: list[WebSocket] = []
        for ws in snapshot:
            try:
                await ws.send_json(message)
            except Exception:  # noqa: BLE001
                stale.append(ws)

        if stale:
            async with self._lock:
                for ws in stale:
                    if ws in self._connections:
                        self._connections.remove(ws)

    @property
    def active_count(self) -> int:
        """Number of active WebSocket connections."""
        return len(self._connections)


# ── Request ID Middleware ──


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach a unique request ID to every request/response.

    - Reads ``X-Request-Id`` from incoming headers (proxy-forwarded)
    - Generates a UUID4 if absent
    - Sets ``request.state.request_id`` for downstream use
    - Echoes ``X-Request-Id`` in the response for client correlation
    """

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Inject request ID into request state and response headers."""
        import uuid

        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response


# ── Security Headers Middleware ──


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses.

    Headers:
    - X-Content-Type-Options: nosniff (prevent MIME sniffing)
    - X-Frame-Options: DENY (prevent clickjacking)
    - Referrer-Policy: strict-origin-when-cross-origin
    - Content-Security-Policy: restrictive CSP for dashboard
    - Permissions-Policy: disable unnecessary browser features
    """

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Apply security headers to all responses."""
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        # CSP: allow self + inline styles (Tailwind) + wss for WebSocket
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "font-src 'self' data:; "
            "img-src 'self' data: blob:; "
            "connect-src 'self' ws: wss:; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        return response


# ── App Factory ──


def _empty_stats_totals() -> dict[str, object]:
    """Default empty totals for /api/stats/history."""
    return {"cost": 0.0, "messages": 0, "llm_calls": 0, "tokens": 0, "days_active": 0}


def _empty_stats_month() -> dict[str, object]:
    """Default empty month totals for /api/stats/history."""
    return {"cost": 0.0, "messages": 0, "llm_calls": 0, "tokens": 0}


def create_app(config: APIConfig | None = None, *, token: str | None = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        config: API configuration. Uses defaults if None.
        token: Override auth token (tests). If None, reads/generates from TOKEN_FILE.
    """
    global _server_token  # noqa: PLW0603
    _server_token = token if token is not None else _ensure_token()

    app = FastAPI(
        title="Sovyx Dashboard",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
    )

    # CORS
    origins = config.cors_origins if config else ["http://localhost:7777"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Request ID tracing
    app.add_middleware(RequestIdMiddleware)

    # Security headers
    app.add_middleware(SecurityHeadersMiddleware)

    # Rate limiting
    from sovyx.dashboard.rate_limit import RateLimitMiddleware

    app.add_middleware(RateLimitMiddleware)

    # Shared state
    ws_manager = ConnectionManager()
    app.state.ws_manager = ws_manager
    app.state.auth_token = _server_token

    # Conversation-import tracker — process-local, one per app instance.
    # Read by routes/conversation_import.py via request.app.state.
    from sovyx.upgrade.conv_import import ImportProgressTracker

    app.state.import_tracker = ImportProgressTracker()

    # ── Auth dependency (using Header) ──

    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

    _security = HTTPBearer(auto_error=False)

    _security_dep = Depends(_security)

    async def verify_token(
        credentials: HTTPAuthorizationCredentials | None = _security_dep,  # noqa: B008
    ) -> str:
        """Verify a dashboard authentication token."""
        if credentials is None:
            raise HTTPException(
                status_code=HTTP_401_UNAUTHORIZED,
                detail="Missing Authorization header",
            )
        if not secrets.compare_digest(credentials.credentials, _server_token):
            raise HTTPException(
                status_code=HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
            )
        return credentials.credentials

    # ── API Routes ──

    from sovyx.dashboard.routes import (
        activity as activity_routes,
    )
    from sovyx.dashboard.routes import (
        brain as brain_routes,
    )
    from sovyx.dashboard.routes import (
        channels as channels_routes,
    )
    from sovyx.dashboard.routes import (
        chat as chat_routes,
    )
    from sovyx.dashboard.routes import (
        config as config_routes,
    )
    from sovyx.dashboard.routes import (
        conversation_import as conversation_import_routes,
    )
    from sovyx.dashboard.routes import (
        conversations as conversations_routes,
    )
    from sovyx.dashboard.routes import (
        data as data_routes,
    )
    from sovyx.dashboard.routes import (
        logs as logs_routes,
    )
    from sovyx.dashboard.routes import (
        plugins as plugins_routes,
    )
    from sovyx.dashboard.routes import (
        providers as providers_routes,
    )
    from sovyx.dashboard.routes import (
        safety as safety_routes,
    )
    from sovyx.dashboard.routes import (
        settings as settings_routes,
    )
    from sovyx.dashboard.routes import (
        setup as setup_routes,
    )
    from sovyx.dashboard.routes import (
        status as status_routes,
    )
    from sovyx.dashboard.routes import (
        telemetry as telemetry_routes,
    )
    from sovyx.dashboard.routes import (
        voice as voice_routes,
    )
    from sovyx.dashboard.routes import (
        websocket as ws_routes,
    )

    app.include_router(status_routes.router)
    app.include_router(status_routes.metrics_router)
    app.include_router(conversations_routes.router)
    app.include_router(brain_routes.router)
    app.include_router(activity_routes.router)
    app.include_router(logs_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(data_routes.router)
    app.include_router(safety_routes.router)
    app.include_router(voice_routes.router)
    app.include_router(plugins_routes.router)
    app.include_router(config_routes.router)
    app.include_router(providers_routes.router)
    app.include_router(channels_routes.router)
    app.include_router(chat_routes.router)
    app.include_router(conversation_import_routes.router)
    app.include_router(telemetry_routes.router)
    app.include_router(setup_routes.router)
    app.include_router(ws_routes.router)

    # ── Static Files + SPA Fallback ──

    if STATIC_DIR.exists() and (STATIC_DIR / "index.html").exists():
        # Serve static assets (JS, CSS, images)
        app.mount(
            "/assets",
            StaticFiles(directory=str(STATIC_DIR / "assets")),
            name="static-assets",
        )

        _static_root = STATIC_DIR.resolve()

        @app.get("/{path:path}")
        async def spa_fallback(path: str) -> FileResponse:
            """SPA fallback — serve index.html for all non-API routes."""
            # Check if a static file exists — with path traversal protection
            file_path = (STATIC_DIR / path).resolve()
            if (
                file_path.is_file()
                and ".." not in path
                and str(file_path).startswith(str(_static_root))
            ):
                return FileResponse(str(file_path))
            # Otherwise serve index.html (SPA routing)
            return FileResponse(str(STATIC_DIR / "index.html"))

    else:
        logger.warning(
            "dashboard_static_missing",
            path=str(STATIC_DIR),
            hint="Run 'npm run build' in dashboard/ to generate static files",
        )

        @app.get("/{path:path}")
        async def no_dashboard(path: str) -> JSONResponse:
            """Placeholder when dashboard isn't built."""
            return JSONResponse(
                {"error": "Dashboard not built. Run 'npm run build' in dashboard/"},
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )

    return app


# ── Server Runner ──


class DashboardServer:
    """Manages the uvicorn server lifecycle.

    Integrates with Engine startup/shutdown.
    """

    def __init__(
        self,
        config: APIConfig | None = None,
        registry: ServiceRegistry | None = None,
    ) -> None:
        self._config = config
        self._registry = registry
        self._server: Any | None = None
        self._app: FastAPI | None = None

    async def _create_health_registry(self) -> HealthRegistry:
        """Create an online HealthRegistry wired to the engine ServiceRegistry.

        Registers **6 online-only checks** that complement the 4 offline
        checks from ``create_offline_registry()`` (Disk, RAM, CPU, Model).
        No overlap — the ``/api/health`` endpoint merges both tiers and
        deduplicates by name (online wins if a collision ever occurs).

        Online checks:
            1. **Database** — SQLite write roundtrip via DatabaseManager
            2. **Brain Index** — EmbeddingEngine loaded flag
            3. **LLM Providers** — cloud providers from LLMRouter (Ollama
               excluded to prevent false positive — see docstring below)
            4. **Channels** — connected bridge adapters (Telegram, Signal)
            5. **Consolidation** — scheduler running flag
            6. **Cost Budget** — daily LLM spend vs budget

        Each callback is wrapped in try/except: if a service is not registered,
        the corresponding check receives ``None`` and returns YELLOW
        "not configured" (safe degradation, zero crash).

        **Ollama exclusion:** Ollama is always registered as a fallback
        provider with ``is_available`` unconditionally ``True``.  Including it
        would make the LLM check permanently green even without any API key
        configured — a false positive.  Ollama-only users are detected via
        ``llm_calls_today > 0`` on the frontend instead (layer 2 fallback).

        Returns:
            HealthRegistry with 6 online checks.
        """
        from sovyx.observability.health import (
            BrainIndexedCheck,
            ChannelConnectedCheck,
            ConsolidationCheck,
            CostBudgetCheck,
            DatabaseCheck,
            HealthRegistry,
            LLMReachableCheck,
        )

        registry = HealthRegistry()

        # ── 1. Database: async write test via system pool ──
        db_write_fn = None
        try:
            from sovyx.persistence.manager import DatabaseManager

            if self._registry is not None and self._registry.is_registered(DatabaseManager):
                db_mgr = await self._registry.resolve(DatabaseManager)
                pool = db_mgr._system_pool

                async def _db_write() -> None:
                    if pool is None:
                        msg = "System pool not initialized"
                        raise RuntimeError(msg)  # noqa: TRY301
                    async with pool.read() as conn:
                        await conn.execute("SELECT 1")

                db_write_fn = _db_write
        except Exception:  # noqa: BLE001
            logger.debug("health_db_wire_failed")

        registry.register(DatabaseCheck(write_fn=db_write_fn))

        # ── 2. Brain Index: embedding engine loaded flag ──
        brain_loaded_fn = None
        try:
            from sovyx.brain.service import BrainService

            if self._registry is not None and self._registry.is_registered(BrainService):
                brain = await self._registry.resolve(BrainService)

                def _brain_loaded() -> bool:
                    return brain._embedding._loaded

                brain_loaded_fn = _brain_loaded
        except Exception:  # noqa: BLE001
            logger.debug("health_brain_wire_failed")

        registry.register(BrainIndexedCheck(is_loaded_fn=brain_loaded_fn))

        # ── 3. LLM Providers: smart Ollama inclusion ──
        # Cloud providers present → exclude Ollama (avoids false positive from
        #   always-verified local installs that aren't the primary provider).
        # No cloud providers → include Ollama with a real ping (it's primary).
        llm_status_fn = None
        try:
            from sovyx.llm.router import LLMRouter

            if self._registry is not None and self._registry.is_registered(LLMRouter):
                router = await self._registry.resolve(LLMRouter)

                async def _llm_status() -> list[tuple[str, bool]]:
                    cloud = [p for p in router._providers if p.name != "ollama"]
                    if cloud:
                        # Cloud providers configured — report only those
                        return [(p.name, p.is_available) for p in cloud]
                    # No cloud — Ollama is the primary provider.
                    # Use real ping for accurate status.
                    from sovyx.llm.providers.ollama import OllamaProvider

                    ollama = next(
                        (p for p in router._providers if isinstance(p, OllamaProvider)),
                        None,
                    )
                    if ollama is not None:
                        reachable = await ollama.ping()
                        return [("ollama", reachable)]
                    return []

                llm_status_fn = _llm_status
        except Exception:  # noqa: BLE001
            logger.debug("health_llm_wire_failed")

        registry.register(LLMReachableCheck(provider_status_fn=llm_status_fn))

        # ── 4. Channels: bridge adapter connection status ──
        channel_status_fn = None
        try:
            from sovyx.bridge.manager import BridgeManager

            if self._registry is not None and self._registry.is_registered(BridgeManager):
                bridge = await self._registry.resolve(BridgeManager)

                def _channel_status() -> list[tuple[str, bool]]:
                    return [
                        (ct.value, getattr(adapter, "is_running", False))
                        for ct, adapter in bridge._adapters.items()
                    ]

                channel_status_fn = _channel_status
        except Exception:  # noqa: BLE001
            logger.debug("health_channel_wire_failed")

        registry.register(ChannelConnectedCheck(channel_status_fn=channel_status_fn))

        # ── 5. Consolidation: scheduler running flag ──
        consolidation_fn = None
        try:
            from sovyx.brain.consolidation import ConsolidationScheduler

            if self._registry is not None and self._registry.is_registered(
                ConsolidationScheduler,
            ):
                scheduler = await self._registry.resolve(ConsolidationScheduler)

                def _consolidation_running() -> bool:
                    return scheduler._running

                consolidation_fn = _consolidation_running
        except Exception:  # noqa: BLE001
            logger.debug("health_consolidation_wire_failed")

        registry.register(ConsolidationCheck(is_running_fn=consolidation_fn))

        # ── 6. Cost Budget: daily LLM spend from DashboardCounters ──
        cost_spend_fn = None
        try:
            from sovyx.dashboard.status import get_counters

            def _cost_spend() -> float:
                _calls, cost, _tokens, _msgs = get_counters().snapshot()
                return cost

            cost_spend_fn = _cost_spend
        except Exception:  # noqa: BLE001
            logger.debug("health_cost_wire_failed")

        # Budget from CostGuard (authoritative — set during bootstrap)
        daily_budget = 2.0  # MindConfig.llm.budget_daily_usd default
        try:
            from sovyx.llm.cost import CostGuard

            if self._registry is not None and self._registry.is_registered(CostGuard):
                guard = await self._registry.resolve(CostGuard)
                daily_budget = guard._daily_budget
        except Exception:  # noqa: BLE001
            logger.debug("health_cost_budget_wire_failed")

        registry.register(CostBudgetCheck(get_spend_fn=cost_spend_fn, daily_budget=daily_budget))

        return registry

    async def _resolve_log_file(self) -> Path | None:
        """Resolve the log file path for dashboard log queries.

        Resolution order:
            1. EngineConfig from registry (authoritative — same instance
               the bootstrap configured, respects data_dir, env vars, YAML).
            2. Fresh EngineConfig() as fallback (if registry unavailable).

        Returns:
            Resolved log file path, or None if resolution fails entirely.
        """
        from sovyx.engine.config import EngineConfig

        # 1. Try registry (authoritative source)
        if self._registry is not None and self._registry.is_registered(EngineConfig):
            try:
                engine_config = await self._registry.resolve(EngineConfig)
                logger.debug(
                    "log_file_resolved_from_registry",
                    path=str(engine_config.log.log_file),
                )
                return engine_config.log.log_file
            except Exception:  # noqa: BLE001
                logger.warning("log_file_registry_resolve_failed")

        # 2. Fallback: fresh EngineConfig (reads env + defaults)
        try:
            engine_config = EngineConfig()
            logger.warning(
                "log_file_resolved_fallback",
                path=str(engine_config.log.log_file),
                hint="EngineConfig not in registry; using defaults",
            )
            return engine_config.log.log_file
        except Exception:  # noqa: BLE001
            logger.error("log_file_resolve_failed_entirely")
            return None

    @property
    def app(self) -> FastAPI | None:
        """ASGI application instance."""
        return self._app

    @property
    def ws_manager(self) -> ConnectionManager | None:
        """WebSocket connection manager."""
        if self._app:
            mgr: ConnectionManager = self._app.state.ws_manager
            return mgr
        return None

    async def start(self) -> None:
        """Start the dashboard server (non-blocking)."""
        import uvicorn

        self._app = create_app(self._config)

        # Wire services if registry available
        if self._registry is not None:
            from sovyx.dashboard.status import StatusCollector

            self._app.state.status_collector = StatusCollector(self._registry)
            self._app.state.registry = self._registry

            # Wire online health checks so /api/health exposes LLM, Brain, etc.
            self._app.state.health_registry = await self._create_health_registry()

            # Wire MindConfig from PersonalityEngine (if registered)
            try:
                from sovyx.mind.personality import PersonalityEngine

                if self._registry.is_registered(PersonalityEngine):
                    personality = await self._registry.resolve(PersonalityEngine)
                    self._app.state.mind_config = personality.config
            except Exception:  # noqa: BLE001
                logger.debug("mind_config_wire_failed")

            # Wire mind.yaml path for LLM config persistence (PUT /api/providers)
            try:
                from sovyx.engine.config import EngineConfig

                if self._registry.is_registered(EngineConfig):
                    eng_cfg = await self._registry.resolve(EngineConfig)
                    # v0.5: single mind "aria" — resolve path from data_dir
                    yaml_path = eng_cfg.database.data_dir / "aria" / "mind.yaml"
                    if yaml_path.exists():
                        self._app.state.mind_yaml_path = yaml_path
                        logger.debug("mind_yaml_path_wired", path=str(yaml_path))
            except Exception:  # noqa: BLE001
                logger.debug("mind_yaml_path_wire_failed")

        # Wire log file path for log queries.
        # Resolve from registry first (same config the bootstrap used),
        # fall back to a fresh EngineConfig only if registry is unavailable.
        self._app.state.log_file = await self._resolve_log_file()

        host = self._config.host if self._config else "127.0.0.1"
        port = self._config.port if self._config else 7777

        uvi_config = uvicorn.Config(
            app=self._app,
            host=host,
            port=port,
            log_level="warning",  # Sovyx handles its own logging
            access_log=False,
        )
        self._server = uvicorn.Server(uvi_config)

        # Run in background task
        asyncio.create_task(self._server.serve())

        logger.info(
            "dashboard_started",
            host=host,
            port=port,
            token_path=str(TOKEN_FILE),
        )

    async def stop(self) -> None:
        """Graceful shutdown."""
        if self._server:
            self._server.should_exit = True
            logger.info("dashboard_stopped")
