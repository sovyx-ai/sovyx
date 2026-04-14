"""Safety endpoints — stats, status, history, rules GET/PUT."""

from __future__ import annotations

import contextlib

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from sovyx.dashboard.routes._deps import verify_token

router = APIRouter(prefix="/api/safety", dependencies=[Depends(verify_token)])


@router.get("/stats")
async def get_safety_stats(request: Request) -> JSONResponse:
    """Safety audit trail stats — blocks by category, direction, recent events."""
    from sovyx.cognitive.safety_audit import get_audit_trail
    from sovyx.cognitive.safety_patterns import get_pattern_count, get_tier_counts

    audit = get_audit_trail()
    stats = audit.get_stats()

    mind_config = getattr(request.app.state, "mind_config", None)
    active_patterns = 0
    if mind_config:
        active_patterns = get_pattern_count(mind_config.safety)

    # Enriched stats from SQLite + classifier cache + escalation.
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
            "tracked_conversations": len(get_injection_tracker()._conversations),
        }
    except Exception:  # noqa: BLE001
        pass

    pii_patterns = 0
    try:
        from sovyx.cognitive.pii_guard import PII_PATTERNS

        pii_patterns = len(PII_PATTERNS)
    except Exception:  # noqa: BLE001
        pass

    shadow_stats: dict[str, object] = {}
    try:
        from sovyx.cognitive.shadow_mode import get_shadow_stats

        if mind_config:
            shadow_stats = {"shadow_mode": get_shadow_stats(mind_config.safety)}
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
            **shadow_stats,
        }
    )


@router.get("/status")
async def get_safety_status(request: Request) -> JSONResponse:
    """Runtime safety status — what is ACTIVE right now."""
    from sovyx.cognitive.safety_patterns import (
        get_pattern_count,
        get_tier_counts,
        resolve_patterns,
    )

    mind_config = getattr(request.app.state, "mind_config", None)
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

        registry = getattr(request.app.state, "registry", None)
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


@router.get("/history")
async def get_safety_history(
    request: Request,
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
            hours=min(hours, 8760),  # Max 1 year.
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


@router.get("/rules")
async def get_custom_rules(request: Request) -> JSONResponse:
    """Get current custom rules and banned topics."""
    mind_config = getattr(request.app.state, "mind_config", None)
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


@router.put("/rules")
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

    mind_config = getattr(request.app.state, "mind_config", None)
    if mind_config is None:
        return JSONResponse(
            {"ok": False, "error": "No mind configuration loaded"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    safety = mind_config.safety

    if "custom_rules" in body:
        try:
            import re as _re

            rules = [CustomRule(**r) for r in body["custom_rules"]]
            for rule in rules:
                _re.compile(rule.pattern)
            safety.custom_rules = rules
        except (_re.error, TypeError, ValueError) as e:
            return JSONResponse(
                {"ok": False, "error": f"Invalid rules: {e}"},
                status_code=422,
            )
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"ok": False, "error": f"Invalid rules: {e}"},
                status_code=422,
            )

    if "banned_topics" in body:
        safety.banned_topics = list(body["banned_topics"])

    ws_manager = getattr(request.app.state, "ws_manager", None)
    if ws_manager is not None:
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
