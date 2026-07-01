"""Sovyx Bootstrap — wire all services in dependency order."""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING

from sovyx.engine.config import EngineConfig
from sovyx.engine.registry import ServiceRegistry
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from sovyx.mind.config import MindConfig

logger = get_logger(__name__)


async def _auto_resume_voice_pipeline(
    *,
    mind_config: MindConfig,
    engine_config: EngineConfig,
    registry: ServiceRegistry,
) -> None:
    """Auto-resume voice pipeline at daemon boot when ``voice_enabled=True``.

    v0.31.4 GAP 4 closure. Operator-perspective contract:
    ``sovyx start`` must not require a separate ``POST /api/voice/enable``
    call before voice works. If voice was enabled in the prior session
    (``MindConfig.voice_enabled=True``), the pipeline reconstructs
    automatically using the same ``create_voice_pipeline`` factory the
    HTTP endpoint uses, then registers the bundle in the service
    registry via ``replace_instance`` (GAP 3 contract — old instances
    from a partial-shutdown scenario get torn down before re-register).

    v0.31.6 paranoid-closure T1.2 (C2): start() runs BEFORE the registry
    mutation. If start() raises (USB unplugged between probe and stream
    open, OOM during model load, sounddevice race), the registry is
    NEVER touched, so a failed auto-resume cannot leak a zombie
    pipeline that subsequent ``is_registered()`` checks would consider
    healthy. On start() failure we best-effort tear down the bundle
    (release any partially-acquired ONNX session + sounddevice handles)
    before re-raising.

    v0.31.7 paranoid-closure T2.1 (M1): mirror the FULL sub-component
    registration set the HTTP enable path uses. Pre-v0.31.7 auto-resume
    only registered ``VoicePipeline`` + ``AudioCaptureTask``, leaving
    ``SileroVAD`` / ``STTEngine`` / ``TTSEngine`` / ``WakeWordDetector``
    / ``VoiceCognitiveBridge`` / ``BootPreflightWarningsStore``
    unregistered. Symptom: ``/api/voice/status`` reported
    ``pipeline.running=true`` but "No engine configured" for STT/TTS/VAD,
    and any caller resolving a sub-component via the registry crashed.
    The fix mirrors the route handler's exact registration set + order.

    The cognitive bridge + ``BootPreflightWarningsStore`` are
    reconstructed here using the same closure pattern the HTTP route
    uses (``bridge_ref[0]`` holder filled after the bundle exists, so
    the on_perception callback the factory wires can find the bridge).
    Per the T1.2 contract, registry mutation runs only AFTER
    ``start()`` succeeds and is NOT rolled back if a later
    ``replace_instance`` fails — already-published instances stay.

    DEFENSIVE: any exception is allowed to propagate; the caller wraps
    in try/except so daemon startup is never blocked by voice failure.
    """
    # Local imports to avoid circular: engine.bootstrap is imported very
    # early; voice.factory imports onnxruntime + sounddevice which are
    # heavy + optional. Deferring keeps cold-start fast for non-voice
    # daemons.
    from sovyx.cognitive.loop import CognitiveLoop
    from sovyx.engine.events import EventBus
    from sovyx.voice._capture_task import AudioCaptureTask
    from sovyx.voice.cognitive_bridge import VoiceCognitiveBridge
    from sovyx.voice.factory import create_voice_pipeline
    from sovyx.voice.health import BootPreflightWarningsStore
    from sovyx.voice.pipeline._orchestrator import VoicePipeline
    from sovyx.voice.stt import STTEngine
    from sovyx.voice.tts_piper import TTSEngine
    from sovyx.voice.vad import SileroVAD
    from sovyx.voice.wake_word import WakeWordDetector

    # Resolve event_bus + cognitive_loop optimistically — both are
    # registered earlier in bootstrap (well before this helper runs),
    # but ``is_registered`` is the contract-compatible probe.
    event_bus = await registry.resolve(EventBus) if registry.is_registered(EventBus) else None
    cognitive_loop = (
        await registry.resolve(CognitiveLoop) if registry.is_registered(CognitiveLoop) else None
    )

    # Bridge holder — same closure pattern as ``_enable_voice_locked``.
    # Filled AFTER bundle creation so the perception callback the
    # factory wires can reach the bridge (chicken-and-egg avoidance:
    # the bridge needs the pipeline, the pipeline needs the callback).
    bridge_ref: list[VoiceCognitiveBridge | None] = [None]

    async def _on_perception(text: str, mind_id_str: str) -> None:
        """Feed a transcription into the cogloop via the bridge.

        Simpler than the HTTP route's variant — auto-resume runs once
        at boot; there's no per-request task lifecycle to track. The
        bridge's own internal cancel-hook bookkeeping handles barge-in
        cancellation.
        """
        if not text.strip():
            return
        bridge = bridge_ref[0]
        if bridge is None:
            # Boot-window race: STT produced a transcript before the
            # cognitive bridge was wired into ``bridge_ref``. The bridge is
            # now constructed BEFORE ``capture_task.start()`` so this window
            # is structurally closed; the WARN is belt-and-suspenders in
            # case a future reorder reopens it. Text length only — never the
            # transcript itself (privacy).
            logger.warning(
                "voice.perception_dropped_bridge_not_ready",
                mind_id=mind_id_str,
                text_length=len(text),
                action_required=(
                    "A voice utterance arrived during the boot window before "
                    "the cognitive bridge was wired and was DISCARDED — the "
                    "user got no response. One-off at boot: ask the user to "
                    "repeat. Recurring outside boot: bridge wire-up bug in "
                    "_auto_resume_voice_pipeline."
                ),
            )
            return
        from uuid import uuid4

        from sovyx.cognitive.gate import CognitiveRequest
        from sovyx.cognitive.perceive import Perception
        from sovyx.engine.types import ConversationId, MindId, PerceptionType

        cog_request = CognitiveRequest(
            perception=Perception(
                id=str(uuid4()),
                type=PerceptionType.USER_MESSAGE,
                source="voice",
                content=text,
            ),
            mind_id=MindId(mind_id_str),
            conversation_id=ConversationId(f"voice-{mind_id_str}"),
            conversation_history=[],
            person_name=None,
        )
        try:
            await bridge.process(cog_request)
        except Exception:  # noqa: BLE001
            logger.exception(
                "voice_auto_resume_bridge_failed",
                mind_id=mind_id_str,
            )

    on_perception_cb = _on_perception if cognitive_loop is not None else None

    # W2.1 / G-P1-4 — opt-in STT failover: when the operator enabled it AND an
    # OPENAI_API_KEY is present, build a CloudSTT secondary so the daemon
    # RECOVERS sustained local-STT failure (raise / S2 timeout) via the cloud
    # instead of producing permanent silence. Best-effort: a construction
    # problem must never block startup — it just leaves failover unwired and
    # the local primary still works. The key is read from the same
    # OPENAI_API_KEY env the LLM provider registry uses (no new config path).
    secondary_stt = None
    if engine_config.tuning.voice.stt_failover_enabled:
        _openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if _openai_key:
            try:
                from sovyx.voice.stt_cloud import CloudSTT, CloudSTTConfig

                secondary_stt = CloudSTT(
                    CloudSTTConfig(
                        api_key=_openai_key,
                        language=mind_config.language,
                        api_timeout=engine_config.tuning.voice.cloud_stt_timeout_seconds,
                    )
                )
                logger.info("voice_stt_failover_secondary_built", engine="cloud_whisper")
            except Exception:  # noqa: BLE001 — never block daemon startup on a secondary
                logger.warning("voice_stt_failover_secondary_build_failed", exc_info=True)
        else:
            logger.info("voice_stt_failover_enabled_no_key", hint="set OPENAI_API_KEY")

    bundle = await create_voice_pipeline(
        data_dir=engine_config.data_dir,
        mind_id=str(mind_config.id),
        language=mind_config.language,
        voice_id=mind_config.voice_id,
        wake_word_enabled=getattr(mind_config, "wake_word_enabled", False),
        input_device_name=mind_config.voice_input_device_name or None,
        input_device_host_api=mind_config.voice_input_device_host_api or None,
        tts_engine_preference=getattr(mind_config, "voice_tts_engine", "auto"),
        event_bus=event_bus,
        on_perception=on_perception_cb,
        secondary_stt=secondary_stt,
        # ``allow_inoperative_capture=True``: even if the mic isn't
        # currently available (USB unplugged, audio service down),
        # daemon still comes up + voice surfaces an error rather than
        # blocking startup. Operator can fix mic + click Recalibrate
        # without daemon restart.
        allow_inoperative_capture=True,
    )

    # Wire the cognitive bridge BEFORE ``capture_task.start()`` so a
    # transcript produced immediately after capture starts always finds
    # the bridge — pre-reorder, an utterance in the start→wire window was
    # silently dropped (``bridge_ref[0]`` still None). Safe to hoist: the
    # bridge constructor is passive (stores cogloop + pipeline refs, no
    # tasks, no I/O) and both dependencies exist once the bundle is built.
    # The T1.2 contract is untouched — REGISTRY mutation still happens
    # only after start() succeeds; on start() failure the bridge instance
    # is discarded with the torn-down bundle. Streaming follows the
    # mind's LLM config (matches HTTP route contract).
    if cognitive_loop is not None:
        # Defensive ``getattr`` — tests build a minimal MindConfig
        # without an ``llm`` field, and v0.1 single-mind doesn't
        # mandate one for voice auto-resume to work.
        llm_cfg = getattr(mind_config, "llm", None)
        streaming = bool(getattr(llm_cfg, "streaming", True)) if llm_cfg is not None else True
        bridge_ref[0] = VoiceCognitiveBridge(
            cognitive_loop,
            bundle.pipeline,
            streaming=streaming,
        )

    try:
        await bundle.capture_task.start()
    except Exception:
        # Best-effort cleanup: stop capture_task FIRST (releases the
        # sounddevice handle + audio thread), then pipeline (releases
        # ONNX session + downstream queues). Order matters because the
        # pipeline holds a reference to the capture task's frame queue.
        # Per CLAUDE.md anti-pattern #27, suppress + debug-log rather
        # than raw try/except: pass — the original failure is the one
        # we care about; cleanup errors are observability-only.
        with contextlib.suppress(Exception):
            await bundle.capture_task.stop()
        with contextlib.suppress(Exception):
            await bundle.pipeline.stop()
        logger.debug(
            "voice_auto_resume_cleanup_skipped",
            reason="start_failed_bundle_torn_down_best_effort",
            mind_id=mind_config.id,
        )
        raise

    # start() succeeded — only NOW publish the running instances into
    # the registry, so downstream callers never observe a half-started
    # bundle. Order mirrors ``_enable_voice_locked`` exactly so any
    # ordering invariant the route handler depends on is preserved.
    #
    # T1.2 contract: if any ``replace_instance`` after the first one
    # fails, the previously-registered instances stay registered (no
    # rollback). That's acceptable because start() already succeeded
    # — the audio thread is alive + producing frames, and partial
    # registration is strictly better than dropping a working
    # pipeline back to a clean slate.
    await registry.replace_instance(VoicePipeline, bundle.pipeline)
    await registry.replace_instance(AudioCaptureTask, bundle.capture_task)
    await registry.replace_instance(SileroVAD, bundle.pipeline.vad)
    # ``STTEngine`` / ``TTSEngine`` are ABCs by design — every concrete
    # backend (Moonshine, Piper, Kokoro, Wyoming) inherits the abstract
    # base. Mypy strict's ``type-abstract`` rule rejects passing an
    # abstract class as ``type[T]``, but the registry semantics expressly
    # use the abstract type as the LOOKUP KEY (so callers resolve the
    # interface, not the implementation). The HTTP route at
    # ``dashboard/routes/voice.py::_enable_voice_locked`` lives behind a
    # ``Any``-typed registry and silently dodges the same check —
    # bootstrap is strict-typed, hence the explicit ignore.
    await registry.replace_instance(STTEngine, bundle.pipeline.stt)  # type: ignore[type-abstract]
    await registry.replace_instance(TTSEngine, bundle.pipeline.tts)  # type: ignore[type-abstract]
    if bundle.pipeline.config.wake_word_enabled:
        await registry.replace_instance(WakeWordDetector, bundle.pipeline.wake_word)
    if bridge_ref[0] is not None:
        await registry.replace_instance(VoiceCognitiveBridge, bridge_ref[0])

    # Boot-preflight warnings store — same publish-or-refresh pattern
    # the HTTP route uses, so dashboard surfaces see the auto-resume
    # warnings just like a manual /enable.
    if registry.is_registered(BootPreflightWarningsStore):
        preflight_store = await registry.resolve(BootPreflightWarningsStore)
    else:
        preflight_store = BootPreflightWarningsStore()
        registry.register_instance(BootPreflightWarningsStore, preflight_store)
    preflight_store.set_warnings(list(bundle.boot_preflight_warnings))

    logger.info(
        "voice_auto_resume_succeeded",
        mind_id=mind_config.id,
    )


class MindManager:
    """Manage Minds within the engine. v0.1: single mind.

    Interface prepared for multi-mind in v1.0.
    """

    def __init__(self) -> None:
        self._minds: dict[str, object] = {}
        self._active: list[str] = []

    async def load_mind(self, mind_id: str, services: dict[str, object]) -> None:
        """Register a mind's services."""
        self._minds[mind_id] = services

    async def start_mind(self, mind_id: str) -> None:
        """Mark mind as active."""
        if mind_id not in self._active:
            self._active.append(mind_id)
        logger.info("mind_started", mind_id=mind_id)

    async def stop_mind(self, mind_id: str) -> None:
        """Mark mind as inactive."""
        if mind_id in self._active:
            self._active.remove(mind_id)
        logger.info("mind_stopped", mind_id=mind_id)

    def get_active_minds(self) -> list[str]:
        """Return list of active mind IDs."""
        return list(self._active)


async def bootstrap(
    engine_config: EngineConfig,
    mind_configs: Sequence[MindConfig],
) -> ServiceRegistry:
    """Initialize all services in dependency order.

    On partial failure, already-initialized resources are cleaned up
    in reverse order (see ``sovyx-imm-d6-bootstrap`` §1).

    Order (SPE-001 §init_order):
    1. EventBus
    2. DatabaseManager (pools + migrations)
    3. Per-mind: Brain → Personality → Context → LLM → Cognitive
    4. PersonResolver + ConversationTracker
    5. BridgeManager + Channels

    Returns:
        ServiceRegistry with all services wired.
    """
    from sovyx.brain.concept_repo import ConceptRepository
    from sovyx.brain.consolidation import ConsolidationCycle, ConsolidationScheduler
    from sovyx.brain.dream import DreamCycle, DreamScheduler
    from sovyx.brain.embedding import EmbeddingEngine
    from sovyx.brain.episode_repo import EpisodeRepository
    from sovyx.brain.learning import EbbinghausDecay, HebbianLearning
    from sovyx.brain.relation_repo import RelationRepository
    from sovyx.brain.retrieval import HybridRetrieval
    from sovyx.brain.service import BrainService
    from sovyx.brain.spreading import SpreadingActivation
    from sovyx.brain.working_memory import WorkingMemory
    from sovyx.bridge.identity import PersonResolver
    from sovyx.bridge.manager import BridgeManager
    from sovyx.bridge.sessions import ConversationTracker
    from sovyx.cognitive.act import ActPhase, ToolExecutor
    from sovyx.cognitive.attend import AttendPhase
    from sovyx.cognitive.gate import CogLoopGate
    from sovyx.cognitive.loop import CognitiveLoop
    from sovyx.cognitive.perceive import PerceivePhase
    from sovyx.cognitive.reflect import ReflectPhase
    from sovyx.cognitive.state import CognitiveStateMachine
    from sovyx.cognitive.think import ThinkPhase
    from sovyx.context.assembler import ContextAssembler
    from sovyx.context.budget import TokenBudgetManager
    from sovyx.context.formatter import ContextFormatter
    from sovyx.context.tokenizer import TokenCounter
    from sovyx.engine.events import EventBus
    from sovyx.engine.types import MindId
    from sovyx.llm.cost import CostGuard
    from sovyx.llm.providers.anthropic import AnthropicProvider
    from sovyx.llm.providers.deepseek import DeepSeekProvider
    from sovyx.llm.providers.fireworks import FireworksProvider
    from sovyx.llm.providers.groq import GroqProvider
    from sovyx.llm.providers.mistral import MistralProvider
    from sovyx.llm.providers.ollama import OllamaProvider
    from sovyx.llm.providers.openai import OpenAIProvider
    from sovyx.llm.providers.together import TogetherProvider
    from sovyx.llm.providers.xai import XAIProvider
    from sovyx.llm.router import LLMRouter
    from sovyx.mind.personality import PersonalityEngine
    from sovyx.persistence.manager import DatabaseManager

    registry = ServiceRegistry()
    _closables: list[object] = []  # cleanup on failure (reverse order)

    try:
        # 0. Load channel.env + secrets.env (tokens/keys saved via dashboard)
        for _env_file_name in ("channel.env", "secrets.env"):
            _env_path = engine_config.data_dir / _env_file_name
            if _env_path.exists():
                for _line in _env_path.read_text(encoding="utf-8").splitlines():
                    _line = _line.strip()  # noqa: PLW2901
                    if _line and not _line.startswith("#") and "=" in _line:
                        _k, _, _v = _line.partition("=")
                        _k, _v = _k.strip(), _v.strip()
                        if _k and _v and _k not in os.environ:
                            os.environ[_k] = _v

        # 0. EngineConfig + logging setup
        registry.register_instance(EngineConfig, engine_config)

        # Setup structured logging with envelope/PII/sampling/async/ringbuffer
        # pipeline (Phase 1 of IMPL-OBSERVABILITY-001). EngineConfig already
        # resolved observability.crash_dump_path against data_dir in its
        # model validator; data_dir is forwarded so persisted runtime
        # log-level overrides survive restarts.
        from sovyx.observability.logging import setup_logging

        setup_logging(
            engine_config.log,
            engine_config.observability,
            data_dir=engine_config.data_dir,
            engine_session_id_in_logs=engine_config.ox1.session_id_in_logs,
        )

        # 0.4. Metrics pipeline (Phase 11 Task 11.6).
        # ``setup_metrics`` builds the OTel MeterProvider on top of an
        # ``InMemoryMetricReader`` we own here so it can be re-exported
        # in three places that all converge on the same metric stream:
        #   * the dashboard ``/metrics`` route (always on, served on
        #     the dashboard port) — wires ``app.state.metrics_reader``
        #     from ``DashboardServer.start()``.
        #   * a dedicated stdlib ``wsgiref`` daemon-thread HTTP server
        #     started below when ``features.metrics_exporter`` is on,
        #     listening on ``observability.metrics_port`` (default 9101)
        #     for Prometheus scrapers that should not transit the
        #     authenticated dashboard surface.
        #   * the ``/api/metrics`` JSON endpoint (``collect_json``).
        # Registering the reader as a ``ServiceRegistry`` singleton lets
        # the dashboard resolve the same instance regardless of bootstrap
        # ordering — no global lookup, no risk of shadowed providers.
        from opentelemetry.sdk.metrics.export import (
            InMemoryMetricReader as _InMemoryMetricReader,
        )

        from sovyx.observability.metrics import (
            MetricsRegistry,
            setup_metrics,
        )

        metrics_reader = _InMemoryMetricReader()
        metrics_registry = setup_metrics(
            readers=[metrics_reader],
            max_series=engine_config.observability.metrics_max_series,
        )
        registry.register_instance(MetricsRegistry, metrics_registry)
        registry.register_instance(_InMemoryMetricReader, metrics_reader)

        if engine_config.observability.features.metrics_exporter:
            from sovyx.observability.prometheus import PrometheusHttpServer

            metrics_http_server = PrometheusHttpServer(
                metrics_reader,
                port=engine_config.observability.metrics_port,
            )
            metrics_http_server.start()
            _closables.append(metrics_http_server)
            registry.register_instance(PrometheusHttpServer, metrics_http_server)
            logger.info(
                "prometheus_exporter_started",
                port=engine_config.observability.metrics_port,
            )

        # 0.45. OpenTelemetry OTLP exporter (Phase 11 Task 11.8).
        # Default OFF — operators opt in via
        # ``SOVYX_OBSERVABILITY__OTEL__ENABLED=true`` so nodes that don't
        # ship to a collector pay zero startup cost (the optional
        # ``opentelemetry-exporter-otlp`` package isn't even imported).
        # When enabled, OtelExporter installs a real ``TracerProvider``
        # with OTLP/gRPC export and standard resource attributes; the
        # closable wrapper lets bootstrap rollback flush in-flight spans
        # via ``await otel.stop()``.
        if engine_config.observability.otel.enabled:
            from sovyx.observability.otel import OtelExporter

            otel_exporter = OtelExporter(engine_config.observability.otel)
            otel_exporter.start()
            _closables.append(otel_exporter)
            registry.register_instance(OtelExporter, otel_exporter)

        # 0.5. ResourceSnapshotter + HotPathSnapshotter (Phase 6 Task 6.8).
        # Both share the ``async_queue`` feature flag — the same flag that
        # enables the non-blocking log handler, since both produce periodic
        # INFO records that must not stall the event loop. Spawned via
        # ``spawn()`` so the tasks appear in the TaskRegistry; their
        # ``except asyncio.CancelledError`` branch emits a final snapshot
        # before the loop tears down, so no entry in ``_closables`` is
        # needed — process-level cancellation already drains them cleanly.
        if engine_config.observability.features.async_queue:
            # Mission B B-P0-2 (B.1.P2 closure 2026-05-21) — prime the
            # ResourceRegistry singleton with the operator-tunable
            # ``exception_cohort_observations_maxlen`` BEFORE the
            # snapshotter starts running. The registry is otherwise a
            # lazy singleton — the snapshotter would trigger
            # construction with the default 128 maxlen, ignoring any
            # env override. This block mirrors the pattern used for the
            # ResourceCohortGovernor below.
            from sovyx.observability import _resource_registry as _registry_mod
            from sovyx.observability._resource_registry import (
                ResourceRegistry,
                reset_default_resource_registry,
            )
            from sovyx.observability.counters import HotPathSnapshotter
            from sovyx.observability.resources import ResourceSnapshotter
            from sovyx.observability.tasks import spawn

            reset_default_resource_registry()
            with _registry_mod._SINGLETON_LOCK:  # noqa: SLF001
                _registry_mod._SINGLETON = ResourceRegistry(  # noqa: SLF001
                    exception_cohort_observations_maxlen=(
                        engine_config.observability.tuning.exception_cohort_observations_maxlen
                    ),
                )

            resource_snapshotter = ResourceSnapshotter(engine_config.observability)
            spawn(resource_snapshotter.run(), name="resource-snapshotter")
            registry.register_instance(ResourceSnapshotter, resource_snapshotter)

            hotpath_snapshotter = HotPathSnapshotter(engine_config.observability)
            spawn(hotpath_snapshotter.run(), name="hotpath-snapshotter")
            registry.register_instance(HotPathSnapshotter, hotpath_snapshotter)

            # ── Mission H4 §T4.5 — ResourceCohortGovernor wire-up ──
            # Gated by ``observability.features.cohort_governor`` (default
            # True per anti-pattern #34 inverse — observability is on by
            # default; the kill-switch exists for clean rollback). The
            # snapshotter calls the governor via its lazy-singleton — this
            # block primes the singleton with the operator-tunable budgets
            # so env-var overrides take effect from tick #1, rather than
            # falling back to the v0.49.17 hardcoded defaults.
            #
            # Mission H4 §ADR-D15 — opt-in tracemalloc. Default OFF (25-30%
            # memory overhead). When True, ``tracemalloc.start()`` runs here
            # so the snapshot's ``tracemalloc.current_kb`` /
            # ``tracemalloc.peak_kb`` fields become meaningful + the Phase
            # 1.E heap-snapshot trigger has real allocator data.
            if engine_config.observability.features.cohort_governor:
                from sovyx.observability._resource_cohort_governor import (
                    ResourceCohortGovernor,
                    reset_default_resource_cohort_governor,
                )

                reset_default_resource_cohort_governor()
                from sovyx.observability._resource_cohort_governor import (
                    _SINGLETON_LOCK,
                )

                # Mission B B-P0-3 — propagate the auto-clear feature
                # flag through from_tuning so the governor's HEALTHY-tick
                # state machine is gated by the operator override.
                # Mission OX-1.B — also propagate the causal-chain flag
                # so the additive ``axis.cleared`` emission at the
                # HEALTHY-edge clear path is gated by the operator
                # override (default False per `feedback_staged_adoption`).
                governor = ResourceCohortGovernor.from_tuning(
                    engine_config.observability.tuning,
                    enabled=True,
                    auto_clear_enabled=(
                        engine_config.observability.features.cohort_axis_auto_clear
                    ),
                    causal_chain_enabled=engine_config.ox1.causal_chain_enabled,
                )
                import sovyx.observability._resource_cohort_governor as _governor_mod

                with _SINGLETON_LOCK:
                    _governor_mod._SINGLETON = governor
                registry.register_instance(ResourceCohortGovernor, governor)

            if engine_config.observability.features.tracemalloc:
                import tracemalloc

                if not tracemalloc.is_tracing():
                    tracemalloc.start(
                        engine_config.observability.tuning.tracemalloc_nframes,
                    )

        # 0.55. Synthetic canary heartbeat (§27.3 — Phase 11+ Task 11+.10).
        # Emits ``meta.canary.tick`` every ``canary_interval_seconds``
        # (default 60 s). An external operator script — outside this repo —
        # checks that ticks reach the log file (and the SIEM if a forwarder
        # is wired) within the expected window. A missing tick is the
        # trivial signal "the logging pipeline stopped" that distinguishes
        # a quiet daemon from a dead one. Runs unconditionally — the cost
        # is one INFO line per minute, dwarfed by every other event in
        # the system.
        from sovyx.observability.canary import CanaryEmitter
        from sovyx.observability.tasks import spawn as _spawn_canary

        canary_emitter = CanaryEmitter(engine_config.observability)
        _spawn_canary(canary_emitter.run(), name="canary-emitter")
        registry.register_instance(CanaryEmitter, canary_emitter)

        # 0.56. Boot-time chain verification (§27.4 — "audit-of-auditor").
        # When tamper_chain is enabled, both sovyx.log and audit.jsonl
        # are written by HashChainHandler. Verify each at startup and
        # emit ``audit.chain.verified``: an unobserved broken chain is
        # the same as no chain at all. The check is best-effort — a
        # missing or empty file degrades to a no-op rather than blocking
        # the boot; the operator notices via the dashboard meta-health
        # endpoint (§27.2).
        if engine_config.observability.features.tamper_chain:
            from sovyx.observability.audit import emit_chain_verified
            from sovyx.observability.tamper import verify_chain

            audit_chain_path = engine_config.data_dir / "audit" / "audit.jsonl"
            for _chain_path in (engine_config.log.log_file, audit_chain_path):
                if _chain_path is None or not _chain_path.is_file():
                    continue
                try:
                    with _chain_path.open("r", encoding="utf-8") as _fh:
                        _entries = sum(1 for _line in _fh if _line.strip())
                    _verified, _idx = verify_chain(_chain_path)
                    emit_chain_verified(
                        _chain_path,
                        intact=_verified,
                        entries=_entries,
                        broken_at=None if _verified else _idx,
                        source="boot",
                    )
                except (OSError, ValueError):
                    emit_chain_verified(
                        _chain_path,
                        intact=False,
                        entries=0,
                        broken_at=None,
                        source="boot",
                    )

        # 0.57. Secret-rotation hygiene check (§22.4 — Phase 11+ Task 11+.11).
        # Emits ``security.secrets.rotation_overdue`` (WARNING) when the
        # operator-stamped ``security.secrets_rotated_at`` is older than
        # ``rotation_warn_days`` (default 90). Fresh installs land on
        # ``rotation_unknown`` (INFO) and stay quiet. The check runs
        # before EventBus so the warning surfaces in the very first batch
        # of boot logs — operators tail those during deploys and a buried
        # rotation reminder gets ignored.
        from sovyx.observability.secret_rotation import check_secret_rotation

        check_secret_rotation(engine_config.security)

        # 1. EventBus
        event_bus = EventBus(
            saga_propagation_enabled=engine_config.observability.features.saga_propagation,
        )
        registry.register_instance(EventBus, event_bus)

        # 1.5. Startup self-diagnosis cascade (Phase 4 of
        # IMPL-OBSERVABILITY-001). Runs *before* heavy subsystems so
        # the platform/hardware/audio fingerprint is captured even if
        # later steps fail. Gated by ``observability.features.startup_cascade``
        # so a regression can be rolled back without disabling the
        # observability stack as a whole.
        if engine_config.observability.features.startup_cascade:
            from sovyx.observability.self_diagnosis import run_startup_cascade

            await run_startup_cascade(engine_config, registry, event_bus)

        # 2. DatabaseManager
        db_manager = DatabaseManager(engine_config, event_bus)
        await db_manager.start()
        _closables.append(db_manager)
        registry.register_instance(DatabaseManager, db_manager)

        # 3. MindManager
        mind_manager = MindManager()
        registry.register_instance(MindManager, mind_manager)

        # Validate minds
        if not mind_configs:
            msg = "No minds configured — at least one MindConfig required"
            raise ValueError(msg)

        # Track last gate for BridgeManager wiring
        gate: CogLoopGate | None = None

        # Process each mind (v0.1: single mind)
        for mind_config in mind_configs:
            mind_id = MindId(mind_config.id)

            # Configure daily counter timezone + persistence from mind config
            from sovyx.dashboard.status import configure_timezone, get_counters

            configure_timezone(
                mind_config.timezone,
                system_pool=db_manager.get_system_pool(),
            )
            await get_counters().restore()

            # Initialize per-mind databases
            await db_manager.initialize_mind_databases(mind_id)
            brain_pool = db_manager.get_brain_pool(mind_id)

            # Brain components — preload embedding model on startup
            embedding = EmbeddingEngine()
            await embedding.ensure_loaded()
            concept_repo = ConceptRepository(brain_pool, embedding)
            episode_repo = EpisodeRepository(brain_pool, embedding)
            relation_repo = RelationRepository(brain_pool)
            working_memory = WorkingMemory()
            spreading = SpreadingActivation(
                relation_repo=relation_repo,
                working_memory=working_memory,
            )
            hebbian = HebbianLearning(
                relation_repo=relation_repo,
                concept_repo=concept_repo,
            )
            ebbinghaus = EbbinghausDecay(
                concept_repo=concept_repo,
                relation_repo=relation_repo,
            )
            retrieval = HybridRetrieval(
                concept_repo=concept_repo,
                episode_repo=episode_repo,
                embedding_engine=embedding,
            )

            brain_service = BrainService(
                concept_repo=concept_repo,
                episode_repo=episode_repo,
                relation_repo=relation_repo,
                embedding_engine=embedding,
                spreading=spreading,
                hebbian=hebbian,
                decay=ebbinghaus,
                retrieval=retrieval,
                working_memory=working_memory,
                event_bus=event_bus,
                emotional_baseline=mind_config.brain.emotional_baseline,
            )
            registry.register_instance(BrainService, brain_service)
            registry.register_instance(ConceptRepository, concept_repo)
            registry.register_instance(RelationRepository, relation_repo)
            registry.register_instance(EpisodeRepository, episode_repo)

            # Consolidation scheduler
            consolidation_cycle = ConsolidationCycle(
                brain_service=brain_service,
                decay=ebbinghaus,
                event_bus=event_bus,
                concept_repo=concept_repo,
                relation_repo=relation_repo,
            )
            consolidation_scheduler = ConsolidationScheduler(
                cycle=consolidation_cycle,
                interval_hours=mind_config.brain.consolidation_interval_hours,
            )
            _closables.append(consolidation_scheduler)
            registry.register_instance(ConsolidationScheduler, consolidation_scheduler)

            # Personality
            personality = PersonalityEngine(mind_config)
            registry.register_instance(PersonalityEngine, personality)

            # Context Assembly
            token_counter = TokenCounter()
            budget_manager = TokenBudgetManager()
            formatter = ContextFormatter(token_counter)
            assembler = ContextAssembler(
                token_counter=token_counter,
                personality_engine=personality,
                brain_service=brain_service,
                budget_manager=budget_manager,
                formatter=formatter,
                mind_config=mind_config,
            )
            registry.register_instance(ContextAssembler, assembler)

            # LLM Providers + Router (Mission C6 §T2.1 — refactored from
            # the pre-C6 10 sequential ``os.environ.get``/``providers.append``
            # blocks to a data-driven loop over the canonical registry).
            from sovyx.llm._provider_registry import LLMProviderKey
            from sovyx.llm.providers.google import GoogleProvider

            _provider_factory: dict[LLMProviderKey, Callable[[str], object]] = {
                LLMProviderKey.ANTHROPIC: lambda k: AnthropicProvider(api_key=k),
                LLMProviderKey.OPENAI: lambda k: OpenAIProvider(api_key=k),
                LLMProviderKey.GOOGLE: lambda k: GoogleProvider(api_key=k),
                LLMProviderKey.XAI: lambda k: XAIProvider(api_key=k),
                LLMProviderKey.DEEPSEEK: lambda k: DeepSeekProvider(api_key=k),
                LLMProviderKey.MISTRAL: lambda k: MistralProvider(api_key=k),
                LLMProviderKey.GROQ: lambda k: GroqProvider(api_key=k),
                LLMProviderKey.TOGETHER: lambda k: TogetherProvider(api_key=k),
                LLMProviderKey.FIREWORKS: lambda k: FireworksProvider(api_key=k),
            }

            providers: list[
                AnthropicProvider
                | OpenAIProvider
                | GoogleProvider
                | OllamaProvider
                | XAIProvider
                | DeepSeekProvider
                | MistralProvider
                | GroqProvider
                | TogetherProvider
                | FireworksProvider
            ] = []

            for _key in LLMProviderKey:
                if not _key.is_cloud:
                    continue
                _env_value = os.environ.get(_key.env_var, "")
                if _env_value:
                    providers.append(_provider_factory[_key](_env_value))  # type: ignore[arg-type]
                    # Dual-emission per ADR-D14 — legacy event name preserved
                    # for one minor cycle (operator playbooks reference it);
                    # dropped at v0.50.0 STRICT flip.
                    logger.info("llm_provider_registered", provider=_key.value)

            ollama_provider = OllamaProvider()
            providers.append(ollama_provider)

            # Always ping Ollama to set _verified flag correctly.
            # Health checks and Settings page need accurate availability.
            await ollama_provider.ping()

            # Auto-detect Ollama when no cloud providers are configured.
            # Preserves the pre-C6 mind.yaml persist-on-auto-detect behaviour
            # so an operator who installs Ollama gets a working router on
            # next boot without manual setup. The `models` tuple is captured
            # for the discovery scan below (Mission C6 §T2.1).
            cloud_providers = [p for p in providers if p.name != "ollama"]
            _ollama_models: tuple[str, ...] = ()
            if ollama_provider.is_available:
                with contextlib.suppress(Exception):
                    _ollama_models = tuple(await ollama_provider.list_models())
            if not cloud_providers and ollama_provider.is_available and _ollama_models:
                selected = _select_best_ollama_model(list(_ollama_models))
                mind_config.llm.default_provider = "ollama"
                mind_config.llm.default_model = selected
                logger.info(
                    "ollama_auto_detected",
                    models=list(_ollama_models),
                    selected=selected,
                    hint="Using local Ollama. No cloud API key needed.",
                )
                _persist_ollama_config(
                    mind_config,
                    engine_config.database.data_dir / mind_config.id / "mind.yaml",
                )

            # fast_model fallback: ThinkPhase uses fast_model for low-complexity
            # queries. If empty, those calls silently fail (model="").
            if not mind_config.llm.fast_model and mind_config.llm.default_model:
                mind_config.llm.fast_model = mind_config.llm.default_model

            # Mission C6 §T2.1 — single canonical discovery report.
            # Replaces the pre-C6 C4 hardcoded single-reason wire with
            # verdict-driven composite-store dispatch covering 7 distinct
            # reason tokens (no_provider_configured / ollama_unreachable /
            # ollama_no_models / cloud_key_invalid / all_providers_unhealthy /
            # default_model_unavailable / partial_health). Legacy
            # `no_llm_provider_detected` + `ollama_no_models` WARNs are
            # dual-emitted by the dispatch helpers per ADR-D14.
            from sovyx.engine._llm_dispatch import dispatch_llm_discovery_verdict
            from sovyx.engine._llm_validation import validate_cloud_keys_at_boot
            from sovyx.llm._provider_health import scan_llm_provider_health

            # Mission C6 §T2.6 — opt-in boot-time cloud-key validation.
            # When ``tuning.llm.boot_key_validation_enabled`` is True the
            # validation runs a bounded-timeout transient probe per key,
            # populating the validation_results map the discovery scanner
            # consumes to refine the ``CLOUD_KEY_INVALID`` verdict. Default
            # OFF per ADR-D10 — cloud probes cost real money.
            _validation_results = await validate_cloud_keys_at_boot(
                env=os.environ,
                config=engine_config.tuning.llm,
            )

            _discovery_report = scan_llm_provider_health(
                env=os.environ,
                ollama_ping_result=ollama_provider.is_available,
                ollama_models=_ollama_models if ollama_provider.is_available else None,
                default_provider=mind_config.llm.default_provider,
                default_model=mind_config.llm.default_model,
                cloud_key_validation_results=_validation_results or None,
            )
            logger.info(
                "llm.discovery.report",
                verdict=_discovery_report.verdict.value,
                configured_count=_discovery_report.configured_count,
                available_count=_discovery_report.available_count,
                default_provider=_discovery_report.default_provider,
                default_model=_discovery_report.default_model,
                scan_duration_ms=round(_discovery_report.scan_duration_ms, 3),
            )
            try:
                dispatch_llm_discovery_verdict(_discovery_report)
            except Exception:  # noqa: BLE001 — observability-only surface
                logger.debug(
                    "c6_degraded_store_dispatch_failed",
                    axis="llm",
                    verdict=_discovery_report.verdict.value,
                )

            logger.info(
                "llm_router_config",
                default_model=mind_config.llm.default_model,
                default_provider=mind_config.llm.default_provider,
                providers=[p.name for p in providers if p.is_available],
            )

            # DailyStatsRecorder for historical usage tracking
            from sovyx.dashboard.daily_stats import DailyStatsRecorder

            stats_recorder = DailyStatsRecorder(db_manager.get_system_pool())
            registry.register_instance(DailyStatsRecorder, stats_recorder)

            # Mission C4 §Phase 3 §T3.2 — operator-acknowledgement
            # store for the composite degraded banner. Server-side
            # persistence (ADR-D2) so ack survives browser tab refresh
            # + multi-tab divergence is impossible.
            from sovyx.engine._operator_acks_store import OperatorAcksStore

            operator_acks_store = OperatorAcksStore(db_manager.get_system_pool())
            registry.register_instance(OperatorAcksStore, operator_acks_store)

            # Mission C4 §Phase 3 §T3.5 — TTL re-surface scheduler.
            # Periodic background task that scans for expired acks,
            # removes them + emits voice.degraded_banner.resurfaced
            # so dashboards see the banner re-surface within one poll
            # interval. Registry.shutdown_all invokes its .shutdown()
            # alias automatically on engine teardown.
            from sovyx.engine._ack_resurface_scheduler import (
                AckResurfaceScheduler,
            )

            ack_resurface_scheduler = AckResurfaceScheduler(operator_acks_store)
            registry.register_instance(
                AckResurfaceScheduler,
                ack_resurface_scheduler,
            )
            await ack_resurface_scheduler.start()

            cost_guard = CostGuard(
                daily_budget=mind_config.llm.budget_daily_usd,
                per_conversation_budget=mind_config.llm.budget_per_conversation_usd,
                monthly_budget=mind_config.llm.budget_monthly_usd,
                system_pool=db_manager.get_system_pool(),
                timezone=mind_config.timezone,
                stats_recorder=stats_recorder,
            )
            await cost_guard.restore()
            registry.register_instance(CostGuard, cost_guard)
            # Circuit breaker tunables consumed from EngineConfig.tuning.llm
            # per the 2026-05-02 fix (T01 of pre-wake-word-hardening
            # mission). Industry-triangulated default of 60 s matches the
            # previous router-side default — see LLMTuningConfig docstring
            # for Hystrix/LiteLLM/Polly/Resilience4j comparison.
            llm_tuning = engine_config.tuning.llm
            router = LLMRouter(
                providers=providers,
                cost_guard=cost_guard,
                event_bus=event_bus,
                circuit_breaker_failures=llm_tuning.circuit_breaker_failures,
                circuit_breaker_reset_s=llm_tuning.circuit_breaker_reset_seconds,
            )
            # Mission C6 §T2.3 — prime the router's cached discovery report
            # so `/api/llm/health` and `LLMRouter.has_available_provider` (the
            # CognitiveLoop dependency gate, Phase 1.D §T4.1) reflect the
            # boot-time state immediately.
            router.update_discovery_report(_discovery_report)
            _closables.append(router)
            registry.register_instance(LLMRouter, router)

            # Mission C6 §T2.5 — single-task periodic liveness probe.
            # Spawned as a Closable so the SIGINT teardown cancels cleanly
            # via the existing _closables propagation. The probe is the
            # producer side of anti-pattern #44 (dependency-gated workers
            # MUST be paired with a liveness probe that transitions the
            # composite-store axis on recovery).
            from sovyx.engine._llm_liveness_probe import LLMLivenessProbe

            llm_liveness_probe = LLMLivenessProbe(
                router=router,
                ollama_provider=ollama_provider,
                config=llm_tuning,
                mind_config=mind_config,
                # LIVE-1 Bug A — seed the probe with the boot verdict so its
                # first tick is a real transition check; a provider configured
                # in the boot→first-tick window then clears axis="llm" instead
                # of being masked by silent baselining.
                boot_verdict=_discovery_report.verdict,
            )
            await llm_liveness_probe.start()
            _closables.append(llm_liveness_probe)
            registry.register_instance(LLMLivenessProbe, llm_liveness_probe)

            # DREAM scheduler (SPE-003 phase 7 — nightly pattern discovery).
            # Wired after the LLM router since DREAM's pattern extraction
            # is a single LLM call per run. dream_max_patterns == 0 is the
            # kill-switch: skip registration entirely so the engine pays
            # zero runtime cost.
            if mind_config.brain.dream_max_patterns > 0:
                dream_cycle = DreamCycle(
                    brain_service=brain_service,
                    episode_repo=episode_repo,
                    concept_repo=concept_repo,
                    hebbian=hebbian,
                    llm_router=router,
                    event_bus=event_bus,
                    lookback_hours=mind_config.brain.dream_lookback_hours,
                    max_patterns=mind_config.brain.dream_max_patterns,
                )
                dream_scheduler = DreamScheduler(
                    cycle=dream_cycle,
                    dream_time=mind_config.brain.dream_time,
                    timezone=mind_config.timezone,
                )
                _closables.append(dream_scheduler)
                registry.register_instance(DreamScheduler, dream_scheduler)

            # ── Retention scheduler — Phase 8 / T8.21 step 6 ──────
            # Auto-prune is OFF by default per ``feedback_staged_adoption``
            # (operator opts in after validating dry-run counts). When
            # disabled the scheduler is not instantiated at all — zero
            # runtime cost.
            if mind_config.retention.auto_prune_enabled:
                from sovyx.mind.retention import (  # noqa: PLC0415
                    MindRetentionService,
                    RetentionScheduler,
                )
                from sovyx.voice._consent_ledger import (  # noqa: PLC0415
                    ConsentLedger,
                )

                ledger_path = engine_config.data_dir / "voice" / "consent.jsonl"
                retention_ledger = ConsentLedger(path=ledger_path)
                # Per-mind conversations pool was initialized by
                # ``initialize_mind_databases`` above (line 344).
                retention_service = MindRetentionService(
                    engine_config=engine_config,
                    brain_pool=brain_pool,
                    conversations_pool=db_manager.get_conversation_pool(mind_id),
                    system_pool=db_manager.get_system_pool(),
                    ledger=retention_ledger,
                )
                retention_scheduler = RetentionScheduler(
                    retention_service,
                    mind_config=mind_config,
                    prune_time=mind_config.retention.prune_time,
                    timezone=mind_config.timezone,
                )
                _closables.append(retention_scheduler)
                registry.register_instance(RetentionScheduler, retention_scheduler)
                logger.info(
                    "retention_scheduler_registered",
                    mind_id=str(mind_id),
                    prune_time=mind_config.retention.prune_time,
                )

            # Cognitive phases
            state_machine = CognitiveStateMachine()
            perceive = PerceivePhase()
            attend = AttendPhase(
                safety_config=mind_config.safety,
                llm_router=router,
            )
            # ── Plugin System ───────────────────────────────────
            from sovyx.plugins.manager import PluginManager

            plugins_cfg = mind_config.plugins
            # v0.32.0 Phase C M1 — wire engine-level supply-chain
            # gate into the plugin manager. Default-deny third-party
            # entry-point plugins; operator opts in via
            # ``EngineConfig.plugins.allow_third_party_plugins`` +
            # ``trusted_plugin_packages``.
            engine_plugin_cfg = engine_config.plugins
            plugin_manager = PluginManager(
                brain=brain_service,
                event_bus=event_bus,
                data_dir=engine_config.data_dir / "plugins",
                enabled=plugins_cfg.get_effective_enabled(),
                disabled=plugins_cfg.get_effective_disabled(),
                plugin_config=plugins_cfg.get_all_plugin_configs(),
                granted_permissions=plugins_cfg.get_all_granted_permissions(),
                allow_third_party_plugins=engine_plugin_cfg.allow_third_party_plugins,
                trusted_plugin_packages=list(engine_plugin_cfg.trusted_plugin_packages),
                # MISSION-plugin-mind-scope-2026-05-13 D-T0-3 (Option F):
                # plugins are mind-scoped at load time per the daemon's
                # single-mind invariant. ``mind_id`` is the per-mind
                # loop's current mind — already resolver-validated.
                mind_id=mind_id,
            )
            loaded = await plugin_manager.load_all()
            if loaded:
                logger.info("plugins_loaded", count=len(loaded), names=loaded)
            registry.register_instance(PluginManager, plugin_manager)
            _closables.append(plugin_manager)  # teardown on failure

            think = ThinkPhase(
                context_assembler=assembler,
                llm_router=router,
                mind_config=mind_config,
                plugin_manager=plugin_manager,
            )
            from sovyx.cognitive.financial_gate import FinancialGate
            from sovyx.cognitive.output_guard import OutputGuard
            from sovyx.cognitive.pii_guard import PIIGuard

            output_guard = OutputGuard(
                safety_config=mind_config.safety,
                llm_router=router,
            )
            financial_gate = FinancialGate(safety_config=mind_config.safety)
            registry.register_instance(FinancialGate, financial_gate)
            pii_guard = PIIGuard(safety=mind_config.safety, llm_router=router)

            act = ActPhase(
                tool_executor=ToolExecutor(plugin_manager=plugin_manager),
                llm_router=router,
                output_guard=output_guard,
                financial_gate=financial_gate,
                pii_guard=pii_guard,
            )
            reflect = ReflectPhase(
                brain_service=brain_service,
                llm_router=router,
                fast_model=mind_config.llm.fast_model,
            )

            cog_loop = CognitiveLoop(
                state_machine=state_machine,
                perceive=perceive,
                attend=attend,
                think=think,
                act=act,
                reflect=reflect,
                event_bus=event_bus,
                brain=brain_service,
                # Mission C6 §T4.1 dependency gate — pass the live router
                # so ``CognitiveLoop.start`` can check ``has_available_provider``
                # and emit ``cognitive.loop.started_in_degraded_mode`` when
                # no provider is available. Anti-pattern #44 compliance.
                llm_router=router,
                cognitive_degraded_mode_fail_fast=llm_tuning.cognitive_degraded_mode_fail_fast,
            )
            registry.register_instance(CognitiveLoop, cog_loop)

            gate = CogLoopGate(cog_loop)
            registry.register_instance(CogLoopGate, gate)
            # Mission C6 §T4.2 — wire the liveness probe's verdict-transition
            # callback to the gate's dependency_ready_event so the probe
            # propagates transitions to the worker-pause signal. Anti-pattern
            # #44 producer→consumer pairing (probe = producer; gate = consumer).
            llm_liveness_probe.set_dependency_state_callback(
                gate.set_dependency_ready,
            )
            # Prime the gate's initial state from the boot-time discovery
            # report. PARTIAL_HEALTH is treated as "ready" (routing continues).
            _boot_dep_ready = router.has_available_provider()
            if not _boot_dep_ready:
                gate.set_dependency_ready(False)

            await mind_manager.load_mind(mind_config.id, {"brain": brain_service})
            await mind_manager.start_mind(mind_config.id)

            # v0.31.4 GAP 4 closure: auto-resume voice pipeline on
            # daemon restart when ``MindConfig.voice_enabled=True``.
            # Pre-v0.31.4 the voice pipeline ONLY started when the
            # operator explicitly hit ``POST /api/voice/enable`` —
            # daemon restart silently dropped voice state, every time.
            # The auto-resume re-creates the pipeline using the same
            # ``create_voice_pipeline`` factory the HTTP endpoint uses,
            # then registers the bundle in the registry.
            #
            # DEFENSIVE: any failure (missing models, mic disconnected,
            # PortAudio unavailable, etc.) is logged + swallowed so
            # daemon startup is never blocked by voice auto-resume.
            # Operator can still enable manually via the dashboard.
            if getattr(mind_config, "voice_enabled", False):
                try:
                    await _auto_resume_voice_pipeline(
                        mind_config=mind_config,
                        engine_config=engine_config,
                        registry=registry,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "voice_auto_resume_failed",
                        mind_id=mind_config.id,
                    )

        # 4. PersonResolver + ConversationTracker
        system_pool = db_manager.get_system_pool()
        # Use first mind's conversation pool (v0.1: single mind)
        first_mind_id = MindId(mind_configs[0].id) if mind_configs else MindId("default")
        conv_pool = db_manager.get_conversation_pool(first_mind_id)

        person_resolver = PersonResolver(system_pool)
        conversation_tracker = ConversationTracker(conv_pool)
        registry.register_instance(PersonResolver, person_resolver)
        registry.register_instance(ConversationTracker, conversation_tracker)

        # 5. BridgeManager
        if gate is None:
            msg = "No minds configured — cannot create BridgeManager"
            raise ValueError(msg)

        # Resolve FinancialGate if registered (v0.6 — button confirmations)
        _fin_gate = None
        try:
            from sovyx.cognitive.financial_gate import (  # noqa: TC001
                FinancialGate as FinancialGateType,
            )

            if registry.is_registered(FinancialGateType):
                _fin_gate = await registry.resolve(FinancialGateType)
        except Exception:  # noqa: BLE001
            logger.debug("financial_gate_not_available_for_bridge")

        bridge = BridgeManager(
            event_bus=event_bus,
            cog_loop_gate=gate,
            person_resolver=person_resolver,
            conversation_tracker=conversation_tracker,
            mind_id=first_mind_id,
            financial_gate=_fin_gate,
        )
        registry.register_instance(BridgeManager, bridge)

        # Telegram channel (if token available)
        telegram_token = os.environ.get("SOVYX_TELEGRAM_TOKEN", "")
        if telegram_token:
            from sovyx.bridge.channels.telegram import TelegramChannel

            telegram = TelegramChannel(token=telegram_token, bridge_manager=bridge)
            bridge.register_channel(telegram)

        # 6. HealthRegistry — wired AFTER every subsystem is registered so
        # the live-callback factories (DatabaseManager, BrainService,
        # LLMRouter, BridgeManager, ConsolidationScheduler, CostGuard)
        # find their dependencies. Registered as a singleton so:
        #   * the dashboard /api/health route resolves it via app.state,
        #   * the startup self-diagnosis cascade can call ``snapshot()``
        #     (Phase 11 Task 11.5 — IMPL-OBSERVABILITY-001 §16),
        #   * future SLOMonitor + AlertManager (Phase 11.7) consume the
        #     same instance instead of spawning their own checks.
        from sovyx.observability.health import (
            HealthRegistry,
            create_engine_health_registry,
        )

        health_registry = await create_engine_health_registry(registry)
        registry.register_instance(HealthRegistry, health_registry)

        # 7. SLOMonitor + AlertManager (Phase 11 Task 11.7).
        # The five default SLOs (brain_search/response_time/availability/
        # error_rate/cost_per_message) and five default alert rules
        # (high_error_rate/disk_space_low/memory_pressure/cost_exceeded/
        # provider_errors) match SPE-026 §6 + §8. Registered as
        # singletons so:
        #   * the dashboard ``GET /api/alerts/active`` route resolves
        #     them via the registry,
        #   * call sites that record SLO events (``record_latency``,
        #     ``record_cost``) and alert metrics (``record_metric``)
        #     reuse the same instance instead of spawning isolated
        #     trackers per module.
        # Alerts fire CRITICAL/WARNING via ``logger.warning`` and emit
        # ``AlertFired``/``AlertResolved`` events on the bus — wired
        # here so the EventBus is the same instance the bridge and
        # cognitive loop publish to.
        from sovyx.observability.alerts import (
            AlertManager,
            create_default_alert_manager,
        )
        from sovyx.observability.slo import (
            SLOMonitor,
            create_default_monitor,
        )

        slo_monitor = create_default_monitor()
        alert_manager = create_default_alert_manager(
            event_bus=event_bus,
            slo_monitor=slo_monitor,
        )
        registry.register_instance(SLOMonitor, slo_monitor)
        registry.register_instance(AlertManager, alert_manager)

        logger.info(
            "bootstrap_complete",
            minds=len(mind_configs),
            health_check_count=health_registry.check_count,
            slo_count=len(slo_monitor.slo_keys),
            alert_rule_count=len(alert_manager.rules),
        )
        return registry

    except Exception:  # noqa: BLE001
        # Cleanup already-initialized resources in reverse order
        for resource in reversed(_closables):
            try:
                stop_fn = getattr(resource, "stop", None)
                close_fn = getattr(resource, "close", None)
                shutdown_fn = getattr(resource, "shutdown", None)
                if stop_fn is not None:
                    await stop_fn()
                elif shutdown_fn is not None:
                    await shutdown_fn()
                elif close_fn is not None:
                    await close_fn()
            except Exception:  # noqa: BLE001 — cleanup in bootstrap rollback — must not raise
                logger.warning(
                    "bootstrap_cleanup_failed",
                    resource=type(resource).__name__,
                    exc_info=True,
                )
        raise


# ── Ollama auto-detection helpers ────────────────────────────


# Priority order: newer/larger models first.
_PREFERRED_OLLAMA_MODELS: list[str] = [
    "llama3.1",
    "llama3",
    "llama3.2",
    "mistral",
    "gemma2",
    "qwen2.5",
    "phi3",
    "codellama",
    "deepseek-coder",
]


def _select_best_ollama_model(models: list[str]) -> str:
    """Pick the best available Ollama model by preference order.

    Strips tag suffixes (e.g. ``"llama3.1:latest"`` → ``"llama3.1"``)
    for matching, then returns the original name with tag.

    Args:
        models: Non-empty list of model names from ``OllamaProvider.list_models()``.

    Returns:
        Best model name (with original tag), or first model as fallback.
    """
    # Build lookup: base_name → full_name (first occurrence wins)
    base_to_full: dict[str, str] = {}
    for m in models:
        base = m.split(":")[0]
        if base not in base_to_full:
            base_to_full[base] = m

    for preferred in _PREFERRED_OLLAMA_MODELS:
        if preferred in base_to_full:
            return base_to_full[preferred]

    return models[0]


def _persist_ollama_config(mind_config: MindConfig, mind_yaml_path: Path) -> None:
    """Persist auto-detected Ollama config to mind.yaml.

    Creates the file if it doesn't exist. Merges with existing YAML
    to preserve user-edited fields in other sections.

    Args:
        mind_config: The MindConfig with updated LLM fields.
        mind_yaml_path: Path to mind.yaml.
    """
    import yaml

    existing: dict[str, object] = {}
    if mind_yaml_path.exists():
        try:
            with open(mind_yaml_path) as f:
                loaded = yaml.safe_load(f)
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, yaml.YAMLError):
            # Stale mind.yaml is tolerable — we'll overwrite it below —
            # but log with full traceback so corruption and permission
            # issues don't get masked as "no config yet".
            logger.debug(
                "mind_yaml_read_failed",
                path=str(mind_yaml_path),
                exc_info=True,
            )

    # Update only the LLM section — don't clobber other config
    existing["llm"] = {
        "default_provider": mind_config.llm.default_provider,
        "default_model": mind_config.llm.default_model,
        "fast_model": mind_config.llm.fast_model,
        "temperature": mind_config.llm.temperature,
        "streaming": mind_config.llm.streaming,
        "budget_daily_usd": mind_config.llm.budget_daily_usd,
        "budget_per_conversation_usd": mind_config.llm.budget_per_conversation_usd,
    }

    try:
        mind_yaml_path.parent.mkdir(parents=True, exist_ok=True)
        with open(mind_yaml_path, "w") as f:
            yaml.safe_dump(existing, f, default_flow_style=False, sort_keys=False)
        logger.info("ollama_config_persisted", path=str(mind_yaml_path))
    except (OSError, yaml.YAMLError):
        # A failed persist means the auto-detected Ollama config
        # won't survive a daemon restart — user-visible only on the
        # next boot. Traceback matters because permission errors,
        # read-only mounts, and disk-full surface differently.
        logger.warning(
            "ollama_config_persist_failed",
            path=str(mind_yaml_path),
            exc_info=True,
        )
