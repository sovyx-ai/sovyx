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
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_503_SERVICE_UNAVAILABLE

from sovyx.dashboard import STATIC_DIR
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.config import APIConfig
    from sovyx.engine.registry import ServiceRegistry

logger = get_logger(__name__)

# ── Token Management ──

TOKEN_FILE = Path.home() / ".sovyx" / "token"


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


def create_app(config: APIConfig | None = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        config: API configuration. Uses defaults if None.
    """
    global _server_token  # noqa: PLW0603
    _server_token = _ensure_token()

    app = FastAPI(
        title="Sovyx Dashboard",
        version="0.1.0",
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

    # Security headers
    app.add_middleware(SecurityHeadersMiddleware)

    # Shared state
    ws_manager = ConnectionManager()
    app.state.ws_manager = ws_manager
    app.state.auth_token = _server_token

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

    @app.get("/api/status", dependencies=[Depends(verify_token)])
    async def get_status() -> JSONResponse:
        """System status overview."""
        collector = getattr(app.state, "status_collector", None)
        if collector is not None:
            from sovyx.dashboard.status import StatusCollector

            if not isinstance(collector, StatusCollector):
                msg = f"status_collector is {type(collector)}, expected StatusCollector"
                raise TypeError(msg)
            snapshot = await collector.collect()
            return JSONResponse(snapshot.to_dict())

        # Fallback when no registry is wired (e.g., tests, standalone)
        from sovyx import __version__

        return JSONResponse(
            {
                "version": __version__,
                "uptime_seconds": 0,
                "mind_name": "sovyx",
                "active_conversations": 0,
                "memory_concepts": 0,
                "memory_episodes": 0,
                "llm_cost_today": 0.0,
                "llm_calls_today": 0,
                "tokens_today": 0,
                "messages_today": 0,
            }
        )

    @app.get("/api/health", dependencies=[Depends(verify_token)])
    async def get_health() -> JSONResponse:
        """Health check results."""
        from sovyx.observability.health import (
            HealthRegistry,
            create_offline_registry,
        )

        all_results = []

        # Tier 1: Offline checks (always available)
        offline = create_offline_registry()
        offline_results = await offline.run_all(timeout=10.0)
        all_results.extend(offline_results)

        # Tier 2: Online checks (if registry has a HealthRegistry)
        health_reg = getattr(app.state, "health_registry", None)
        if health_reg is not None and isinstance(health_reg, HealthRegistry):
            online_results = await health_reg.run_all(timeout=10.0)
            all_results.extend(online_results)

        # Compute overall status
        overall = HealthRegistry().summary(all_results)

        checks_json = [
            {
                "name": r.name,
                "status": r.status.value,
                "message": r.message,
                **({"latency_ms": r.metadata["latency_ms"]} if "latency_ms" in r.metadata else {}),
            }
            for r in all_results
        ]

        return JSONResponse(
            {
                "overall": overall.value,
                "checks": checks_json,
            }
        )

    @app.get("/metrics")
    async def prometheus_metrics() -> Response:
        """Prometheus scrape endpoint — OpenMetrics text format.

        No authentication required (Prometheus scrapers don't send Bearer).
        Reads from the OTel InMemoryMetricReader and converts to Prometheus
        exposition format.
        """
        reader = getattr(app.state, "metrics_reader", None)
        if reader is None:
            return Response(
                content="# No metrics available\n",
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )

        from sovyx.observability.prometheus import PrometheusExporter

        exporter = PrometheusExporter(reader)
        text = exporter.export()
        return Response(
            content=text or "# No metrics collected yet\n",
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/api/conversations", dependencies=[Depends(verify_token)])
    async def get_conversations(
        limit: int = Query(default=50, ge=0, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        """List conversations ordered by most recent activity."""
        registry = getattr(app.state, "registry", None)
        if registry is not None:
            from sovyx.dashboard.conversations import list_conversations

            convos = await list_conversations(registry, limit=limit, offset=offset)
            return JSONResponse({"conversations": convos})
        return JSONResponse({"conversations": []})

    @app.get("/api/conversations/{conversation_id}", dependencies=[Depends(verify_token)])
    async def get_conversation_detail(
        conversation_id: str,
        limit: int = Query(default=100, ge=0, le=1000),
    ) -> JSONResponse:
        """Get messages for a specific conversation."""
        registry = getattr(app.state, "registry", None)
        if registry is not None:
            from sovyx.dashboard.conversations import get_conversation_messages

            messages = await get_conversation_messages(
                registry,
                conversation_id,
                limit=limit,
            )
            return JSONResponse({"conversation_id": conversation_id, "messages": messages})
        return JSONResponse({"conversation_id": conversation_id, "messages": []})

    @app.get("/api/brain/graph", dependencies=[Depends(verify_token)])
    async def get_brain_graph(limit: int = Query(default=200, ge=0, le=1000)) -> JSONResponse:
        """Brain knowledge graph (nodes + links for react-force-graph-2d)."""
        registry = getattr(app.state, "registry", None)
        if registry is not None:
            from sovyx.dashboard.brain import get_brain_graph as _get_graph

            graph = await _get_graph(registry, limit=limit)
            return JSONResponse(graph)
        return JSONResponse({"nodes": [], "links": []})

    @app.get("/api/brain/search", dependencies=[Depends(verify_token)])
    async def brain_search(
        q: str = Query(default="", max_length=500),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> JSONResponse:
        """Semantic search over brain concepts (hybrid FTS+vector)."""
        registry = getattr(app.state, "registry", None)
        if registry is not None:
            from sovyx.dashboard.brain import search_brain

            results = await search_brain(registry, q, limit=limit)
            return JSONResponse({"results": results, "query": q})
        return JSONResponse({"results": [], "query": q})

    @app.get("/api/logs", dependencies=[Depends(verify_token)])
    async def get_logs(
        level: str | None = None,
        module: str | None = None,
        search: str | None = None,
        limit: int = Query(default=100, ge=0, le=1000),
    ) -> JSONResponse:
        """Query structured JSON logs with filters."""
        from sovyx.dashboard.logs import query_logs

        log_file = getattr(app.state, "log_file", None)
        entries = query_logs(
            log_file,
            level=level,
            module=module,
            search=search,
            limit=limit,
        )
        return JSONResponse({"entries": entries})

    @app.get("/api/settings", dependencies=[Depends(verify_token)])
    async def get_settings() -> JSONResponse:
        """Current engine settings."""
        from sovyx.dashboard.settings import get_settings as _get_settings
        from sovyx.engine.config import EngineConfig

        config = getattr(app.state, "engine_config", None)
        if config is None:
            try:
                config = EngineConfig()
            except Exception:  # noqa: BLE001
                return JSONResponse({"log_level": "INFO", "data_dir": str(Path.home() / ".sovyx")})

        return JSONResponse(_get_settings(config))

    @app.put("/api/settings", dependencies=[Depends(verify_token)])
    async def update_settings(request: Request) -> JSONResponse:
        """Update mutable settings (e.g. log_level)."""
        from sovyx.dashboard.settings import apply_settings

        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(
                {"ok": False, "error": "Invalid JSON body"},
                status_code=422,
            )

        if not isinstance(body, dict):
            return JSONResponse(
                {"ok": False, "error": "Expected JSON object"},
                status_code=422,
            )
        config = getattr(app.state, "engine_config", None)
        if config is None:
            from sovyx.engine.config import EngineConfig

            try:
                config = EngineConfig()
                app.state.engine_config = config
            except Exception:  # noqa: BLE001
                return JSONResponse({"ok": False, "error": "no config"}, status_code=500)

        config_path = getattr(app.state, "config_path", None)
        changes = apply_settings(config, body, config_path=config_path)

        return JSONResponse({"ok": True, "changes": changes})

    # ── Mind Config (personality, OCEAN, safety) ──

    @app.get("/api/config", dependencies=[Depends(verify_token)])
    async def get_config() -> JSONResponse:
        """Current mind configuration (personality, OCEAN, safety, brain, LLM)."""
        from sovyx.dashboard.config import get_config as _get_config

        mind_config = getattr(app.state, "mind_config", None)
        if mind_config is None:
            return JSONResponse(
                {"error": "No mind configuration loaded"},
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )

        return JSONResponse(_get_config(mind_config))

    @app.put("/api/config", dependencies=[Depends(verify_token)])
    async def update_config(request: Request) -> JSONResponse:
        """Update mutable mind config (personality, OCEAN, safety, name, language, timezone)."""
        from sovyx.dashboard.config import apply_config

        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(
                {"ok": False, "error": "Invalid JSON body"},
                status_code=422,
            )

        if not isinstance(body, dict):
            return JSONResponse(
                {"ok": False, "error": "Expected JSON object"},
                status_code=422,
            )

        mind_config = getattr(app.state, "mind_config", None)
        if mind_config is None:
            return JSONResponse(
                {"ok": False, "error": "No mind configuration loaded"},
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )

        mind_yaml_path = getattr(app.state, "mind_yaml_path", None)
        changes = apply_config(mind_config, body, mind_yaml_path=mind_yaml_path)

        # Broadcast config change event to WebSocket clients
        if changes:
            await ws_manager.broadcast(
                {
                    "type": "ConfigUpdated",
                    "data": {"changes": changes},
                }
            )

        return JSONResponse({"ok": True, "changes": changes})

    # ── Channels (active channel status) ──

    @app.get("/api/channels", dependencies=[Depends(verify_token)])
    async def channels(request: Request) -> JSONResponse:
        """Return active channel status.

        Lists all available channels and whether they are connected.
        Dashboard is always active when the engine is running.
        """
        registry = getattr(app.state, "registry", None)
        channel_list: list[dict[str, object]] = [
            {
                "name": "dashboard",
                "type": "dashboard",
                "connected": registry is not None,
            },
        ]

        if registry is not None:
            # Check registered channel adapters
            try:
                from sovyx.bridge.channels.telegram import TelegramChannel

                if registry.is_registered(TelegramChannel):
                    channel_list.append(
                        {
                            "name": "Telegram",
                            "type": "telegram",
                            "connected": True,
                        }
                    )
                else:
                    channel_list.append(
                        {
                            "name": "Telegram",
                            "type": "telegram",
                            "connected": False,
                        }
                    )
            except ImportError:
                channel_list.append(
                    {
                        "name": "Telegram",
                        "type": "telegram",
                        "connected": False,
                    }
                )

            try:
                from sovyx.bridge.channels.signal import SignalChannel

                if registry.is_registered(SignalChannel):
                    channel_list.append(
                        {
                            "name": "Signal",
                            "type": "signal",
                            "connected": True,
                        }
                    )
                else:
                    channel_list.append(
                        {
                            "name": "Signal",
                            "type": "signal",
                            "connected": False,
                        }
                    )
            except ImportError:
                channel_list.append(
                    {
                        "name": "Signal",
                        "type": "signal",
                        "connected": False,
                    }
                )
        else:
            channel_list.extend(
                [
                    {"name": "Telegram", "type": "telegram", "connected": False},
                    {"name": "Signal", "type": "signal", "connected": False},
                ]
            )

        return JSONResponse({"channels": channel_list})

    # ── Chat (direct conversation via dashboard) ──

    @app.post("/api/chat", dependencies=[Depends(verify_token)])
    async def chat(request: Request) -> JSONResponse:
        """Send a message and get AI response — no external channel needed.

        Request body:
            message (str): User message text. Required.
            user_name (str): Display name. Default "Dashboard".
            conversation_id (str|null): Continue existing conversation.

        Returns:
            JSON with response, conversation_id, mind_id, timestamp.
        """
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(
                {"error": "Invalid JSON body"},
                status_code=422,
            )

        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "Expected JSON object"},
                status_code=422,
            )

        message_text = body.get("message")
        if not message_text or not isinstance(message_text, str) or not message_text.strip():
            return JSONResponse(
                {"error": "Field 'message' is required and must be a non-empty string"},
                status_code=422,
            )

        user_name = body.get("user_name", "Dashboard")
        if not isinstance(user_name, str):
            user_name = "Dashboard"

        conversation_id = body.get("conversation_id")
        if conversation_id is not None and not isinstance(conversation_id, str):
            return JSONResponse(
                {"error": "Field 'conversation_id' must be a string or null"},
                status_code=422,
            )

        registry = getattr(app.state, "registry", None)
        if registry is None:
            return JSONResponse(
                {"error": "Engine not running — no registry available"},
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )

        from sovyx.dashboard.chat import handle_chat_message

        try:
            result = await handle_chat_message(
                registry=registry,
                message=message_text,
                user_name=user_name,
                conversation_id=conversation_id,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        except Exception:
            logger.exception("dashboard_chat_failed")
            return JSONResponse(
                {"error": "Failed to process message. Please try again."},
                status_code=500,
            )

        # Broadcast chat event to WebSocket clients for real-time updates
        await ws_manager.broadcast(
            {
                "type": "ChatMessage",
                "data": {
                    "conversation_id": result["conversation_id"],
                    "response_preview": result["response"][:200] if result["response"] else "",
                },
            }
        )

        return JSONResponse(result)

    # ── WebSocket ──

    @app.websocket("/ws")
    async def websocket_endpoint(
        websocket: WebSocket,
        token: str | None = Query(default=None),
    ) -> None:
        """Real-time event stream.

        Auth via query param: /ws?token=<token>
        (WebSocket doesn't support Authorization header easily)
        """
        if not token or not secrets.compare_digest(token, _server_token):
            await websocket.close(code=4001, reason="Unauthorized")
            return

        await ws_manager.connect(websocket)
        try:
            while True:
                # Keep connection alive, handle client pings
                data = await websocket.receive_text()
                if data == "ping":
                    await websocket.send_text("pong")
        except WebSocketDisconnect:
            logger.debug("ws_client_disconnected")
        finally:
            await ws_manager.disconnect(websocket)

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

            # Wire MindConfig from PersonalityEngine (if registered)
            try:
                from sovyx.mind.personality import PersonalityEngine

                if self._registry.is_registered(PersonalityEngine):
                    personality = await self._registry.resolve(PersonalityEngine)
                    self._app.state.mind_config = personality._config  # noqa: SLF001
            except Exception:  # noqa: BLE001
                logger.debug("mind_config_wire_failed")

        # Wire log file path for log queries
        if self._config is not None:
            from sovyx.engine.config import EngineConfig

            try:
                engine_config = EngineConfig()
                self._app.state.log_file = engine_config.log.log_file
            except Exception:  # noqa: BLE001
                logger.debug("engine_config_load_failed")

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
