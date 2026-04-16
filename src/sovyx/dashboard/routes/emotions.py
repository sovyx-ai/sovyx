"""Emotion analytics endpoints — PAD 3D emotional state visualization."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/emotions", dependencies=[Depends(verify_token)])

# ── PAD → human label mapping ──

_QUADRANT_LABELS: dict[str, dict[str, str]] = {
    "positive_active": {"label": "Excited", "description": "Enthusiastic and engaged"},
    "positive_passive": {"label": "Calm", "description": "Content and at ease"},
    "negative_active": {"label": "Stressed", "description": "Tense and restless"},
    "negative_passive": {"label": "Melancholy", "description": "Quiet and withdrawn"},
    "neutral": {"label": "Neutral", "description": "Balanced and steady"},
}


def _classify_quadrant(v: float, a: float) -> str:
    if abs(v) < 0.2 and abs(a) < 0.2:  # noqa: PLR2004
        return "neutral"
    if v >= 0.2:  # noqa: PLR2004
        return "positive_active" if a >= 0.2 else "positive_passive"  # noqa: PLR2004
    return "negative_active" if a >= 0.2 else "negative_passive"  # noqa: PLR2004


def _mood_label(v: float, a: float, d: float) -> dict[str, str]:
    q = _classify_quadrant(v, a)
    info = _QUADRANT_LABELS[q]
    label = info["label"]
    desc = info["description"]
    if d >= 0.3:  # noqa: PLR2004
        label += " & Confident"
    elif d <= -0.3:  # noqa: PLR2004
        label += " & Uncertain"
    return {"label": label, "description": desc, "quadrant": q}


def _period_hours(period: str) -> int:
    mapping = {"24h": 24, "7d": 168, "30d": 720, "all": 0}
    return mapping.get(period, 168)


@router.get("/current")
async def get_current_mood(request: Request) -> JSONResponse:
    """Current emotional state from the last 20 episodes."""
    pool = await _get_brain_pool(request)
    if pool is None:
        return JSONResponse(_empty_current())

    try:
        async with pool.read() as conn:
            cursor = await conn.execute(
                """SELECT emotional_valence, emotional_arousal, emotional_dominance
                   FROM episodes
                   ORDER BY created_at DESC
                   LIMIT 20""",
            )
            rows = await cursor.fetchall()

        if not rows:
            return JSONResponse(_empty_current())

        avg_v = sum(r[0] for r in rows) / len(rows)
        avg_a = sum(r[1] for r in rows) / len(rows)
        avg_d = sum(r[2] for r in rows) / len(rows)
        mood = _mood_label(avg_v, avg_a, avg_d)

        return JSONResponse(
            {
                "valence": round(avg_v, 3),
                "arousal": round(avg_a, 3),
                "dominance": round(avg_d, 3),
                **mood,
                "episode_count": len(rows),
            }
        )
    except Exception:  # noqa: BLE001
        logger.debug("emotions_current_failed", exc_info=True)
        return JSONResponse(_empty_current())


@router.get("/timeline")
async def get_timeline(request: Request) -> JSONResponse:
    """Emotional timeline — one point per episode or bucketed by day."""
    period = request.query_params.get("period", "7d")
    pool = await _get_brain_pool(request)
    if pool is None:
        return JSONResponse({"points": [], "period": period})

    hours = _period_hours(period)
    try:
        if hours > 0:
            cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
            query = """SELECT id, created_at, emotional_valence, emotional_arousal,
                              emotional_dominance, summary
                       FROM episodes
                       WHERE created_at >= ?
                       ORDER BY created_at ASC"""
            params: tuple[str, ...] = (cutoff,)
        else:
            query = """SELECT id, created_at, emotional_valence, emotional_arousal,
                              emotional_dominance, summary
                       FROM episodes
                       ORDER BY created_at ASC"""
            params = ()

        async with pool.read() as conn:
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()

        points = [
            {
                "timestamp": row[1],
                "valence": round(row[2], 3),
                "arousal": round(row[3], 3),
                "dominance": round(row[4], 3),
                "episode_id": row[0],
                "summary": (row[5] or "")[:120],
            }
            for row in rows
        ]

        return JSONResponse({"points": points, "period": period})
    except Exception:  # noqa: BLE001
        logger.debug("emotions_timeline_failed", exc_info=True)
        return JSONResponse({"points": [], "period": period})


@router.get("/triggers")
async def get_triggers(request: Request) -> JSONResponse:
    """Concepts most associated with strong emotions."""
    limit = min(int(request.query_params.get("limit", "10")), 20)
    pool = await _get_brain_pool(request)
    if pool is None:
        return JSONResponse({"triggers": []})

    try:
        async with pool.read() as conn:
            cursor = await conn.execute(
                """SELECT name, category, emotional_valence, emotional_arousal,
                          emotional_dominance, access_count
                   FROM concepts
                   WHERE abs(emotional_valence) > 0.1 OR abs(emotional_arousal) > 0.1
                   ORDER BY (abs(emotional_valence) * 0.45 +
                             abs(emotional_arousal) * 0.30 +
                             abs(emotional_dominance) * 0.25) DESC
                   LIMIT ?""",
                (limit,),
            )
            rows = await cursor.fetchall()

        triggers = []
        for row in rows:
            v, a, d = row[2], row[3], row[4]
            mood = _mood_label(v, a, d)
            triggers.append(
                {
                    "concept": row[0],
                    "category": row[1],
                    "valence": round(v, 3),
                    "arousal": round(a, 3),
                    "dominance": round(d, 3),
                    "label": mood["label"],
                    "access_count": row[5],
                }
            )

        return JSONResponse({"triggers": triggers})
    except Exception:  # noqa: BLE001
        logger.debug("emotions_triggers_failed", exc_info=True)
        return JSONResponse({"triggers": []})


@router.get("/distribution")
async def get_distribution(request: Request) -> JSONResponse:
    """Mood distribution by quadrant over a period."""
    period = request.query_params.get("period", "30d")
    pool = await _get_brain_pool(request)
    if pool is None:
        return JSONResponse(_empty_distribution(period))

    hours = _period_hours(period)
    try:
        if hours > 0:
            cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
            query = """SELECT emotional_valence, emotional_arousal
                       FROM episodes WHERE created_at >= ?"""
            params_t: tuple[str, ...] = (cutoff,)
        else:
            query = "SELECT emotional_valence, emotional_arousal FROM episodes"
            params_t = ()

        async with pool.read() as conn:
            cursor = await conn.execute(query, params_t)
            rows = await cursor.fetchall()

        if not rows:
            return JSONResponse(_empty_distribution(period))

        counts: dict[str, int] = {
            "positive_active": 0,
            "positive_passive": 0,
            "negative_active": 0,
            "negative_passive": 0,
            "neutral": 0,
        }
        for row in rows:
            q = _classify_quadrant(row[0], row[1])
            counts[q] += 1

        total = len(rows)
        pcts = {k: round(v / total * 100, 1) for k, v in counts.items()}

        return JSONResponse({"distribution": pcts, "total": total, "period": period})
    except Exception:  # noqa: BLE001
        logger.debug("emotions_distribution_failed", exc_info=True)
        return JSONResponse(_empty_distribution(period))


# ── Helpers ──


def _empty_current() -> dict[str, object]:
    return {
        "valence": 0.0,
        "arousal": 0.0,
        "dominance": 0.0,
        "label": "No data",
        "description": "Start a conversation to see your emotional landscape",
        "quadrant": "neutral",
        "episode_count": 0,
    }


def _empty_distribution(period: str) -> dict[str, object]:
    return {
        "distribution": {
            "positive_active": 0,
            "positive_passive": 0,
            "negative_active": 0,
            "negative_passive": 0,
            "neutral": 0,
        },
        "total": 0,
        "period": period,
    }


async def _get_brain_pool(request: Request) -> object | None:
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return None
    try:
        from sovyx.persistence.manager import DatabaseManager

        if not registry.is_registered(DatabaseManager):
            return None
        db = await registry.resolve(DatabaseManager)
        mind_config = getattr(request.app.state, "mind_config", None)
        if mind_config is None:
            return None
        return db.get_mind_pool(str(mind_config.id))
    except Exception:  # noqa: BLE001
        return None
