"""Sovyx Dashboard Server — FastAPI application with WebSocket support.

Serves:
- /api/* REST endpoints (status, health, conversations, brain, logs, settings)
- /ws WebSocket for real-time events
- /* Static files (Vite build) with SPA fallback

Integrated into Engine lifecycle via start()/stop().
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from datetime import UTC, datetime
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


def create_app(config: APIConfig | None = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        config: API configuration. Uses defaults if None.
    """
    global _server_token  # noqa: PLW0603
    _server_token = _ensure_token()

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
                "cost_history": [],
            }
        )

    @app.get("/api/stats/history", dependencies=[Depends(verify_token)])
    async def stats_history(request: Request) -> JSONResponse:
        """Usage history — last N days with live data for today.

        Query params:
            days: Number of days to return (1-365, default 30).

        Returns daily cost, messages, LLM calls, tokens; plus totals
        and current-month aggregates. Today's entry uses live in-memory
        counters (not yet snapshotted to daily_stats).
        """
        from sovyx.dashboard.daily_stats import DailyStatsRecorder
        from sovyx.dashboard.status import _now_date_str, get_counters
        from sovyx.llm.cost import CostGuard

        # Parse and cap days
        try:
            days = int(request.query_params.get("days", "30"))
        except (ValueError, TypeError):
            days = 30
        days = max(1, min(days, 365))

        registry = getattr(app.state, "registry", None)
        if registry is None:
            return JSONResponse(
                {
                    "days": [],
                    "totals": _empty_stats_totals(),
                    "current_month": _empty_stats_month(),
                }
            )

        # Historical data from daily_stats
        try:
            recorder: DailyStatsRecorder = await registry.resolve(DailyStatsRecorder)
            history = await recorder.get_history(days=days)
        except Exception:  # noqa: BLE001
            history = []

        # Live data for today (not yet snapshotted)
        counters = get_counters()
        calls, _cost_counter, tokens, msgs = counters.snapshot()

        try:
            cost_guard: CostGuard = await registry.resolve(CostGuard)
            breakdown = cost_guard.get_breakdown("day")
            live_cost = breakdown.total_cost
        except Exception:  # noqa: BLE001
            live_cost = _cost_counter  # fallback to counter's cost

        today_str = _now_date_str(counters._tz)
        today_entry = {
            "date": today_str,
            "cost": round(live_cost, 6),
            "messages": msgs,
            "llm_calls": calls,
            "tokens": tokens,
            "is_live": True,
        }

        # Replace existing today entry or append
        if history and history[-1]["date"] == today_str:
            history[-1] = today_entry
        else:
            history.append(today_entry)

        # Totals (historical + live)
        try:
            totals = await recorder.get_totals()
        except Exception:  # noqa: BLE001
            totals = _empty_stats_totals()
        totals["cost"] = round(totals["cost"] + live_cost, 6)
        totals["messages"] += msgs
        totals["llm_calls"] += calls
        totals["tokens"] += tokens
        if msgs > 0 or calls > 0:
            totals["days_active"] += 1  # today counts as active

        # Current month (historical + live)
        from datetime import datetime as dt_cls
        from zoneinfo import ZoneInfo

        try:
            now = dt_cls.now(tz=counters._tz)
        except Exception:  # noqa: BLE001
            now = dt_cls.now(tz=ZoneInfo("UTC"))

        try:
            month = await recorder.get_month_totals(now.year, now.month)
        except Exception:  # noqa: BLE001
            month = _empty_stats_month()
        month["cost"] = round(month["cost"] + live_cost, 6)
        month["messages"] += msgs
        month["llm_calls"] += calls
        month["tokens"] += tokens

        return JSONResponse({"days": history, "totals": totals, "current_month": month})

    @app.get("/api/health", dependencies=[Depends(verify_token)])
    async def get_health() -> JSONResponse:
        """Health check results."""
        from sovyx.observability.health import (
            CheckResult,
            HealthRegistry,
            create_offline_registry,
        )

        all_results: list[CheckResult] = []
        seen_names: set[str] = set()

        # Tier 1: Offline checks (always available — no engine needed)
        offline = create_offline_registry()
        offline_results = await offline.run_all(timeout=10.0)
        for r in offline_results:
            all_results.append(r)
            seen_names.add(r.name)

        # Tier 2: Online checks (engine ServiceRegistry wired)
        # Deduplicate: if an online check shares a name with an offline
        # check, the online result wins (more authoritative with live data).
        health_reg = getattr(app.state, "health_registry", None)
        if health_reg is not None and isinstance(health_reg, HealthRegistry):
            online_results = await health_reg.run_all(timeout=10.0)
            for r in online_results:
                if r.name in seen_names:
                    # Replace offline result with online (more authoritative)
                    all_results = [x for x in all_results if x.name != r.name]
                all_results.append(r)
                seen_names.add(r.name)

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

    @app.get("/api/activity/timeline", dependencies=[Depends(verify_token)])
    async def get_activity_timeline(
        hours: int = Query(default=24, ge=1, le=168),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> JSONResponse:
        """Unified cognitive activity timeline from persistent storage."""
        registry = getattr(app.state, "registry", None)
        if registry is not None:
            from sovyx.dashboard.activity import get_activity_timeline as _get_timeline

            timeline = await _get_timeline(registry, hours=hours, limit=limit)
            return JSONResponse(timeline)
        empty_meta = {"hours": hours, "limit": limit, "total_before_limit": 0, "cutoff": ""}
        return JSONResponse({"entries": [], "meta": empty_meta})

    @app.get("/api/logs", dependencies=[Depends(verify_token)])
    async def get_logs(
        level: str | None = None,
        module: str | None = None,
        search: str | None = None,
        after: str | None = None,
        limit: int = Query(default=100, ge=0, le=1000),
    ) -> JSONResponse:
        """Query structured JSON logs with filters.

        Use ``after`` (ISO-8601 timestamp) for incremental polling:
        only entries newer than the given timestamp are returned.
        """
        from sovyx.dashboard.logs import query_logs

        log_file = getattr(app.state, "log_file", None)
        entries = query_logs(
            log_file,
            level=level,
            module=module,
            search=search,
            after=after,
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

    # ── Export / Import ──

    @app.get("/api/export", dependencies=[Depends(verify_token)])
    async def export_mind_endpoint(
        request: Request,
    ) -> Response:
        """Export the active mind as a .sovyx-mind ZIP archive download.

        Returns a streaming ZIP file attachment.
        """
        registry = getattr(app.state, "registry", None)
        if registry is None:
            return JSONResponse(
                {"error": "Engine not running"},
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )

        from sovyx.dashboard._shared import get_active_mind_id
        from sovyx.dashboard.export_import import export_mind

        mind_id = await get_active_mind_id(registry)
        try:
            archive_path = await export_mind(registry, mind_id)
        except RuntimeError as exc:
            return JSONResponse(
                {"error": str(exc)},
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception:
            logger.exception("export_mind_failed")
            return JSONResponse(
                {"error": "Export failed"},
                status_code=500,
            )

        return FileResponse(
            path=str(archive_path),
            media_type="application/zip",
            filename=f"{mind_id}.sovyx-mind",
            headers={"Content-Disposition": f'attachment; filename="{mind_id}.sovyx-mind"'},
        )

    @app.post("/api/import", dependencies=[Depends(verify_token)])
    async def import_mind_endpoint(request: Request) -> JSONResponse:
        """Import a mind from an uploaded .sovyx-mind ZIP archive.

        Expects multipart/form-data with a ``file`` field containing
        the archive.  Optional query param ``overwrite=true`` to replace
        an existing mind.
        """
        import shutil
        import tempfile

        registry = getattr(app.state, "registry", None)
        if registry is None:
            return JSONResponse(
                {"error": "Engine not running"},
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )

        # Parse overwrite flag from query string
        overwrite = request.query_params.get("overwrite", "").lower() in (
            "true",
            "1",
            "yes",
        )

        # Read uploaded file

        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" not in content_type:
            return JSONResponse(
                {"error": "Expected multipart/form-data with a 'file' field"},
                status_code=422,
            )

        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return JSONResponse(
                {"error": "Missing 'file' in form data"},
                status_code=422,
            )

        # Write upload to a temp file
        tmp_dir = Path(tempfile.mkdtemp(prefix="sovyx-import-"))
        tmp_path = tmp_dir / "upload.sovyx-mind"
        try:
            data = await upload.read()
            tmp_path.write_bytes(data)

            from sovyx.dashboard.export_import import import_mind

            result = await import_mind(registry, tmp_path, overwrite=overwrite)
            return JSONResponse({"ok": True, **result})
        except Exception as exc:
            logger.exception("import_mind_failed")
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=500,
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── Safety Stats ──

    @app.get("/api/safety/stats", dependencies=[Depends(verify_token)])
    async def get_safety_stats() -> JSONResponse:
        """Safety audit trail stats — blocks by category, direction, recent events."""
        from sovyx.cognitive.safety_audit import get_audit_trail
        from sovyx.cognitive.safety_patterns import get_pattern_count, get_tier_counts

        audit = get_audit_trail()
        stats = audit.get_stats()

        mind_config = getattr(app.state, "mind_config", None)
        active_patterns = 0
        if mind_config:
            active_patterns = get_pattern_count(mind_config.safety)

        # Enriched stats from SQLite + classifier cache + escalation
        sqlite_stats: dict[str, object] = {}
        try:
            from sovyx.cognitive.audit_store import get_audit_store

            store = get_audit_store()
            sqlite_stats = {
                "persistent_blocks_24h": store.count(hours=24),
                "persistent_blocks_7d": store.count(hours=168),
                "persistent_blocks_30d": store.count(hours=720),
            }
        except Exception:  # noqa: BLE001
            pass

        classifier_stats: dict[str, object] = {}
        try:
            from sovyx.cognitive.safety_classifier import get_classification_cache

            cache = get_classification_cache()
            classifier_stats = {
                "cache_size": cache.size,
                "cache_hit_rate": round(cache.hit_rate, 3),
            }
        except Exception:  # noqa: BLE001
            pass

        escalation_stats: dict[str, object] = {}
        try:
            from sovyx.cognitive.safety_escalation import get_escalation_tracker
            from sovyx.cognitive.safety_notifications import get_notifier

            escalation_stats = {
                "tracked_sources": get_escalation_tracker()._sources.__len__(),
                "alerts_sent": get_notifier().alert_count,
            }
        except Exception:  # noqa: BLE001
            pass

        injection_stats: dict[str, object] = {}
        try:
            from sovyx.cognitive.injection_tracker import get_injection_tracker

            injection_stats = {
                "tracked_conversations": get_injection_tracker().tracked_conversations,
            }
        except Exception:  # noqa: BLE001
            pass

        pii_patterns = 0
        try:
            from sovyx.cognitive.pii_guard import PII_PATTERNS

            pii_patterns = len(PII_PATTERNS)
        except Exception:  # noqa: BLE001
            pass

        return JSONResponse(
            {
                "ok": True,
                "total_blocks_24h": stats.total_blocks_24h,
                "total_blocks_7d": stats.total_blocks_7d,
                "total_blocks_30d": stats.total_blocks_30d,
                "blocks_by_category": stats.blocks_by_category,
                "blocks_by_direction": stats.blocks_by_direction,
                "recent_events": stats.recent_events,
                "active_patterns": active_patterns,
                "pii_patterns": pii_patterns,
                "tier_counts": get_tier_counts(),
                **sqlite_stats,
                **classifier_stats,
                **escalation_stats,
                **injection_stats,
            }
        )

    @app.get("/api/safety/status", dependencies=[Depends(verify_token)])
    async def get_safety_status() -> JSONResponse:
        """Runtime safety status — what is ACTIVE right now."""
        from sovyx.cognitive.safety_patterns import (
            get_pattern_count,
            get_tier_counts,
            resolve_patterns,
        )

        mind_config = getattr(app.state, "mind_config", None)
        if mind_config is None:
            return JSONResponse(
                {"error": "No mind configuration loaded"},
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )

        safety = mind_config.safety
        patterns = resolve_patterns(safety)

        # ── Financial confirmation details ──
        confirmation_channels: list[dict[str, object]] = []
        confirmation_method = "disabled"
        classification_fallback = "regex"

        if safety.financial_confirmation:
            confirmation_method = "inline_buttons"
            classification_fallback = "llm"

            # Discover channel capabilities from BridgeManager
            registry = getattr(app.state, "registry", None)
            if registry is not None:
                from sovyx.bridge.manager import BridgeManager

                bridge: BridgeManager | None = None
                with contextlib.suppress(Exception):
                    bridge = await registry.resolve(BridgeManager)

                if bridge is not None:
                    for ct, adapter in bridge._adapters.items():
                        caps = adapter.capabilities
                        confirmation_channels.append(
                            {
                                "channel": ct.value,
                                "inline_buttons": "inline_buttons" in caps,
                                "method": (
                                    "inline_buttons"
                                    if "inline_buttons" in caps
                                    else "text_classification"
                                ),
                            }
                        )

        return JSONResponse(
            {
                "ok": True,
                "content_filter": safety.content_filter,
                "child_safe_mode": safety.child_safe_mode,
                "financial_confirmation": safety.financial_confirmation,
                "confirmation_method": confirmation_method,
                "confirmation_channels": confirmation_channels,
                "classification_fallback": classification_fallback,
                "active_patterns": len(patterns),
                "tier_counts": get_tier_counts(),
                "total_patterns": get_pattern_count(safety),
            }
        )

    # ── Voice Status ──

    @app.get("/api/voice/status", dependencies=[Depends(verify_token)])
    async def get_voice_status_endpoint() -> JSONResponse:
        """Voice pipeline status — running state, models, hardware tier."""
        registry = getattr(app.state, "registry", None)
        if registry is None:
            return JSONResponse(
                {"error": "Engine not running"},
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )

        from sovyx.dashboard.voice_status import get_voice_status

        status = await get_voice_status(registry)
        return JSONResponse(status)

    @app.get("/api/voice/models", dependencies=[Depends(verify_token)])
    async def get_voice_models_endpoint() -> JSONResponse:
        """Available voice models by hardware tier, with detected/active info."""
        registry = getattr(app.state, "registry", None)
        if registry is None:
            return JSONResponse(
                {"error": "Engine not running"},
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )

        from sovyx.dashboard.voice_status import get_voice_models

        models = await get_voice_models(registry)
        return JSONResponse(models)

    @app.get("/api/safety/history", dependencies=[Depends(verify_token)])
    async def get_safety_history(
        hours: int = 24,
        category: str | None = None,
        direction: str | None = None,
        limit: int = 50,
    ) -> JSONResponse:
        """Query historical safety events from SQLite."""
        try:
            from sovyx.cognitive.audit_store import get_audit_store

            store = get_audit_store()
            result = store.query(
                hours=min(hours, 8760),  # Max 1 year
                category=category,
                direction=direction,
                limit=min(limit, 500),
            )
            return JSONResponse(
                {
                    "ok": True,
                    "total": result.total,
                    "events": result.events,
                }
            )
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"ok": False, "error": str(e)},
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )

    # ── Custom Rules API ──

    @app.get("/api/safety/rules", dependencies=[Depends(verify_token)])
    async def get_custom_rules() -> JSONResponse:
        """Get current custom rules and banned topics."""
        mind_config = getattr(app.state, "mind_config", None)
        if mind_config is None:
            return JSONResponse(
                {"error": "No mind configuration loaded"},
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )
        safety = mind_config.safety
        return JSONResponse(
            {
                "custom_rules": [
                    {
                        "name": r.name,
                        "pattern": r.pattern,
                        "action": r.action,
                        "message": r.message,
                    }
                    for r in safety.custom_rules
                ],
                "banned_topics": list(safety.banned_topics),
            }
        )

    @app.put("/api/safety/rules", dependencies=[Depends(verify_token)])
    async def update_custom_rules(request: Request) -> JSONResponse:
        """Update custom rules and/or banned topics."""
        from sovyx.mind.config import CustomRule

        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(
                {"ok": False, "error": "Invalid JSON body"},
                status_code=422,
            )

        mind_config = getattr(app.state, "mind_config", None)
        if mind_config is None:
            return JSONResponse(
                {"ok": False, "error": "No mind configuration loaded"},
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )

        safety = mind_config.safety

        if "custom_rules" in body:
            try:
                safety.custom_rules = [CustomRule(**r) for r in body["custom_rules"]]
            except Exception as e:  # noqa: BLE001
                return JSONResponse(
                    {"ok": False, "error": f"Invalid rules: {e}"},
                    status_code=422,
                )

        if "banned_topics" in body:
            safety.banned_topics = list(body["banned_topics"])

        # Broadcast update
        await ws_manager.broadcast(
            {
                "type": "SafetyConfigUpdated",
                "data": {"changes": {"safety.custom_rules": True}},
            }
        )

        return JSONResponse(
            {
                "ok": True,
                "rules_count": len(safety.custom_rules),
                "topics_count": len(safety.banned_topics),
            }
        )

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

            # Safety-specific event for targeted UI updates
            safety_changes = {k: v for k, v in changes.items() if k.startswith("safety.")}
            if safety_changes:
                await ws_manager.broadcast(
                    {
                        "type": "SafetyConfigUpdated",
                        "data": {"changes": safety_changes},
                    }
                )

        return JSONResponse({"ok": True, "changes": changes})

    # ── Providers (LLM provider status + models) ──

    @app.get("/api/providers", dependencies=[Depends(verify_token)])
    async def get_providers() -> JSONResponse:
        """LLM provider status, availability, and available models.

        Returns all registered providers with their configuration state,
        plus the currently active provider/model from MindConfig.
        Cloud providers report ``configured`` based on API key presence.
        Ollama reports ``reachable`` via a live ping and lists installed models.
        """
        from sovyx.llm.providers.ollama import OllamaProvider

        registry = getattr(app.state, "registry", None)
        mind_config = getattr(app.state, "mind_config", None)

        if registry is None:
            return JSONResponse(
                {"error": "Engine not running"},
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )

        providers_out: list[dict[str, object]] = []

        try:
            from sovyx.llm.router import LLMRouter

            if registry.is_registered(LLMRouter):
                router = await registry.resolve(LLMRouter)

                for p in router._providers:
                    entry: dict[str, object] = {
                        "name": p.name,
                        "configured": p.is_available,
                        "available": p.is_available,
                    }
                    if isinstance(p, OllamaProvider):
                        reachable = await p.ping()
                        models = await p.list_models() if reachable else []
                        entry.update(
                            {
                                "configured": True,  # always registered
                                "available": reachable,
                                "reachable": reachable,
                                "models": models,
                                "base_url": p.base_url,
                            }
                        )
                    providers_out.append(entry)
        except Exception:  # noqa: BLE001
            logger.debug("providers_list_failed")

        active: dict[str, str] = {"provider": "", "model": "", "fast_model": ""}
        if mind_config is not None:
            active = {
                "provider": mind_config.llm.default_provider,
                "model": mind_config.llm.default_model,
                "fast_model": mind_config.llm.fast_model,
            }

        return JSONResponse({"providers": providers_out, "active": active})

    @app.put("/api/providers", dependencies=[Depends(verify_token)])
    async def update_provider(request: Request) -> JSONResponse:
        """Change active LLM provider and model at runtime.

        Updates ``MindConfig.llm`` in-place (no restart needed — ThinkPhase
        reads config per-call) and persists to ``mind.yaml``.

        Request body::

            {"provider": "ollama", "model": "llama3.1:latest"}
        """
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

        new_provider = body.get("provider", "")
        new_model = body.get("model", "")
        if not new_provider or not new_model:
            return JSONResponse(
                {"ok": False, "error": "Both 'provider' and 'model' are required"},
                status_code=422,
            )

        mind_config = getattr(app.state, "mind_config", None)
        if mind_config is None:
            return JSONResponse(
                {"ok": False, "error": "No mind configuration loaded"},
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )

        registry = getattr(app.state, "registry", None)
        if registry is None:
            return JSONResponse(
                {"ok": False, "error": "Engine not running"},
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )

        # Validate provider exists and is available
        try:
            from sovyx.llm.providers.ollama import OllamaProvider
            from sovyx.llm.router import LLMRouter

            if not registry.is_registered(LLMRouter):
                return JSONResponse(
                    {"ok": False, "error": "LLM router not available"},
                    status_code=HTTP_503_SERVICE_UNAVAILABLE,
                )

            router = await registry.resolve(LLMRouter)
            target = next((p for p in router._providers if p.name == new_provider), None)

            if target is None:
                return JSONResponse(
                    {"ok": False, "error": f"Unknown provider: {new_provider}"},
                    status_code=422,
                )

            # Validate availability: cloud checks is_available, Ollama pings
            if isinstance(target, OllamaProvider):
                reachable = await target.ping()
                if not reachable:
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": f"Ollama not reachable at {target.base_url}",
                        },
                        status_code=422,
                    )
            elif not target.is_available:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": f"Provider '{new_provider}' is not configured (missing API key?)",
                    },
                    status_code=422,
                )
        except Exception:
            logger.warning("provider_switch_validation_failed", exc_info=True)
            return JSONResponse(
                {"ok": False, "error": "Provider validation failed"},
                status_code=500,
            )

        # Apply runtime update (immediate — no restart needed)
        old_provider = mind_config.llm.default_provider
        old_model = mind_config.llm.default_model
        mind_config.llm.default_provider = new_provider
        mind_config.llm.default_model = new_model

        changes = {
            "provider": f"{old_provider} → {new_provider}",
            "model": f"{old_model} → {new_model}",
        }

        # Persist to mind.yaml
        mind_yaml_path = getattr(app.state, "mind_yaml_path", None)
        if mind_yaml_path is not None:
            from sovyx.dashboard.config import _persist_to_yaml

            _persist_to_yaml(mind_config, mind_yaml_path)
            logger.info(
                "provider_switch_persisted",
                provider=new_provider,
                model=new_model,
            )

        # Broadcast change to WebSocket clients
        await ws_manager.broadcast({"type": "config_updated", "changes": changes})

        logger.info(
            "provider_switched",
            old_provider=old_provider,
            new_provider=new_provider,
            old_model=old_model,
            new_model=new_model,
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
            # Check BridgeManager for actually registered channel adapters
            from sovyx.bridge.manager import BridgeManager
            from sovyx.engine.types import ChannelType

            bridge: BridgeManager | None = None
            with contextlib.suppress(Exception):
                bridge = await registry.resolve(BridgeManager)

            active_types: set[str] = set()
            if bridge is not None:
                active_types = {ct.value for ct in bridge._adapters}

            channel_list.append(
                {
                    "name": "Telegram",
                    "type": "telegram",
                    "connected": ChannelType.TELEGRAM.value in active_types,
                }
            )
            channel_list.append(
                {
                    "name": "Signal",
                    "type": "signal",
                    "connected": ChannelType.SIGNAL.value in active_types,
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

    @app.post("/api/channels/telegram/setup", dependencies=[Depends(verify_token)])
    async def setup_telegram(request: Request) -> JSONResponse:
        """Validate a Telegram bot token and persist it for next restart.

        1. Calls Telegram getMe to validate the token.
        2. Writes SOVYX_TELEGRAM_TOKEN to {data_dir}/channel.env.
        3. Returns bot info on success.
        """
        import aiohttp

        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(
                {"ok": False, "error": "Invalid JSON body"},
                status_code=422,
            )

        token = (body.get("token") or "").strip() if isinstance(body, dict) else ""
        if not token:
            return JSONResponse(
                {"ok": False, "error": "Token is required"},
                status_code=422,
            )

        # Validate via Telegram API
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    f"https://api.telegram.org/bot{token}/getMe",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp,
            ):
                data = await resp.json()
                if not data.get("ok"):
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": data.get("description", "Invalid token"),
                        },
                        status_code=400,
                    )
                bot_info = data["result"]
        except Exception:  # noqa: BLE001
            return JSONResponse(
                {"ok": False, "error": "Could not reach Telegram API"},
                status_code=502,
            )

        # Persist token to channel.env in data_dir
        engine_config = getattr(app.state, "engine_config", None)
        data_dir = engine_config.data_dir if engine_config is not None else Path.home() / ".sovyx"
        env_path = data_dir / "channel.env"
        try:
            # Read existing env vars (preserve non-telegram ones)
            existing: dict[str, str] = {}
            if env_path.exists():
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()  # noqa: PLW2901
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        existing[k.strip()] = v.strip()
            existing["SOVYX_TELEGRAM_TOKEN"] = token

            env_path.parent.mkdir(parents=True, exist_ok=True)
            lines = [f"{k}={v}" for k, v in existing.items()]
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            env_path.chmod(0o600)
        except Exception:  # noqa: BLE001
            logger.warning("channel_env_write_failed", path=str(env_path))

        bot_username = bot_info.get("username", "")
        logger.info(
            "telegram_token_validated",
            bot_username=bot_username,
        )

        return JSONResponse(
            {
                "ok": True,
                "bot_username": bot_username,
                "bot_name": bot_info.get("first_name", ""),
                "requires_restart": True,
            }
        )

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
        from sovyx.dashboard.status import get_counters

        get_counters().record_message()

        try:
            result = await handle_chat_message(
                registry=registry,
                message=message_text,
                user_name=user_name,
                conversation_id=conversation_id,
            )
        except ValueError:
            logger.warning("dashboard_chat_validation_failed", exc_info=True)
            return JSONResponse(
                {"error": "Invalid message format."},
                status_code=422,
            )
        except Exception:
            logger.exception("dashboard_chat_failed")
            return JSONResponse(
                {"error": "Failed to process message. Please try again."},
                status_code=500,
            )

        # Count AI response as a message too (user expects total, not just inbound)
        get_counters().record_message()

        # Broadcast chat event to WebSocket clients for real-time updates
        await ws_manager.broadcast(
            {
                "type": "ChatMessage",
                "timestamp": datetime.now(UTC).isoformat(),
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
