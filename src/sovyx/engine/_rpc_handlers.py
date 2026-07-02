"""RPC handlers for CLI ↔ daemon communication (SPE-015 §2).

The CLI talks to the daemon via JSON-RPC 2.0 over a Unix-domain socket
exposed by :class:`sovyx.engine.rpc_server.DaemonRPCServer`. Handlers
live in this module so that ``cli/main.py::start`` can wire them with
a single call (``register_cli_handlers(rpc, registry)``) instead of
inlining a dozen lambdas in the bootstrap function.

Each handler is a thin adapter: it resolves the right service from
the :class:`ServiceRegistry` and delegates to existing engine code.
No business logic lives here — keep this file as a routing table
for the CLI surface.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from sovyx.engine.registry import ServiceRegistry
    from sovyx.engine.rpc_server import DaemonRPCServer
    from sovyx.engine.types import MindId
    from sovyx.mind.config import MindConfig

logger = get_logger(__name__)


def _load_mind_config_best_effort(
    data_dir: Path,
    mind_id: MindId,
) -> MindConfig | None:
    """Best-effort load of ``MindConfig`` from ``<data_dir>/<mind_id>/mind.yaml``.

    Used by retention RPC handler to resolve per-mind retention
    overrides. Returns ``None`` on any failure (missing file,
    malformed YAML, schema violation) — the caller falls back to
    global defaults; retention is still functional, just without
    per-mind overrides.

    The "best-effort" semantics matter because retention is a
    privacy-sensitive scheduled operation: a malformed mind.yaml
    must NOT block retention from running on the global defaults
    (operator's compliance posture is more important than perfect
    config resolution).
    """
    try:
        import yaml  # noqa: PLC0415 — lazy

        from sovyx.mind.config import MindConfig as _MindConfig  # noqa: PLC0415

        path = data_dir / str(mind_id) / "mind.yaml"
        if not path.exists():
            return None
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return _MindConfig.model_validate(raw)
    except Exception:  # noqa: BLE001 — best-effort by design
        return None


def collect_voice_health_snapshot() -> dict[str, Any]:
    """Serialize this process's voice-health stores to a JSON-safe dict.

    DOCTOR-3 closure (AP #70/#71 class) — single serializer for the
    quarantine / failover-history / degraded-store triple, shared by:

    * the daemon-side ``voice.health.snapshot`` RPC handler (the
      producer the CLI prefers), and
    * ``sovyx.cli.commands.doctor``'s local fallback (same function,
      run inside the CLI process when no daemon is reachable),

    so the wire shape and the fallback shape cannot drift (AP #40 /
    #53 sibling — one shared symbol, never two independent literals).

    Field notes:

    * ``recheck_in_s`` is computed HERE (producer-side) because
      ``expires_at_monotonic`` is a ``time.monotonic()`` value that is
      meaningless across process boundaries.
    * Degraded entries carry ``title_token``/``body_token`` +
      full action chips so the CLI can rebuild real
      :class:`~sovyx.engine._degraded_store.DegradedEntry` objects and
      reuse the dashboard's SSoT composite-severity helpers.

    Every axis degrades independently to an empty list — a partially
    wired process (e.g. voice disabled) must never break the snapshot.
    """
    import time  # noqa: PLC0415 — lazy; only needed for quarantine TTLs

    quarantine_rows: list[dict[str, Any]] = []
    try:
        from sovyx.voice.health._quarantine import get_default_quarantine  # noqa: PLC0415

        now = time.monotonic()
        for entry in get_default_quarantine().snapshot():
            quarantine_rows.append(
                {
                    "endpoint_guid": entry.endpoint_guid,
                    "device_friendly_name": entry.device_friendly_name,
                    "host_api": entry.host_api,
                    "reason": entry.reason,
                    "derived_reason": entry.derived_reason,
                    "resolved_reason": entry.resolved_reason,
                    "recheck_in_s": max(0.0, entry.expires_at_monotonic - now),
                },
            )
    except Exception:  # noqa: BLE001 — observability-only surface
        logger.debug("voice_health_snapshot_quarantine_failed")

    failover_rows: list[dict[str, Any]] = []
    try:
        from sovyx.voice.health._failover_history import (  # noqa: PLC0415
            get_default_failover_history,
        )

        for run in get_default_failover_history().entries():
            failover_rows.append(
                {
                    "ladder_id": run.ladder_id,
                    "verdict": run.verdict,
                    "candidates_tried": run.candidates_tried,
                    "elapsed_ms": run.elapsed_ms,
                    "from_endpoint": run.from_endpoint,
                    "candidates": [
                        {
                            "index": candidate.index,
                            "verdict": candidate.verdict,
                            "target_endpoint": candidate.target_endpoint,
                            "elapsed_ms": candidate.elapsed_ms,
                            "error_class": candidate.error_class,
                            "skipped_reason": candidate.skipped_reason,
                        }
                        for candidate in run.candidates
                    ],
                },
            )
    except Exception:  # noqa: BLE001 — observability-only surface
        logger.debug("voice_health_snapshot_failover_failed")

    degraded_rows: list[dict[str, Any]] = []
    try:
        from sovyx.engine._degraded_store import get_default_degraded_store  # noqa: PLC0415

        for degraded in get_default_degraded_store().snapshot():
            degraded_rows.append(
                {
                    "axis": degraded.axis,
                    "reason": degraded.reason,
                    "severity": degraded.severity,
                    "title_token": degraded.title_token,
                    "body_token": degraded.body_token,
                    "action_chips": [
                        {
                            "label_token": chip.label_token,
                            "action": chip.action,
                            "target": chip.target,
                            "style": chip.style,
                        }
                        for chip in degraded.action_chips
                    ],
                },
            )
    except Exception:  # noqa: BLE001 — observability-only surface
        logger.debug("voice_health_snapshot_degraded_failed")

    return {
        "quarantine": quarantine_rows,
        "failover_history": failover_rows,
        "degraded": degraded_rows,
    }


async def _brain_row_counts(
    registry: ServiceRegistry,
    mind_id: MindId,
) -> tuple[int, int]:
    """Best-effort ``(concepts, episodes)`` row counts for one mind.

    Mirrors ``StatusCollector._get_memory_stats`` (the dashboard's
    stats provider): each axis degrades to 0 independently when its
    repository is unregistered or the count query fails — a partially
    booted brain must never crash a status/stats RPC.
    """
    concepts = 0
    episodes = 0
    try:
        from sovyx.brain.concept_repo import ConceptRepository  # noqa: PLC0415

        if registry.is_registered(ConceptRepository):
            concept_repo = await registry.resolve(ConceptRepository)
            concepts = await concept_repo.count(mind_id)
    except Exception:  # noqa: BLE001 — best-effort by design
        logger.debug("rpc_brain_concept_count_failed", mind_id=str(mind_id))

    try:
        from sovyx.brain.episode_repo import EpisodeRepository  # noqa: PLC0415

        if registry.is_registered(EpisodeRepository):
            episode_repo = await registry.resolve(EpisodeRepository)
            episodes = await episode_repo.count(mind_id)
    except Exception:  # noqa: BLE001 — best-effort by design
        logger.debug("rpc_brain_episode_count_failed", mind_id=str(mind_id))

    return concepts, episodes


async def _relation_count_best_effort(
    registry: ServiceRegistry,
    mind_id: MindId,
) -> int:
    """Best-effort relation row count for one mind (0 on any failure).

    ``RelationRepository`` exposes no ``count`` — this queries the
    mind's brain pool directly with the same concept-join filter
    ``RelationRepository.delete_weak`` uses (relations carry no
    ``mind_id`` column; membership is derived via ``source_id``).
    """
    try:
        from sovyx.persistence.manager import DatabaseManager  # noqa: PLC0415

        if not registry.is_registered(DatabaseManager):
            return 0
        db_manager = await registry.resolve(DatabaseManager)
        pool = db_manager.get_brain_pool(mind_id)
        async with pool.read() as conn:
            cursor = await conn.execute(
                """SELECT COUNT(*) FROM relations
                WHERE source_id IN (SELECT id FROM concepts WHERE mind_id = ?)""",
                (str(mind_id),),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001 — best-effort by design
        logger.debug("rpc_brain_relation_count_failed", mind_id=str(mind_id))
        return 0


# Stable identity for every CLI session. PersonResolver attaches all
# CLI traffic to the same person row regardless of which terminal the
# user is running from — matches the dashboard's single-identity
# treatment (see ``dashboard/chat.py::_DASHBOARD_CHANNEL_USER_ID``).
CLI_CHANNEL_USER_ID = "cli-user"

# Display name surfaced to the LLM and conversation history. Kept
# short and identifying so the assistant knows it is talking to the
# operator at the terminal, not a Telegram contact.
CLI_USER_NAME = "CLI"

# Per-check budget for the daemon-side ``doctor`` RPC sweep. Checks run
# concurrently (``HealthRegistry.run_all`` gathers them), so happy-path
# wall time is ~max(single check), not the sum. A check exceeding this
# budget yields its own RED "Check timed out" row while its siblings
# still report — partial results, never a hung CLI.
_DOCTOR_CHECK_TIMEOUT_S = 5.0

# Outer safety bound on the whole ``doctor`` sweep (belt-and-braces over
# the per-check bound — covers a pathological gather stall). On expiry
# the handler returns a single synthetic RED row + ``note="timed_out"``
# instead of hanging. The CLI-side call budget
# (``sovyx.cli.commands.doctor._ONLINE_CHECKS_RPC_TIMEOUT_S``) must stay
# ABOVE this value so a slow sweep surfaces as check rows, not as a
# client-side transport error.
_DOCTOR_TOTAL_TIMEOUT_S = 8.0


def register_cli_handlers(
    rpc: DaemonRPCServer,
    registry: ServiceRegistry,
) -> None:
    """Register every RPC method exposed to the CLI on ``rpc``.

    Idempotent: re-registration overwrites the previous handler. Safe
    to call after construction without any guard.
    """

    async def _chat(
        message: str,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Run a single chat turn through the cognitive loop."""
        from sovyx.dashboard.chat import handle_chat_message  # noqa: PLC0415
        from sovyx.engine.types import ChannelType  # noqa: PLC0415

        return await handle_chat_message(
            registry=registry,
            message=message,
            user_name=CLI_USER_NAME,
            conversation_id=conversation_id,
            channel=ChannelType.CLI,
            channel_user_id=CLI_CHANNEL_USER_ID,
        )

    async def _mind_list() -> dict[str, Any]:
        """Return the list of active minds and which one is the default."""
        from sovyx.engine.bootstrap import MindManager  # noqa: PLC0415

        mgr = await registry.resolve(MindManager)
        active = mgr.get_active_minds()
        return {
            "minds": active,
            "active": active[0] if active else None,
        }

    async def _brain_search(
        query: str,
        mind_id: str = "default",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Search a mind's brain concepts (``sovyx brain search``).

        AP #53 closure — the CLI has called ``brain.search`` since the
        command shipped, but no daemon ever registered it, so the
        command always failed with 'Method not found'. Producer half of
        the contract; the consumer is ``cli/main.py::brain_search``,
        which renders a list (one bullet per item) or the raw payload.

        Wiring: delegates to :meth:`sovyx.brain.service.BrainService.search`
        — the same hybrid-retrieval + spreading-activation entrypoint the
        cognitive loop's phases use — honouring the CLI's ``mind_id``
        parameter (unlike the dashboard's ``search_brain``, which always
        resolves the active mind).

        Returns:
            JSON-serialisable list of compact concept projections:
            ``{id, name, category, importance, confidence, score}``,
            score-descending. Empty list for a blank query (mirrors
            the dashboard's graceful-degradation contract).

        Raises:
            BrainError: BrainService not registered (daemon booting or
                brain subsystem disabled).
        """
        from sovyx.brain.service import BrainService  # noqa: PLC0415
        from sovyx.engine.errors import BrainError  # noqa: PLC0415
        from sovyx.engine.types import MindId  # noqa: PLC0415

        if not query.strip():
            return []
        bounded_limit = max(1, min(limit, 100))

        if not registry.is_registered(BrainService):
            msg = (
                "brain subsystem not available (BrainService not "
                "registered) — the daemon may still be booting"
            )
            raise BrainError(msg)

        brain = await registry.resolve(BrainService)
        results = await brain.search(
            query.strip(),
            MindId(mind_id),
            limit=bounded_limit,
        )
        return [
            {
                "id": str(concept.id),
                "name": concept.name,
                "category": concept.category.value,
                "importance": round(concept.importance, 3),
                "confidence": round(concept.confidence, 3),
                "score": round(score, 4),
            }
            for concept, score in results
        ]

    async def _brain_stats(mind_id: str = "default") -> dict[str, Any]:
        """Per-mind brain row counts (``sovyx brain stats``).

        AP #53 closure — sibling of ``brain.search``; the consumer is
        ``cli/main.py::brain_stats``, which renders each dict entry as
        a ``key: value`` line.

        Wiring: ``ConceptRepository.count`` + ``EpisodeRepository.count``
        (the same providers ``StatusCollector._get_memory_stats``
        mirrors for the dashboard) plus a best-effort relation count
        via the mind's brain pool (:func:`_relation_count_best_effort`).
        The repositories are REQUIRED — a stats command that silently
        reports 0 when the brain is absent would be a semantic lie
        (AP #48); the relation axis alone degrades to 0 because it has
        no repository-level counter to fail loudly through.

        Returns:
            ``{"mind_id": str, "concepts": int, "episodes": int,
            "relations": int}``.

        Raises:
            ValueError: Empty / whitespace ``mind_id``.
            BrainError: Brain repositories not registered.
        """
        from sovyx.brain.concept_repo import ConceptRepository  # noqa: PLC0415
        from sovyx.brain.episode_repo import EpisodeRepository  # noqa: PLC0415
        from sovyx.engine.errors import BrainError  # noqa: PLC0415
        from sovyx.engine.types import MindId  # noqa: PLC0415

        if not mind_id.strip():
            msg = "mind_id must be a non-empty string"
            raise ValueError(msg)

        if not (
            registry.is_registered(ConceptRepository) and registry.is_registered(EpisodeRepository)
        ):
            msg = (
                "brain subsystem not available (repositories not "
                "registered) — the daemon may still be booting"
            )
            raise BrainError(msg)

        mid = MindId(mind_id)
        concept_repo = await registry.resolve(ConceptRepository)
        episode_repo = await registry.resolve(EpisodeRepository)
        return {
            "mind_id": mind_id,
            "concepts": await concept_repo.count(mid),
            "episodes": await episode_repo.count(mid),
            "relations": await _relation_count_best_effort(registry, mid),
        }

    async def _mind_status(mind_id: str = "default") -> dict[str, Any]:
        """Per-mind status snapshot (``sovyx mind status``).

        AP #53 closure — the consumer is ``cli/main.py::mind_status``,
        which renders each dict entry as a ``key: value`` line.

        Wiring: activity from :class:`MindManager.get_active_minds`
        (the same source ``mind.list`` serves), identity from the
        mind's ``mind.yaml`` via :func:`_load_mind_config_best_effort`
        (same resolver the retention handler uses), memory counts via
        :func:`_brain_row_counts` (best-effort, mirrors the dashboard's
        ``StatusCollector``). ``name``/``language`` are ``None`` when
        the mind is active but its YAML is missing or malformed —
        honest absence over a fabricated value (AP #48).

        Returns:
            ``{"mind_id": str, "active": bool, "name": str | None,
            "language": str | None, "concepts": int, "episodes": int}``.

        Raises:
            ValueError: Empty / whitespace ``mind_id``.
            MindNotFoundError: The mind is neither active nor onboarded
                (no ``<data_dir>/<mind_id>/mind.yaml``) — surfaces
                operator typos loudly instead of an all-zero snapshot.
        """
        from sovyx.engine.bootstrap import MindManager  # noqa: PLC0415
        from sovyx.engine.config import EngineConfig  # noqa: PLC0415
        from sovyx.engine.errors import MindNotFoundError  # noqa: PLC0415
        from sovyx.engine.types import MindId  # noqa: PLC0415

        if not mind_id.strip():
            msg = "mind_id must be a non-empty string"
            raise ValueError(msg)

        mid = MindId(mind_id)

        active = False
        if registry.is_registered(MindManager):
            mgr = await registry.resolve(MindManager)
            active = mind_id in mgr.get_active_minds()

        mind_config: MindConfig | None = None
        if registry.is_registered(EngineConfig):
            config = await registry.resolve(EngineConfig)
            mind_config = _load_mind_config_best_effort(config.data_dir, mid)

        if not active and mind_config is None:
            raise MindNotFoundError(mind_id=mind_id)

        concepts, episodes = await _brain_row_counts(registry, mid)
        return {
            "mind_id": mind_id,
            "active": active,
            "name": mind_config.name if mind_config is not None else None,
            "language": mind_config.language if mind_config is not None else None,
            "concepts": concepts,
            "episodes": episodes,
        }

    async def _config_get() -> dict[str, Any]:
        """Return the active mind's config in a shape friendly for tabular display.

        The full :class:`MindConfig` is large; we surface the fields
        the REPL needs (name, language, timezone, brain knobs, llm
        provider/model). The dashboard pulls the same struct via
        ``PersonalityEngine.config``; the CLI handler reuses that
        accessor so both surfaces stay in sync without a second
        registration of MindConfig.
        """
        from sovyx.bridge.manager import BridgeManager  # noqa: PLC0415
        from sovyx.engine.bootstrap import MindManager  # noqa: PLC0415
        from sovyx.mind.personality import PersonalityEngine  # noqa: PLC0415

        mind_id: str | None = None
        mgr = await registry.resolve(MindManager)
        active = mgr.get_active_minds()
        if active:
            mind_id = active[0]
        elif registry.is_registered(BridgeManager):
            bridge = await registry.resolve(BridgeManager)
            mind_id = str(bridge.mind_id)

        if not registry.is_registered(PersonalityEngine):
            return {"mind_id": mind_id, "available": False}

        personality = await registry.resolve(PersonalityEngine)
        cfg = personality.config
        return {
            "mind_id": mind_id or cfg.name,
            "available": True,
            "name": cfg.name,
            "language": cfg.language,
            "timezone": cfg.timezone,
            "template": cfg.template,
            "llm": {
                "default_provider": cfg.llm.default_provider,
                "default_model": cfg.llm.default_model,
                "fast_model": cfg.llm.fast_model,
                "temperature": cfg.llm.temperature,
                "budget_daily_usd": cfg.llm.budget_daily_usd,
                "budget_monthly_usd": cfg.llm.budget_monthly_usd,
            },
            "brain": {
                "consolidation_interval_hours": cfg.brain.consolidation_interval_hours,
                "dream_time": cfg.brain.dream_time,
                "dream_lookback_hours": cfg.brain.dream_lookback_hours,
                "dream_max_patterns": cfg.brain.dream_max_patterns,
                "max_concepts": cfg.brain.max_concepts,
                "forgetting_enabled": cfg.brain.forgetting_enabled,
                "decay_rate": cfg.brain.decay_rate,
            },
        }

    async def _mind_forget(
        mind_id: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Right-to-erasure: wipe every per-mind row across all pools.

        Phase 8 / T8.21 step 4 — daemon-side surface for the
        ``sovyx mind forget <mind_id>`` CLI. Resolves the live
        :class:`DatabaseManager` + :class:`EngineConfig` from the
        registry, builds a :class:`MindForgetService` against the
        target mind's pools + the shared system pool + the consent
        ledger, and runs the wipe.

        Args:
            mind_id: Target mind. Empty / whitespace is rejected by
                the underlying service.
            dry_run: When True, return the count report without
                writing. CLI's ``--dry-run`` confirmation flow.

        Returns:
            JSON-serialisable dict mirroring :class:`MindForgetReport`
            field-for-field. Every count is an int; ``dry_run`` is
            a bool.
        """
        from sovyx.engine.config import EngineConfig  # noqa: PLC0415
        from sovyx.engine.types import MindId  # noqa: PLC0415
        from sovyx.mind.forget import MindForgetService  # noqa: PLC0415
        from sovyx.persistence.manager import DatabaseManager  # noqa: PLC0415
        from sovyx.voice._consent_ledger import ConsentLedger  # noqa: PLC0415

        config = await registry.resolve(EngineConfig)
        db_manager = await registry.resolve(DatabaseManager)

        mid = MindId(mind_id)
        # The brain + conversations pools are per-mind so resolving
        # them validates that the mind actually exists; a missing
        # mind raises DatabaseConnectionError which the RPC layer
        # surfaces to the operator as a clear error.
        brain_pool = db_manager.get_brain_pool(mid)
        conv_pool = db_manager.get_conversation_pool(mid)
        system_pool = db_manager.get_system_pool()

        # ConsentLedger is path-resolved (no registry instance);
        # missing file is treated as an empty ledger by the service
        # so the path is always passed even when the file doesn't
        # exist yet.
        ledger_path = config.data_dir / "voice" / "consent.jsonl"
        ledger = ConsentLedger(path=ledger_path)

        service = MindForgetService(
            brain_pool=brain_pool,
            conversations_pool=conv_pool,
            system_pool=system_pool,
            ledger=ledger,
        )
        report = await service.forget_mind(mid, dry_run=dry_run)

        return {
            "mind_id": str(report.mind_id),
            "concepts_purged": report.concepts_purged,
            "relations_purged": report.relations_purged,
            "episodes_purged": report.episodes_purged,
            "concept_embeddings_purged": report.concept_embeddings_purged,
            "episode_embeddings_purged": report.episode_embeddings_purged,
            "conversation_imports_purged": report.conversation_imports_purged,
            "consolidation_log_purged": report.consolidation_log_purged,
            "conversations_purged": report.conversations_purged,
            "conversation_turns_purged": report.conversation_turns_purged,
            "daily_stats_purged": report.daily_stats_purged,
            "consent_ledger_purged": report.consent_ledger_purged,
            "total_brain_rows_purged": report.total_brain_rows_purged,
            "total_conversations_rows_purged": report.total_conversations_rows_purged,
            "total_system_rows_purged": report.total_system_rows_purged,
            "total_rows_purged": report.total_rows_purged,
            "dry_run": report.dry_run,
        }

    async def _mind_retention_prune(
        mind_id: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Time-based retention prune for a mind.

        Phase 8 / T8.21 step 6 — daemon-side surface for the
        ``sovyx mind retention prune <mind_id>`` CLI. Sibling to
        ``mind.forget``: wipes records older than per-surface
        horizons (configured via ``EngineConfig.tuning.retention.*``
        + ``MindConfig.retention.*`` overrides), writes a
        :data:`ConsentAction.RETENTION_PURGE` tombstone (distinct
        from the operator-invoked DELETE).

        Args:
            mind_id: Target mind.
            dry_run: When True, return counts without writing.

        Returns:
            JSON-serialisable dict mirroring
            :class:`MindRetentionReport` plus the four aggregate
            properties.
        """
        from sovyx.engine.config import EngineConfig  # noqa: PLC0415
        from sovyx.engine.types import MindId  # noqa: PLC0415
        from sovyx.mind.retention import MindRetentionService  # noqa: PLC0415
        from sovyx.persistence.manager import DatabaseManager  # noqa: PLC0415
        from sovyx.voice._consent_ledger import ConsentLedger  # noqa: PLC0415

        config = await registry.resolve(EngineConfig)
        db_manager = await registry.resolve(DatabaseManager)

        mid = MindId(mind_id)
        brain_pool = db_manager.get_brain_pool(mid)
        conv_pool = db_manager.get_conversation_pool(mid)
        system_pool = db_manager.get_system_pool()

        ledger_path = config.data_dir / "voice" / "consent.jsonl"
        ledger = ConsentLedger(path=ledger_path)

        # Per-mind retention overrides: best-effort load from
        # ``<data_dir>/<mind_id>/mind.yaml`` so
        # ``MindConfig.retention.<surface>_days`` is honoured. Falls
        # back to global defaults from ``EngineConfig.tuning.retention``
        # when the file is missing or malformed — the service is still
        # functional, just without per-mind overrides.
        mind_config = _load_mind_config_best_effort(config.data_dir, mid)

        service = MindRetentionService(
            engine_config=config,
            brain_pool=brain_pool,
            conversations_pool=conv_pool,
            system_pool=system_pool,
            ledger=ledger,
        )
        report = await service.prune_mind(
            mid,
            mind_config=mind_config,
            dry_run=dry_run,
        )

        return {
            "mind_id": str(report.mind_id),
            "cutoff_utc": report.cutoff_utc,
            "episodes_purged": report.episodes_purged,
            "conversations_purged": report.conversations_purged,
            "conversation_turns_purged": report.conversation_turns_purged,
            "daily_stats_purged": report.daily_stats_purged,
            "consolidation_log_purged": report.consolidation_log_purged,
            "consent_ledger_purged": report.consent_ledger_purged,
            "effective_horizons": dict(report.effective_horizons),
            "total_brain_rows_purged": report.total_brain_rows_purged,
            "total_conversations_rows_purged": report.total_conversations_rows_purged,
            "total_system_rows_purged": report.total_system_rows_purged,
            "total_rows_purged": report.total_rows_purged,
            "dry_run": report.dry_run,
        }

    async def _wake_word_register_mind(
        mind_id: str,
        model_path: str,
    ) -> dict[str, Any]:
        """Hot-reload a mind's wake-word ONNX model into the live router.

        Phase 8 / T8.15 wire-up — surfaces
        :meth:`sovyx.voice.pipeline._orchestrator.VoicePipeline.register_mind_wake_word`
        as a daemon RPC so the CLI's ``sovyx voice train-wake-word`` (or
        a future dashboard endpoint) can activate a freshly trained
        ``.onnx`` without restarting the daemon. The router-side
        primitive is idempotent — re-registering the same ``mind_id``
        replaces the prior detector, the prior ONNX session is
        GC'd by Python's ref-count.

        Validation order (defense-in-depth):
          1. ``mind_id`` non-empty after strip().
          2. ``model_path`` exists on disk.
          3. ``model_path`` ends in ``.onnx`` (string-match on suffix —
             the router itself does not enforce this, but mismatched
             extensions are a strong signal of operator error).
          4. ``VoicePipeline`` is registered (voice subsystem enabled).
          5. The pipeline must have a ``WakeWordRouter`` (multi-mind
             mode). Raises :class:`VoiceError` with a remediation hint
             when the router is missing.

        Args:
            mind_id: Target mind. Empty/whitespace rejected.
            model_path: Filesystem path to the trained ``.onnx``
                checkpoint.

        Returns:
            ``{"mind_id": ..., "model_path": ..., "hot_reload_succeeded":
            True}`` on success. Failures raise — the RPC framework
            surfaces them as JSON-RPC error responses.

        Raises:
            ValueError: Empty mind_id OR non-``.onnx`` model_path.
            FileNotFoundError: Model file does not exist.
            VoiceError: Voice pipeline not registered, OR pipeline
                lacks a multi-mind WakeWordRouter (single-mind mode).
        """
        from pathlib import Path  # noqa: PLC0415

        from sovyx.engine.errors import VoiceError  # noqa: PLC0415
        from sovyx.engine.types import MindId  # noqa: PLC0415
        from sovyx.voice.pipeline._orchestrator import (  # noqa: PLC0415
            VoicePipeline,
        )

        if not mind_id.strip():
            msg = "mind_id must be a non-empty string"
            raise ValueError(msg)

        path = Path(model_path)
        if not path.exists():
            msg = f"model_path does not exist: {path}"
            raise FileNotFoundError(msg)
        if path.suffix.lower() != ".onnx":
            msg = f"model_path must end in .onnx (got {path.suffix!r}): {path}"
            raise ValueError(msg)

        if not registry.is_registered(VoicePipeline):
            msg = (
                "voice subsystem not enabled (VoicePipeline not "
                "registered); enable voice in the dashboard first"
            )
            raise VoiceError(msg)

        pipeline = await registry.resolve(VoicePipeline)
        # Delegate to the public method on VoicePipeline; this raises
        # ``VoiceError`` if the multi-mind router isn't configured.
        pipeline.register_mind_wake_word(MindId(mind_id), model_path=path)

        logger.info(
            "voice.wake_word.rpc.register_mind_succeeded",
            **{
                "voice.mind_id": mind_id,
                "voice.model_path": str(path),
            },
        )
        return {
            "mind_id": mind_id,
            "model_path": str(path),
            "hot_reload_succeeded": True,
        }

    async def _wake_word_unregister_mind(mind_id: str) -> dict[str, Any]:
        """Drop a mind's wake-word detector from the live router.

        Mission ``MISSION-wake-word-runtime-wireup-2026-05-03.md`` §T2/T4
        — the symmetric inverse of ``wake_word.register_mind``. Surfaces
        :meth:`sovyx.voice.pipeline._orchestrator.VoicePipeline.unregister_mind_wake_word`
        as a daemon RPC so the dashboard's per-mind wake-word toggle
        endpoint (T3) can disable a mind without restarting the daemon.

        Idempotent: unregistering an unknown ``mind_id`` is a no-op.
        The return payload distinguishes "actually disabled" from
        "already disabled" via the ``unregistered`` boolean so the
        caller can surface the right status to operators.

        Args:
            mind_id: Target mind. Empty/whitespace rejected.

        Returns:
            ``{"mind_id": ..., "unregistered": bool}`` — ``True`` when
            a detector was removed, ``False`` when no detector existed
            (idempotent no-op).

        Raises:
            ValueError: Empty mind_id.
            VoiceError: Voice pipeline not registered, OR pipeline
                lacks a multi-mind WakeWordRouter (single-mind mode).
        """
        from sovyx.engine.errors import VoiceError  # noqa: PLC0415
        from sovyx.engine.types import MindId  # noqa: PLC0415
        from sovyx.voice.pipeline._orchestrator import (  # noqa: PLC0415
            VoicePipeline,
        )

        if not mind_id.strip():
            msg = "mind_id must be a non-empty string"
            raise ValueError(msg)

        if not registry.is_registered(VoicePipeline):
            msg = (
                "voice subsystem not enabled (VoicePipeline not "
                "registered); enable voice in the dashboard first"
            )
            raise VoiceError(msg)

        pipeline = await registry.resolve(VoicePipeline)
        unregistered = pipeline.unregister_mind_wake_word(MindId(mind_id))

        logger.info(
            "voice.wake_word.rpc.unregister_mind_succeeded",
            **{
                "voice.mind_id": mind_id,
                "voice.unregistered": unregistered,
            },
        )
        return {"mind_id": mind_id, "unregistered": unregistered}

    async def _engine_resources_snapshot() -> dict[str, Any]:
        """Mission H4 §0 item 14 + §T3.2 — live resource-cohort snapshot.

        Returns the same payload ``sovyx doctor resources`` renders in
        the CLI: in-process :class:`ResourceRegistry` fields + cohort-
        governor breaker state + last-N heap/thread snapshot manifest.
        Read-only; safe to call at any cadence.
        """
        from sovyx.observability._resource_cohort_governor import (  # noqa: PLC0415
            _diagnostics_dir,
            get_default_resource_cohort_governor,
        )
        from sovyx.observability._resource_registry import (  # noqa: PLC0415
            CohortAxis,
            get_default_resource_registry,
        )

        fields = get_default_resource_registry().snapshot_fields()
        governor = get_default_resource_cohort_governor()
        breaker_state: dict[str, bool] = {}
        for axis in CohortAxis:
            try:
                breaker_state[str(axis)] = governor.is_breaker_engaged(axis)
            except Exception:  # noqa: BLE001 — governor MUST never break the RPC
                breaker_state[str(axis)] = False

        heap_manifest: list[dict[str, Any]] = []
        thread_manifest: list[dict[str, Any]] = []
        try:
            diag_dir = _diagnostics_dir()
            if diag_dir.exists():
                for path in sorted(
                    diag_dir.glob("heap-snapshot-*.json"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )[:10]:
                    stat = path.stat()
                    heap_manifest.append(
                        {
                            "name": path.name,
                            "size_bytes": stat.st_size,
                            "mtime": stat.st_mtime,
                        },
                    )
                for path in sorted(
                    diag_dir.glob("thread-snapshot-*.txt"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )[:10]:
                    stat = path.stat()
                    thread_manifest.append(
                        {
                            "name": path.name,
                            "size_bytes": stat.st_size,
                            "mtime": stat.st_mtime,
                        },
                    )
        except OSError:
            pass

        return {
            "fields": dict(fields),
            "breaker_state": breaker_state,
            "heap_snapshot_manifest": heap_manifest,
            "thread_snapshot_manifest": thread_manifest,
        }

    async def _engine_resources_tracemalloc_snapshot() -> dict[str, Any]:
        """Mission H4 §T3.2 — operator-on-demand tracemalloc heap snapshot.

        Triggers the cohort-governor's ``request_heap_snapshot`` path
        with ``cohort="cli_on_demand"``. Honors the existing
        ``observability.features.tracemalloc`` opt-in: returns
        ``{"skipped": True, "reason": "tracemalloc_not_enabled"}`` when
        tracemalloc is OFF rather than raising — the CLI surfaces the
        skip with an actionable hint pointing at the feature flag.
        """
        from sovyx.observability._resource_cohort_governor import (  # noqa: PLC0415
            get_default_resource_cohort_governor,
        )

        governor = get_default_resource_cohort_governor()
        path = governor.request_heap_snapshot(
            cohort="cli_on_demand",
            extra_metadata={"source": "cli_doctor_resources"},
        )
        if path is None:
            return {
                "skipped": True,
                "reason": "tracemalloc_not_enabled_or_persist_failed",
                "hint": (
                    "Set SOVYX_OBSERVABILITY__FEATURES__TRACEMALLOC=true "
                    "and restart the daemon to enable allocator-level "
                    "heap snapshots. (Adds ~25-30% memory overhead.)"
                ),
            }
        return {
            "skipped": False,
            "path": str(path),
            "name": path.name,
        }

    async def _voice_health_snapshot() -> dict[str, Any]:
        """Daemon-side voice-health triple for ``sovyx doctor voice``.

        DOCTOR-3 closure — pre-fix the CLI rendered quarantine /
        failover-history / degraded-banner from its OWN process
        singletons (always empty in a non-daemon process), so a live
        daemon with a quarantined mic still printed "No endpoints in
        quarantine" (AP #70/#71 class: surface claims state it never
        observed). This handler is the producer half; the consumer is
        ``sovyx.cli.commands.doctor._fetch_voice_health_payload``,
        which prefers this RPC and falls back to the CLI process's own
        (disclosed) local state.

        Wiring: :func:`collect_voice_health_snapshot` — the SAME
        serializer the CLI fallback runs locally, so producer and
        consumer share one shape symbol (AP #40 / #53). Read-only;
        safe at any cadence.
        """
        return collect_voice_health_snapshot()

    async def _doctor() -> dict[str, Any]:
        """Run the daemon-side online health checks for ``sovyx doctor``.

        DOCTOR-1 closure — the CLI called this method since the doctor
        command shipped, but no daemon ever registered it (AP #53 /
        AP #70 class), so a healthy running daemon always rendered a
        RED 'Daemon RPC' row. This is the producer half of the
        contract; the consumer is
        ``sovyx.cli.commands.doctor._online_checks_from_rpc``, which
        parses ``{"checks": {name: {status, message, metadata}}}``
        into table rows.

        Wiring: reuses the bootstrap-registered
        :class:`~sovyx.observability.health.HealthRegistry` singleton
        (the online checks wired to the live engine — Database, Brain
        Index, LLM Providers, Channels, Consolidation, Cost Budget);
        falls back to
        :func:`~sovyx.observability.health.create_engine_health_registry`
        over the live :class:`ServiceRegistry` when the singleton is
        absent (harnesses that wire RPC without full bootstrap). A
        check whose dependency is unavailable reports its own
        YELLOW/RED row via the ``HealthRegistry._safe_run`` boundary —
        it never crashes the handler.

        Timeout discipline: each check is bounded by
        ``_DOCTOR_CHECK_TIMEOUT_S`` (a slow check becomes a RED
        "Check timed out" row while siblings still report — partial
        results by construction); the whole sweep is additionally
        bounded by ``_DOCTOR_TOTAL_TIMEOUT_S``, on which the handler
        returns a synthetic RED row + ``note="timed_out"`` instead of
        hanging the CLI.
        """
        from sovyx.observability.health import (  # noqa: PLC0415
            HealthRegistry,
            create_engine_health_registry,
        )

        if registry.is_registered(HealthRegistry):
            health = await registry.resolve(HealthRegistry)
        else:
            health = await create_engine_health_registry(registry)

        try:
            results = await asyncio.wait_for(
                health.run_all(timeout=_DOCTOR_CHECK_TIMEOUT_S),
                timeout=_DOCTOR_TOTAL_TIMEOUT_S,
            )
        except TimeoutError:
            logger.warning(
                "doctor_rpc_sweep_timed_out",
                timeout_s=_DOCTOR_TOTAL_TIMEOUT_S,
            )
            return {
                "overall": "red",
                "check_count": 0,
                "note": "timed_out",
                "checks": {
                    "Online Checks": {
                        "status": "red",
                        "message": (
                            f"online health checks timed out after {_DOCTOR_TOTAL_TIMEOUT_S:g}s"
                        ),
                    },
                },
            }

        return {
            "overall": health.summary(results).value,
            "check_count": len(results),
            "checks": {
                r.name: {
                    "status": r.status.value,
                    "message": r.message,
                    "metadata": r.metadata,
                }
                for r in results
            },
        }

    rpc.register_method("chat", _chat)
    rpc.register_method("brain.search", _brain_search)
    rpc.register_method("brain.stats", _brain_stats)
    rpc.register_method("mind.list", _mind_list)
    rpc.register_method("mind.status", _mind_status)
    rpc.register_method("mind.forget", _mind_forget)
    rpc.register_method("mind.retention.prune", _mind_retention_prune)
    rpc.register_method("config.get", _config_get)
    rpc.register_method("wake_word.register_mind", _wake_word_register_mind)
    rpc.register_method("wake_word.unregister_mind", _wake_word_unregister_mind)
    rpc.register_method("engine.resources.snapshot", _engine_resources_snapshot)
    rpc.register_method(
        "engine.resources.tracemalloc_snapshot",
        _engine_resources_tracemalloc_snapshot,
    )
    rpc.register_method("voice.health.snapshot", _voice_health_snapshot)
    rpc.register_method("doctor", _doctor)
    logger.debug("cli_rpc_handlers_registered", count=14)
