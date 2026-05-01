"""Per-mind retention service + scheduler — Phase 8 / T8.21 step 6.

Time-based retention enforcer. Sibling to
:class:`sovyx.mind.forget.MindForgetService`:

* **MindForgetService** — operator-invoked right-to-erasure (GDPR
  Art. 17 / LGPD Art. 18 VI). Wipes every record for a mind, writes
  :data:`ConsentAction.DELETE` tombstone.
* **MindRetentionService** (this module) — scheduled policy enforcer
  (GDPR Art. 5(1)(e) "storage limitation" / LGPD Art. 16). Removes
  records older than per-surface horizons, writes
  :data:`ConsentAction.RETENTION_PURGE` tombstone.

The two services share the per-mind data surface model + the
multi-pool (brain / conversations / system) + ConsentLedger
architecture. They differ in:

* **Filter axis** — forget by identity (mind_id), retention by
  timestamp (age).
* **Trigger** — forget is operator-driven (CLI / dashboard /
  daemon-side RPC); retention is scheduled (DreamScheduler hook,
  next commit) + on-demand (CLI / dashboard, future commits).
* **Tombstone** — DELETE vs. RETENTION_PURGE.

Per-surface horizons resolve in priority order:

1. ``MindConfig.retention.<surface>_days`` (per-mind override; None = inherit)
2. ``EngineConfig.tuning.retention.<surface>_days`` (global default)
3. Default = 0 = disabled / infinite.

When horizon = 0 the surface is skipped entirely (no count, no
delete). This matches the convention of every other retention knob
in the codebase (``LoggingConfig.retention_days``,
``voice_audio_retention_days``, ``FtsIndexer._retention_days``).

Concepts + relations are intentionally NOT subject to time-based
retention here. They have their own importance-based decay via
``MindConfig.brain.forgetting_enabled`` + ``decay_rate``; layering
two policies on the same surface causes double-deletion.

Reference: master mission ``MISSION-voice-final-skype-grade-2026.md``
§Phase 8 / T8.21 step 6;
``OPERATOR-DEBT-MASTER-2026-05-01.md`` D9 / D17 / D18 (defaults
ratified); ``docs/compliance.md``;
``docs/modules/voice-privacy.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from datetime import time as dt_time
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.observability.tasks import spawn

if TYPE_CHECKING:
    from sovyx.engine.config import EngineConfig
    from sovyx.engine.types import MindId
    from sovyx.mind.config import MindConfig
    from sovyx.persistence.pool import DatabasePool
    from sovyx.voice._consent_ledger import ConsentLedger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class MindRetentionReport:
    """Forensic record of what a :meth:`MindRetentionService.prune_mind`
    call destroyed (or would destroy in dry_run).

    Per-surface counts are pre-delete row counts. Counts of 0 indicate
    either "no rows old enough to prune" OR "horizon = 0 = retention
    disabled for this surface" — the report does not distinguish; the
    operator inspects the ``effective_horizons`` field for clarity.

    Attributes:
        mind_id: Target mind.
        cutoff_utc: ISO-8601 UTC cutoff timestamp; rows with
            ``timestamp < cutoff_utc`` are pruned. One value per
            invocation, not per surface — if surfaces have different
            horizons each gets its own derived cutoff (returned in
            ``effective_horizons``).
        episodes_purged: Rows removed from ``episodes``.
        conversations_purged: Rows removed from ``conversations``
            (cascades turns via FK).
        conversation_turns_purged: Rows removed from
            ``conversation_turns`` via cascade.
        daily_stats_purged: Rows removed from ``daily_stats``.
        consolidation_log_purged: Rows removed from
            ``consolidation_log``.
        consent_ledger_purged: Records removed from the consent
            ledger (excludes the RETENTION_PURGE tombstone written
            by the prune itself).
        effective_horizons: Per-surface horizon (days) actually
            applied. ``0`` means the surface was skipped (retention
            disabled for it).
        dry_run: ``True`` when the call was a preview.
    """

    mind_id: MindId
    cutoff_utc: str
    episodes_purged: int
    conversations_purged: int
    conversation_turns_purged: int
    daily_stats_purged: int
    consolidation_log_purged: int
    consent_ledger_purged: int
    effective_horizons: dict[str, int]
    dry_run: bool

    @property
    def total_brain_rows_purged(self) -> int:
        """Episodes + consolidation_log (the brain-pool surfaces)."""
        return self.episodes_purged + self.consolidation_log_purged

    @property
    def total_conversations_rows_purged(self) -> int:
        """Conversations + conversation_turns (the conversations-pool surfaces)."""
        return self.conversations_purged + self.conversation_turns_purged

    @property
    def total_system_rows_purged(self) -> int:
        """daily_stats only (the system-pool surface)."""
        return self.daily_stats_purged

    @property
    def total_rows_purged(self) -> int:
        """Aggregate across every relational pool. Excludes consent
        ledger (JSONL, separate compliance surface)."""
        return (
            self.total_brain_rows_purged
            + self.total_conversations_rows_purged
            + self.total_system_rows_purged
        )


class MindRetentionService:
    """Apply time-based retention horizons to a single mind's data.

    Invoked by:

    * The DreamScheduler hook (next commit) — once-per-day at
      ``mind.dream_time`` in the mind's timezone.
    * The CLI ``sovyx mind retention prune`` (next commit).
    * The dashboard ``POST /api/mind/{mind_id}/retention/prune``
      endpoint (next commit).

    Args:
        engine_config: Source of global retention defaults
            (``EngineConfig.tuning.retention.*``).
        brain_pool: Required. Brain DB (episodes + consolidation_log).
        conversations_pool: Optional. When ``None``, conversations +
            turns are skipped (counts stay at 0).
        system_pool: Optional. When ``None``, daily_stats is skipped.
        ledger: Optional :class:`ConsentLedger`. When supplied,
            :meth:`prune_mind` calls ``ledger.prune_old`` after the
            relational pools commit.

    Thread safety:
        Holds no mutable state; concurrent invocations on different
        mind_ids are safe. Concurrent invocations on the SAME mind_id
        serialise via each pool's writer lock.
    """

    def __init__(
        self,
        *,
        engine_config: EngineConfig,
        brain_pool: DatabasePool,
        conversations_pool: DatabasePool | None = None,
        system_pool: DatabasePool | None = None,
        ledger: ConsentLedger | None = None,
    ) -> None:
        self._engine_config = engine_config
        self._brain_pool = brain_pool
        self._conversations_pool = conversations_pool
        self._system_pool = system_pool
        self._ledger = ledger

    async def prune_mind(
        self,
        mind_id: MindId,
        *,
        mind_config: MindConfig | None = None,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> MindRetentionReport:
        """Prune records older than per-surface horizons.

        Args:
            mind_id: Target mind.
            mind_config: Optional. When supplied, per-mind retention
                overrides (``MindConfig.retention.<surface>_days``)
                are honoured. When ``None``, only global defaults
                from ``EngineConfig.tuning.retention`` apply.
            dry_run: When True, returns counts without writing.
            now: Injectable clock for deterministic tests. Defaults
                to ``datetime.now(UTC)``.

        Returns:
            :class:`MindRetentionReport` documenting what was (or
            would have been) destroyed + per-surface effective
            horizons applied.

        Raises:
            ValueError: Empty / whitespace ``mind_id``.
        """
        if not str(mind_id).strip():
            msg = (
                "MindRetentionService.prune_mind requires a non-empty "
                "mind_id; an empty value would match every empty-mind_id "
                "row and is rejected as a defensive guard"
            )
            raise ValueError(msg)

        ts_now = now if now is not None else datetime.now(UTC)
        horizons = self._effective_horizons(mind_config)

        cutoffs = {
            surface: self._cutoff_for_horizon(ts_now, days) for surface, days in horizons.items()
        }

        # Count phase — runs even on dry_run to populate the report.
        episodes_count = await self._count_old_episodes(
            mind_id,
            cutoffs["episodes"],
        )
        conv_count, turns_count = await self._count_old_conversations(
            mind_id,
            cutoffs["conversations"],
        )
        consolidation_count = await self._count_old_consolidation_log(
            mind_id,
            cutoffs["consolidation_log"],
        )
        daily_stats_count = await self._count_old_daily_stats(
            mind_id,
            cutoffs["daily_stats"],
        )

        if dry_run:
            return MindRetentionReport(
                mind_id=mind_id,
                cutoff_utc=ts_now.isoformat(),
                episodes_purged=episodes_count,
                conversations_purged=conv_count,
                conversation_turns_purged=turns_count,
                consolidation_log_purged=consolidation_count,
                daily_stats_purged=daily_stats_count,
                consent_ledger_purged=0,
                effective_horizons=horizons,
                dry_run=True,
            )

        # Real run — per-pool transactions.
        await self._delete_old_episodes(mind_id, cutoffs["episodes"])
        await self._delete_old_conversations(mind_id, cutoffs["conversations"])
        await self._delete_old_consolidation_log(
            mind_id,
            cutoffs["consolidation_log"],
        )
        await self._delete_old_daily_stats(mind_id, cutoffs["daily_stats"])

        consent_purged = 0
        if self._ledger is not None and horizons["consent_ledger"] > 0:
            consent_purged = self._ledger.prune_old(
                before=cutoffs["consent_ledger"],
                mind_id=str(mind_id),
            )

        report = MindRetentionReport(
            mind_id=mind_id,
            cutoff_utc=ts_now.isoformat(),
            episodes_purged=episodes_count,
            conversations_purged=conv_count,
            conversation_turns_purged=turns_count,
            consolidation_log_purged=consolidation_count,
            daily_stats_purged=daily_stats_count,
            consent_ledger_purged=consent_purged,
            effective_horizons=horizons,
            dry_run=False,
        )
        logger.info(
            "mind.retention.prune_complete",
            mind_id=str(mind_id),
            **{
                "mind.episodes_purged": report.episodes_purged,
                "mind.conversations_purged": report.conversations_purged,
                "mind.conversation_turns_purged": report.conversation_turns_purged,
                "mind.consolidation_log_purged": report.consolidation_log_purged,
                "mind.daily_stats_purged": report.daily_stats_purged,
                "mind.consent_ledger_purged": report.consent_ledger_purged,
                "mind.total_rows_purged": report.total_rows_purged,
            },
        )
        return report

    # ── Horizon resolution ─────────────────────────────────────────

    def _effective_horizons(self, mind_config: MindConfig | None) -> dict[str, int]:
        """Resolve per-surface effective horizon (days).

        Priority: mind_config override > engine_config global default.
        """
        global_cfg = self._engine_config.tuning.retention
        out: dict[str, int] = {
            "episodes": global_cfg.episodes_days,
            "conversations": global_cfg.conversations_days,
            "consolidation_log": global_cfg.consolidation_log_days,
            "daily_stats": global_cfg.daily_stats_days,
            "consent_ledger": global_cfg.consent_ledger_days,
        }
        if mind_config is None:
            return out
        override = mind_config.retention
        for surface in out:
            field_name = f"{surface}_days"
            override_value = getattr(override, field_name, None)
            if override_value is not None:
                out[surface] = override_value
        return out

    @staticmethod
    def _cutoff_for_horizon(now: datetime, days: int) -> str:
        """Return the ISO-8601 UTC cutoff string for a horizon.

        ``days = 0`` returns an empty string sentinel — the
        "disabled" marker that count + delete helpers check before
        firing any SQL. Non-zero days return ``(now - days).isoformat()``.
        """
        if days <= 0:
            return ""
        cutoff = now - timedelta(days=days)
        return cutoff.isoformat()

    # ── Brain pool — episodes + consolidation_log ──────────────────

    async def _count_old_episodes(self, mind_id: MindId, cutoff: str) -> int:
        if not cutoff:
            return 0
        async with self._brain_pool.read() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM episodes WHERE mind_id = ? AND created_at < ?",
                (str(mind_id), cutoff),
            )
            row = await cursor.fetchone()
            return int((row or [0])[0])

    async def _delete_old_episodes(self, mind_id: MindId, cutoff: str) -> None:
        if not cutoff:
            return
        async with self._brain_pool.transaction() as conn:
            # Manual delete of episode_embeddings BEFORE episodes (vec0
            # virtual table has no FK cascade; mirror MindForgetService
            # ordering).
            if self._brain_pool.has_sqlite_vec:
                await conn.execute(
                    """DELETE FROM episode_embeddings
                       WHERE episode_id IN
                           (SELECT id FROM episodes
                            WHERE mind_id = ? AND created_at < ?)""",
                    (str(mind_id), cutoff),
                )
            # Episodes cascade -> conversation_imports (FK ON DELETE CASCADE).
            await conn.execute(
                "DELETE FROM episodes WHERE mind_id = ? AND created_at < ?",
                (str(mind_id), cutoff),
            )

    async def _count_old_consolidation_log(
        self,
        mind_id: MindId,
        cutoff: str,
    ) -> int:
        if not cutoff:
            return 0
        async with self._brain_pool.read() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM consolidation_log WHERE mind_id = ? AND started_at < ?",
                (str(mind_id), cutoff),
            )
            row = await cursor.fetchone()
            return int((row or [0])[0])

    async def _delete_old_consolidation_log(
        self,
        mind_id: MindId,
        cutoff: str,
    ) -> None:
        if not cutoff:
            return
        async with self._brain_pool.transaction() as conn:
            await conn.execute(
                "DELETE FROM consolidation_log WHERE mind_id = ? AND started_at < ?",
                (str(mind_id), cutoff),
            )

    # ── Conversations pool ─────────────────────────────────────────

    async def _count_old_conversations(
        self,
        mind_id: MindId,
        cutoff: str,
    ) -> tuple[int, int]:
        """Returns (conversations_count, turns_count_via_cascade)."""
        if not cutoff or self._conversations_pool is None:
            return 0, 0
        async with self._conversations_pool.read() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE mind_id = ? AND last_message_at < ?",
                (str(mind_id), cutoff),
            )
            conv_count = int((await cursor.fetchone() or [0])[0])

            cursor = await conn.execute(
                """SELECT COUNT(*) FROM conversation_turns
                   WHERE conversation_id IN
                       (SELECT id FROM conversations
                        WHERE mind_id = ? AND last_message_at < ?)""",
                (str(mind_id), cutoff),
            )
            turns_count = int((await cursor.fetchone() or [0])[0])
        return conv_count, turns_count

    async def _delete_old_conversations(
        self,
        mind_id: MindId,
        cutoff: str,
    ) -> None:
        if not cutoff or self._conversations_pool is None:
            return
        async with self._conversations_pool.transaction() as conn:
            await conn.execute(
                "DELETE FROM conversations WHERE mind_id = ? AND last_message_at < ?",
                (str(mind_id), cutoff),
            )

    # ── System pool — daily_stats ──────────────────────────────────

    async def _count_old_daily_stats(
        self,
        mind_id: MindId,
        cutoff: str,
    ) -> int:
        """``daily_stats.date`` is YYYY-MM-DD string; comparison is
        lexicographic (matches chronological for ISO date format)."""
        if not cutoff or self._system_pool is None:
            return 0
        cutoff_date = self._iso_to_date(cutoff)
        if not cutoff_date:
            return 0
        async with self._system_pool.read() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM daily_stats WHERE mind_id = ? AND date < ?",
                (str(mind_id), cutoff_date),
            )
            row = await cursor.fetchone()
            return int((row or [0])[0])

    async def _delete_old_daily_stats(
        self,
        mind_id: MindId,
        cutoff: str,
    ) -> None:
        if not cutoff or self._system_pool is None:
            return
        cutoff_date = self._iso_to_date(cutoff)
        if not cutoff_date:
            return
        async with self._system_pool.transaction() as conn:
            await conn.execute(
                "DELETE FROM daily_stats WHERE mind_id = ? AND date < ?",
                (str(mind_id), cutoff_date),
            )

    @staticmethod
    def _iso_to_date(iso_ts: str) -> str:
        """Extract YYYY-MM-DD from an ISO-8601 timestamp string.

        Returns empty string on parse failure — the caller treats
        empty cutoff as "skip surface".
        """
        if not iso_ts:
            return ""
        # ISO-8601 always has "T" between date and time; the first 10
        # chars are the YYYY-MM-DD date. Defensive parse in case of
        # malformed input.
        try:
            parsed = datetime.fromisoformat(iso_ts)
        except ValueError:
            return ""
        return parsed.date().isoformat()


# ── Auto-prune scheduler ─────────────────────────────────────────────


_FALLBACK_PRUNE_TIME = dt_time(hour=3, minute=0)
"""Fallback when ``MindConfig.retention.prune_time`` is malformed —
03:00 = 1 hour after typical dream_time of 02:00."""

_MIN_SLEEP_S = 60.0
"""Minimum sleep between cycles. Even if ``next prune`` is "right
now" (clock skew, just-resumed laptop), we wait at least this long
to prevent tight loops on edge cases. Mirrors DreamScheduler."""

_PRUNE_JITTER_S = 900.0
"""±15-minute jitter spreads retention runs across a 30-minute band
on multi-mind deployments — prevents thundering herd if multiple
minds share the same prune_time + timezone. Mirrors DreamScheduler."""


def _parse_prune_time(raw: str) -> dt_time:
    """Parse ``"HH:MM"`` into :class:`datetime.time`.

    Falls back to 03:00 on parse failure — a malformed config must
    not prevent the scheduler from starting (retention is a
    privacy-sensitive surface; failing closed = no retention =
    storage limitation breach risk).
    """
    try:
        parts = raw.split(":")
        if len(parts) != 2:  # noqa: PLR2004
            raise ValueError("prune_time must be HH:MM")  # noqa: TRY301
        hour = int(parts[0])
        minute = int(parts[1])
        return dt_time(hour=hour, minute=minute)
    except (ValueError, TypeError):
        logger.warning("retention.prune_time_invalid_fallback", raw=raw)
        return _FALLBACK_PRUNE_TIME


def _resolve_timezone(name: str) -> tzinfo:
    """Resolve a tz name to a tzinfo, falling back to UTC on error."""
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        logger.warning("retention.timezone_invalid_fallback", name=name)
        return UTC


class RetentionScheduler:
    """Run :meth:`MindRetentionService.prune_mind` daily at ``prune_time``.

    Mirrors :class:`sovyx.brain.dream.DreamScheduler` lifecycle +
    timing pattern (once-per-day at HH:MM in mind's timezone, with
    ±15-minute jitter, surviving exceptions on a "tomorrow is another
    day" basis). The DREAM cycle runs first (typical dream_time
    02:00); retention runs after (default prune_time 03:00) so
    consolidation_log entries from the night's DREAM are still
    available for retention to evaluate.

    The scheduler is **off by default** — ``MindConfig.retention.auto_prune_enabled``
    must be True for the daemon's lifecycle to start it. This
    follows the staged-adoption discipline (foundation lands;
    operator opts in after validating dry-run counts).

    Args:
        service: :class:`MindRetentionService` to invoke each cycle.
        mind_config: Mind config — passed to ``service.prune_mind``
            so per-mind retention overrides are honoured.
        prune_time: ``"HH:MM"`` in the mind's timezone.
        timezone: IANA timezone name (e.g. ``"America/Sao_Paulo"``).
            UTC fallback on parse failure.
    """

    def __init__(
        self,
        service: MindRetentionService,
        *,
        mind_config: MindConfig | None = None,
        prune_time: str = "03:00",
        timezone: str = "UTC",
    ) -> None:
        self._service = service
        self._mind_config = mind_config
        self._prune_time = _parse_prune_time(prune_time)
        self._timezone = timezone
        self._tzinfo = _resolve_timezone(timezone)
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self, mind_id: MindId) -> None:
        """Start the background retention loop. Idempotent."""
        if self._task is not None:
            return
        self._running = True
        self._task = spawn(self._loop(mind_id), name="retention-scheduler")
        logger.info(
            "retention_scheduler_started",
            mind_id=str(mind_id),
            prune_time=self._prune_time.isoformat(timespec="minutes"),
            timezone=self._timezone,
        )

    async def stop(self) -> None:
        """Stop the background retention loop. Idempotent."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("retention_scheduler_stopped")

    async def _loop(self, mind_id: MindId) -> None:
        while self._running:
            try:
                delta = self._seconds_until_next_prune()
                jitter = random.uniform(-_PRUNE_JITTER_S, _PRUNE_JITTER_S)  # nosec B311
                await asyncio.sleep(max(_MIN_SLEEP_S, delta + jitter))
                await self._service.prune_mind(
                    mind_id,
                    mind_config=self._mind_config,
                )
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                # Survive cycle exceptions — retention is best-effort
                # daily; tomorrow is another day. A wedged prune cycle
                # must not crash the daemon.
                logger.exception("retention_cycle_failed", mind_id=str(mind_id))

    def _seconds_until_next_prune(self, *, now: datetime | None = None) -> float:
        """Seconds from ``now`` until the next ``prune_time`` occurrence.

        Injectable ``now`` for deterministic testing. Mirror of
        :meth:`DreamScheduler._seconds_until_next_dream`.
        """
        current = now if now is not None else datetime.now(self._tzinfo)
        if current.tzinfo is None:
            current = current.replace(tzinfo=self._tzinfo)
        target_today = current.replace(
            hour=self._prune_time.hour,
            minute=self._prune_time.minute,
            second=0,
            microsecond=0,
        )
        if target_today <= current:
            target_today = target_today + timedelta(days=1)
        return (target_today - current).total_seconds()

    @property
    def is_running(self) -> bool:
        """True when the scheduler's background task is alive."""
        return self._running and self._task is not None


__all__ = [
    "MindRetentionReport",
    "MindRetentionService",
    "RetentionScheduler",
]
