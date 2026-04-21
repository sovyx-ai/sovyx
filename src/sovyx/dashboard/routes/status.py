"""Status, stats/history, health, and Prometheus metrics endpoints."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from sovyx.dashboard.routes._deps import verify_token


def _empty_stats_totals() -> dict[str, object]:
    return {"cost": 0.0, "messages": 0, "llm_calls": 0, "tokens": 0, "days_active": 0}


def _empty_stats_month() -> dict[str, object]:
    return {"cost": 0.0, "messages": 0, "llm_calls": 0, "tokens": 0}


router = APIRouter(prefix="/api")


@router.get("/status", dependencies=[Depends(verify_token)])
async def get_status(request: Request) -> JSONResponse:
    """System status overview."""
    app = request.app
    collector = getattr(app.state, "status_collector", None)
    if collector is not None:
        from sovyx.dashboard.status import StatusCollector

        if not isinstance(collector, StatusCollector):
            msg = f"status_collector is {type(collector)}, expected StatusCollector"
            raise TypeError(msg)
        snapshot = await collector.collect()
        return JSONResponse(snapshot.to_dict())

    # Fallback when no registry is wired (e.g., tests, standalone).
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


@router.get("/stats/history", dependencies=[Depends(verify_token)])
async def stats_history(request: Request) -> JSONResponse:
    """Usage history — last N days with live data for today.

    Query params:
        days: Number of days to return (1-365, default 30).

    Returns daily cost, messages, LLM calls, tokens; plus totals and
    current-month aggregates. Today's entry uses live in-memory counters
    (not yet snapshotted to daily_stats).
    """
    from sovyx.dashboard.daily_stats import DailyStatsRecorder
    from sovyx.dashboard.status import _now_date_str, get_counters
    from sovyx.llm.cost import CostGuard

    try:
        days = int(request.query_params.get("days", "30"))
    except (ValueError, TypeError):
        days = 30
    days = max(1, min(days, 365))

    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return JSONResponse(
            {
                "days": [],
                "totals": _empty_stats_totals(),
                "current_month": _empty_stats_month(),
            }
        )

    try:
        recorder: DailyStatsRecorder = await registry.resolve(DailyStatsRecorder)
        history = await recorder.get_history(days=days)
    except Exception:  # noqa: BLE001
        history = []

    counters = get_counters()
    calls, _cost_counter, tokens, msgs = counters.snapshot()

    try:
        cost_guard: CostGuard = await registry.resolve(CostGuard)
        breakdown = cost_guard.get_breakdown("day")
        live_cost = breakdown.total_cost
    except Exception:  # noqa: BLE001
        live_cost = _cost_counter

    today_str = _now_date_str(counters._tz)
    today_entry = {
        "date": today_str,
        "cost": round(live_cost, 6),
        "messages": msgs,
        "llm_calls": calls,
        "tokens": tokens,
        "is_live": True,
    }

    if history and history[-1]["date"] == today_str:
        history[-1] = today_entry
    else:
        history.append(today_entry)

    try:
        totals = await recorder.get_totals()
    except Exception:  # noqa: BLE001
        totals = _empty_stats_totals()
    totals["cost"] = round(totals["cost"] + live_cost, 6)
    totals["messages"] += msgs
    totals["llm_calls"] += calls
    totals["tokens"] += tokens
    if msgs > 0 or calls > 0:
        totals["days_active"] += 1

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


@router.get("/health", dependencies=[Depends(verify_token)])
async def get_health(request: Request) -> JSONResponse:
    """Health check results."""
    from sovyx.observability.health import (
        CheckResult,
        HealthRegistry,
        create_offline_registry,
    )

    all_results: list[CheckResult] = []
    seen_names: set[str] = set()

    # Tier 1: offline checks (always available — no engine needed).
    offline = create_offline_registry()
    offline_results = await offline.run_all(timeout=10.0)
    for r in offline_results:
        all_results.append(r)
        seen_names.add(r.name)

    # Tier 2: online checks (engine ServiceRegistry wired).
    # Deduplicate: if an online check shares a name with an offline check,
    # the online result wins (more authoritative with live data).
    health_reg = getattr(request.app.state, "health_registry", None)
    if health_reg is not None and isinstance(health_reg, HealthRegistry):
        online_results = await health_reg.run_all(timeout=10.0)
        for r in online_results:
            if r.name in seen_names:
                all_results = [x for x in all_results if x.name != r.name]
            all_results.append(r)
            seen_names.add(r.name)

    overall = HealthRegistry().summary(all_results)

    # Emit ServiceHealthChanged on first poll (seeds Live Feed) and on status change
    prev: dict[str, str] = getattr(request.app.state, "_prev_health", {})
    ws_manager = getattr(request.app.state, "ws_manager", None)
    is_first_poll = len(prev) == 0
    for r in all_results:
        old_status = prev.get(r.name)
        changed = old_status is not None and old_status != r.status.value
        if (is_first_poll or changed) and ws_manager is not None:
            await ws_manager.broadcast(
                {
                    "type": "ServiceHealthChanged",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "correlation_id": "",
                    "data": {"service": r.name, "status": r.status.value},
                }
            )
    request.app.state._prev_health = {r.name: r.status.value for r in all_results}

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


# ── Active alerts (Phase 11 Task 11.7) ──


@router.get("/alerts/active", dependencies=[Depends(verify_token)])
async def get_active_alerts(request: Request) -> JSONResponse:
    """Currently firing alerts.

    Resolves the engine ``AlertManager`` from the registry, runs an
    on-demand ``evaluate()`` so the response reflects the latest
    metric samples + SLO burn rates, and returns the firing alerts
    plus their grouped severity counts.

    Returns ``{"firing": [], "summary": {...}}`` when the registry
    isn't wired (e.g., dashboard-only test apps) — never raises.
    """
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return JSONResponse(
            {
                "firing": [],
                "summary": {
                    "total_rules": 0,
                    "firing_count": 0,
                    "firing_rules": [],
                    "severity_counts": {"info": 0, "warning": 0, "critical": 0},
                },
            }
        )

    from sovyx.observability.alerts import AlertManager

    if not registry.is_registered(AlertManager):
        return JSONResponse(
            {
                "firing": [],
                "summary": {
                    "total_rules": 0,
                    "firing_count": 0,
                    "firing_rules": [],
                    "severity_counts": {"info": 0, "warning": 0, "critical": 0},
                },
            }
        )

    alert_manager: AlertManager = await registry.resolve(AlertManager)
    fired = await alert_manager.evaluate()

    return JSONResponse(
        {
            "firing": [
                {
                    "rule_name": a.rule_name,
                    "severity": a.severity.value,
                    "message": a.message,
                    "metric_name": a.metric_name,
                    "current_value": a.current_value,
                    "threshold": a.threshold,
                    "timestamp": a.timestamp,
                }
                for a in fired
            ],
            "summary": alert_manager.get_alert_summary(),
        }
    )


# ── Cardinality budget snapshot (Phase 11+ Task 11+.2) ──


@router.get("/observability/metrics/cardinality", dependencies=[Depends(verify_token)])
async def get_metrics_cardinality(request: Request) -> JSONResponse:
    """Top-N metrics by Prometheus series count + global budget posture.

    Resolves the engine ``MetricsRegistry`` from the registry and
    delegates to :meth:`MetricsRegistry.cardinality_report`. Operators
    poll this endpoint to see which metric is driving the most series
    *before* the global budget (default 10 000 series) trips — once it
    trips, new label combinations are silently folded into a single
    ``_overflow=true`` series per metric and a one-shot WARNING fires.

    Returns ``{"max_series": 0, "total_series": 0, "metrics": []}``
    when the registry isn't wired (e.g., dashboard-only test apps).
    """
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return JSONResponse(
            {"max_series": 0, "total_series": 0, "metrics": []},
        )

    from sovyx.observability.metrics import MetricsRegistry

    if not registry.is_registered(MetricsRegistry):
        return JSONResponse(
            {"max_series": 0, "total_series": 0, "metrics": []},
        )

    metrics_registry: MetricsRegistry = await registry.resolve(MetricsRegistry)
    try:
        top_n = int(request.query_params.get("top_n", "20"))
    except (ValueError, TypeError):
        top_n = 20
    top_n = max(1, min(top_n, 200))

    return JSONResponse(metrics_registry.cardinality_report(top_n=top_n))


# ── Prometheus /metrics (no auth — scrapers don't send Bearer) ──

metrics_router = APIRouter()


@metrics_router.get("/metrics")
async def prometheus_metrics(request: Request) -> Response:
    """Prometheus scrape endpoint — OpenMetrics text format.

    No authentication required (Prometheus scrapers don't send Bearer).
    Reads from the OTel InMemoryMetricReader and converts to Prometheus
    exposition format.
    """
    reader = getattr(request.app.state, "metrics_reader", None)
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
