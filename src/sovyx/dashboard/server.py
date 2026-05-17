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
import threading
import time
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
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_503_SERVICE_UNAVAILABLE

from sovyx import __version__
from sovyx.dashboard import STATIC_DIR
from sovyx.dashboard._integrity import (
    BundleIntegrityReport,
    BundleVerdict,
    scan_bundle_integrity,
)
from sovyx.observability.logging import get_logger
from sovyx.observability.tasks import spawn

if TYPE_CHECKING:
    from starlette.types import Scope

    from sovyx.engine.config import APIConfig, DashboardTuningConfig
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
        # Per-socket connect timestamp (for net.ws.disconnect duration_ms).
        # Indexed by id() to avoid hashing the WebSocket object itself,
        # which is unhashable on some Starlette versions.
        self._connect_times: dict[int, float] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        """Accept and register a WebSocket connection."""
        await websocket.accept()
        async with self._lock:
            self._connections.append(websocket)
            self._connect_times[id(websocket)] = time.monotonic()
            count = len(self._connections)
        client = self._client_repr(websocket)
        logger.debug("ws_connected", count=count)
        logger.info(
            "net.ws.connect",
            **{
                "net.client": client,
                "net.active_count": count,
            },
        )

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket connection."""
        async with self._lock:
            if websocket in self._connections:
                self._connections.remove(websocket)
            connected_at = self._connect_times.pop(id(websocket), None)
            count = len(self._connections)
        duration_ms = 0
        if connected_at is not None:
            duration_ms = int((time.monotonic() - connected_at) * 1000)
        logger.debug("ws_disconnected", count=count)
        logger.info(
            "net.ws.disconnect",
            **{
                "net.client": self._client_repr(websocket),
                "net.duration_ms": duration_ms,
                "net.active_count": count,
            },
        )

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Send JSON message to all connected clients.

        Copies the connection list and releases the lock before sending,
        so a slow client doesn't block other sends or connect/disconnect.
        """
        async with self._lock:
            snapshot = list(self._connections)

        if not snapshot:
            return

        # Approx payload size — JSON-encoded length matches what FastAPI
        # actually pushes onto the wire. Computed once so every per-socket
        # send.event reports the same byte count.
        try:
            import json as _json

            message_bytes = len(_json.dumps(message).encode("utf-8"))
        except (TypeError, ValueError):
            message_bytes = -1
        event_type = str(message.get("type", "")) if isinstance(message, dict) else ""

        stale: list[WebSocket] = []
        for ws in snapshot:
            send_started_at = time.monotonic()
            try:
                await ws.send_json(message)
                logger.debug(
                    "net.ws.send",
                    **{
                        "net.client": self._client_repr(ws),
                        "net.message_bytes": message_bytes,
                        "net.event_type": event_type,
                        "net.send_latency_ms": int((time.monotonic() - send_started_at) * 1000),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                stale.append(ws)
                logger.warning(
                    "net.ws.send",
                    **{
                        "net.client": self._client_repr(ws),
                        "net.message_bytes": message_bytes,
                        "net.event_type": event_type,
                        "net.send_failed": True,
                        "net.error_type": type(exc).__name__,
                    },
                )

        if stale:
            async with self._lock:
                for ws in stale:
                    if ws in self._connections:
                        self._connections.remove(ws)
                    self._connect_times.pop(id(ws), None)

    @property
    def active_count(self) -> int:
        """Number of active WebSocket connections."""
        return len(self._connections)

    @staticmethod
    def _client_repr(websocket: WebSocket) -> str:
        """Best-effort ``host:port`` string for telemetry — never raises."""
        try:
            client = websocket.client
            if client is None:
                return "unknown"
            return f"{client.host}:{client.port}"
        except (AttributeError, RuntimeError):
            return "unknown"


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


# ── HTTP Telemetry Middleware ──


class HttpTelemetryMiddleware(BaseHTTPMiddleware):
    """Emit ``net.http.request`` / ``net.http.response`` for every HTTP call.

    Lives outside RequestIdMiddleware so the response side can read
    ``request.state.request_id`` (populated by the inner middleware) and
    correlate the pair. Body bytes are read from ``Content-Length`` —
    consuming the request body here would break downstream handlers, so
    chunked uploads with no length header report ``-1``.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Time the request and emit the request/response pair."""
        method = request.method
        path = request.url.path
        client_repr = "unknown"
        if request.client is not None:
            client_repr = f"{request.client.host}:{request.client.port}"
        try:
            request_bytes = int(request.headers.get("content-length", "0"))
        except ValueError:
            request_bytes = -1

        logger.debug(
            "net.http.request",
            **{
                "net.method": method,
                "net.path": path,
                "net.client": client_repr,
                "net.request_bytes": request_bytes,
            },
        )

        started_at = time.monotonic()
        try:
            response = await call_next(request)
        except Exception as exc:
            latency_ms = int((time.monotonic() - started_at) * 1000)
            logger.warning(
                "net.http.response",
                **{
                    "net.method": method,
                    "net.path": path,
                    "net.client": client_repr,
                    "net.status_code": 500,
                    "net.response_bytes": -1,
                    "net.latency_ms": latency_ms,
                    "net.failed": True,
                    "net.error_type": type(exc).__name__,
                },
            )
            raise

        latency_ms = int((time.monotonic() - started_at) * 1000)
        try:
            response_bytes = int(response.headers.get("content-length", "-1"))
        except ValueError:
            response_bytes = -1

        log_method = logger.warning if response.status_code >= 500 else logger.info  # noqa: PLR2004
        log_method(
            "net.http.response",
            **{
                "net.method": method,
                "net.path": path,
                "net.client": client_repr,
                "net.status_code": response.status_code,
                "net.response_bytes": response_bytes,
                "net.latency_ms": latency_ms,
            },
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

    # HTTP telemetry — added LAST so it is the OUTERMOST middleware.
    # Starlette wraps in reverse-add order: the outermost layer sees the
    # full request/response cycle including time spent in CORS, RequestId,
    # SecurityHeaders, and RateLimit, giving accurate end-to-end latency.
    app.add_middleware(HttpTelemetryMiddleware)

    # Shared state
    ws_manager = ConnectionManager()
    app.state.ws_manager = ws_manager
    app.state.auth_token = _server_token

    # Conversation-import tracker — process-local, one per app instance.
    # Read by routes/conversation_import.py via request.app.state.
    from sovyx.upgrade.conv_import import ImportProgressTracker

    app.state.import_tracker = ImportProgressTracker()

    # Voice setup wizard recorder — production binding. The
    # ``/api/voice/wizard/test-record`` route (voice_wizard.py:519)
    # resolves this off ``request.app.state`` and returns 503 when
    # absent. The class is constructed lazily — it does NOT import
    # ``sounddevice`` until ``record()`` is called — so wiring it here
    # remains safe on hosts without audio hardware. Tests that need
    # the 503 path or a deterministic stub override the attribute
    # explicitly after :func:`create_app` returns.
    from sovyx.dashboard.routes.voice_wizard import SoundDeviceWizardRecorder

    app.state.wizard_recorder = SoundDeviceWizardRecorder()

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
        emotions as emotions_routes,
    )
    from sovyx.dashboard.routes import (
        engine_degraded as engine_degraded_routes,
    )
    from sovyx.dashboard.routes import (
        logs as logs_routes,
    )
    from sovyx.dashboard.routes import (
        mind as mind_routes,
    )
    from sovyx.dashboard.routes import (
        observability as observability_routes,
    )
    from sovyx.dashboard.routes import (
        onboarding as onboarding_routes,
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
        voice_calibration as voice_calibration_routes,
    )
    from sovyx.dashboard.routes import (
        voice_health as voice_health_routes,
    )
    from sovyx.dashboard.routes import (
        voice_kb as voice_kb_routes,
    )
    from sovyx.dashboard.routes import (
        voice_kb_contribute as voice_kb_contribute_routes,
    )
    from sovyx.dashboard.routes import (
        voice_platform_diagnostics as voice_platform_diag_routes,
    )
    from sovyx.dashboard.routes import (
        voice_test as voice_test_routes,
    )
    from sovyx.dashboard.routes import (
        voice_training as voice_training_routes,
    )
    from sovyx.dashboard.routes import (
        voice_wizard as voice_wizard_routes,
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
    app.include_router(logs_routes.ws_router)
    app.include_router(mind_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(data_routes.router)
    app.include_router(emotions_routes.router)
    app.include_router(safety_routes.router)
    app.include_router(engine_degraded_routes.router)
    app.include_router(voice_routes.router)
    app.include_router(voice_health_routes.router)
    app.include_router(voice_kb_routes.router)
    app.include_router(voice_kb_contribute_routes.router)
    app.include_router(voice_platform_diag_routes.router)
    app.include_router(voice_test_routes.router)
    app.include_router(voice_training_routes.router)
    app.include_router(voice_training_routes.ws_router)
    app.include_router(voice_wizard_routes.router)
    app.include_router(voice_calibration_routes.router)
    app.include_router(voice_calibration_routes.ws_router)
    app.include_router(plugins_routes.router)
    app.include_router(config_routes.router)
    app.include_router(providers_routes.router)
    app.include_router(channels_routes.router)
    app.include_router(chat_routes.router)
    app.include_router(conversation_import_routes.router)
    app.include_router(telemetry_routes.router)
    app.include_router(setup_routes.router)
    app.include_router(onboarding_routes.router)
    app.include_router(observability_routes.router)
    app.include_router(ws_routes.router)

    # ── Static Files + SPA Fallback ──
    #
    # Mission C5 §T2.1 — replace the historical two-state gate
    # (``STATIC_DIR.exists() and (STATIC_DIR / "index.html").exists()``)
    # with a four-state classifier driven by
    # :func:`sovyx.dashboard.scan_bundle_integrity`. Each verdict wires
    # an :class:`EngineDegradedStore` axis entry (``axis="dashboard"``)
    # for the C4 composite banner; the LENIENT phase preserves the
    # legacy ``dashboard_static_missing`` WARN for one minor cycle
    # (ADR-D14) so operator playbooks have time to migrate to the new
    # ``dashboard.distribution.*`` event names.

    # Resolve dashboard tuning — best-effort: a tuning-load failure
    # cannot block app construction (legacy callers and tests reach
    # ``create_app`` without a full EngineConfig stack).
    try:
        from sovyx.engine.config import DashboardTuningConfig

        _dashboard_tuning = DashboardTuningConfig()
    except Exception:  # noqa: BLE001 — config layer is best-effort here
        _dashboard_tuning = None

    _integrity_report = scan_bundle_integrity(STATIC_DIR)

    # Cache the report on app.state so the doctor surface + future
    # reactive callers can read it without re-scanning. Phase 1.B
    # readers (``cli/commands/doctor.py::_render_dashboard_integrity_surface``)
    # consult this attribute first and fall back to a fresh scan.
    app.state.dashboard_integrity_report = _integrity_report

    # Phase 1.B telemetry — always emit the structured event.
    logger.info(
        "dashboard.distribution.bundle_scanned",
        verdict=_integrity_report.verdict.value,
        static_dir=str(STATIC_DIR),
        referenced_count=len(_integrity_report.referenced_assets),
        missing_count=len(_integrity_report.missing_assets),
        scan_duration_ms=_integrity_report.scan_duration_ms,
    )

    if _integrity_report.verdict is BundleVerdict.FULLY_PRESENT:
        # Healthy path — explicit clear so prior boot's transient state
        # does not linger after a reinstall.
        _clear_dashboard_axis()
        # Serve static assets (JS, CSS, images). Wrap in the integrity-
        # aware subclass so on-404 we trigger a debounced rescan that
        # catches mid-daemon corruption (file deletion, AV quarantine,
        # disk fault) without a server restart.
        app.mount(
            "/assets",
            _IntegrityAwareStaticFiles(
                directory=str(STATIC_DIR / "assets"),
                static_dir=STATIC_DIR,
                tuning=_dashboard_tuning,
            ),
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

    elif _integrity_report.verdict is BundleVerdict.PARTIAL:
        # Mission C5 — operator's v0.43.1 forensic case. Mount StaticFiles
        # anyway so the partial SPA tries to render (the operator may
        # still need /chat even when /voice-health chunks are missing),
        # AND surface the partial verdict via the composite banner.
        _record_dashboard_bundle_incomplete(
            _integrity_report,
            severity="error",
            tuning=_dashboard_tuning,
        )
        app.mount(
            "/assets",
            _IntegrityAwareStaticFiles(
                directory=str(STATIC_DIR / "assets"),
                static_dir=STATIC_DIR,
                tuning=_dashboard_tuning,
            ),
            name="static-assets",
        )

        _static_root = STATIC_DIR.resolve()

        @app.get("/{path:path}")
        async def spa_fallback(path: str) -> FileResponse:
            """SPA fallback — serve index.html for all non-API routes."""
            file_path = (STATIC_DIR / path).resolve()
            if (
                file_path.is_file()
                and ".." not in path
                and str(file_path).startswith(str(_static_root))
            ):
                return FileResponse(str(file_path))
            return FileResponse(str(STATIC_DIR / "index.html"))

    else:
        # INDEX_HTML_MISSING / STATIC_DIR_MISSING / LEGACY_INDEX_HTML_NO_ASSETS —
        # no SPA can render. Preserve the legacy WARN per ADR-D14
        # dual-emission (drop at v0.48.0 STRICT flip) and surface the
        # composite banner via the engine degraded store.
        logger.warning(
            "dashboard_static_missing",
            path=str(STATIC_DIR),
            hint="Run 'npm run build' in dashboard/ to generate static files",
        )
        _record_dashboard_bundle_incomplete(
            _integrity_report,
            severity="critical",
            tuning=_dashboard_tuning,
        )

        @app.get("/{path:path}")
        async def no_dashboard(path: str) -> JSONResponse:
            """Placeholder when dashboard isn't built."""
            return JSONResponse(
                {"error": "Dashboard not built. Run 'npm run build' in dashboard/"},
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )

    return app


def _clear_dashboard_axis() -> None:
    """Drop any ``axis="dashboard"`` entries from the composite store.

    Mission C5 §T2.1 — called on a FULLY_PRESENT boot so a prior
    partial-install's stale entry does not survive a successful repair.
    Best-effort — store unavailability cannot block app construction.
    """
    try:
        from sovyx.engine._degraded_store import get_default_degraded_store

        get_default_degraded_store().clear_axis("dashboard")
    except Exception:  # noqa: BLE001 — observability-only surface
        logger.debug("c5_degraded_store_clear_failed", axis="dashboard")


def _record_dashboard_bundle_incomplete(
    report: BundleIntegrityReport,
    *,
    severity: str,
    tuning: DashboardTuningConfig | None,
) -> None:
    """Compose the :class:`DegradedEntry` for a partial / missing bundle
    and record it in the C4 composite store.

    Mission C5 §T2.1 / §T2.2 producer wire. Mirrors the C4 producer
    shims at ``engine/bootstrap.py:735`` (no-LLM-provider) and
    ``voice/factory/_validate.py:542`` (stt-language-coerced).
    """
    try:
        from sovyx.engine._degraded_store import (
            DegradedEntry,
            get_default_degraded_store,
            make_action_chip,
            now_monotonic,
        )

        verdict_value = report.verdict.value
        is_partial = report.verdict is BundleVerdict.PARTIAL
        reason = "bundle_partial" if is_partial else "bundle_missing"
        body_token = f"degraded.dashboard.{reason}.{verdict_value}.body"
        # Action-chip URL overrides — operator may point at self-hosted
        # docs via ``SOVYX_TUNING__DASHBOARD__INTEGRITY_ACTION_CHIP_*_URL``.
        reinstall_url = (
            tuning.integrity_action_chip_reinstall_url
            if tuning is not None
            else "https://sovyx.dev/docs/install/troubleshooting#reinstall"
        )
        doctor_url = (
            tuning.integrity_action_chip_doctor_url
            if tuning is not None
            else "https://sovyx.dev/docs/cli/doctor#dashboard"
        )

        now = now_monotonic()
        get_default_degraded_store().record(
            DegradedEntry(
                axis="dashboard",
                reason=reason,
                severity=severity,
                title_token=f"degraded.dashboard.{reason}.title",
                body_token=body_token,
                action_chips=(
                    make_action_chip(
                        "degraded.dashboard.reinstall",
                        "external_link",
                        reinstall_url,
                        style="primary",
                    ),
                    make_action_chip(
                        "degraded.dashboard.runDoctor",
                        "external_link",
                        doctor_url,
                    ),
                ),
                metadata={
                    "verdict": verdict_value,
                    "missing_count": len(report.missing_assets),
                    "missing_sample": list(report.missing_assets[:5]),
                    "static_dir": str(report.static_dir),
                    "scan_duration_ms": report.scan_duration_ms,
                },
                first_observed_monotonic=now,
                last_observed_monotonic=now,
                occurrence_count=1,
            ),
        )
    except Exception:  # noqa: BLE001 — observability-only surface
        logger.debug("c5_degraded_store_record_failed", axis="dashboard")

    event_suffix = (
        "bundle_partial" if report.verdict is BundleVerdict.PARTIAL else "bundle_missing"
    )
    logger.warning(
        f"dashboard.distribution.{event_suffix}",
        verdict=report.verdict.value,
        missing_count=len(report.missing_assets),
        missing_sample=list(report.missing_assets[:5]),
        static_dir=str(report.static_dir),
        hint="Run 'sovyx dashboard doctor' or reinstall via pipx.",
    )


class _IntegrityAwareStaticFiles(StaticFiles):
    """StaticFiles subclass that triggers a debounced bundle rescan on 404.

    Mission C5 §T2.2 reactive arm. When a ``/assets/*`` request 404s,
    the underlying bundle may have been corrupted post-boot (file
    deletion, AV quarantine, disk fault). The override catches the
    Starlette-raised ``HTTPException(status_code=404)``, fires a
    debounced re-scan off-thread, and re-raises so the 404 still
    propagates to the caller.

    Bounded by :attr:`_debounce_sec` (default 60 s, tunable via
    ``SOVYX_TUNING__DASHBOARD__INTEGRITY_REACTIVE_DEBOUNCE_SEC``) so a
    2-Hz polling failure (H8-class) cannot trigger a scan storm.

    Anti-pattern #14 compliance — the rescan runs via
    ``asyncio.create_task`` + ``asyncio.to_thread`` for the synchronous
    file-system scan, never blocking the event loop on the hot path.
    """

    def __init__(
        self,
        *,
        directory: str,
        static_dir: Path,
        tuning: DashboardTuningConfig | None,
    ) -> None:
        super().__init__(directory=directory)
        self._static_dir = static_dir
        if tuning is not None:
            self._enabled = tuning.integrity_reactive_enabled
            self._debounce_sec = tuning.integrity_reactive_debounce_sec
        else:
            self._enabled = True
            self._debounce_sec = 60.0
        self._last_scan_at = 0.0
        self._scan_lock = threading.Lock()

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and self._enabled:
                now = time.monotonic()
                if now - self._last_scan_at >= self._debounce_sec:
                    with self._scan_lock:
                        # Double-check inside the lock.
                        if now - self._last_scan_at >= self._debounce_sec:
                            self._last_scan_at = now
                            asyncio.create_task(  # noqa: RUF006
                                self._reactive_rescan(),
                            )
            raise

    async def _reactive_rescan(self) -> None:
        """Off-thread re-scan + composite-store update.

        Anti-pattern #14 — synchronous scan wrapped in
        :func:`asyncio.to_thread`. Best-effort: a scan failure cannot
        propagate to the caller (the 404 already propagated synchronously
        in :meth:`get_response`).
        """
        try:
            report = await asyncio.to_thread(scan_bundle_integrity, self._static_dir)
        except Exception:  # noqa: BLE001 — observability-only surface
            logger.debug("c5_reactive_rescan_failed")
            return
        if report.verdict is BundleVerdict.FULLY_PRESENT:
            _clear_dashboard_axis()
            logger.info("dashboard.distribution.reactive_rescan_healthy")
            return
        severity = "error" if report.verdict is BundleVerdict.PARTIAL else "critical"
        _record_dashboard_bundle_incomplete(
            report,
            severity=severity,
            tuning=None,  # action-chip URLs already baked into prior call
        )
        logger.info(
            "dashboard.distribution.reactive_rescan_degraded",
            verdict=report.verdict.value,
        )


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
        """Resolve the engine HealthRegistry, falling back to local wiring.

        Phase 11 Task 11.5 of IMPL-OBSERVABILITY-001 moved health-check
        construction into ``observability.health.create_engine_health_registry``
        and registers a singleton in the ServiceRegistry from
        ``bootstrap()``.  The dashboard preferentially resolves that
        singleton so every consumer (``/api/health`` route, startup
        self-diagnosis cascade, future SLOMonitor) shares one
        instance.

        The fallback path (``create_engine_health_registry`` invoked
        in-line) keeps the dashboard standalone — when the dashboard
        is started against a hand-rolled ServiceRegistry that didn't
        run the full bootstrap (e.g. in legacy tests), the same wiring
        helper rebuilds the registry on demand.

        Returns:
            HealthRegistry with the 6 online checks.
        """
        from sovyx.observability.health import (
            HealthRegistry,
            create_engine_health_registry,
        )

        if self._registry is None:
            return HealthRegistry()

        if self._registry.is_registered(HealthRegistry):
            return await self._registry.resolve(HealthRegistry)

        return await create_engine_health_registry(self._registry)

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

            # Wire OTel InMemoryMetricReader so /metrics exposes Prometheus
            # exposition. Bootstrap (Phase 11 Task 11.6) registers the
            # singleton from ``setup_metrics``; dashboards started against
            # a hand-rolled registry (legacy tests) leave it None and the
            # /metrics route degrades to "# No metrics available" rather
            # than 500'ing — same contract the route already documents.
            try:
                from opentelemetry.sdk.metrics.export import InMemoryMetricReader

                if self._registry.is_registered(InMemoryMetricReader):
                    self._app.state.metrics_reader = await self._registry.resolve(
                        InMemoryMetricReader,
                    )
            except Exception:  # noqa: BLE001 — reader missing degrades gracefully.
                logger.debug("metrics_reader_wire_failed")

            # Wire MindConfig from PersonalityEngine (if registered)
            try:
                from sovyx.mind.personality import PersonalityEngine

                if self._registry.is_registered(PersonalityEngine):
                    personality = await self._registry.resolve(PersonalityEngine)
                    self._app.state.mind_config = personality.config
            except Exception:  # noqa: BLE001
                logger.debug("mind_config_wire_failed")

            # ── mind.yaml path resolution: per-request, NOT boot-time ──
            #
            # Pre-Phase-3.A this block hardcoded ``data_dir / "aria" / "mind.yaml"``
            # at boot and cached it onto ``app.state.mind_yaml_path``. Multi-mind
            # operators had voice / config / onboarding / setup / providers
            # persistence written to the phantom ``"aria"`` mind regardless of
            # which mind they were actively using.
            #
            # The replacement pattern lives in
            # :func:`sovyx.dashboard._shared.resolve_mind_yaml_path_for_request`
            # — every consumer route resolves the path per-request via the
            # active-mind resolver + ``EngineConfig.database.data_dir``. Tests
            # may still set ``application.state.mind_yaml_path = path`` for
            # dependency injection (the resolver honours that override).
            #
            # Mission: ``MISSION-voice-zero-defect-2026-05-08.md`` Phase 3.A
            # Layer B — closes anti-pattern #35 reincidence #6 cluster.

            # Wire active mind_id for downstream routes (Mission
            # ``MISSION-voice-linux-silent-mic-remediation-2026-05-04.md``
            # §Phase 1 T1.2).
            #
            # Pre-fix: ``dashboard/routes/voice.py`` read
            # ``getattr(request.app.state, "mind_id", "default")``
            # without anywhere in production code assigning that
            # attribute → the voice pipeline always launched under
            # the phantom ``"default"`` mind. Forensic anchor:
            # ``c:\\Users\\guipe\\Downloads\\logs_01.txt`` line 1342
            # (every ``voice_pipeline_heartbeat`` shows
            # ``mind_id=default`` despite the operator's mind being
            # ``jonny``).
            #
            # We populate the cache here so the resolver in
            # ``_shared.resolve_active_mind_id_for_request`` has a
            # zero-latency happy path; multi-mind reroutes still work
            # because the resolver does a live MindManager lookup
            # whenever the cache is absent or matches the fallback
            # sentinel.
            try:
                from sovyx.engine.bootstrap import MindManager

                if self._registry.is_registered(MindManager):
                    mind_manager = await self._registry.resolve(MindManager)
                    actives = mind_manager.get_active_minds()
                    if actives:
                        self._app.state.mind_id = actives[0]
                        logger.debug(
                            "mind_id_wired",
                            mind_id=actives[0],
                            active_count=len(actives),
                        )
            except Exception:  # noqa: BLE001 — defensive per anti-pattern #33
                logger.debug("mind_id_wire_failed")

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
        spawn(self._server.serve(), name="dashboard-uvicorn-server")

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
