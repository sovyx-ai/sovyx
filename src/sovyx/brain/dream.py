"""DREAM phase — nightly pattern discovery (SPE-003 phase 7).

DREAM is the seventh and final phase of the cognitive loop. It is
*not* request-driven: it runs on a time-of-day schedule (typically
02:00 in the mind's timezone) while the user is likely asleep, which
mirrors the biological inspiration — REM-era hippocampal replay and
pattern extraction (Buzsáki 2006).

Responsibilities (roadmap §Cognitive loop — DREAM phase):

1. **Pattern extraction** — one LLM call over a window of recent
   episode summaries, asking for recurring themes that span at
   least two conversations.
2. **Derived concept generation** — each pattern becomes a
   :class:`Concept` with ``source="dream:pattern"`` and a modest
   initial confidence (default 0.4). Access-driven reinforcement
   lifts confidence organically if the theme is revisited.
3. **Cross-episode Hebbian** — concepts that co-occur across the
   window (not just within a turn) get their relations
   strengthened via :class:`HebbianLearning.strengthen`, with
   activations attenuated vs. within-turn to reflect the weaker
   signal.

CONSOLIDATE vs. DREAM:

* CONSOLIDATE runs every 6 h (interval-based) and does *maintenance*
  — decay, merge, prune.
* DREAM runs once per day (time-of-day) and is *generative* —
  surfaces new concepts + relations the online loop never saw.

The two schedulers are independent. Both are idempotent over the
same tables and can safely overlap; SQLite WAL handles write
interleaving. See ``docs-internal/modules/cognitive.md §Phase 7``.

Kill-switch: ``BrainConfig.dream_max_patterns == 0`` disables DREAM
without any dedicated feature flag. Bootstrap skips scheduler
registration in that case, so there is zero runtime overhead.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from collections import Counter
from datetime import UTC, datetime, timedelta, tzinfo
from datetime import time as dt_time
from typing import TYPE_CHECKING, Any

from sovyx.brain._dream_prompts import build_pattern_prompt
from sovyx.engine.events import DreamCompleted
from sovyx.engine.types import ConceptCategory
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.brain.concept_repo import ConceptRepository
    from sovyx.brain.episode_repo import EpisodeRepository
    from sovyx.brain.learning import HebbianLearning
    from sovyx.brain.models import Episode
    from sovyx.brain.service import BrainService
    from sovyx.engine.events import EventBus
    from sovyx.engine.types import ConceptId, MindId
    from sovyx.llm.router import LLMRouter

logger = get_logger(__name__)

# Minimum episodes in the lookback window for pattern extraction to
# run. Below this, there is not enough signal for the LLM to surface
# *recurring* themes — we short-circuit to keep LLM cost at zero on
# quiet days.
_MIN_EPISODES_FOR_PATTERNS = 3

# Cap how much text we feed the LLM per episode. Typical summaries
# are 1-3 sentences; this trims pathological outliers (pasted logs)
# so a single noisy episode can't blow out the prompt budget.
_EPISODE_DIGEST_MAX_CHARS = 280

# Cross-episode Hebbian attenuation. Within-turn co-occurrence is a
# strong signal (speaker explicitly mentioned both); cross-episode
# is weaker, so we pass a damped activation.
_CROSS_EPISODE_ACTIVATION = 0.5

# A concept must appear in at least this many distinct episodes in
# the window to be a cross-episode Hebbian candidate.
_MIN_EPISODES_PER_CONCEPT = 2

# Max number of cross-episode concepts to feed to Hebbian in one
# run. strengthen() is O(n²) on the within-pair path, so we cap the
# group size to keep DREAM well under the 30 s LLM budget.
_HEBBIAN_MAX_CONCEPTS = 12


class DreamCycle:
    """One run of the DREAM phase.

    Orchestrates episode fetch → LLM pattern extraction → derived
    concept learning → cross-episode Hebbian → event emission.
    Designed to be safe under partial failure: a dead LLM or an
    unparseable response yields an empty pattern list rather than
    crashing the scheduler.
    """

    def __init__(
        self,
        brain_service: BrainService,
        episode_repo: EpisodeRepository,
        concept_repo: ConceptRepository,
        hebbian: HebbianLearning,
        llm_router: LLMRouter,
        event_bus: EventBus,
        *,
        lookback_hours: int = 24,
        max_patterns: int = 5,
        derived_confidence: float = 0.4,
    ) -> None:
        self._brain = brain_service
        self._episodes = episode_repo
        self._concepts = concept_repo
        self._hebbian = hebbian
        self._llm = llm_router
        self._events = event_bus
        self._lookback = timedelta(hours=lookback_hours)
        self._max_patterns = max_patterns
        self._derived_confidence = derived_confidence

    async def run(self, mind_id: MindId) -> DreamCompleted:
        """Execute one DREAM cycle for the given mind.

        Returns a :class:`DreamCompleted` event describing the run.
        The event is always emitted, even on short-circuits (empty
        window, LLM failure) — dashboard visibility matters more
        than pretending the run didn't happen.
        """
        start = time.monotonic()

        if self._max_patterns == 0:
            event = DreamCompleted(duration_s=round(time.monotonic() - start, 3))
            await self._events.emit(event)
            logger.info("dream_cycle_disabled", mind_id=str(mind_id))
            return event

        since = datetime.now(UTC) - self._lookback
        episodes = await self._episodes.get_since(mind_id, since)

        if len(episodes) < _MIN_EPISODES_FOR_PATTERNS:
            event = DreamCompleted(
                episodes_analyzed=len(episodes),
                duration_s=round(time.monotonic() - start, 3),
            )
            await self._events.emit(event)
            logger.info(
                "dream_cycle_skip_thin_window",
                mind_id=str(mind_id),
                episodes=len(episodes),
                min_required=_MIN_EPISODES_FOR_PATTERNS,
            )
            return event

        # ── Pattern extraction ────────────────────────────────
        patterns = await self._extract_patterns(episodes)
        logger.info(
            "dream_patterns_extracted",
            mind_id=str(mind_id),
            patterns=len(patterns),
            episodes=len(episodes),
        )

        # ── Derived concept learning ──────────────────────────
        concepts_derived = await self._learn_derived_concepts(mind_id, patterns)

        # ── Cross-episode Hebbian ─────────────────────────────
        relations_strengthened = await self._cross_episode_hebbian(episodes)

        duration = time.monotonic() - start
        event = DreamCompleted(
            patterns_found=len(patterns),
            concepts_derived=concepts_derived,
            relations_strengthened=relations_strengthened,
            episodes_analyzed=len(episodes),
            duration_s=round(duration, 3),
        )
        await self._events.emit(event)

        logger.info(
            "dream_cycle_complete",
            mind_id=str(mind_id),
            patterns_found=event.patterns_found,
            concepts_derived=event.concepts_derived,
            relations_strengthened=event.relations_strengthened,
            episodes_analyzed=event.episodes_analyzed,
            duration_s=event.duration_s,
        )
        return event

    # ── Pattern extraction (LLM) ──────────────────────────────

    async def _extract_patterns(self, episodes: list[Episode]) -> list[dict[str, Any]]:
        """Ask the LLM for recurring themes across ``episodes``.

        Returns an empty list if the LLM is unreachable, raises, or
        returns unparseable text. This keeps DREAM robust: a bad
        LLM day reduces to "no new insights today", never a crash.
        """
        digest = _render_episodes(episodes)
        prompt = build_pattern_prompt(
            episode_digest=digest,
            count=len(episodes),
            max_patterns=self._max_patterns,
        )

        try:
            response = await self._llm.generate(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=800,
            )
        except Exception:  # noqa: BLE001 — DREAM is a background maintenance
            # cycle and must survive every transient provider failure
            # (budget exhausted, timeout, HTTP 5xx, malformed JSON in cost
            # accounting, etc.). The scheduler loop re-runs tomorrow.
            logger.warning("dream_llm_generate_failed", exc_info=True)
            return []

        parsed = _parse_patterns(response.content, cap=self._max_patterns)
        return parsed

    # ── Derived concept learning ──────────────────────────────

    async def _learn_derived_concepts(
        self,
        mind_id: MindId,
        patterns: list[dict[str, Any]],
    ) -> int:
        """Materialize each pattern as a Concept via BrainService.

        ``source="dream:pattern"`` lets downstream queries filter
        derived concepts apart from user-observed ones. ``BELIEF``
        category matches the epistemic status: a pattern is an
        inference, not a directly-observed fact.
        """
        learned = 0
        for pattern in patterns:
            name = str(pattern.get("name") or "").strip()
            content = str(pattern.get("content") or "").strip()
            if not name or not content:
                continue

            importance_raw = pattern.get("importance", 0.5)
            try:
                importance = max(0.0, min(1.0, float(importance_raw)))
            except (TypeError, ValueError):
                importance = 0.5

            try:
                await self._brain.learn_concept(
                    mind_id=mind_id,
                    name=name,
                    content=content,
                    category=ConceptCategory.BELIEF,
                    source="dream:pattern",
                    importance=importance,
                    confidence=self._derived_confidence,
                )
                learned += 1
            except Exception:  # noqa: BLE001 — per-pattern resilience: one
                # failed learn_concept (DB error, dedup race, validation,
                # …) must not abort the whole DREAM cycle. Same rationale
                # as ConsolidationCycle._merge_similar (consolidation.py).
                logger.warning(
                    "dream_learn_concept_failed",
                    pattern_name=name,
                    exc_info=True,
                )

        return learned

    # ── Cross-episode Hebbian ─────────────────────────────────

    async def _cross_episode_hebbian(self, episodes: list[Episode]) -> int:
        """Strengthen relations between concepts co-occurring across episodes.

        Selects concepts that appear in at least two distinct
        episodes in the window, takes the top ``_HEBBIAN_MAX_CONCEPTS``
        by episode frequency, and feeds them to
        :meth:`HebbianLearning.strengthen` with attenuated
        activations to reflect the weaker cross-episode signal.
        """
        counts: Counter[str] = Counter()
        for ep in episodes:
            # One count per (concept, episode) pair — not per
            # mention — so we measure *episode breadth*, not
            # within-episode frequency.
            seen_this_ep: set[str] = set()
            for cid in ep.concepts_mentioned:
                cid_str = str(cid)
                if cid_str not in seen_this_ep:
                    seen_this_ep.add(cid_str)
                    counts[cid_str] += 1

        cross_episode = [
            cid_str for cid_str, n in counts.items() if n >= _MIN_EPISODES_PER_CONCEPT
        ]
        if len(cross_episode) < 2:  # noqa: PLR2004 — Hebbian needs a pair
            return 0

        # Top-N by episode frequency keeps strengthen() bounded.
        top_ids_str = sorted(cross_episode, key=lambda c: counts[c], reverse=True)[
            :_HEBBIAN_MAX_CONCEPTS
        ]

        # Rebuild as ConceptId via an imported factory — keeps this
        # module free of direct NewType calls while matching the
        # signature HebbianLearning expects.
        from sovyx.engine.types import ConceptId  # noqa: PLC0415

        concept_ids: list[ConceptId] = [ConceptId(c) for c in top_ids_str]
        activations: dict[ConceptId, float] = {
            cid: _CROSS_EPISODE_ACTIVATION for cid in concept_ids
        }

        try:
            return await self._hebbian.strengthen(
                concept_ids=concept_ids,
                activations=activations,
            )
        except Exception:  # noqa: BLE001 — same survival rule: a Hebbian
            # batch failure (sqlite3.Error, attribute drift, …) must
            # not bubble up and kill the scheduler task.
            logger.warning("dream_hebbian_failed", exc_info=True)
            return 0


# ── Helpers ──────────────────────────────────────────────────────────


def _render_episodes(episodes: list[Episode]) -> str:
    """Render episodes as a compact numbered digest for the LLM.

    Each line is trimmed to ``_EPISODE_DIGEST_MAX_CHARS`` to keep
    pathological episode content (pasted logs, code dumps) from
    dominating the prompt.
    """
    lines: list[str] = []
    for idx, ep in enumerate(episodes, start=1):
        summary = ep.summary or f"{ep.user_input}\n{ep.assistant_response}".strip()
        summary = summary.strip().replace("\n", " ")
        if len(summary) > _EPISODE_DIGEST_MAX_CHARS:
            summary = summary[: _EPISODE_DIGEST_MAX_CHARS - 1] + "…"
        ts = ep.created_at.date().isoformat() if ep.created_at else "?"
        lines.append(f"{idx}. [{ts}] {summary}")
    return "\n".join(lines)


def _parse_patterns(raw: str, *, cap: int) -> list[dict[str, Any]]:
    """Best-effort JSON parse with graceful fallback to ``[]``.

    The LLM is asked for a bare JSON array. Some providers wrap
    responses in code fences or prose despite the instruction, so
    we strip common wrappers before handing to ``json.loads``.
    """
    cleaned = raw.strip()
    # Strip markdown code fences if the model wrapped the array.
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        # Drop leading language tag (```json).
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        logger.warning("dream_pattern_parse_failed", raw_preview=raw[:200])
        return []

    if not isinstance(parsed, list):
        return []

    # Keep only dicts (defensive against LLM returning strings).
    patterns: list[dict[str, Any]] = [p for p in parsed if isinstance(p, dict)]
    return patterns[:cap]


# ── Scheduler ────────────────────────────────────────────────────────


# Fallback dream time when the configured value is malformed.
_FALLBACK_DREAM_TIME = dt_time(hour=2, minute=0)

# Minimum sleep between cycles. Even if the next dream window is
# "right now" (clock skew, just-woken-up laptop), we wait at least
# this long to prevent tight loops on edge cases.
_MIN_SLEEP_S = 60.0

# Time-of-day jitter in seconds (± half-window). Spreads DREAM runs
# across a 30-minute band to prevent thundering herd on multi-mind
# deployments, without moving the run outside the user's sleep
# hours.
_DREAM_JITTER_S = 900.0  # ±15 min


class DreamScheduler:
    """Run :class:`DreamCycle` once per day at ``dream_time``.

    Differs from :class:`ConsolidationScheduler` in one respect: it
    wakes at a specific local time (typically 02:00 in the mind's
    timezone) rather than every N hours. The underlying asyncio
    loop pattern is otherwise identical.

    Graceful stop: cancels the task on shutdown.
    """

    def __init__(
        self,
        cycle: DreamCycle,
        *,
        dream_time: str = "02:00",
        timezone: str = "UTC",
    ) -> None:
        self._cycle = cycle
        self._dream_time = _parse_dream_time(dream_time)
        self._timezone = timezone
        self._tzinfo = _resolve_timezone(timezone)
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self, mind_id: MindId) -> None:
        """Start the background DREAM loop."""
        if self._task is not None:
            return

        self._running = True
        self._task = asyncio.create_task(self._loop(mind_id))
        logger.info(
            "dream_scheduler_started",
            mind_id=str(mind_id),
            dream_time=self._dream_time.isoformat(timespec="minutes"),
            timezone=self._timezone,
        )

    async def stop(self) -> None:
        """Stop the background DREAM loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("dream_scheduler_stopped")

    async def _loop(self, mind_id: MindId) -> None:
        while self._running:
            try:
                delta = self._seconds_until_next_dream()
                jitter = random.uniform(-_DREAM_JITTER_S, _DREAM_JITTER_S)  # nosec B311
                await asyncio.sleep(max(_MIN_SLEEP_S, delta + jitter))
                await self._cycle.run(mind_id)
            except asyncio.CancelledError:
                break
            except Exception:
                # Survive cycle exceptions: tomorrow is another day.
                logger.exception("dream_cycle_failed", mind_id=str(mind_id))

    def _seconds_until_next_dream(self, *, now: datetime | None = None) -> float:
        """Seconds from ``now`` until the next ``dream_time`` occurrence.

        Exposed as an internal method (with injectable ``now``) so
        unit tests can exercise the time arithmetic deterministically
        without patching the clock globally.
        """
        current = now if now is not None else datetime.now(self._tzinfo)
        if current.tzinfo is None:
            current = current.replace(tzinfo=self._tzinfo)
        target_today = current.replace(
            hour=self._dream_time.hour,
            minute=self._dream_time.minute,
            second=0,
            microsecond=0,
        )
        if target_today <= current:
            target_today = target_today + timedelta(days=1)
        return (target_today - current).total_seconds()


def _parse_dream_time(raw: str) -> dt_time:
    """Parse ``"HH:MM"`` into :class:`datetime.time`.

    Falls back to 02:00 on any parse failure — a malformed mind
    config must not prevent the scheduler from starting.
    """
    try:
        parts = raw.split(":")
        if len(parts) != 2:  # noqa: PLR2004
            raise ValueError("dream_time must be HH:MM")  # noqa: TRY301
        hour = int(parts[0])
        minute = int(parts[1])
        return dt_time(hour=hour, minute=minute)
    except (ValueError, TypeError):
        logger.warning("dream_time_invalid_fallback", raw=raw)
        return _FALLBACK_DREAM_TIME


def _resolve_timezone(name: str) -> tzinfo:
    """Resolve a tz name to a tzinfo, falling back to UTC on error.

    Uses :mod:`zoneinfo` (stdlib, 3.9+). If the name is unknown —
    e.g. old glibc tzdata missing a newly-added zone — we log and
    return UTC so DREAM still runs (just shifted by a few hours).
    """
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # noqa: PLC0415

        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning("dream_timezone_unknown_fallback", name=name)
        return UTC
    except Exception:  # noqa: BLE001 — unknown stdlib failure modes
        # (corrupted tzdata, OS-level lookups). Fall back to UTC and
        # log so we can investigate; never crash bootstrap on this.
        logger.warning("dream_timezone_resolve_failed", name=name, exc_info=True)
        return UTC
