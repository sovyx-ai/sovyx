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

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_503_SERVICE_UNAVAILABLE

from sovyx.dashboard import STATIC_DIR
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.config import APIConfig

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


# ── Auth Dependency ──


def _verify_bearer(authorization: str | None = None) -> str:
    """Validate Bearer token from Authorization header.

    Returns the token if valid, raises 401 otherwise.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(token, _server_token):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )
    return token


async def require_auth(
    authorization: str | None = None,  # noqa: ARG001 — injected by FastAPI from header
) -> None:
    """FastAPI dependency — validates Bearer token."""
    # FastAPI extracts the Authorization header automatically via parameter name
    pass


# Will be set during create_app()
_server_token: str = ""


# ── WebSocket Connection Manager ──


class ConnectionManager:
    """Manage WebSocket connections for real-time event broadcasting."""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.append(websocket)
        logger.debug("ws_connected", count=len(self._connections))

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            if websocket in self._connections:
                self._connections.remove(websocket)
        logger.debug("ws_disconnected", count=len(self._connections))

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Send JSON message to all connected clients."""
        async with self._lock:
            stale: list[WebSocket] = []
            for ws in self._connections:
                try:
                    await ws.send_json(message)
                except Exception:  # noqa: BLE001
                    stale.append(ws)
            for ws in stale:
                self._connections.remove(ws)

    @property
    def active_count(self) -> int:
        return len(self._connections)


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

    # Shared state
    ws_manager = ConnectionManager()
    app.state.ws_manager = ws_manager

    # ── Auth dependency (using Header) ──

    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

    _security = HTTPBearer(auto_error=False)

    _security_dep = Depends(_security)

    async def verify_token(
        credentials: HTTPAuthorizationCredentials | None = _security_dep,  # noqa: B008
    ) -> str:
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
        # Placeholder — will be wired to real services in DASH-03
        return JSONResponse(
            {
                "version": "0.1.0",
                "uptime_seconds": 0,
                "mind_name": "sovyx",
                "active_conversations": 0,
                "memory_concepts": 0,
                "memory_episodes": 0,
                "llm_cost_today": 0.0,
                "llm_calls_today": 0,
                "tokens_today": 0,
            }
        )

    @app.get("/api/health", dependencies=[Depends(verify_token)])
    async def get_health() -> JSONResponse:
        """Health check results."""
        # Placeholder — will integrate health.py in DASH-04
        return JSONResponse({"checks": []})

    @app.get("/api/conversations", dependencies=[Depends(verify_token)])
    async def get_conversations() -> JSONResponse:
        """List conversations."""
        # Placeholder — DASH-05
        return JSONResponse({"conversations": []})

    @app.get("/api/brain/graph", dependencies=[Depends(verify_token)])
    async def get_brain_graph() -> JSONResponse:
        """Brain knowledge graph (nodes + edges)."""
        # Placeholder — DASH-07
        return JSONResponse({"nodes": [], "edges": []})

    @app.get("/api/logs", dependencies=[Depends(verify_token)])
    async def get_logs(
        level: str | None = None,
        module: str | None = None,
        limit: int = 100,
    ) -> JSONResponse:
        """Query structured logs."""
        # Placeholder — DASH-08
        return JSONResponse({"entries": []})

    @app.get("/api/settings", dependencies=[Depends(verify_token)])
    async def get_settings() -> JSONResponse:
        """Current settings."""
        # Placeholder — DASH-09
        return JSONResponse(
            {
                "mind_name": "sovyx",
                "log_level": "INFO",
                "data_dir": str(Path.home() / ".sovyx"),
                "personality": {
                    "openness": 0.7,
                    "conscientiousness": 0.8,
                    "extraversion": 0.4,
                    "agreeableness": 0.6,
                    "neuroticism": 0.3,
                },
                "channels": [],
            }
        )

    @app.put("/api/settings", dependencies=[Depends(verify_token)])
    async def update_settings() -> JSONResponse:
        """Update settings."""
        # Placeholder — DASH-09
        return JSONResponse({"ok": True})

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
            pass
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

        @app.get("/{path:path}")
        async def spa_fallback(path: str) -> FileResponse:
            """SPA fallback — serve index.html for all non-API routes."""
            # Check if a static file exists
            file_path = STATIC_DIR / path
            if file_path.is_file() and ".." not in path:
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

    def __init__(self, config: APIConfig | None = None) -> None:
        self._config = config
        self._server: Any | None = None
        self._app: FastAPI | None = None

    @property
    def app(self) -> FastAPI | None:
        return self._app

    @property
    def ws_manager(self) -> ConnectionManager | None:
        if self._app:
            mgr: ConnectionManager = self._app.state.ws_manager
            return mgr
        return None

    async def start(self) -> None:
        """Start the dashboard server (non-blocking)."""
        import uvicorn

        self._app = create_app(self._config)

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
