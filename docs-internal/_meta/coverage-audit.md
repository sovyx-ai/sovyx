# Coverage Audit — Public Symbols in Docs

**Gerado em**: 2026-04-14
**Escopo**: `src/sovyx/` (todas funções/classes/métodos públicos, sem `_` prefix)
**Docs cruzados**: 38 arquivos em `docs/` (exclui `_meta/batches`, `_meta/gap-inputs`)

## Sumário

| Categoria | Total | Documentado | % |
|---|---:|---:|---:|
| Classes | 511 | 405 | 79.3% |
| Funções top-level | 156 | 35 | 22.4% |
| Métodos públicos | 962 | 241 | 25.1% |
| **TOTAL** | **1629** | **681** | **41.8%** |

## Undocumented por módulo

| Módulo | Símbolos não-documentados |
|---|---:|
| plugins | 169 |
| cloud | 156 |
| voice | 143 |
| cognitive | 96 |
| engine | 71 |
| brain | 67 |
| observability | 61 |
| llm | 35 |
| dashboard | 34 |
| bridge | 33 |
| upgrade | 33 |
| persistence | 18 |
| cli | 14 |
| mind | 7 |
| benchmarks | 6 |
| context | 5 |

## Detalhe por arquivo

### `src/sovyx/benchmarks/baseline.py` — 7 ✅ / 3 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `RegressionDetected` | 25 | ✅ | docs/modules/benchmarks.md |
| class | `MetricComparison` | 30 | ✅ | docs/modules/benchmarks.md |
| class | `ComparisonReport` | 53 | ✅ | docs/modules/benchmarks.md |
| method | `ComparisonReport.to_dict` | 66 | ✅ | docs/modules/benchmarks.md, docs/modules/dashboard.md |
| class | `BaselineManager` | 93 | ✅ | docs/modules/benchmarks.md |
| method | `BaselineManager.baselines_dir` | 114 | ❌ | — |
| method | `BaselineManager.tolerance` | 119 | ❌ | — |
| method | `BaselineManager.save_baseline` | 123 | ✅ | docs/modules/benchmarks.md |
| method | `BaselineManager.load_baseline` | 152 | ❌ | — |
| method | `BaselineManager.compare` | 186 | ✅ | docs/modules/benchmarks.md |

### `src/sovyx/benchmarks/budgets.py` — 9 ✅ / 3 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `HardwareTier` | 20 | ✅ | docs/modules/benchmarks.md, docs/modules/voice.md |
| class | `TierLimits` | 29 | ✅ | docs/modules/benchmarks.md |
| class | `BenchmarkResult` | 74 | ✅ | docs/modules/benchmarks.md |
| method | `BenchmarkResult.to_dict` | 87 | ✅ | docs/modules/benchmarks.md, docs/modules/dashboard.md |
| class | `BudgetCheck` | 93 | ✅ | docs/modules/benchmarks.md |
| class | `PerformanceBudget` | 113 | ✅ | docs/modules/benchmarks.md |
| method | `PerformanceBudget.tier` | 125 | ❌ | — |
| method | `PerformanceBudget.limits` | 130 | ❌ | — |
| method | `PerformanceBudget.check` | 134 | ✅ | docs/modules/benchmarks.md, docs/security/obsidian-protocol.md |
| method | `PerformanceBudget.check_all` | 174 | ✅ | docs/modules/benchmarks.md |
| method | `PerformanceBudget.all_passed` | 190 | ✅ | docs/modules/benchmarks.md |
| method | `PerformanceBudget.get_tier_limits` | 203 | ❌ | — |

### `src/sovyx/brain/concept_repo.py` — 3 ✅ / 14 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `ConceptRepository` | 46 | ✅ | docs/architecture/brain-graph.md, docs/modules/brain.md |
| method | `ConceptRepository.create` | 57 | ❌ | — |
| method | `ConceptRepository.get` | 106 | ✅ | docs/architecture/data-flow.md, docs/modules/benchmarks.md, docs/modules/engine.md +3 |
| method | `ConceptRepository.get_by_mind` | 125 | ❌ | — |
| method | `ConceptRepository.get_recent` | 139 | ✅ | docs/research/embedding-strategies.md |
| method | `ConceptRepository.update` | 157 | ❌ | — |
| method | `ConceptRepository.delete` | 182 | ❌ | — |
| method | `ConceptRepository.record_access` | 195 | ❌ | — |
| method | `ConceptRepository.boost_importance` | 206 | ❌ | — |
| method | `ConceptRepository.search_by_embedding` | 220 | ❌ | — |
| method | `ConceptRepository.search_by_text` | 258 | ❌ | — |
| method | `ConceptRepository.find_merge_candidates` | 294 | ❌ | — |
| method | `ConceptRepository.get_embeddings_by_category` | 356 | ❌ | — |
| method | `ConceptRepository.count_by_category` | 401 | ❌ | — |
| method | `ConceptRepository.get_categories` | 425 | ❌ | — |
| method | `ConceptRepository.batch_update_scores` | 445 | ❌ | — |
| method | `ConceptRepository.count` | 472 | ❌ | — |

### `src/sovyx/brain/consolidation.py` — 4 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `ConsolidationCycle` | 28 | ✅ | docs/architecture/brain-graph.md, docs/architecture/cognitive-loop.md, docs/architecture/data-flow.md +4 |
| method | `ConsolidationCycle.run` | 62 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/data-flow.md, docs/modules/observability.md |
| class | `ConsolidationScheduler` | 470 | ✅ | docs/architecture/brain-graph.md, docs/architecture/cognitive-loop.md, docs/modules/brain.md +1 |
| method | `ConsolidationScheduler.start` | 487 | ❌ | — |
| method | `ConsolidationScheduler.stop` | 504 | ✅ | docs/modules/llm.md |

### `src/sovyx/brain/contradiction.py` — 2 ✅ / 0 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `ContentRelation` | 29 | ✅ | docs/architecture/brain-graph.md, docs/modules/brain.md |
| function | `detect_contradiction` | 108 | ✅ | docs/modules/brain.md |

### `src/sovyx/brain/embedding.py` — 4 ✅ / 7 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `ModelDownloader` | 119 | ✅ | docs/architecture/brain-graph.md, docs/modules/brain.md |
| method | `ModelDownloader.ensure_model` | 148 | ❌ | — |
| class | `EmbeddingEngine` | 440 | ✅ | docs/architecture/brain-graph.md, docs/modules/brain.md, docs/research/embedding-strategies.md +1 |
| method | `EmbeddingEngine.ensure_loaded` | 471 | ✅ | docs/modules/brain.md, docs/research/embedding-strategies.md |
| method | `EmbeddingEngine.encode` | 544 | ✅ | docs/modules/bridge.md, docs/modules/persistence.md, docs/research/embedding-strategies.md |
| method | `EmbeddingEngine.encode_batch` | 573 | ❌ | — |
| method | `EmbeddingEngine.compute_category_centroid` | 642 | ❌ | — |
| method | `EmbeddingEngine.cosine_similarity` | 676 | ❌ | — |
| method | `EmbeddingEngine.dimensions` | 693 | ❌ | — |
| method | `EmbeddingEngine.is_loaded` | 698 | ❌ | — |
| method | `EmbeddingEngine.has_embeddings` | 703 | ❌ | — |

### `src/sovyx/brain/episode_repo.py` — 3 ✅ / 5 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `EpisodeRepository` | 24 | ✅ | docs/architecture/brain-graph.md, docs/modules/brain.md |
| method | `EpisodeRepository.create` | 35 | ❌ | — |
| method | `EpisodeRepository.get` | 84 | ✅ | docs/architecture/data-flow.md, docs/modules/benchmarks.md, docs/modules/engine.md +3 |
| method | `EpisodeRepository.get_by_conversation` | 102 | ❌ | — |
| method | `EpisodeRepository.get_recent` | 115 | ✅ | docs/research/embedding-strategies.md |
| method | `EpisodeRepository.search_by_embedding` | 126 | ❌ | — |
| method | `EpisodeRepository.delete` | 164 | ❌ | — |
| method | `EpisodeRepository.count` | 177 | ❌ | — |

### `src/sovyx/brain/learning.py` — 4 ✅ / 2 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `HebbianLearning` | 26 | ✅ | docs/architecture/brain-graph.md, docs/modules/brain.md, docs/research/memory-systems.md +1 |
| method | `HebbianLearning.strengthen` | 56 | ✅ | docs/architecture/brain-graph.md, docs/modules/brain.md |
| method | `HebbianLearning.strengthen_star` | 99 | ✅ | docs/modules/brain.md |
| class | `EbbinghausDecay` | 314 | ✅ | docs/architecture/brain-graph.md, docs/modules/brain.md, docs/research/memory-systems.md +1 |
| method | `EbbinghausDecay.apply_decay` | 340 | ❌ | — |
| method | `EbbinghausDecay.prune_weak` | 380 | ❌ | — |

### `src/sovyx/brain/models.py` — 3 ✅ / 0 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `Concept` | 25 | ✅ | docs/architecture/brain-graph.md, docs/architecture/overview.md, docs/modules/brain.md +6 |
| class | `Episode` | 49 | ✅ | docs/architecture/brain-graph.md, docs/architecture/overview.md, docs/modules/brain.md +8 |
| class | `Relation` | 70 | ✅ | docs/architecture/brain-graph.md, docs/architecture/overview.md, docs/modules/brain.md +2 |

### `src/sovyx/brain/relation_repo.py` — 2 ✅ / 10 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `RelationRepository` | 40 | ✅ | docs/architecture/brain-graph.md, docs/modules/brain.md |
| method | `RelationRepository.create` | 50 | ❌ | — |
| method | `RelationRepository.get` | 91 | ✅ | docs/architecture/data-flow.md, docs/modules/benchmarks.md, docs/modules/engine.md +3 |
| method | `RelationRepository.get_relations_for` | 104 | ❌ | — |
| method | `RelationRepository.get_neighbors` | 122 | ❌ | — |
| method | `RelationRepository.update_weight` | 156 | ❌ | — |
| method | `RelationRepository.increment_co_occurrence` | 165 | ❌ | — |
| method | `RelationRepository.get_or_create` | 211 | ❌ | — |
| method | `RelationRepository.delete` | 254 | ❌ | — |
| method | `RelationRepository.delete_weak` | 263 | ❌ | — |
| method | `RelationRepository.transfer_relations` | 286 | ❌ | — |
| method | `RelationRepository.get_degree_centrality` | 347 | ❌ | — |

### `src/sovyx/brain/retrieval.py` — 3 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `HybridRetrieval` | 23 | ✅ | docs/architecture/brain-graph.md, docs/architecture/data-flow.md, docs/modules/brain.md +2 |
| method | `HybridRetrieval.search_concepts` | 49 | ✅ | docs/architecture/data-flow.md |
| method | `HybridRetrieval.search_episodes` | 91 | ✅ | docs/architecture/data-flow.md |
| method | `HybridRetrieval.search_all` | 129 | ❌ | — |

### `src/sovyx/brain/scoring.py` — 6 ✅ / 13 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `ImportanceWeights` | 31 | ✅ | docs/architecture/brain-graph.md, docs/architecture/cognitive-loop.md, docs/modules/brain.md +4 |
| class | `ConfidenceWeights` | 65 | ✅ | docs/architecture/brain-graph.md, docs/modules/brain.md |
| class | `EvolutionWeights` | 90 | ✅ | docs/architecture/brain-graph.md, docs/modules/brain.md |
| class | `ImportanceScorer` | 122 | ✅ | docs/architecture/brain-graph.md, docs/modules/brain.md |
| method | `ImportanceScorer.score_initial` | 156 | ❌ | — |
| method | `ImportanceScorer.score_access_boost` | 193 | ❌ | — |
| method | `ImportanceScorer.score_connectivity` | 210 | ❌ | — |
| method | `ImportanceScorer.score_recency` | 234 | ❌ | — |
| method | `ImportanceScorer.recalculate` | 249 | ❌ | — |
| class | `ConfidenceScorer` | 317 | ✅ | docs/architecture/brain-graph.md, docs/modules/brain.md |
| method | `ConfidenceScorer.get_source_confidence` | 355 | ❌ | — |
| method | `ConfidenceScorer.score_initial` | 367 | ❌ | — |
| method | `ConfidenceScorer.score_corroboration` | 400 | ❌ | — |
| method | `ConfidenceScorer.score_staleness_decay` | 420 | ❌ | — |
| method | `ConfidenceScorer.score_content_update` | 441 | ❌ | — |
| method | `ConfidenceScorer.score_contradiction` | 457 | ❌ | — |
| class | `ScoreNormalizer` | 475 | ✅ | docs/architecture/brain-graph.md, docs/modules/brain.md |
| method | `ScoreNormalizer.normalize` | 507 | ❌ | — |
| method | `ScoreNormalizer.normalize_by_category` | 553 | ❌ | — |

### `src/sovyx/brain/service.py` — 6 ✅ / 8 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `BrainService` | 43 | ✅ | docs/architecture/brain-graph.md, docs/architecture/data-flow.md, docs/modules/brain.md +6 |
| method | `BrainService.start` | 83 | ❌ | — |
| method | `BrainService.stop` | 95 | ✅ | docs/modules/llm.md |
| method | `BrainService.search` | 102 | ❌ | — |
| method | `BrainService.recall` | 156 | ✅ | docs/modules/brain.md, docs/modules/context.md |
| method | `BrainService.get_concept` | 171 | ❌ | — |
| method | `BrainService.get_related` | 175 | ❌ | — |
| method | `BrainService.learn_concept` | 187 | ❌ | — |
| method | `BrainService.encode_episode` | 367 | ✅ | docs/architecture/data-flow.md, docs/modules/brain.md |
| method | `BrainService.strengthen_connection` | 449 | ✅ | docs/modules/brain.md |
| method | `BrainService.decay_working_memory` | 466 | ✅ | docs/modules/brain.md |
| method | `BrainService.compute_novelty` | 506 | ❌ | — |
| method | `BrainService.refresh_centroid_cache` | 648 | ❌ | — |
| method | `BrainService.invalidate_centroid_cache` | 699 | ❌ | — |

### `src/sovyx/brain/spreading.py` — 3 ✅ / 0 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `SpreadingActivation` | 21 | ✅ | docs/architecture/brain-graph.md, docs/architecture/data-flow.md, docs/modules/brain.md +3 |
| method | `SpreadingActivation.activate` | 53 | ✅ | docs/modules/brain.md |
| method | `SpreadingActivation.activate_from_text` | 115 | ✅ | docs/modules/brain.md |

### `src/sovyx/brain/working_memory.py` — 3 ✅ / 6 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `WorkingMemory` | 13 | ✅ | docs/architecture/brain-graph.md, docs/architecture/data-flow.md, docs/modules/brain.md +2 |
| method | `WorkingMemory.activate` | 31 | ✅ | docs/modules/brain.md |
| method | `WorkingMemory.get_activation` | 72 | ❌ | — |
| method | `WorkingMemory.get_importance` | 83 | ❌ | — |
| method | `WorkingMemory.get_active_concepts` | 94 | ❌ | — |
| method | `WorkingMemory.decay_all` | 109 | ❌ | — |
| method | `WorkingMemory.clear` | 123 | ✅ | docs/development/testing.md |
| method | `WorkingMemory.size` | 129 | ❌ | — |
| method | `WorkingMemory.capacity` | 134 | ❌ | — |

### `src/sovyx/bridge/channels/signal.py` — 4 ✅ / 11 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `SignalChannel` | 44 | ✅ | docs/modules/bridge.md, docs/_meta/gap-analysis.md |
| method | `SignalChannel.channel_type` | 74 | ❌ | — |
| method | `SignalChannel.capabilities` | 85 | ❌ | — |
| method | `SignalChannel.format_capabilities` | 90 | ❌ | — |
| method | `SignalChannel.is_running` | 99 | ❌ | — |
| method | `SignalChannel.phone_number` | 104 | ❌ | — |
| method | `SignalChannel.api_url` | 109 | ❌ | — |
| method | `SignalChannel.initialize` | 113 | ✅ | docs/modules/persistence.md |
| method | `SignalChannel.start` | 138 | ❌ | — |
| method | `SignalChannel.stop` | 147 | ✅ | docs/modules/llm.md |
| method | `SignalChannel.send` | 159 | ✅ | docs/modules/bridge.md |
| method | `SignalChannel.send_typing` | 220 | ❌ | — |
| method | `SignalChannel.edit` | 258 | ❌ | — |
| method | `SignalChannel.delete` | 269 | ❌ | — |
| method | `SignalChannel.react` | 274 | ❌ | — |

### `src/sovyx/bridge/channels/telegram.py` — 4 ✅ / 9 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `TelegramChannel` | 36 | ✅ | docs/modules/bridge.md, docs/_meta/gap-analysis.md |
| method | `TelegramChannel.channel_type` | 61 | ❌ | — |
| method | `TelegramChannel.capabilities` | 66 | ❌ | — |
| method | `TelegramChannel.format_capabilities` | 71 | ❌ | — |
| method | `TelegramChannel.is_running` | 80 | ❌ | — |
| method | `TelegramChannel.initialize` | 84 | ✅ | docs/modules/persistence.md |
| method | `TelegramChannel.start` | 87 | ❌ | — |
| method | `TelegramChannel.stop` | 95 | ✅ | docs/modules/llm.md |
| method | `TelegramChannel.send` | 105 | ✅ | docs/modules/bridge.md |
| method | `TelegramChannel.edit` | 171 | ❌ | — |
| method | `TelegramChannel.delete` | 233 | ❌ | — |
| method | `TelegramChannel.react` | 238 | ❌ | — |
| method | `TelegramChannel.send_typing` | 243 | ❌ | — |

### `src/sovyx/bridge/identity.py` — 2 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `PersonResolver` | 18 | ✅ | docs/modules/bridge.md |
| method | `PersonResolver.resolve` | 28 | ✅ | docs/architecture/overview.md, docs/modules/engine.md |
| method | `PersonResolver.get_person` | 94 | ❌ | — |

### `src/sovyx/bridge/manager.py` — 5 ✅ / 6 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `PersonResolver` | 75 | ✅ | docs/modules/bridge.md |
| method | `PersonResolver.resolve` | 78 | ✅ | docs/architecture/overview.md, docs/modules/engine.md |
| class | `ConversationTracker` | 86 | ✅ | docs/modules/bridge.md |
| method | `ConversationTracker.get_or_create` | 89 | ❌ | — |
| method | `ConversationTracker.add_turn` | 96 | ❌ | — |
| class | `BridgeManager` | 119 | ✅ | docs/architecture/overview.md, docs/modules/bridge.md, docs/modules/engine.md +2 |
| method | `BridgeManager.mind_id` | 154 | ❌ | — |
| method | `BridgeManager.register_channel` | 158 | ❌ | — |
| method | `BridgeManager.start` | 166 | ❌ | — |
| method | `BridgeManager.stop` | 175 | ✅ | docs/modules/llm.md |
| method | `BridgeManager.handle_inbound` | 181 | ❌ | — |

### `src/sovyx/bridge/protocol.py` — 3 ✅ / 3 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `InlineButton` | 18 | ✅ | docs/modules/bridge.md |
| class | `InboundMessage` | 44 | ✅ | docs/architecture/data-flow.md, docs/architecture/overview.md, docs/modules/bridge.md +2 |
| method | `InboundMessage.is_callback` | 73 | ❌ | — |
| method | `InboundMessage.callback_data` | 78 | ❌ | — |
| method | `InboundMessage.callback_message_id` | 84 | ❌ | — |
| class | `OutboundMessage` | 91 | ✅ | docs/architecture/data-flow.md, docs/architecture/overview.md, docs/modules/bridge.md +1 |

### `src/sovyx/bridge/sessions.py` — 1 ✅ / 3 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `ConversationTracker` | 19 | ✅ | docs/modules/bridge.md |
| method | `ConversationTracker.get_or_create` | 37 | ❌ | — |
| method | `ConversationTracker.add_turn` | 107 | ❌ | — |
| method | `ConversationTracker.end_conversation` | 134 | ❌ | — |

### `src/sovyx/cli/commands/brain_analyze.py` — 0 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `analyze_scores` | 88 | ❌ | — |

### `src/sovyx/cli/commands/dashboard.py` — 0 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `dashboard_info` | 17 | ❌ | — |

### `src/sovyx/cli/commands/logs.py` — 1 ✅ / 0 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `logs` | 273 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/data-flow.md, docs/architecture/overview.md +15 |

### `src/sovyx/cli/commands/plugin.py` — 0 ✅ / 8 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `plugin_list` | 52 | ❌ | — |
| function | `plugin_info` | 98 | ❌ | — |
| function | `plugin_install` | 152 | ❌ | — |
| function | `plugin_enable` | 228 | ❌ | — |
| function | `plugin_disable` | 240 | ❌ | — |
| function | `plugin_remove` | 255 | ❌ | — |
| function | `plugin_validate` | 274 | ❌ | — |
| function | `plugin_create` | 407 | ❌ | — |

### `src/sovyx/cli/main.py` — 8 ✅ / 3 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `main` | 47 | ✅ | docs/modules/cli.md, docs/modules/dashboard.md, docs/modules/upgrade.md +8 |
| function | `token` | 57 | ✅ | docs/architecture/data-flow.md, docs/architecture/llm-router.md, docs/architecture/overview.md +18 |
| function | `init` | 106 | ✅ | docs/architecture/data-flow.md, docs/architecture/overview.md, docs/modules/cli.md +4 |
| function | `start` | 151 | ✅ | docs/architecture/overview.md, docs/modules/benchmarks.md, docs/modules/cli.md +9 |
| function | `stop` | 204 | ✅ | docs/architecture/overview.md, docs/modules/cli.md, docs/modules/dashboard.md +4 |
| function | `status` | 220 | ✅ | docs/architecture/data-flow.md, docs/architecture/llm-router.md, docs/architecture/overview.md +11 |
| function | `doctor` | 244 | ✅ | docs/architecture/data-flow.md, docs/architecture/overview.md, docs/modules/cli.md +9 |
| function | `brain_search` | 399 | ✅ | docs/modules/benchmarks.md |
| function | `brain_stats` | 423 | ❌ | — |
| function | `mind_list` | 445 | ❌ | — |
| function | `mind_status` | 464 | ❌ | — |

### `src/sovyx/cli/rpc_client.py` — 2 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `DaemonClient` | 18 | ✅ | docs/architecture/overview.md, docs/modules/cli.md, docs/development/contributing.md +1 |
| method | `DaemonClient.is_daemon_running` | 28 | ✅ | docs/modules/cli.md |
| method | `DaemonClient.call` | 48 | ❌ | — |

### `src/sovyx/cloud/apikeys.py` — 4 ✅ / 18 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `Scope` | 40 | ✅ | docs/modules/cloud.md, docs/research/competitive-analysis.md, docs/research/embedding-strategies.md +9 |
| method | `Scope.from_strings` | 48 | ❌ | — |
| method | `Scope.to_strings` | 69 | ❌ | — |
| class | `APIKeyRecord` | 75 | ❌ | — |
| method | `APIKeyRecord.is_revoked` | 92 | ❌ | — |
| method | `APIKeyRecord.is_expired` | 97 | ❌ | — |
| class | `APIKeyInfo` | 105 | ✅ | docs/modules/cloud.md |
| method | `APIKeyInfo.is_active` | 120 | ❌ | — |
| class | `APIKeyValidation` | 128 | ✅ | docs/modules/cloud.md |
| class | `APIKeyStore` | 138 | ❌ | — |
| method | `APIKeyStore.insert` | 148 | ❌ | — |
| method | `APIKeyStore.get_by_hash` | 154 | ❌ | — |
| method | `APIKeyStore.get_by_id` | 158 | ❌ | — |
| method | `APIKeyStore.list_by_user` | 162 | ❌ | — |
| method | `APIKeyStore.update` | 167 | ❌ | — |
| method | `APIKeyStore.touch` | 172 | ❌ | — |
| class | `APIKeyService` | 200 | ✅ | docs/modules/cloud.md |
| method | `APIKeyService.create` | 222 | ❌ | — |
| method | `APIKeyService.validate` | 290 | ❌ | — |
| method | `APIKeyService.revoke` | 327 | ❌ | — |
| method | `APIKeyService.list_keys` | 363 | ❌ | — |
| method | `APIKeyService.get_key` | 375 | ❌ | — |

### `src/sovyx/cloud/backup.py` — 8 ✅ / 13 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `BackupConfig` | 42 | ✅ | docs/modules/cloud.md |
| class | `BackupMetadata` | 63 | ✅ | docs/modules/cloud.md |
| class | `RestoreResult` | 90 | ✅ | docs/modules/cloud.md |
| class | `BackupInfo` | 107 | ✅ | docs/modules/cloud.md, docs/modules/upgrade.md |
| class | `PruneResult` | 124 | ✅ | docs/modules/cloud.md |
| class | `R2Client` | 139 | ✅ | docs/modules/cloud.md |
| method | `R2Client.upload_bytes` | 145 | ❌ | — |
| method | `R2Client.download_bytes` | 149 | ❌ | — |
| method | `R2Client.list_objects` | 153 | ❌ | — |
| method | `R2Client.delete_objects` | 157 | ❌ | — |
| class | `Boto3R2Client` | 162 | ✅ | docs/modules/cloud.md |
| method | `Boto3R2Client.upload_bytes` | 180 | ❌ | — |
| method | `Boto3R2Client.download_bytes` | 184 | ❌ | — |
| method | `Boto3R2Client.list_objects` | 190 | ❌ | — |
| method | `Boto3R2Client.delete_objects` | 205 | ❌ | — |
| class | `BackupService` | 271 | ✅ | docs/modules/cloud.md |
| method | `BackupService.r2` | 307 | ❌ | — |
| method | `BackupService.backup_config` | 312 | ❌ | — |
| method | `BackupService.create_backup` | 316 | ❌ | — |
| method | `BackupService.restore_backup` | 409 | ❌ | — |
| method | `BackupService.list_backups` | 474 | ❌ | — |

### `src/sovyx/cloud/billing.py` — 13 ✅ / 13 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `SubscriptionTier` | 61 | ✅ | docs/modules/cloud.md, docs/planning/gtm-strategy.md, docs/_meta/gap-analysis.md |
| class | `BillingConfig` | 95 | ✅ | docs/modules/cloud.md |
| class | `CheckoutResult` | 108 | ✅ | docs/modules/cloud.md |
| class | `PortalResult` | 118 | ✅ | docs/modules/cloud.md |
| class | `WebhookEvent` | 126 | ✅ | docs/modules/cloud.md |
| class | `WebhookResult` | 137 | ✅ | docs/modules/cloud.md |
| class | `SubscriptionInfo` | 147 | ✅ | docs/modules/cloud.md |
| class | `WebhookSignatureError` | 164 | ✅ | docs/modules/cloud.md |
| class | `WebhookPayloadError` | 168 | ✅ | docs/modules/cloud.md |
| function | `verify_webhook_signature` | 172 | ❌ | — |
| class | `EventStore` | 247 | ❌ | — |
| method | `EventStore.is_processed` | 253 | ❌ | — |
| method | `EventStore.mark_processed` | 257 | ❌ | — |
| class | `InMemoryEventStore` | 268 | ❌ | — |
| method | `InMemoryEventStore.is_processed` | 274 | ❌ | — |
| method | `InMemoryEventStore.mark_processed` | 278 | ❌ | — |
| function | `map_stripe_status` | 307 | ❌ | — |
| function | `tier_from_price_id` | 319 | ❌ | — |
| class | `WebhookHandler` | 341 | ✅ | docs/modules/cloud.md |
| method | `WebhookHandler.register` | 376 | ✅ | docs/modules/cloud.md |
| method | `WebhookHandler.registered_events` | 386 | ❌ | — |
| method | `WebhookHandler.process` | 390 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/llm-router.md |
| class | `BillingService` | 488 | ✅ | docs/modules/cloud.md |
| method | `BillingService.create_checkout` | 530 | ❌ | — |
| method | `BillingService.create_portal_session` | 619 | ❌ | — |
| method | `BillingService.extract_subscription_info` | 658 | ❌ | — |

### `src/sovyx/cloud/crypto.py` — 3 ✅ / 3 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `DerivedKey` | 39 | ✅ | docs/modules/cloud.md |
| class | `BackupCrypto` | 46 | ✅ | docs/modules/cloud.md, docs/security/obsidian-protocol.md |
| method | `BackupCrypto.derive_key` | 61 | ❌ | — |
| method | `BackupCrypto.encrypt` | 102 | ✅ | docs/modules/cloud.md |
| method | `BackupCrypto.decrypt` | 129 | ❌ | — |
| method | `BackupCrypto.verify_password` | 158 | ❌ | — |

### `src/sovyx/cloud/dunning.py` — 8 ✅ / 26 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `DunningState` | 46 | ✅ | docs/modules/cloud.md |
| class | `EmailType` | 57 | ✅ | docs/modules/cloud.md |
| class | `DunningEmail` | 109 | ❌ | — |
| class | `DunningRecord` | 119 | ✅ | docs/modules/cloud.md |
| method | `DunningRecord.days_elapsed` | 137 | ❌ | — |
| method | `DunningRecord.next_retry_delay` | 144 | ❌ | — |
| method | `DunningRecord.should_retry` | 150 | ❌ | — |
| class | `EmailSender` | 190 | ❌ | — |
| method | `EmailSender.send` | 196 | ✅ | docs/modules/bridge.md |
| class | `DunningStore` | 217 | ❌ | — |
| method | `DunningStore.get` | 223 | ✅ | docs/architecture/data-flow.md, docs/modules/benchmarks.md, docs/modules/engine.md +3 |
| method | `DunningStore.save` | 227 | ❌ | — |
| method | `DunningStore.delete` | 231 | ❌ | — |
| method | `DunningStore.list_active` | 239 | ❌ | — |
| class | `SubscriptionDowngrader` | 244 | ❌ | — |
| method | `SubscriptionDowngrader.downgrade_to_free` | 247 | ❌ | — |
| class | `CustomerResolver` | 267 | ❌ | — |
| method | `CustomerResolver.get_email` | 270 | ❌ | — |
| class | `InMemoryDunningStore` | 285 | ❌ | — |
| method | `InMemoryDunningStore.get` | 291 | ✅ | docs/architecture/data-flow.md, docs/modules/benchmarks.md, docs/modules/engine.md +3 |
| method | `InMemoryDunningStore.save` | 295 | ❌ | — |
| method | `InMemoryDunningStore.delete` | 300 | ❌ | — |
| method | `InMemoryDunningStore.list_active` | 307 | ❌ | — |
| class | `InMemoryEmailSender` | 312 | ❌ | — |
| method | `InMemoryEmailSender.send` | 318 | ✅ | docs/modules/bridge.md |
| class | `NoopSubscriptionDowngrader` | 337 | ❌ | — |
| method | `NoopSubscriptionDowngrader.downgrade_to_free` | 343 | ❌ | — |
| class | `InMemoryCustomerResolver` | 361 | ❌ | — |
| method | `InMemoryCustomerResolver.get_email` | 367 | ❌ | — |
| class | `DunningService` | 381 | ✅ | docs/modules/cloud.md |
| method | `DunningService.handle_payment_failed` | 440 | ❌ | — |
| method | `DunningService.handle_payment_succeeded` | 496 | ❌ | — |
| method | `DunningService.process_dunning_cycle` | 529 | ❌ | — |
| method | `DunningService.get_status` | 590 | ❌ | — |

### `src/sovyx/cloud/flex.py` — 12 ✅ / 18 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `TopupStatus` | 54 | ✅ | docs/modules/cloud.md |
| class | `TransactionType` | 63 | ✅ | docs/modules/cloud.md |
| class | `FlexBalance` | 77 | ✅ | docs/modules/cloud.md |
| class | `TopupResult` | 98 | ✅ | docs/modules/cloud.md |
| class | `DeductionResult` | 117 | ✅ | docs/modules/cloud.md |
| class | `BalanceTransaction` | 134 | ✅ | docs/modules/cloud.md |
| class | `FlexStore` | 159 | ❌ | — |
| method | `FlexStore.get_balance` | 162 | ❌ | — |
| method | `FlexStore.save_balance` | 166 | ❌ | — |
| method | `FlexStore.add_transaction` | 170 | ❌ | — |
| class | `StripePaymentGateway` | 175 | ❌ | — |
| method | `StripePaymentGateway.create_payment_intent` | 181 | ❌ | — |
| method | `StripePaymentGateway.charge_saved_method` | 205 | ❌ | — |
| class | `FlexError` | 224 | ✅ | docs/modules/cloud.md |
| class | `InvalidTopupAmountError` | 228 | ✅ | docs/modules/cloud.md |
| class | `InsufficientBalanceError` | 232 | ✅ | docs/modules/cloud.md |
| class | `MaxBalanceExceededError` | 236 | ✅ | docs/modules/cloud.md |
| class | `PaymentError` | 240 | ✅ | docs/modules/cloud.md |
| class | `InMemoryFlexStore` | 247 | ❌ | — |
| method | `InMemoryFlexStore.get_balance` | 254 | ❌ | — |
| method | `InMemoryFlexStore.save_balance` | 258 | ❌ | — |
| method | `InMemoryFlexStore.add_transaction` | 262 | ❌ | — |
| method | `InMemoryFlexStore.transactions` | 267 | ❌ | — |
| class | `FlexBalanceService` | 275 | ✅ | docs/modules/cloud.md |
| method | `FlexBalanceService.get_balance` | 299 | ❌ | — |
| method | `FlexBalanceService.get_balance_details` | 313 | ❌ | — |
| method | `FlexBalanceService.deduct` | 327 | ❌ | — |
| method | `FlexBalanceService.topup` | 408 | ❌ | — |
| method | `FlexBalanceService.configure_auto_topup` | 492 | ❌ | — |
| method | `FlexBalanceService.get_status` | 621 | ❌ | — |

### `src/sovyx/cloud/license.py` — 4 ✅ / 12 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `LicenseStatus` | 81 | ✅ | docs/modules/cloud.md |
| class | `LicenseClaims` | 91 | ✅ | docs/modules/cloud.md |
| method | `LicenseClaims.account_id` | 103 | ❌ | — |
| method | `LicenseClaims.is_refresh_due` | 108 | ❌ | — |
| method | `LicenseClaims.seconds_until_expiry` | 113 | ❌ | — |
| class | `LicenseInfo` | 119 | ✅ | docs/modules/cloud.md |
| method | `LicenseInfo.is_valid` | 130 | ❌ | — |
| class | `LicenseService` | 139 | ✅ | docs/modules/cloud.md |
| method | `LicenseService.public_key` | 176 | ❌ | — |
| method | `LicenseService.current_token` | 181 | ❌ | — |
| method | `LicenseService.issue_license` | 185 | ❌ | — |
| method | `LicenseService.validate` | 226 | ❌ | — |
| method | `LicenseService.is_valid` | 319 | ❌ | — |
| method | `LicenseService.set_token` | 330 | ❌ | — |
| method | `LicenseService.start_refresh` | 344 | ❌ | — |
| method | `LicenseService.stop_refresh` | 362 | ❌ | — |

### `src/sovyx/cloud/llm_proxy.py` — 13 ✅ / 20 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `RateTier` | 36 | ✅ | docs/modules/cloud.md |
| class | `ProxyConfig` | 75 | ✅ | docs/modules/cloud.md |
| class | `UsageRecord` | 98 | ❌ | — |
| method | `UsageRecord.total_tokens` | 126 | ❌ | — |
| class | `ProxyResponse` | 132 | ✅ | docs/modules/cloud.md |
| method | `ProxyResponse.total_tokens` | 156 | ❌ | — |
| class | `MeteringSnapshot` | 162 | ✅ | docs/modules/cloud.md |
| class | `ProxyError` | 189 | ✅ | docs/modules/cloud.md |
| class | `RateLimitExceededError` | 193 | ✅ | docs/modules/cloud.md |
| class | `ModelNotFoundError` | 205 | ✅ | docs/modules/cloud.md |
| class | `AllProvidersFailedError` | 213 | ✅ | docs/modules/cloud.md |
| class | `MeteringStore` | 294 | ❌ | — |
| method | `MeteringStore.record` | 297 | ✅ | docs/modules/llm.md |
| method | `MeteringStore.get_snapshot` | 305 | ❌ | — |
| method | `MeteringStore.get_daily_tokens` | 317 | ❌ | — |
| class | `InMemoryMeteringStore` | 330 | ❌ | — |
| method | `InMemoryMeteringStore.record` | 336 | ✅ | docs/modules/llm.md |
| method | `InMemoryMeteringStore.get_snapshot` | 344 | ❌ | — |
| method | `InMemoryMeteringStore.get_daily_tokens` | 383 | ❌ | — |
| method | `InMemoryMeteringStore.records` | 401 | ❌ | — |
| class | `LLMProviderBackend` | 409 | ✅ | docs/modules/cloud.md |
| method | `LLMProviderBackend.completion` | 415 | ❌ | — |
| class | `LiteLLMBackend` | 444 | ✅ | docs/modules/cloud.md |
| method | `LiteLLMBackend.completion` | 447 | ❌ | — |
| class | `LLMProxyService` | 519 | ✅ | docs/modules/cloud.md |
| method | `LLMProxyService.config` | 547 | ❌ | — |
| method | `LLMProxyService.metering` | 552 | ❌ | — |
| method | `LLMProxyService.rate_limiter` | 557 | ❌ | — |
| method | `LLMProxyService.resolve_model` | 561 | ❌ | — |
| method | `LLMProxyService.get_fallbacks` | 583 | ❌ | — |
| method | `LLMProxyService.route_request` | 594 | ❌ | — |
| method | `LLMProxyService.get_usage` | 722 | ❌ | — |
| method | `LLMProxyService.get_daily_tokens` | 735 | ❌ | — |

### `src/sovyx/cloud/scheduler.py` — 5 ✅ / 17 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `RetentionPolicy` | 43 | ✅ | docs/modules/cloud.md |
| method | `RetentionPolicy.apply` | 71 | ❌ | — |
| class | `RetentionResult` | 157 | ❌ | — |
| class | `ScheduleTier` | 174 | ✅ | docs/modules/cloud.md |
| class | `TierSchedule` | 183 | ✅ | docs/modules/cloud.md |
| class | `SchedulerCallback` | 228 | ❌ | — |
| method | `SchedulerCallback.on_backup_completed` | 231 | ❌ | — |
| method | `SchedulerCallback.on_backup_failed` | 235 | ❌ | — |
| method | `SchedulerCallback.on_prune_completed` | 239 | ❌ | — |
| class | `BackupScheduler` | 265 | ✅ | docs/modules/cloud.md |
| method | `BackupScheduler.tier` | 309 | ❌ | — |
| method | `BackupScheduler.schedule` | 314 | ❌ | — |
| method | `BackupScheduler.is_running` | 319 | ❌ | — |
| method | `BackupScheduler.last_backup_at` | 324 | ❌ | — |
| method | `BackupScheduler.consecutive_failures` | 329 | ❌ | — |
| method | `BackupScheduler.start` | 333 | ❌ | — |
| method | `BackupScheduler.stop` | 351 | ✅ | docs/modules/llm.md |
| method | `BackupScheduler.should_backup` | 365 | ❌ | — |
| method | `BackupScheduler.run_once` | 413 | ❌ | — |
| method | `BackupScheduler.update_tier` | 526 | ❌ | — |
| method | `BackupScheduler.record_last_backup` | 536 | ❌ | — |
| method | `BackupScheduler.status` | 544 | ❌ | — |

### `src/sovyx/cloud/usage.py` — 5 ✅ / 16 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `UsageTier` | 46 | ✅ | docs/modules/cloud.md |
| class | `CascadeStage` | 71 | ✅ | docs/modules/cloud.md |
| class | `ChargeResult` | 84 | ✅ | docs/modules/cloud.md |
| class | `AccountUsage` | 103 | ✅ | docs/modules/cloud.md |
| class | `FlexAccount` | 118 | ❌ | — |
| class | `UsageStore` | 137 | ❌ | — |
| method | `UsageStore.get_usage` | 140 | ❌ | — |
| method | `UsageStore.save_usage` | 144 | ❌ | — |
| method | `UsageStore.get_flex` | 148 | ❌ | — |
| method | `UsageStore.save_flex` | 152 | ❌ | — |
| class | `AutoTopupCharger` | 157 | ❌ | — |
| method | `AutoTopupCharger.charge` | 163 | ❌ | — |
| class | `InMemoryUsageStore` | 171 | ❌ | — |
| method | `InMemoryUsageStore.get_usage` | 178 | ❌ | — |
| method | `InMemoryUsageStore.save_usage` | 182 | ❌ | — |
| method | `InMemoryUsageStore.get_flex` | 186 | ❌ | — |
| method | `InMemoryUsageStore.save_flex` | 190 | ❌ | — |
| class | `UsageCascade` | 198 | ✅ | docs/modules/cloud.md |
| method | `UsageCascade.charge` | 223 | ❌ | — |
| method | `UsageCascade.get_account_status` | 355 | ❌ | — |
| method | `UsageCascade.reset_period` | 377 | ❌ | — |

### `src/sovyx/cognitive/act.py` — 5 ✅ / 2 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `ActionResult` | 34 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/data-flow.md, docs/modules/cognitive.md |
| class | `ToolExecutor` | 52 | ✅ | docs/modules/cognitive.md |
| method | `ToolExecutor.max_depth` | 69 | ❌ | — |
| method | `ToolExecutor.has_tools` | 74 | ❌ | — |
| method | `ToolExecutor.execute` | 80 | ✅ | docs/modules/persistence.md |
| class | `ActPhase` | 131 | ✅ | docs/architecture/cognitive-loop.md, docs/modules/bridge.md, docs/modules/cognitive.md +2 |
| method | `ActPhase.process` | 270 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/llm-router.md |

### `src/sovyx/cognitive/attend.py` — 2 ✅ / 0 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `AttendPhase` | 40 | ✅ | docs/architecture/cognitive-loop.md, docs/modules/cognitive.md, docs/_meta/gap-analysis.md |
| method | `AttendPhase.process` | 72 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/llm-router.md |

### `src/sovyx/cognitive/audit_store.py` — 4 ✅ / 4 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `AuditQueryResult` | 59 | ❌ | — |
| class | `AuditStore` | 66 | ✅ | docs/modules/cognitive.md |
| method | `AuditStore.append` | 90 | ✅ | docs/modules/context.md, docs/modules/mind.md, docs/modules/persistence.md |
| method | `AuditStore.flush` | 101 | ✅ | docs/security/obsidian-protocol.md |
| method | `AuditStore.query` | 136 | ❌ | — |
| method | `AuditStore.count` | 195 | ❌ | — |
| method | `AuditStore.close` | 209 | ✅ | docs/modules/cli.md, docs/modules/persistence.md, docs/development/testing.md |
| function | `get_audit_store` | 217 | ❌ | — |

### `src/sovyx/cognitive/custom_rules.py` — 0 ✅ / 4 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `CustomRuleMatch` | 40 | ❌ | — |
| function | `check_custom_rules` | 79 | ❌ | — |
| function | `check_banned_topics` | 124 | ❌ | — |
| function | `clear_compiled_cache` | 167 | ❌ | — |

### `src/sovyx/cognitive/financial_gate.py` — 3 ✅ / 14 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `PendingConfirmation` | 101 | ✅ | docs/modules/cognitive.md |
| method | `PendingConfirmation.expired` | 117 | ❌ | — |
| class | `FinancialGateState` | 123 | ❌ | — |
| method | `FinancialGateState.add` | 131 | ✅ | docs/modules/observability.md |
| method | `FinancialGateState.get_pending` | 135 | ❌ | — |
| method | `FinancialGateState.confirm` | 151 | ❌ | — |
| method | `FinancialGateState.cancel_all` | 155 | ❌ | — |
| function | `is_confirmation` | 178 | ❌ | — |
| function | `is_cancellation` | 183 | ❌ | — |
| function | `classify_intent_llm` | 199 | ❌ | — |
| function | `classify_intent` | 235 | ❌ | — |
| class | `FinancialGate` | 257 | ✅ | docs/modules/bridge.md, docs/modules/cognitive.md |
| method | `FinancialGate.state` | 273 | ❌ | — |
| method | `FinancialGate.check_tool_call` | 277 | ❌ | — |
| method | `FinancialGate.handle_user_response` | 309 | ❌ | — |
| method | `FinancialGate.handle_user_response_async` | 337 | ❌ | — |
| method | `FinancialGate.has_pending` | 406 | ❌ | — |

### `src/sovyx/cognitive/gate.py` — 4 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `CognitiveRequest` | 28 | ✅ | docs/modules/cognitive.md |
| class | `CogLoopGate` | 42 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/data-flow.md, docs/architecture/overview.md +5 |
| method | `CogLoopGate.submit` | 59 | ✅ | docs/architecture/data-flow.md, docs/modules/cognitive.md |
| method | `CogLoopGate.start` | 96 | ❌ | — |
| method | `CogLoopGate.stop` | 102 | ✅ | docs/modules/llm.md |

### `src/sovyx/cognitive/injection_tracker.py` — 3 ✅ / 7 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `InjectionVerdict` | 45 | ✅ | docs/modules/cognitive.md, docs/security/best-practices.md |
| class | `ScoredMessage` | 54 | ❌ | — |
| class | `InjectionAnalysis` | 63 | ❌ | — |
| class | `SuspicionSignal` | 81 | ❌ | — |
| class | `InjectionContextTracker` | 294 | ✅ | docs/modules/cognitive.md |
| method | `InjectionContextTracker.analyze` | 322 | ❌ | — |
| method | `InjectionContextTracker.reset_conversation` | 393 | ❌ | — |
| method | `InjectionContextTracker.get_conversation_score` | 397 | ❌ | — |
| method | `InjectionContextTracker.clear` | 404 | ✅ | docs/development/testing.md |
| function | `get_injection_tracker` | 448 | ❌ | — |

### `src/sovyx/cognitive/loop.py` — 3 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `CognitiveLoop` | 57 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/data-flow.md, docs/modules/brain.md +6 |
| method | `CognitiveLoop.start` | 84 | ❌ | — |
| method | `CognitiveLoop.stop` | 88 | ✅ | docs/modules/llm.md |
| method | `CognitiveLoop.process_request` | 92 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/data-flow.md, docs/modules/cognitive.md |

### `src/sovyx/cognitive/output_guard.py` — 2 ✅ / 2 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `OutputFilterResult` | 51 | ❌ | — |
| class | `OutputGuard` | 72 | ✅ | docs/modules/cognitive.md |
| method | `OutputGuard.check` | 91 | ✅ | docs/modules/benchmarks.md, docs/security/obsidian-protocol.md |
| method | `OutputGuard.check_async` | 129 | ❌ | — |

### `src/sovyx/cognitive/perceive.py` — 4 ✅ / 0 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `Perception` | 48 | ✅ | docs/architecture/cognitive-loop.md, docs/modules/bridge.md, docs/modules/cognitive.md |
| class | `PerceivePhase` | 62 | ✅ | docs/architecture/cognitive-loop.md, docs/modules/cognitive.md, docs/_meta/gap-analysis.md |
| method | `PerceivePhase.process` | 73 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/llm-router.md |
| method | `PerceivePhase.classify_complexity` | 113 | ✅ | docs/architecture/llm-router.md, docs/modules/cognitive.md, docs/modules/llm.md +2 |

### `src/sovyx/cognitive/pii_guard.py` — 3 ✅ / 2 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `PIIPattern` | 37 | ✅ | docs/modules/cognitive.md |
| class | `PIIFilterResult` | 235 | ❌ | — |
| class | `PIIGuard` | 273 | ✅ | docs/modules/cognitive.md |
| method | `PIIGuard.check` | 291 | ✅ | docs/modules/benchmarks.md, docs/security/obsidian-protocol.md |
| method | `PIIGuard.check_async` | 359 | ❌ | — |

### `src/sovyx/cognitive/reflect.py` — 3 ✅ / 6 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `ExtractedConcept` | 30 | ✅ | docs/architecture/cognitive-loop.md, docs/modules/cognitive.md |
| function | `resolve_category` | 381 | ❌ | — |
| function | `get_importance` | 396 | ❌ | — |
| function | `get_source_confidence` | 408 | ❌ | — |
| function | `detect_explicit_importance` | 424 | ❌ | — |
| function | `compute_episode_importance` | 442 | ❌ | — |
| function | `clamp_sentiment` | 492 | ❌ | — |
| class | `ReflectPhase` | 504 | ✅ | docs/architecture/cognitive-loop.md, docs/modules/cognitive.md, docs/_meta/gap-analysis.md |
| method | `ReflectPhase.process` | 523 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/llm-router.md |

### `src/sovyx/cognitive/safety_audit.py` — 5 ✅ / 6 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `FilterDirection` | 28 | ✅ | docs/modules/cognitive.md |
| class | `FilterAction` | 36 | ✅ | docs/modules/cognitive.md, docs/security/obsidian-protocol.md |
| class | `SafetyEvent` | 46 | ❌ | — |
| class | `SafetyStats` | 67 | ❌ | — |
| class | `SafetyAuditTrail` | 78 | ✅ | docs/modules/cognitive.md |
| method | `SafetyAuditTrail.record` | 92 | ✅ | docs/modules/llm.md |
| method | `SafetyAuditTrail.get_stats` | 153 | ❌ | — |
| method | `SafetyAuditTrail.event_count` | 199 | ❌ | — |
| method | `SafetyAuditTrail.clear` | 203 | ✅ | docs/development/testing.md |
| function | `get_audit_trail` | 211 | ❌ | — |
| function | `setup_audit_trail` | 222 | ❌ | — |

### `src/sovyx/cognitive/safety_classifier.py` — 5 ✅ / 18 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `ClassificationBudget` | 85 | ✅ | docs/modules/cognitive.md |
| method | `ClassificationBudget.can_classify` | 99 | ❌ | — |
| method | `ClassificationBudget.record_call` | 106 | ❌ | — |
| method | `ClassificationBudget.calls_this_hour` | 121 | ❌ | — |
| method | `ClassificationBudget.total_calls` | 127 | ❌ | — |
| method | `ClassificationBudget.estimated_cost_usd` | 132 | ❌ | — |
| method | `ClassificationBudget.hourly_cap` | 137 | ❌ | — |
| method | `ClassificationBudget.set_cap` | 141 | ❌ | — |
| function | `get_classification_budget` | 149 | ❌ | — |
| class | `SafetyCategory` | 160 | ✅ | docs/modules/cognitive.md |
| class | `SafetyVerdict` | 178 | ❌ | — |
| class | `ClassificationCache` | 216 | ✅ | docs/modules/cognitive.md |
| method | `ClassificationCache.get` | 243 | ✅ | docs/architecture/data-flow.md, docs/modules/benchmarks.md, docs/modules/engine.md +3 |
| method | `ClassificationCache.put` | 260 | ❌ | — |
| method | `ClassificationCache.hit_rate` | 273 | ❌ | — |
| method | `ClassificationCache.size` | 281 | ❌ | — |
| method | `ClassificationCache.clear` | 285 | ✅ | docs/development/testing.md |
| function | `get_classification_cache` | 295 | ❌ | — |
| function | `classify_content` | 359 | ❌ | — |
| class | `BatchClassificationResult` | 523 | ❌ | — |
| function | `batch_classify_content` | 539 | ❌ | — |
| class | `CacheStats` | 671 | ❌ | — |
| function | `get_cache_stats` | 691 | ❌ | — |

### `src/sovyx/cognitive/safety_container.py` — 2 ✅ / 4 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `SafetyContainer` | 72 | ✅ | docs/modules/cognitive.md |
| method | `SafetyContainer.reset` | 101 | ✅ | docs/modules/cognitive.md |
| method | `SafetyContainer.for_testing` | 113 | ❌ | — |
| function | `get_safety_container` | 153 | ❌ | — |
| function | `set_safety_container` | 165 | ❌ | — |
| function | `reset_safety_container` | 182 | ❌ | — |

### `src/sovyx/cognitive/safety_escalation.py` — 3 ✅ / 5 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `EscalationLevel` | 37 | ✅ | docs/modules/cognitive.md, docs/security/best-practices.md |
| class | `SourceState` | 47 | ❌ | — |
| class | `SafetyEscalationTracker` | 60 | ✅ | docs/modules/cognitive.md |
| method | `SafetyEscalationTracker.record_block` | 75 | ❌ | — |
| method | `SafetyEscalationTracker.get_level` | 165 | ❌ | — |
| method | `SafetyEscalationTracker.is_rate_limited` | 180 | ❌ | — |
| method | `SafetyEscalationTracker.clear` | 185 | ✅ | docs/development/testing.md |
| function | `get_escalation_tracker` | 193 | ❌ | — |

### `src/sovyx/cognitive/safety_i18n.py` — 0 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `get_safety_message` | 122 | ❌ | — |

### `src/sovyx/cognitive/safety_notifications.py` — 5 ✅ / 6 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `NotificationSink` | 32 | ❌ | — |
| method | `NotificationSink.send` | 35 | ✅ | docs/modules/bridge.md |
| class | `LogNotificationSink` | 40 | ✅ | docs/modules/cognitive.md |
| method | `LogNotificationSink.send` | 43 | ✅ | docs/modules/bridge.md |
| class | `SafetyAlert` | 49 | ❌ | — |
| class | `SafetyNotifier` | 65 | ✅ | docs/modules/cognitive.md |
| method | `SafetyNotifier.notify_escalation` | 83 | ❌ | — |
| method | `SafetyNotifier.alert_count` | 131 | ❌ | — |
| method | `SafetyNotifier.clear` | 135 | ✅ | docs/development/testing.md |
| function | `get_notifier` | 144 | ❌ | — |
| function | `setup_notifier` | 155 | ❌ | — |

### `src/sovyx/cognitive/safety_patterns.py` — 3 ✅ / 5 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `PatternCategory` | 35 | ✅ | docs/modules/cognitive.md |
| class | `FilterTier` | 52 | ✅ | docs/modules/cognitive.md |
| class | `SafetyPattern` | 61 | ✅ | docs/modules/cognitive.md |
| class | `FilterMatch` | 1070 | ❌ | — |
| function | `resolve_patterns` | 1090 | ❌ | — |
| function | `check_content` | 1110 | ❌ | — |
| function | `get_pattern_count` | 1147 | ❌ | — |
| function | `get_tier_counts` | 1155 | ❌ | — |

### `src/sovyx/cognitive/shadow_mode.py` — 0 ✅ / 6 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `CompiledShadowPattern` | 45 | ❌ | — |
| class | `ShadowMatch` | 64 | ❌ | — |
| function | `compile_shadow_patterns` | 88 | ❌ | — |
| function | `invalidate_cache` | 148 | ❌ | — |
| function | `evaluate_shadow` | 155 | ❌ | — |
| function | `get_shadow_stats` | 262 | ❌ | — |

### `src/sovyx/cognitive/state.py` — 3 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `CognitiveStateMachine` | 25 | ✅ | docs/architecture/cognitive-loop.md, docs/modules/cognitive.md, docs/_meta/gap-analysis.md |
| method | `CognitiveStateMachine.current` | 36 | ✅ | docs/development/anti-patterns.md |
| method | `CognitiveStateMachine.transition` | 40 | ❌ | — |
| method | `CognitiveStateMachine.reset` | 64 | ✅ | docs/modules/cognitive.md |

### `src/sovyx/cognitive/text_normalizer.py` — 0 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `normalize_text` | 187 | ❌ | — |

### `src/sovyx/cognitive/think.py` — 2 ✅ / 0 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `ThinkPhase` | 21 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/llm-router.md, docs/modules/cognitive.md +4 |
| method | `ThinkPhase.process` | 46 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/llm-router.md |

### `src/sovyx/context/assembler.py` — 3 ✅ / 0 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `AssembledContext` | 29 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/data-flow.md, docs/modules/context.md +1 |
| class | `ContextAssembler` | 39 | ✅ | docs/architecture/brain-graph.md, docs/architecture/cognitive-loop.md, docs/architecture/data-flow.md +11 |
| method | `ContextAssembler.assemble` | 67 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/data-flow.md, docs/architecture/llm-router.md +1 |

### `src/sovyx/context/budget.py` — 4 ✅ / 0 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `TokenBudgetError` | 24 | ✅ | docs/modules/context.md |
| class | `TokenBudget` | 29 | ✅ | docs/modules/context.md |
| class | `TokenBudgetManager` | 41 | ✅ | docs/modules/context.md, docs/_meta/gap-analysis.md |
| method | `TokenBudgetManager.allocate` | 55 | ✅ | docs/modules/context.md |

### `src/sovyx/context/formatter.py` — 4 ✅ / 2 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `ContextFormatter` | 35 | ✅ | docs/modules/context.md, docs/_meta/gap-analysis.md |
| method | `ContextFormatter.format_concept` | 41 | ❌ | — |
| method | `ContextFormatter.format_episode` | 79 | ❌ | — |
| method | `ContextFormatter.format_concepts_block` | 96 | ✅ | docs/modules/context.md |
| method | `ContextFormatter.format_episodes_block` | 129 | ✅ | docs/modules/context.md |
| method | `ContextFormatter.format_temporal` | 153 | ✅ | docs/modules/context.md |

### `src/sovyx/context/tokenizer.py` — 2 ✅ / 3 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `TokenCounter` | 24 | ✅ | docs/modules/context.md, docs/_meta/gap-analysis.md |
| method | `TokenCounter.count` | 45 | ❌ | — |
| method | `TokenCounter.count_messages` | 58 | ✅ | docs/modules/context.md |
| method | `TokenCounter.truncate` | 76 | ❌ | — |
| method | `TokenCounter.fits` | 108 | ❌ | — |

### `src/sovyx/dashboard/_shared.py` — 0 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `get_active_mind_id` | 19 | ❌ | — |

### `src/sovyx/dashboard/activity.py` — 0 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `get_activity_timeline` | 295 | ❌ | — |

### `src/sovyx/dashboard/brain.py` — 1 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `get_brain_graph` | 26 | ❌ | — |
| function | `search_brain` | 126 | ✅ | docs/research/llm-landscape.md |

### `src/sovyx/dashboard/chat.py` — 1 ✅ / 0 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `handle_chat_message` | 98 | ✅ | docs/modules/dashboard.md |

### `src/sovyx/dashboard/config.py` — 1 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `get_config` | 38 | ✅ | docs/modules/dashboard.md |
| function | `apply_config` | 114 | ❌ | — |

### `src/sovyx/dashboard/conversations.py` — 2 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `list_conversations` | 80 | ✅ | docs/modules/dashboard.md |
| function | `get_conversation_messages` | 124 | ✅ | docs/modules/dashboard.md |
| function | `count_active_conversations` | 161 | ❌ | — |

### `src/sovyx/dashboard/daily_stats.py` — 1 ✅ / 4 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `DailyStatsRecorder` | 27 | ✅ | docs/modules/dashboard.md |
| method | `DailyStatsRecorder.snapshot_day` | 36 | ❌ | — |
| method | `DailyStatsRecorder.get_history` | 103 | ❌ | — |
| method | `DailyStatsRecorder.get_totals` | 152 | ❌ | — |
| method | `DailyStatsRecorder.get_month_totals` | 190 | ❌ | — |

### `src/sovyx/dashboard/events.py` — 1 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `DashboardEventBridge` | 35 | ✅ | docs/architecture/data-flow.md, docs/modules/dashboard.md |
| method | `DashboardEventBridge.subscribe_all` | 47 | ❌ | — |

### `src/sovyx/dashboard/export_import.py` — 0 ✅ / 2 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `export_mind` | 23 | ❌ | — |
| function | `import_mind` | 88 | ❌ | — |

### `src/sovyx/dashboard/logs.py` — 0 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `query_logs` | 21 | ❌ | — |

### `src/sovyx/dashboard/plugins.py` — 0 ✅ / 3 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `get_plugins_status` | 92 | ❌ | — |
| function | `get_plugin_detail` | 191 | ❌ | — |
| function | `get_tools_list` | 246 | ❌ | — |

### `src/sovyx/dashboard/rate_limit.py` — 1 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `RateLimitMiddleware` | 108 | ✅ | docs/modules/dashboard.md |
| method | `RateLimitMiddleware.dispatch` | 115 | ❌ | — |

### `src/sovyx/dashboard/server.py` — 9 ✅ / 6 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `ConnectionManager` | 73 | ✅ | docs/modules/dashboard.md |
| method | `ConnectionManager.connect` | 80 | ✅ | docs/modules/cli.md, docs/modules/persistence.md |
| method | `ConnectionManager.disconnect` | 87 | ❌ | — |
| method | `ConnectionManager.broadcast` | 94 | ✅ | docs/modules/dashboard.md |
| method | `ConnectionManager.active_count` | 120 | ❌ | — |
| class | `RequestIdMiddleware` | 128 | ✅ | docs/modules/dashboard.md, docs/security/obsidian-protocol.md |
| method | `RequestIdMiddleware.dispatch` | 137 | ❌ | — |
| class | `SecurityHeadersMiddleware` | 155 | ✅ | docs/modules/dashboard.md, docs/security/obsidian-protocol.md, docs/security/threat-model.md |
| method | `SecurityHeadersMiddleware.dispatch` | 166 | ❌ | — |
| function | `create_app` | 207 | ✅ | docs/architecture/data-flow.md, docs/modules/dashboard.md, docs/security/best-practices.md +4 |
| class | `DashboardServer` | 1813 | ✅ | docs/modules/dashboard.md |
| method | `DashboardServer.app` | 2051 | ✅ | docs/security/best-practices.md |
| method | `DashboardServer.ws_manager` | 2056 | ❌ | — |
| method | `DashboardServer.start` | 2063 | ❌ | — |
| method | `DashboardServer.stop` | 2130 | ✅ | docs/modules/llm.md |

### `src/sovyx/dashboard/settings.py` — 1 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `get_settings` | 25 | ✅ | docs/modules/dashboard.md |
| function | `apply_settings` | 40 | ❌ | — |

### `src/sovyx/dashboard/status.py` — 5 ✅ / 8 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `StatusSnapshot` | 27 | ✅ | docs/modules/dashboard.md |
| method | `StatusSnapshot.to_dict` | 45 | ✅ | docs/modules/benchmarks.md, docs/modules/dashboard.md |
| class | `DashboardCounters` | 65 | ✅ | docs/modules/dashboard.md |
| method | `DashboardCounters.record_llm_call` | 94 | ❌ | — |
| method | `DashboardCounters.record_message` | 103 | ❌ | — |
| method | `DashboardCounters.snapshot` | 110 | ❌ | — |
| method | `DashboardCounters.consume_pending_day_snapshot` | 116 | ❌ | — |
| method | `DashboardCounters.persist` | 155 | ❌ | — |
| method | `DashboardCounters.restore` | 191 | ❌ | — |
| function | `get_counters` | 260 | ✅ | docs/modules/llm.md |
| function | `configure_timezone` | 265 | ❌ | — |
| class | `StatusCollector` | 286 | ✅ | docs/architecture/data-flow.md, docs/modules/dashboard.md |
| method | `StatusCollector.collect` | 297 | ❌ | — |

### `src/sovyx/dashboard/voice_status.py` — 0 ✅ / 2 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `get_voice_status` | 20 | ❌ | — |
| function | `get_voice_models` | 154 | ❌ | — |

### `src/sovyx/engine/bootstrap.py` — 2 ✅ / 4 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `MindManager` | 21 | ✅ | docs/architecture/overview.md, docs/modules/engine.md, docs/_meta/gap-analysis.md |
| method | `MindManager.load_mind` | 31 | ❌ | — |
| method | `MindManager.start_mind` | 35 | ❌ | — |
| method | `MindManager.stop_mind` | 41 | ❌ | — |
| method | `MindManager.get_active_minds` | 47 | ❌ | — |
| function | `bootstrap` | 52 | ✅ | docs/architecture/data-flow.md, docs/architecture/overview.md, docs/modules/brain.md +10 |

### `src/sovyx/engine/config.py` — 10 ✅ / 3 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `LoggingConfig` | 20 | ✅ | docs/modules/engine.md, docs/modules/observability.md, docs/development/anti-patterns.md |
| class | `DatabaseConfig` | 43 | ✅ | docs/modules/engine.md |
| class | `TelemetryConfig` | 53 | ✅ | docs/modules/engine.md |
| class | `RelayConfig` | 59 | ✅ | docs/modules/engine.md |
| class | `APIConfig` | 65 | ✅ | docs/modules/dashboard.md, docs/modules/engine.md, docs/development/anti-patterns.md +1 |
| class | `HardwareConfig` | 74 | ✅ | docs/modules/engine.md |
| class | `LLMProviderConfig` | 81 | ✅ | docs/modules/engine.md |
| class | `LLMDefaultsConfig` | 93 | ✅ | docs/modules/engine.md |
| class | `SocketConfig` | 105 | ✅ | docs/modules/cli.md, docs/modules/engine.md |
| method | `SocketConfig.resolve_path` | 114 | ❌ | — |
| class | `EngineConfig` | 125 | ✅ | docs/architecture/overview.md, docs/modules/bridge.md, docs/modules/cli.md +6 |
| method | `EngineConfig.resolve_log_file` | 149 | ❌ | — |
| function | `load_engine_config` | 165 | ❌ | — |

### `src/sovyx/engine/degradation.py` — 2 ✅ / 8 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `DegradationLevel` | 20 | ❌ | — |
| class | `ComponentStatus` | 28 | ❌ | — |
| method | `ComponentStatus.to_dict` | 37 | ✅ | docs/modules/benchmarks.md, docs/modules/dashboard.md |
| class | `DegradationManager` | 47 | ✅ | docs/architecture/overview.md, docs/modules/engine.md, docs/security/obsidian-protocol.md +3 |
| method | `DegradationManager.register_fallback` | 69 | ❌ | — |
| method | `DegradationManager.handle_failure` | 84 | ❌ | — |
| method | `DegradationManager.handle_recovery` | 118 | ❌ | — |
| method | `DegradationManager.check_disk_space` | 131 | ❌ | — |
| method | `DegradationManager.level` | 149 | ❌ | — |
| method | `DegradationManager.status` | 164 | ❌ | — |

### `src/sovyx/engine/errors.py` — 22 ✅ / 27 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `SovyxError` | 10 | ✅ | docs/modules/engine.md, docs/development/anti-patterns.md |
| class | `EngineError` | 25 | ✅ | docs/modules/engine.md |
| class | `BootstrapError` | 29 | ✅ | docs/modules/engine.md |
| class | `ShutdownError` | 33 | ✅ | docs/modules/engine.md |
| class | `ServiceNotRegisteredError` | 37 | ✅ | docs/modules/engine.md, docs/development/anti-patterns.md, docs/development/testing.md |
| class | `LifecycleError` | 41 | ✅ | docs/modules/engine.md |
| class | `HealthCheckError` | 45 | ✅ | docs/modules/engine.md |
| class | `ConfigError` | 52 | ✅ | docs/modules/engine.md |
| class | `ConfigNotFoundError` | 56 | ✅ | docs/modules/engine.md |
| class | `ConfigValidationError` | 60 | ✅ | docs/modules/engine.md |
| class | `PersistenceError` | 67 | ✅ | docs/modules/upgrade.md |
| class | `DatabaseConnectionError` | 71 | ✅ | docs/modules/persistence.md |
| class | `MigrationError` | 75 | ✅ | docs/modules/persistence.md, docs/modules/upgrade.md |
| class | `SchemaError` | 79 | ❌ | — |
| class | `TransactionError` | 83 | ❌ | — |
| class | `BrainError` | 90 | ❌ | — |
| class | `ConceptNotFoundError` | 94 | ❌ | — |
| class | `EpisodeNotFoundError` | 114 | ❌ | — |
| class | `EmbeddingError` | 134 | ❌ | — |
| class | `SearchError` | 138 | ❌ | — |
| class | `ConsolidationError` | 142 | ❌ | — |
| class | `CognitiveError` | 149 | ✅ | docs/modules/cognitive.md, docs/modules/engine.md |
| class | `PerceptionError` | 153 | ✅ | docs/modules/engine.md |
| class | `AttentionError` | 157 | ❌ | — |
| class | `ThinkError` | 161 | ❌ | — |
| class | `ActionError` | 165 | ❌ | — |
| class | `ReflectionError` | 169 | ❌ | — |
| class | `LLMError` | 176 | ❌ | — |
| class | `ProviderUnavailableError` | 180 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/llm-router.md, docs/modules/cognitive.md +2 |
| class | `CostLimitExceededError` | 184 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/llm-router.md, docs/modules/cognitive.md +2 |
| class | `CircuitOpenError` | 188 | ❌ | — |
| class | `TokenBudgetExceededError` | 192 | ❌ | — |
| class | `ContextError` | 199 | ❌ | — |
| class | `TokenBudgetError` | 203 | ✅ | docs/modules/context.md |
| class | `ContextAssemblyError` | 207 | ❌ | — |
| class | `BridgeError` | 214 | ❌ | — |
| class | `ChannelConnectionError` | 218 | ✅ | docs/modules/bridge.md, docs/modules/cli.md |
| class | `ChannelSendError` | 222 | ❌ | — |
| class | `MessageRoutingError` | 226 | ❌ | — |
| class | `MindError` | 233 | ❌ | — |
| class | `MindNotFoundError` | 237 | ❌ | — |
| class | `MindConfigError` | 257 | ✅ | docs/modules/mind.md |
| class | `PersonalityError` | 261 | ❌ | — |
| class | `CLIError` | 268 | ❌ | — |
| class | `PluginError` | 275 | ✅ | docs/modules/plugins.md, docs/security/best-practices.md |
| class | `PluginLoadError` | 279 | ❌ | — |
| class | `PluginCrashError` | 283 | ❌ | — |
| class | `CloudError` | 287 | ✅ | docs/modules/cloud.md |
| class | `VoiceError` | 291 | ❌ | — |

### `src/sovyx/engine/events.py` — 33 ✅ / 2 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `EventCategory` | 25 | ✅ | docs/modules/engine.md |
| class | `Event` | 41 | ✅ | docs/architecture/data-flow.md, docs/architecture/overview.md, docs/modules/benchmarks.md +15 |
| method | `Event.category` | 53 | ✅ | docs/modules/engine.md |
| class | `EngineStarted` | 62 | ✅ | docs/architecture/data-flow.md, docs/architecture/overview.md, docs/modules/dashboard.md +1 |
| method | `EngineStarted.category` | 69 | ✅ | docs/modules/engine.md |
| class | `EngineStopping` | 75 | ✅ | docs/architecture/data-flow.md, docs/architecture/overview.md, docs/modules/dashboard.md +1 |
| method | `EngineStopping.category` | 81 | ✅ | docs/modules/engine.md |
| class | `ServiceHealthChanged` | 87 | ✅ | docs/architecture/data-flow.md, docs/architecture/overview.md, docs/modules/dashboard.md +1 |
| method | `ServiceHealthChanged.category` | 95 | ✅ | docs/modules/engine.md |
| class | `PerceptionReceived` | 104 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/data-flow.md, docs/architecture/overview.md +3 |
| method | `PerceptionReceived.category` | 114 | ✅ | docs/modules/engine.md |
| class | `ThinkCompleted` | 120 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/data-flow.md, docs/architecture/llm-router.md +5 |
| method | `ThinkCompleted.category` | 132 | ✅ | docs/modules/engine.md |
| class | `ResponseSent` | 138 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/data-flow.md, docs/architecture/overview.md +4 |
| method | `ResponseSent.category` | 147 | ✅ | docs/modules/engine.md |
| class | `ConceptCreated` | 156 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/data-flow.md, docs/architecture/overview.md +2 |
| method | `ConceptCreated.category` | 166 | ✅ | docs/modules/engine.md |
| class | `EpisodeEncoded` | 172 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/data-flow.md, docs/architecture/overview.md +2 |
| method | `EpisodeEncoded.category` | 180 | ✅ | docs/modules/engine.md |
| class | `ConceptContradicted` | 186 | ✅ | docs/architecture/data-flow.md, docs/architecture/overview.md, docs/modules/brain.md +1 |
| method | `ConceptContradicted.category` | 201 | ✅ | docs/modules/engine.md |
| class | `ConceptForgotten` | 207 | ✅ | docs/architecture/data-flow.md, docs/architecture/overview.md, docs/modules/engine.md |
| method | `ConceptForgotten.category` | 220 | ✅ | docs/modules/engine.md |
| class | `ConsolidationCompleted` | 226 | ✅ | docs/architecture/data-flow.md, docs/architecture/overview.md, docs/modules/brain.md +2 |
| method | `ConsolidationCompleted.category` | 235 | ✅ | docs/modules/engine.md |
| class | `ChannelConnected` | 244 | ✅ | docs/architecture/data-flow.md, docs/architecture/overview.md, docs/modules/bridge.md +2 |
| method | `ChannelConnected.category` | 251 | ✅ | docs/modules/engine.md |
| class | `ChannelDisconnected` | 257 | ✅ | docs/architecture/data-flow.md, docs/architecture/overview.md, docs/modules/bridge.md +2 |
| method | `ChannelDisconnected.category` | 264 | ✅ | docs/modules/engine.md |
| class | `EventBus` | 274 | ✅ | docs/architecture/data-flow.md, docs/architecture/overview.md, docs/modules/brain.md +10 |
| method | `EventBus.subscribe` | 291 | ✅ | docs/development/testing.md |
| method | `EventBus.unsubscribe` | 300 | ❌ | — |
| method | `EventBus.emit` | 311 | ✅ | docs/modules/engine.md |
| method | `EventBus.handler_count` | 343 | ❌ | — |
| method | `EventBus.clear` | 347 | ✅ | docs/development/testing.md |

### `src/sovyx/engine/health.py` — 3 ✅ / 2 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `HealthStatus` | 25 | ✅ | docs/modules/dashboard.md |
| class | `HealthChecker` | 34 | ✅ | docs/architecture/data-flow.md, docs/architecture/overview.md, docs/modules/engine.md +3 |
| method | `HealthChecker.check_all` | 61 | ✅ | docs/modules/benchmarks.md |
| method | `HealthChecker.check_liveness` | 123 | ❌ | — |
| method | `HealthChecker.check_readiness` | 127 | ❌ | — |

### `src/sovyx/engine/lifecycle.py` — 3 ✅ / 4 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `PidLock` | 22 | ✅ | docs/modules/engine.md |
| method | `PidLock.acquire` | 36 | ❌ | — |
| method | `PidLock.release` | 65 | ❌ | — |
| class | `LifecycleManager` | 91 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/overview.md, docs/modules/engine.md +1 |
| method | `LifecycleManager.start` | 115 | ❌ | — |
| method | `LifecycleManager.stop` | 145 | ✅ | docs/modules/llm.md |
| method | `LifecycleManager.run_forever` | 173 | ❌ | — |

### `src/sovyx/engine/protocols.py` — 15 ✅ / 17 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `BrainReader` | 25 | ✅ | docs/modules/brain.md |
| method | `BrainReader.search` | 31 | ❌ | — |
| method | `BrainReader.get_concept` | 37 | ❌ | — |
| method | `BrainReader.recall` | 41 | ✅ | docs/modules/brain.md, docs/modules/context.md |
| method | `BrainReader.get_related` | 47 | ❌ | — |
| class | `BrainWriter` | 53 | ✅ | docs/modules/brain.md |
| method | `BrainWriter.learn_concept` | 59 | ❌ | — |
| method | `BrainWriter.encode_episode` | 81 | ✅ | docs/architecture/data-flow.md, docs/modules/brain.md |
| method | `BrainWriter.strengthen_connection` | 92 | ✅ | docs/modules/brain.md |
| class | `LLMProvider` | 98 | ✅ | docs/architecture/llm-router.md, docs/modules/llm.md, docs/research/llm-landscape.md |
| method | `LLMProvider.name` | 102 | ❌ | — |
| method | `LLMProvider.is_available` | 107 | ❌ | — |
| method | `LLMProvider.supports_model` | 111 | ❌ | — |
| method | `LLMProvider.get_context_window` | 115 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/llm-router.md, docs/modules/llm.md |
| method | `LLMProvider.close` | 119 | ✅ | docs/modules/cli.md, docs/modules/persistence.md, docs/development/testing.md |
| method | `LLMProvider.generate` | 123 | ✅ | docs/modules/cognitive.md, docs/modules/llm.md, docs/modules/observability.md |
| class | `ChannelAdapter` | 136 | ✅ | docs/modules/bridge.md |
| method | `ChannelAdapter.channel_type` | 140 | ❌ | — |
| method | `ChannelAdapter.capabilities` | 145 | ❌ | — |
| method | `ChannelAdapter.format_capabilities` | 150 | ❌ | — |
| method | `ChannelAdapter.initialize` | 154 | ✅ | docs/modules/persistence.md |
| method | `ChannelAdapter.start` | 158 | ❌ | — |
| method | `ChannelAdapter.stop` | 162 | ✅ | docs/modules/llm.md |
| method | `ChannelAdapter.send` | 166 | ✅ | docs/modules/bridge.md |
| method | `ChannelAdapter.edit` | 185 | ❌ | — |
| method | `ChannelAdapter.delete` | 202 | ❌ | — |
| method | `ChannelAdapter.react` | 206 | ❌ | — |
| method | `ChannelAdapter.send_typing` | 210 | ❌ | — |
| class | `Lifecycle` | 216 | ✅ | docs/modules/engine.md, docs/modules/persistence.md |
| method | `Lifecycle.start` | 219 | ❌ | — |
| method | `Lifecycle.stop` | 223 | ✅ | docs/modules/llm.md |
| method | `Lifecycle.is_running` | 228 | ❌ | — |

### `src/sovyx/engine/registry.py` — 4 ✅ / 2 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `ServiceRegistry` | 29 | ✅ | docs/architecture/data-flow.md, docs/architecture/overview.md, docs/modules/dashboard.md +5 |
| method | `ServiceRegistry.register_singleton` | 45 | ✅ | docs/architecture/overview.md, docs/modules/engine.md |
| method | `ServiceRegistry.register_instance` | 64 | ✅ | docs/architecture/overview.md, docs/modules/engine.md |
| method | `ServiceRegistry.resolve` | 85 | ✅ | docs/architecture/overview.md, docs/modules/engine.md |
| method | `ServiceRegistry.is_registered` | 117 | ❌ | — |
| method | `ServiceRegistry.shutdown_all` | 122 | ❌ | — |

### `src/sovyx/engine/rpc_protocol.py` — 2 ✅ / 0 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `rpc_send` | 25 | ✅ | docs/modules/cli.md |
| function | `rpc_recv` | 43 | ✅ | docs/modules/cli.md |

### `src/sovyx/engine/rpc_server.py` — 2 ✅ / 2 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `DaemonRPCServer` | 23 | ✅ | docs/architecture/overview.md, docs/modules/cli.md, docs/modules/engine.md +2 |
| method | `DaemonRPCServer.register_method` | 38 | ❌ | — |
| method | `DaemonRPCServer.start` | 43 | ❌ | — |
| method | `DaemonRPCServer.stop` | 59 | ✅ | docs/modules/llm.md |

### `src/sovyx/engine/types.py` — 6 ✅ / 0 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `generate_id` | 24 | ✅ | docs/modules/brain.md |
| class | `ConceptCategory` | 39 | ✅ | docs/architecture/brain-graph.md, docs/modules/brain.md, docs/research/memory-systems.md |
| class | `RelationType` | 51 | ✅ | docs/architecture/brain-graph.md, docs/modules/brain.md |
| class | `ChannelType` | 63 | ✅ | docs/modules/bridge.md |
| class | `CognitivePhase` | 74 | ✅ | docs/modules/cognitive.md |
| class | `PerceptionType` | 94 | ✅ | docs/architecture/cognitive-loop.md, docs/modules/bridge.md |

### `src/sovyx/llm/circuit.py` — 4 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `CircuitBreaker` | 13 | ✅ | docs/architecture/data-flow.md, docs/architecture/llm-router.md, docs/modules/llm.md |
| method | `CircuitBreaker.state` | 32 | ❌ | — |
| method | `CircuitBreaker.can_call` | 40 | ✅ | docs/modules/llm.md |
| method | `CircuitBreaker.record_success` | 49 | ✅ | docs/modules/llm.md |
| method | `CircuitBreaker.record_failure` | 54 | ✅ | docs/modules/llm.md |

### `src/sovyx/llm/cost.py` — 5 ✅ / 11 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `CostBreakdown` | 44 | ✅ | docs/modules/llm.md |
| class | `CostGuard` | 66 | ✅ | docs/architecture/data-flow.md, docs/architecture/llm-router.md, docs/modules/llm.md |
| method | `CostGuard.restore` | 107 | ❌ | — |
| method | `CostGuard.persist` | 196 | ❌ | — |
| method | `CostGuard.can_afford` | 336 | ✅ | docs/modules/llm.md |
| method | `CostGuard.record` | 362 | ✅ | docs/modules/llm.md |
| method | `CostGuard.record_cost` | 413 | ❌ | — |
| method | `CostGuard.get_daily_spend` | 445 | ❌ | — |
| method | `CostGuard.get_remaining_budget` | 450 | ✅ | docs/modules/llm.md |
| method | `CostGuard.get_conversation_spend` | 455 | ❌ | — |
| method | `CostGuard.get_conversation_remaining` | 459 | ❌ | — |
| method | `CostGuard.get_breakdown` | 464 | ❌ | — |
| method | `CostGuard.get_provider_spend` | 495 | ❌ | — |
| method | `CostGuard.get_mind_spend` | 500 | ❌ | — |
| method | `CostGuard.get_model_spend` | 505 | ❌ | — |
| method | `CostGuard.get_cost_history` | 510 | ❌ | — |

### `src/sovyx/llm/models.py` — 3 ✅ / 0 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `ToolCall` | 9 | ✅ | docs/modules/llm.md, docs/research/llm-landscape.md |
| class | `ToolResult` | 18 | ✅ | docs/architecture/data-flow.md, docs/modules/llm.md, docs/modules/plugins.md |
| class | `LLMResponse` | 28 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/data-flow.md, docs/architecture/llm-router.md +2 |

### `src/sovyx/llm/providers/_shared.py` — 0 ✅ / 8 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `safe_parse_json` | 29 | ❌ | — |
| function | `format_tools_openai` | 96 | ❌ | — |
| function | `format_tools_anthropic` | 119 | ❌ | — |
| function | `format_tools_google` | 137 | ❌ | — |
| function | `parse_tool_calls_openai` | 155 | ❌ | — |
| function | `parse_tool_calls_anthropic` | 181 | ❌ | — |
| function | `parse_tool_calls_google` | 200 | ❌ | — |
| function | `retry_delay` | 220 | ❌ | — |

### `src/sovyx/llm/providers/anthropic.py` — 4 ✅ / 3 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `AnthropicProvider` | 40 | ✅ | docs/modules/llm.md, docs/_meta/gap-analysis.md |
| method | `AnthropicProvider.name` | 50 | ❌ | — |
| method | `AnthropicProvider.is_available` | 55 | ❌ | — |
| method | `AnthropicProvider.supports_model` | 59 | ❌ | — |
| method | `AnthropicProvider.get_context_window` | 63 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/llm-router.md, docs/modules/llm.md |
| method | `AnthropicProvider.close` | 67 | ✅ | docs/modules/cli.md, docs/modules/persistence.md, docs/development/testing.md |
| method | `AnthropicProvider.generate` | 71 | ✅ | docs/modules/cognitive.md, docs/modules/llm.md, docs/modules/observability.md |

### `src/sovyx/llm/providers/google.py` — 4 ✅ / 3 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `GoogleProvider` | 52 | ✅ | docs/modules/llm.md, docs/_meta/gap-analysis.md |
| method | `GoogleProvider.name` | 65 | ❌ | — |
| method | `GoogleProvider.is_available` | 70 | ❌ | — |
| method | `GoogleProvider.supports_model` | 74 | ❌ | — |
| method | `GoogleProvider.get_context_window` | 78 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/llm-router.md, docs/modules/llm.md |
| method | `GoogleProvider.close` | 84 | ✅ | docs/modules/cli.md, docs/modules/persistence.md, docs/development/testing.md |
| method | `GoogleProvider.generate` | 88 | ✅ | docs/modules/cognitive.md, docs/modules/llm.md, docs/modules/observability.md |

### `src/sovyx/llm/providers/ollama.py` — 4 ✅ / 6 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `OllamaProvider` | 43 | ✅ | docs/modules/llm.md, docs/_meta/gap-analysis.md |
| method | `OllamaProvider.name` | 68 | ❌ | — |
| method | `OllamaProvider.base_url` | 73 | ❌ | — |
| method | `OllamaProvider.is_available` | 78 | ❌ | — |
| method | `OllamaProvider.supports_model` | 86 | ❌ | — |
| method | `OllamaProvider.get_context_window` | 96 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/llm-router.md, docs/modules/llm.md |
| method | `OllamaProvider.ping` | 104 | ❌ | — |
| method | `OllamaProvider.list_models` | 136 | ❌ | — |
| method | `OllamaProvider.close` | 166 | ✅ | docs/modules/cli.md, docs/modules/persistence.md, docs/development/testing.md |
| method | `OllamaProvider.generate` | 172 | ✅ | docs/modules/cognitive.md, docs/modules/llm.md, docs/modules/observability.md |

### `src/sovyx/llm/providers/openai.py` — 4 ✅ / 3 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `OpenAIProvider` | 46 | ✅ | docs/modules/llm.md, docs/_meta/gap-analysis.md |
| method | `OpenAIProvider.name` | 56 | ❌ | — |
| method | `OpenAIProvider.is_available` | 61 | ❌ | — |
| method | `OpenAIProvider.supports_model` | 65 | ❌ | — |
| method | `OpenAIProvider.get_context_window` | 69 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/llm-router.md, docs/modules/llm.md |
| method | `OpenAIProvider.close` | 75 | ✅ | docs/modules/cli.md, docs/modules/persistence.md, docs/development/testing.md |
| method | `OpenAIProvider.generate` | 79 | ✅ | docs/modules/cognitive.md, docs/modules/llm.md, docs/modules/observability.md |

### `src/sovyx/llm/router.py` — 10 ✅ / 0 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `ComplexityLevel` | 37 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/llm-router.md, docs/architecture/overview.md +5 |
| class | `ComplexitySignals` | 46 | ✅ | docs/architecture/llm-router.md, docs/modules/llm.md, docs/_meta/gap-analysis.md |
| function | `classify_complexity` | 84 | ✅ | docs/architecture/llm-router.md, docs/modules/cognitive.md, docs/modules/llm.md +2 |
| function | `extract_signals` | 128 | ✅ | docs/architecture/llm-router.md, docs/modules/llm.md |
| function | `select_model_for_complexity` | 150 | ✅ | docs/architecture/llm-router.md, docs/modules/llm.md |
| class | `LLMRouter` | 176 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/data-flow.md, docs/architecture/llm-router.md +5 |
| method | `LLMRouter.get_context_window` | 203 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/llm-router.md, docs/modules/llm.md |
| method | `LLMRouter.generate` | 221 | ✅ | docs/modules/cognitive.md, docs/modules/llm.md, docs/modules/observability.md |
| method | `LLMRouter.tool_definitions_to_dicts` | 416 | ✅ | docs/modules/llm.md |
| method | `LLMRouter.stop` | 512 | ✅ | docs/modules/llm.md |

### `src/sovyx/mind/config.py` — 21 ✅ / 6 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `PersonalityConfig` | 24 | ✅ | docs/modules/mind.md, docs/_meta/gap-analysis.md |
| class | `OceanConfig` | 36 | ✅ | docs/modules/mind.md |
| class | `LLMConfig` | 46 | ✅ | docs/architecture/llm-router.md, docs/modules/mind.md, docs/_meta/gap-analysis.md |
| method | `LLMConfig.resolve_provider_at_runtime` | 71 | ✅ | docs/modules/mind.md |
| class | `ScoringConfig` | 112 | ✅ | docs/modules/mind.md |
| method | `ScoringConfig.validate_weight_sums` | 133 | ✅ | docs/modules/mind.md |
| class | `BrainConfig` | 157 | ✅ | docs/modules/brain.md, docs/modules/cognitive.md, docs/modules/mind.md +2 |
| class | `TelegramChannelConfig` | 173 | ✅ | docs/modules/mind.md |
| class | `DiscordChannelConfig` | 184 | ✅ | docs/modules/mind.md |
| class | `ChannelsConfig` | 190 | ✅ | docs/modules/mind.md |
| class | `Guardrail` | 197 | ✅ | docs/modules/mind.md |
| class | `CustomRule` | 238 | ✅ | docs/modules/mind.md |
| class | `ShadowPattern` | 254 | ✅ | docs/modules/mind.md, docs/security/obsidian-protocol.md |
| class | `SafetyConfig` | 275 | ✅ | docs/modules/dashboard.md, docs/modules/mind.md, docs/security/obsidian-protocol.md +2 |
| class | `PluginConfigEntry` | 291 | ✅ | docs/modules/mind.md |
| class | `PluginsConfig` | 305 | ✅ | docs/modules/mind.md |
| method | `PluginsConfig.get_effective_enabled` | 324 | ❌ | — |
| method | `PluginsConfig.get_effective_disabled` | 338 | ❌ | — |
| method | `PluginsConfig.get_plugin_config` | 348 | ❌ | — |
| method | `PluginsConfig.get_all_plugin_configs` | 353 | ❌ | — |
| method | `PluginsConfig.get_granted_permissions` | 359 | ❌ | — |
| method | `PluginsConfig.get_all_granted_permissions` | 364 | ❌ | — |
| class | `MindConfig` | 373 | ✅ | docs/architecture/llm-router.md, docs/architecture/overview.md, docs/modules/cognitive.md +7 |
| method | `MindConfig.set_default_id` | 393 | ✅ | docs/modules/mind.md |
| function | `load_mind_config` | 400 | ✅ | docs/modules/mind.md |
| function | `create_default_mind_config` | 447 | ✅ | docs/modules/mind.md |
| function | `validate_plugin_config` | 490 | ✅ | docs/modules/mind.md |

### `src/sovyx/mind/personality.py` — 3 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `PersonalityEngine` | 70 | ✅ | docs/architecture/data-flow.md, docs/architecture/overview.md, docs/modules/context.md +4 |
| method | `PersonalityEngine.config` | 82 | ❌ | — |
| method | `PersonalityEngine.generate_system_prompt` | 86 | ✅ | docs/modules/mind.md |
| method | `PersonalityEngine.get_personality_summary` | 195 | ✅ | docs/modules/mind.md |

### `src/sovyx/observability/alerts.py` — 10 ✅ / 11 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `AlertSeverity` | 59 | ✅ | docs/modules/observability.md |
| class | `AlertState` | 67 | ✅ | docs/modules/observability.md |
| class | `AlertRule` | 78 | ✅ | docs/modules/observability.md |
| class | `Alert` | 101 | ✅ | docs/modules/observability.md |
| class | `AlertFired` | 127 | ✅ | docs/modules/observability.md |
| method | `AlertFired.category` | 138 | ✅ | docs/modules/engine.md |
| class | `AlertResolved` | 144 | ✅ | docs/modules/observability.md |
| method | `AlertResolved.category` | 152 | ✅ | docs/modules/engine.md |
| class | `MetricSample` | 161 | ✅ | docs/modules/observability.md |
| class | `AlertManager` | 176 | ✅ | docs/modules/observability.md, docs/security/best-practices.md, docs/security/obsidian-protocol.md +2 |
| method | `AlertManager.rules` | 214 | ❌ | — |
| method | `AlertManager.states` | 219 | ❌ | — |
| method | `AlertManager.add_rule` | 223 | ❌ | — |
| method | `AlertManager.remove_rule` | 239 | ❌ | — |
| method | `AlertManager.record_metric` | 255 | ❌ | — |
| method | `AlertManager.get_metric_value_in_window` | 266 | ❌ | — |
| method | `AlertManager.evaluate` | 366 | ❌ | — |
| method | `AlertManager.get_firing_alerts` | 464 | ❌ | — |
| method | `AlertManager.get_alert_summary` | 472 | ❌ | — |
| function | `create_default_rules` | 500 | ❌ | — |
| function | `create_default_alert_manager` | 552 | ❌ | — |

### `src/sovyx/observability/health.py` — 27 ✅ / 16 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `CheckStatus` | 33 | ✅ | docs/modules/dashboard.md, docs/modules/observability.md |
| class | `CheckResult` | 42 | ✅ | docs/modules/observability.md |
| method | `CheckResult.ok` | 58 | ❌ | — |
| class | `HealthCheck` | 66 | ✅ | docs/modules/dashboard.md, docs/modules/observability.md, docs/development/testing.md |
| method | `HealthCheck.name` | 71 | ❌ | — |
| method | `HealthCheck.check` | 75 | ✅ | docs/modules/benchmarks.md, docs/security/obsidian-protocol.md |
| class | `HealthRegistry` | 85 | ✅ | docs/modules/cli.md, docs/modules/dashboard.md, docs/modules/engine.md +1 |
| method | `HealthRegistry.register` | 95 | ✅ | docs/modules/cloud.md |
| method | `HealthRegistry.check_count` | 100 | ❌ | — |
| method | `HealthRegistry.run_all` | 104 | ✅ | docs/modules/upgrade.md |
| method | `HealthRegistry.summary` | 135 | ❌ | — |
| class | `DiskSpaceCheck` | 147 | ✅ | docs/modules/observability.md |
| method | `DiskSpaceCheck.name` | 159 | ❌ | — |
| method | `DiskSpaceCheck.check` | 163 | ✅ | docs/modules/benchmarks.md, docs/security/obsidian-protocol.md |
| class | `RAMCheck` | 202 | ✅ | docs/modules/observability.md |
| method | `RAMCheck.name` | 211 | ❌ | — |
| method | `RAMCheck.check` | 215 | ✅ | docs/modules/benchmarks.md, docs/security/obsidian-protocol.md |
| class | `CPUCheck` | 249 | ✅ | docs/modules/observability.md |
| method | `CPUCheck.name` | 258 | ❌ | — |
| method | `CPUCheck.check` | 262 | ✅ | docs/modules/benchmarks.md, docs/security/obsidian-protocol.md |
| class | `DatabaseCheck` | 292 | ✅ | docs/modules/observability.md |
| method | `DatabaseCheck.name` | 303 | ❌ | — |
| method | `DatabaseCheck.check` | 307 | ✅ | docs/modules/benchmarks.md, docs/security/obsidian-protocol.md |
| class | `BrainIndexedCheck` | 330 | ✅ | docs/modules/observability.md |
| method | `BrainIndexedCheck.name` | 338 | ❌ | — |
| method | `BrainIndexedCheck.check` | 342 | ✅ | docs/modules/benchmarks.md, docs/security/obsidian-protocol.md |
| class | `LLMReachableCheck` | 371 | ✅ | docs/modules/observability.md |
| method | `LLMReachableCheck.name` | 379 | ❌ | — |
| method | `LLMReachableCheck.check` | 383 | ✅ | docs/modules/benchmarks.md, docs/security/obsidian-protocol.md |
| class | `ModelLoadedCheck` | 418 | ✅ | docs/modules/observability.md |
| method | `ModelLoadedCheck.name` | 425 | ❌ | — |
| method | `ModelLoadedCheck.check` | 429 | ✅ | docs/modules/benchmarks.md, docs/security/obsidian-protocol.md |
| class | `ChannelConnectedCheck` | 453 | ✅ | docs/modules/observability.md |
| method | `ChannelConnectedCheck.name` | 461 | ❌ | — |
| method | `ChannelConnectedCheck.check` | 465 | ✅ | docs/modules/benchmarks.md, docs/security/obsidian-protocol.md |
| class | `ConsolidationCheck` | 499 | ✅ | docs/modules/observability.md |
| method | `ConsolidationCheck.name` | 507 | ❌ | — |
| method | `ConsolidationCheck.check` | 511 | ✅ | docs/modules/benchmarks.md, docs/security/obsidian-protocol.md |
| class | `CostBudgetCheck` | 540 | ✅ | docs/modules/observability.md |
| method | `CostBudgetCheck.name` | 558 | ❌ | — |
| method | `CostBudgetCheck.check` | 562 | ✅ | docs/modules/benchmarks.md, docs/security/obsidian-protocol.md |
| function | `create_default_registry` | 615 | ❌ | — |
| function | `create_offline_registry` | 650 | ❌ | — |

### `src/sovyx/observability/logging.py` — 5 ✅ / 4 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `bind_request_context` | 38 | ✅ | docs/modules/cognitive.md, docs/modules/observability.md |
| function | `clear_request_context` | 78 | ❌ | — |
| function | `get_request_context` | 87 | ❌ | — |
| function | `bound_request_context` | 93 | ❌ | — |
| function | `set_correlation_id` | 130 | ✅ | docs/modules/engine.md |
| function | `get_correlation_id` | 142 | ❌ | — |
| class | `SecretMasker` | 157 | ✅ | docs/modules/observability.md |
| function | `setup_logging` | 197 | ✅ | docs/modules/engine.md, docs/modules/observability.md, docs/development/anti-patterns.md |
| function | `get_logger` | 316 | ✅ | docs/architecture/cognitive-loop.md, docs/modules/benchmarks.md, docs/modules/engine.md +4 |

### `src/sovyx/observability/metrics.py` — 3 ✅ / 3 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `MetricsRegistry` | 57 | ✅ | docs/modules/observability.md |
| method | `MetricsRegistry.measure_latency` | 188 | ✅ | docs/modules/cognitive.md, docs/modules/observability.md |
| function | `setup_metrics` | 259 | ❌ | — |
| function | `teardown_metrics` | 294 | ❌ | — |
| function | `get_metrics` | 308 | ✅ | docs/modules/observability.md |
| function | `collect_json` | 320 | ❌ | — |

### `src/sovyx/observability/prometheus.py` — 1 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `PrometheusExporter` | 90 | ✅ | docs/modules/observability.md |
| method | `PrometheusExporter.export` | 103 | ❌ | — |

### `src/sovyx/observability/slo.py` — 9 ✅ / 20 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `SLOStatus` | 46 | ✅ | docs/modules/observability.md |
| class | `AlertSeverity` | 54 | ✅ | docs/modules/observability.md |
| class | `SLODefinition` | 66 | ✅ | docs/modules/observability.md |
| method | `SLODefinition.error_budget` | 86 | ❌ | — |
| class | `BurnRateAlertRule` | 92 | ✅ | docs/modules/observability.md |
| method | `BurnRateAlertRule.check` | 104 | ✅ | docs/modules/benchmarks.md, docs/security/obsidian-protocol.md |
| class | `SLOEvent` | 119 | ✅ | docs/modules/observability.md |
| class | `SLOReport` | 134 | ✅ | docs/modules/observability.md |
| class | `SLOTracker` | 214 | ✅ | docs/modules/observability.md |
| method | `SLOTracker.definition` | 237 | ❌ | — |
| method | `SLOTracker.event_count` | 242 | ❌ | — |
| method | `SLOTracker.record_event` | 246 | ❌ | — |
| method | `SLOTracker.error_rate_in_window` | 261 | ❌ | — |
| method | `SLOTracker.success_rate` | 284 | ❌ | — |
| method | `SLOTracker.get_burn_rate` | 295 | ❌ | — |
| method | `SLOTracker.get_error_budget_remaining_pct` | 313 | ❌ | — |
| method | `SLOTracker.check_alerts` | 326 | ❌ | — |
| method | `SLOTracker.get_status` | 345 | ❌ | — |
| method | `SLOTracker.get_report` | 365 | ❌ | — |
| class | `SLOMonitor` | 388 | ✅ | docs/modules/observability.md |
| method | `SLOMonitor.slo_keys` | 414 | ❌ | — |
| method | `SLOMonitor.get_tracker` | 418 | ❌ | — |
| method | `SLOMonitor.record_event` | 432 | ❌ | — |
| method | `SLOMonitor.record_latency` | 456 | ❌ | — |
| method | `SLOMonitor.record_cost` | 469 | ❌ | — |
| method | `SLOMonitor.get_report` | 479 | ❌ | — |
| method | `SLOMonitor.get_breached_slos` | 487 | ❌ | — |
| method | `SLOMonitor.get_active_alerts` | 499 | ❌ | — |
| function | `create_default_monitor` | 516 | ❌ | — |

### `src/sovyx/observability/tracing.py` — 3 ✅ / 6 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `SovyxTracer` | 57 | ✅ | docs/modules/observability.md |
| method | `SovyxTracer.start_cognitive_span` | 71 | ❌ | — |
| method | `SovyxTracer.start_llm_span` | 102 | ✅ | docs/modules/llm.md |
| method | `SovyxTracer.start_brain_span` | 130 | ❌ | — |
| method | `SovyxTracer.start_context_span` | 151 | ❌ | — |
| method | `SovyxTracer.start_span` | 169 | ✅ | docs/modules/cognitive.md |
| function | `setup_tracing` | 196 | ❌ | — |
| function | `teardown_tracing` | 232 | ❌ | — |
| function | `get_tracer` | 241 | ❌ | — |

### `src/sovyx/persistence/datetime_utils.py` — 0 ✅ / 3 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `parse_db_datetime` | 19 | ❌ | — |
| function | `parse_db_datetime` | 23 | ❌ | — |
| function | `parse_db_datetime` | 26 | ❌ | — |

### `src/sovyx/persistence/manager.py` — 3 ✅ / 6 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `DatabaseManager` | 27 | ✅ | docs/architecture/data-flow.md, docs/architecture/overview.md, docs/modules/engine.md +2 |
| method | `DatabaseManager.start` | 49 | ❌ | — |
| method | `DatabaseManager.stop` | 76 | ✅ | docs/modules/llm.md |
| method | `DatabaseManager.initialize_mind_databases` | 95 | ❌ | — |
| method | `DatabaseManager.get_system_pool` | 144 | ❌ | — |
| method | `DatabaseManager.get_brain_pool` | 155 | ✅ | docs/modules/persistence.md |
| method | `DatabaseManager.get_conversation_pool` | 170 | ❌ | — |
| method | `DatabaseManager.has_sqlite_vec` | 186 | ❌ | — |
| method | `DatabaseManager.is_running` | 191 | ❌ | — |

### `src/sovyx/persistence/migrations.py` — 4 ✅ / 3 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `Migration` | 26 | ✅ | docs/architecture/overview.md, docs/modules/brain.md, docs/modules/persistence.md +1 |
| method | `Migration.compute_checksum` | 42 | ✅ | docs/modules/persistence.md |
| class | `MigrationRunner` | 65 | ✅ | docs/modules/persistence.md, docs/modules/upgrade.md |
| method | `MigrationRunner.initialize` | 78 | ✅ | docs/modules/persistence.md |
| method | `MigrationRunner.get_current_version` | 83 | ❌ | — |
| method | `MigrationRunner.run_migrations` | 96 | ❌ | — |
| method | `MigrationRunner.verify_integrity` | 133 | ❌ | — |

### `src/sovyx/persistence/pool.py` — 5 ✅ / 4 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `DatabasePool` | 40 | ✅ | docs/architecture/data-flow.md, docs/architecture/overview.md, docs/modules/brain.md +3 |
| method | `DatabasePool.db_path` | 75 | ❌ | — |
| method | `DatabasePool.initialize` | 79 | ✅ | docs/modules/persistence.md |
| method | `DatabasePool.close` | 106 | ✅ | docs/modules/cli.md, docs/modules/persistence.md, docs/development/testing.md |
| method | `DatabasePool.has_sqlite_vec` | 224 | ❌ | — |
| method | `DatabasePool.read` | 229 | ✅ | docs/modules/plugins.md |
| method | `DatabasePool.write` | 244 | ✅ | docs/modules/plugins.md |
| method | `DatabasePool.transaction` | 258 | ❌ | — |
| method | `DatabasePool.is_initialized` | 276 | ❌ | — |

### `src/sovyx/persistence/schemas/brain.py` — 1 ✅ / 0 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `get_brain_migrations` | 231 | ✅ | docs/research/embedding-strategies.md |

### `src/sovyx/persistence/schemas/conversations.py` — 0 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `get_conversation_migrations` | 69 | ❌ | — |

### `src/sovyx/persistence/schemas/system.py` — 0 ✅ / 1 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| function | `get_system_migrations` | 76 | ❌ | — |

### `src/sovyx/plugins/context.py` — 6 ✅ / 17 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `BrainAccess` | 40 | ✅ | docs/modules/plugins.md |
| method | `BrainAccess.search` | 71 | ❌ | — |
| method | `BrainAccess.find_similar` | 96 | ❌ | — |
| method | `BrainAccess.get_related` | 158 | ❌ | — |
| method | `BrainAccess.search_episodes` | 186 | ✅ | docs/architecture/data-flow.md |
| method | `BrainAccess.learn` | 239 | ❌ | — |
| method | `BrainAccess.forget` | 312 | ❌ | — |
| method | `BrainAccess.forget_all` | 379 | ❌ | — |
| method | `BrainAccess.update` | 424 | ❌ | — |
| method | `BrainAccess.create_relation` | 487 | ❌ | — |
| method | `BrainAccess.get_top_concepts` | 543 | ❌ | — |
| method | `BrainAccess.classify_content` | 597 | ❌ | — |
| method | `BrainAccess.reinforce` | 634 | ❌ | — |
| method | `BrainAccess.boost_importance` | 723 | ❌ | — |
| method | `BrainAccess.get_stats` | 767 | ❌ | — |
| class | `EventBusAccess` | 860 | ✅ | docs/modules/plugins.md |
| method | `EventBusAccess.subscribe` | 883 | ✅ | docs/development/testing.md |
| method | `EventBusAccess.emit` | 903 | ✅ | docs/modules/engine.md |
| method | `EventBusAccess.cleanup` | 918 | ❌ | — |
| method | `EventBusAccess.subscription_count` | 925 | ❌ | — |
| class | `PluginContext` | 934 | ✅ | docs/modules/plugins.md, docs/security/best-practices.md |
| method | `PluginContext.call_tool` | 965 | ❌ | — |
| method | `PluginContext.is_plugin_available` | 993 | ❌ | — |

### `src/sovyx/plugins/events.py` — 10 ✅ / 0 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `PluginStateChanged` | 18 | ✅ | docs/modules/dashboard.md, docs/modules/plugins.md |
| method | `PluginStateChanged.category` | 27 | ✅ | docs/modules/engine.md |
| class | `PluginLoaded` | 33 | ✅ | docs/modules/plugins.md |
| method | `PluginLoaded.category` | 44 | ✅ | docs/modules/engine.md |
| class | `PluginUnloaded` | 50 | ✅ | docs/modules/plugins.md |
| method | `PluginUnloaded.category` | 60 | ✅ | docs/modules/engine.md |
| class | `PluginToolExecuted` | 66 | ✅ | docs/modules/plugins.md |
| method | `PluginToolExecuted.category` | 76 | ✅ | docs/modules/engine.md |
| class | `PluginAutoDisabled` | 82 | ✅ | docs/modules/plugins.md |
| method | `PluginAutoDisabled.category` | 90 | ✅ | docs/modules/engine.md |

### `src/sovyx/plugins/hot_reload.py` — 2 ✅ / 3 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `PluginFileWatcher` | 31 | ✅ | docs/modules/plugins.md |
| method | `PluginFileWatcher.is_running` | 53 | ❌ | — |
| method | `PluginFileWatcher.reload_count` | 58 | ❌ | — |
| method | `PluginFileWatcher.start` | 70 | ❌ | — |
| method | `PluginFileWatcher.stop` | 101 | ✅ | docs/modules/llm.md |

### `src/sovyx/plugins/lifecycle.py` — 3 ✅ / 6 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `PluginState` | 28 | ✅ | docs/modules/plugins.md |
| class | `PluginStateTracker` | 59 | ✅ | docs/modules/plugins.md |
| method | `PluginStateTracker.state` | 90 | ❌ | — |
| method | `PluginStateTracker.error_message` | 95 | ❌ | — |
| method | `PluginStateTracker.history` | 100 | ❌ | — |
| method | `PluginStateTracker.uptime_seconds` | 105 | ❌ | — |
| method | `PluginStateTracker.transition` | 115 | ❌ | — |
| method | `PluginStateTracker.reset_to_discovered` | 158 | ❌ | — |
| class | `InvalidTransitionError` | 200 | ✅ | docs/modules/plugins.md |

### `src/sovyx/plugins/manager.py` — 6 ✅ / 14 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `PluginError` | 49 | ✅ | docs/modules/plugins.md, docs/security/best-practices.md |
| class | `PluginDisabledError` | 53 | ✅ | docs/modules/plugins.md |
| class | `LoadedPlugin` | 68 | ✅ | docs/modules/plugins.md |
| class | `PluginManager` | 129 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/data-flow.md, docs/architecture/overview.md +4 |
| method | `PluginManager.register_class` | 180 | ❌ | — |
| method | `PluginManager.load_all` | 190 | ❌ | — |
| method | `PluginManager.load_single` | 241 | ❌ | — |
| method | `PluginManager.execute` | 370 | ✅ | docs/modules/persistence.md |
| method | `PluginManager.get_tool_definitions` | 642 | ❌ | — |
| method | `PluginManager.get_plugin` | 659 | ❌ | — |
| method | `PluginManager.is_plugin_loaded` | 663 | ❌ | — |
| method | `PluginManager.is_plugin_disabled` | 667 | ❌ | — |
| method | `PluginManager.get_plugin_health` | 672 | ❌ | — |
| method | `PluginManager.re_enable_plugin` | 693 | ❌ | — |
| method | `PluginManager.disable_plugin` | 714 | ❌ | — |
| method | `PluginManager.loaded_plugins` | 737 | ❌ | — |
| method | `PluginManager.plugin_count` | 742 | ❌ | — |
| method | `PluginManager.unload` | 748 | ❌ | — |
| method | `PluginManager.shutdown` | 779 | ✅ | docs/modules/engine.md |
| method | `PluginManager.reload` | 788 | ❌ | — |

### `src/sovyx/plugins/manifest.py` — 7 ✅ / 5 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `NetworkConfig` | 26 | ✅ | docs/modules/plugins.md |
| class | `PluginDependency` | 32 | ✅ | docs/modules/plugins.md |
| class | `EventDeclaration` | 39 | ✅ | docs/modules/plugins.md |
| class | `EventsConfig` | 49 | ✅ | docs/modules/plugins.md |
| class | `ToolDeclaration` | 56 | ✅ | docs/modules/plugins.md |
| class | `PluginManifest` | 66 | ✅ | docs/modules/plugins.md |
| method | `PluginManifest.validate_permissions` | 118 | ❌ | — |
| method | `PluginManifest.validate_name` | 129 | ❌ | — |
| method | `PluginManifest.validate_version` | 141 | ❌ | — |
| method | `PluginManifest.get_permission_enums` | 149 | ❌ | — |
| class | `ManifestError` | 157 | ✅ | docs/modules/plugins.md |
| function | `load_manifest` | 166 | ❌ | — |

### `src/sovyx/plugins/official/calculator.py` — 0 ✅ / 5 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `CalculatorPlugin` | 26 | ❌ | — |
| method | `CalculatorPlugin.name` | 34 | ❌ | — |
| method | `CalculatorPlugin.version` | 38 | ❌ | — |
| method | `CalculatorPlugin.description` | 42 | ❌ | — |
| method | `CalculatorPlugin.calculate` | 51 | ❌ | — |

### `src/sovyx/plugins/official/financial_math.py` — 0 ✅ / 12 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `FinancialMathPlugin` | 256 | ❌ | — |
| method | `FinancialMathPlugin.name` | 268 | ❌ | — |
| method | `FinancialMathPlugin.version` | 272 | ❌ | — |
| method | `FinancialMathPlugin.description` | 276 | ❌ | — |
| method | `FinancialMathPlugin.calculate` | 291 | ❌ | — |
| method | `FinancialMathPlugin.percentage` | 342 | ❌ | — |
| method | `FinancialMathPlugin.interest` | 530 | ❌ | — |
| method | `FinancialMathPlugin.tvm` | 759 | ❌ | — |
| method | `FinancialMathPlugin.amortization` | 1016 | ❌ | — |
| method | `FinancialMathPlugin.portfolio` | 1284 | ❌ | — |
| method | `FinancialMathPlugin.position_size` | 1592 | ❌ | — |
| method | `FinancialMathPlugin.currency` | 1800 | ❌ | — |

### `src/sovyx/plugins/official/knowledge.py` — 0 ✅ / 9 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `KnowledgePlugin` | 134 | ❌ | — |
| method | `KnowledgePlugin.name` | 169 | ❌ | — |
| method | `KnowledgePlugin.version` | 173 | ❌ | — |
| method | `KnowledgePlugin.description` | 177 | ❌ | — |
| method | `KnowledgePlugin.remember` | 183 | ❌ | — |
| method | `KnowledgePlugin.search` | 462 | ❌ | — |
| method | `KnowledgePlugin.forget` | 541 | ❌ | — |
| method | `KnowledgePlugin.recall_about` | 634 | ❌ | — |
| method | `KnowledgePlugin.what_do_you_know` | 729 | ❌ | — |

### `src/sovyx/plugins/official/weather.py` — 0 ✅ / 7 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `WeatherPlugin` | 49 | ❌ | — |
| method | `WeatherPlugin.name` | 60 | ❌ | — |
| method | `WeatherPlugin.version` | 64 | ❌ | — |
| method | `WeatherPlugin.description` | 68 | ❌ | — |
| method | `WeatherPlugin.get_weather` | 72 | ❌ | — |
| method | `WeatherPlugin.get_forecast` | 105 | ❌ | — |
| method | `WeatherPlugin.will_it_rain` | 142 | ❌ | — |

### `src/sovyx/plugins/official/web_intelligence.py` — 4 ✅ / 26 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `CredibilityScore` | 175 | ❌ | — |
| method | `CredibilityScore.to_dict` | 193 | ✅ | docs/modules/benchmarks.md, docs/modules/dashboard.md |
| function | `score_credibility` | 203 | ❌ | — |
| class | `QueryIntent` | 287 | ❌ | — |
| method | `QueryIntent.to_dict` | 305 | ✅ | docs/modules/benchmarks.md, docs/modules/dashboard.md |
| function | `classify_query` | 416 | ❌ | — |
| class | `SearchResult` | 619 | ❌ | — |
| method | `SearchResult.to_dict` | 648 | ✅ | docs/modules/benchmarks.md, docs/modules/dashboard.md |
| class | `SearchBackend` | 667 | ❌ | — |
| method | `SearchBackend.search_text` | 672 | ❌ | — |
| method | `SearchBackend.search_news` | 680 | ❌ | — |
| class | `DuckDuckGoBackend` | 689 | ❌ | — |
| method | `DuckDuckGoBackend.search_text` | 694 | ❌ | — |
| method | `DuckDuckGoBackend.search_news` | 720 | ❌ | — |
| class | `SearXNGBackend` | 749 | ❌ | — |
| method | `SearXNGBackend.search_text` | 757 | ❌ | — |
| method | `SearXNGBackend.search_news` | 765 | ❌ | — |
| class | `BraveBackend` | 821 | ❌ | — |
| method | `BraveBackend.search_text` | 831 | ❌ | — |
| method | `BraveBackend.search_news` | 839 | ❌ | — |
| class | `WebIntelligencePlugin` | 943 | ❌ | — |
| method | `WebIntelligencePlugin.name` | 1025 | ❌ | — |
| method | `WebIntelligencePlugin.version` | 1029 | ❌ | — |
| method | `WebIntelligencePlugin.description` | 1033 | ❌ | — |
| method | `WebIntelligencePlugin.search` | 1049 | ❌ | — |
| method | `WebIntelligencePlugin.fetch` | 1148 | ✅ | docs/development/testing.md |
| method | `WebIntelligencePlugin.research` | 1232 | ❌ | — |
| method | `WebIntelligencePlugin.learn_from_web` | 1402 | ❌ | — |
| method | `WebIntelligencePlugin.recall_web` | 1479 | ❌ | — |
| method | `WebIntelligencePlugin.lookup` | 1554 | ❌ | — |

### `src/sovyx/plugins/permissions.py` — 5 ✅ / 8 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `Permission` | 24 | ✅ | docs/modules/plugins.md, docs/security/best-practices.md, docs/security/obsidian-protocol.md +1 |
| function | `get_risk` | 98 | ❌ | — |
| function | `get_risk_emoji` | 110 | ❌ | — |
| function | `get_description` | 123 | ❌ | — |
| class | `PermissionDeniedError` | 138 | ✅ | docs/modules/plugins.md, docs/security/best-practices.md, docs/security/obsidian-protocol.md +1 |
| class | `PluginAutoDisabledError` | 150 | ✅ | docs/modules/plugins.md |
| class | `PermissionEnforcer` | 165 | ✅ | docs/modules/plugins.md, docs/security/obsidian-protocol.md |
| method | `PermissionEnforcer.plugin_name` | 200 | ❌ | — |
| method | `PermissionEnforcer.granted_permissions` | 205 | ❌ | — |
| method | `PermissionEnforcer.denied_count` | 210 | ❌ | — |
| method | `PermissionEnforcer.is_disabled` | 215 | ❌ | — |
| method | `PermissionEnforcer.check` | 219 | ✅ | docs/modules/benchmarks.md, docs/security/obsidian-protocol.md |
| method | `PermissionEnforcer.has` | 254 | ❌ | — |

### `src/sovyx/plugins/sandbox_fs.py` — 4 ✅ / 6 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `SandboxedFsAccess` | 39 | ✅ | docs/modules/plugins.md, docs/security/best-practices.md, docs/security/obsidian-protocol.md |
| method | `SandboxedFsAccess.read` | 138 | ✅ | docs/modules/plugins.md |
| method | `SandboxedFsAccess.read_bytes` | 159 | ❌ | — |
| method | `SandboxedFsAccess.write` | 180 | ✅ | docs/modules/plugins.md |
| method | `SandboxedFsAccess.write_bytes` | 195 | ❌ | — |
| method | `SandboxedFsAccess.delete` | 224 | ❌ | — |
| method | `SandboxedFsAccess.exists` | 251 | ✅ | docs/modules/cli.md, docs/modules/dashboard.md |
| method | `SandboxedFsAccess.list_dir` | 267 | ❌ | — |
| method | `SandboxedFsAccess.storage_used` | 292 | ❌ | — |
| method | `SandboxedFsAccess.storage_remaining` | 297 | ❌ | — |

### `src/sovyx/plugins/sandbox_http.py` — 3 ✅ / 2 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `SandboxedHttpClient` | 135 | ✅ | docs/modules/plugins.md |
| method | `SandboxedHttpClient.get` | 233 | ✅ | docs/architecture/data-flow.md, docs/modules/benchmarks.md, docs/modules/engine.md +3 |
| method | `SandboxedHttpClient.post` | 249 | ❌ | — |
| method | `SandboxedHttpClient.close` | 289 | ✅ | docs/modules/cli.md, docs/modules/persistence.md, docs/development/testing.md |
| method | `SandboxedHttpClient.remaining_requests` | 294 | ❌ | — |

### `src/sovyx/plugins/sdk.py` — 3 ✅ / 9 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `ToolDefinition` | 29 | ✅ | docs/modules/llm.md, docs/modules/plugins.md |
| method | `ToolDefinition.to_openai_schema` | 53 | ❌ | — |
| method | `ToolDefinition.to_anthropic_schema` | 69 | ❌ | — |
| function | `tool` | 103 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/data-flow.md, docs/architecture/llm-router.md +12 |
| class | `ISovyxPlugin` | 332 | ✅ | docs/modules/plugins.md |
| method | `ISovyxPlugin.name` | 369 | ❌ | — |
| method | `ISovyxPlugin.version` | 379 | ❌ | — |
| method | `ISovyxPlugin.description` | 385 | ❌ | — |
| method | `ISovyxPlugin.permissions` | 390 | ❌ | — |
| method | `ISovyxPlugin.setup` | 407 | ❌ | — |
| method | `ISovyxPlugin.teardown` | 420 | ❌ | — |
| method | `ISovyxPlugin.get_tools` | 429 | ❌ | — |

### `src/sovyx/plugins/security.py` — 3 ✅ / 8 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `SecurityFinding` | 35 | ✅ | docs/modules/plugins.md, docs/security/obsidian-protocol.md |
| class | `PluginSecurityScanner` | 54 | ✅ | docs/modules/plugins.md, docs/security/obsidian-protocol.md |
| method | `PluginSecurityScanner.scan_source` | 135 | ❌ | — |
| method | `PluginSecurityScanner.scan_directory` | 158 | ❌ | — |
| method | `PluginSecurityScanner.has_critical` | 247 | ❌ | — |
| class | `ImportGuard` | 255 | ✅ | docs/architecture/overview.md, docs/modules/plugins.md, docs/security/best-practices.md +3 |
| method | `ImportGuard.find_spec` | 300 | ❌ | — |
| method | `ImportGuard.install` | 332 | ❌ | — |
| method | `ImportGuard.uninstall` | 338 | ❌ | — |
| method | `ImportGuard.denial_count` | 346 | ❌ | — |
| method | `ImportGuard.is_installed` | 351 | ❌ | — |

### `src/sovyx/plugins/testing.py` — 9 ✅ / 32 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `MockBrainAccess` | 37 | ❌ | — |
| method | `MockBrainAccess.seed` | 49 | ❌ | — |
| method | `MockBrainAccess.search` | 57 | ❌ | — |
| method | `MockBrainAccess.learn` | 76 | ❌ | — |
| method | `MockBrainAccess.find_similar` | 104 | ❌ | — |
| method | `MockBrainAccess.classify_content` | 114 | ❌ | — |
| method | `MockBrainAccess.reinforce` | 122 | ❌ | — |
| method | `MockBrainAccess.forget` | 132 | ❌ | — |
| method | `MockBrainAccess.forget_all` | 138 | ❌ | — |
| method | `MockBrainAccess.create_relation` | 147 | ❌ | — |
| method | `MockBrainAccess.boost_importance` | 156 | ❌ | — |
| method | `MockBrainAccess.get_related` | 159 | ❌ | — |
| method | `MockBrainAccess.search_episodes` | 168 | ✅ | docs/architecture/data-flow.md |
| method | `MockBrainAccess.get_stats` | 177 | ❌ | — |
| method | `MockBrainAccess.get_top_concepts` | 186 | ❌ | — |
| method | `MockBrainAccess.update` | 195 | ❌ | — |
| method | `MockBrainAccess.learned_concepts` | 206 | ❌ | — |
| method | `MockBrainAccess.search_history` | 211 | ❌ | — |
| method | `MockBrainAccess.assert_learned` | 215 | ❌ | — |
| method | `MockBrainAccess.assert_searched` | 222 | ❌ | — |
| class | `MockEventBus` | 233 | ❌ | — |
| method | `MockEventBus.emit` | 240 | ✅ | docs/modules/engine.md |
| method | `MockEventBus.emitted_events` | 245 | ❌ | — |
| method | `MockEventBus.assert_emitted` | 249 | ❌ | — |
| method | `MockEventBus.assert_not_emitted` | 256 | ❌ | — |
| method | `MockEventBus.clear` | 263 | ✅ | docs/development/testing.md |
| class | `MockHttpResponse` | 272 | ❌ | — |
| method | `MockHttpResponse.json` | 279 | ✅ | docs/development/testing.md |
| class | `MockHttpClient` | 284 | ❌ | — |
| method | `MockHttpClient.add_response` | 295 | ❌ | — |
| method | `MockHttpClient.get` | 310 | ✅ | docs/architecture/data-flow.md, docs/modules/benchmarks.md, docs/modules/engine.md +3 |
| method | `MockHttpClient.post` | 319 | ❌ | — |
| method | `MockHttpClient.request_history` | 336 | ❌ | — |
| method | `MockHttpClient.assert_called` | 340 | ❌ | — |
| class | `MockFsAccess` | 351 | ❌ | — |
| method | `MockFsAccess.write` | 360 | ✅ | docs/modules/plugins.md |
| method | `MockFsAccess.read` | 364 | ✅ | docs/modules/plugins.md |
| method | `MockFsAccess.exists` | 368 | ✅ | docs/modules/cli.md, docs/modules/dashboard.md |
| method | `MockFsAccess.list_files` | 372 | ❌ | — |
| method | `MockFsAccess.assert_written` | 376 | ❌ | — |
| class | `MockPluginContext` | 386 | ✅ | docs/modules/plugins.md |

### `src/sovyx/upgrade/backup_manager.py` — 5 ✅ / 4 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `BackupTrigger` | 50 | ✅ | docs/modules/upgrade.md |
| class | `BackupInfo` | 59 | ✅ | docs/modules/cloud.md, docs/modules/upgrade.md |
| class | `BackupError` | 77 | ✅ | docs/modules/upgrade.md |
| class | `BackupIntegrityError` | 81 | ✅ | docs/modules/upgrade.md |
| class | `BackupManager` | 88 | ✅ | docs/modules/upgrade.md |
| method | `BackupManager.create_backup` | 118 | ❌ | — |
| method | `BackupManager.list_backups` | 170 | ❌ | — |
| method | `BackupManager.restore_backup` | 195 | ❌ | — |
| method | `BackupManager.prune` | 248 | ❌ | — |

### `src/sovyx/upgrade/blue_green.py` — 9 ✅ / 6 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `UpgradePhase` | 36 | ✅ | docs/modules/upgrade.md |
| class | `UpgradeResult` | 52 | ✅ | docs/modules/upgrade.md |
| method | `UpgradeResult.to_dict` | 79 | ✅ | docs/modules/benchmarks.md, docs/modules/dashboard.md |
| class | `UpgradeError` | 99 | ✅ | docs/modules/upgrade.md |
| class | `InstallError` | 103 | ✅ | docs/modules/upgrade.md |
| class | `VerificationError` | 107 | ✅ | docs/modules/upgrade.md |
| class | `RollbackError` | 111 | ✅ | docs/modules/upgrade.md |
| class | `VersionInstaller` | 118 | ✅ | docs/modules/upgrade.md |
| method | `VersionInstaller.install` | 125 | ❌ | — |
| method | `VersionInstaller.swap` | 136 | ❌ | — |
| method | `VersionInstaller.swap_back` | 143 | ❌ | — |
| method | `VersionInstaller.cleanup` | 150 | ❌ | — |
| class | `BlueGreenUpgrader` | 161 | ✅ | docs/modules/upgrade.md |
| method | `BlueGreenUpgrader.upgrade` | 209 | ❌ | — |
| method | `BlueGreenUpgrader.check_upgrade_available` | 431 | ❌ | — |

### `src/sovyx/upgrade/doctor.py` — 7 ✅ / 11 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `DiagnosticStatus` | 39 | ✅ | docs/modules/upgrade.md |
| class | `DiagnosticResult` | 48 | ✅ | docs/modules/upgrade.md |
| method | `DiagnosticResult.to_dict` | 65 | ✅ | docs/modules/benchmarks.md, docs/modules/dashboard.md |
| class | `DiagnosticReport` | 80 | ✅ | docs/modules/upgrade.md |
| method | `DiagnosticReport.passed` | 93 | ❌ | — |
| method | `DiagnosticReport.warned` | 98 | ❌ | — |
| method | `DiagnosticReport.failed` | 103 | ❌ | — |
| method | `DiagnosticReport.healthy` | 108 | ❌ | — |
| method | `DiagnosticReport.to_dict` | 112 | ✅ | docs/modules/benchmarks.md, docs/modules/dashboard.md |
| method | `DiagnosticReport.to_json` | 122 | ❌ | — |
| class | `Doctor` | 747 | ✅ | docs/architecture/overview.md, docs/modules/upgrade.md, docs/_meta/gap-analysis.md |
| method | `Doctor.data_dir` | 773 | ❌ | — |
| method | `Doctor.db_path` | 778 | ❌ | — |
| method | `Doctor.config_path` | 783 | ❌ | — |
| method | `Doctor.port` | 788 | ❌ | — |
| method | `Doctor.run_all` | 792 | ✅ | docs/modules/upgrade.md |
| method | `Doctor.run_check` | 825 | ❌ | — |
| method | `Doctor.list_checks` | 845 | ❌ | — |

### `src/sovyx/upgrade/exporter.py` — 4 ✅ / 2 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `ExportManifest` | 31 | ✅ | docs/modules/upgrade.md |
| method | `ExportManifest.to_dict` | 50 | ✅ | docs/modules/benchmarks.md, docs/modules/dashboard.md |
| class | `ExportInfo` | 68 | ✅ | docs/modules/upgrade.md |
| class | `MindExporter` | 93 | ✅ | docs/modules/upgrade.md |
| method | `MindExporter.export_smf` | 118 | ❌ | — |
| method | `MindExporter.export_archive` | 200 | ❌ | — |

### `src/sovyx/upgrade/importer.py` — 3 ✅ / 2 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `ImportValidationError` | 34 | ✅ | docs/modules/upgrade.md |
| class | `ImportInfo` | 42 | ✅ | docs/modules/upgrade.md |
| class | `MindImporter` | 70 | ✅ | docs/modules/upgrade.md, docs/_meta/gap-analysis.md |
| method | `MindImporter.import_smf` | 99 | ❌ | — |
| method | `MindImporter.import_archive` | 178 | ❌ | — |

### `src/sovyx/upgrade/schema.py` — 9 ✅ / 8 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `SemVer` | 48 | ✅ | docs/architecture/data-flow.md, docs/architecture/overview.md, docs/modules/persistence.md +4 |
| method | `SemVer.parse` | 70 | ✅ | docs/security/obsidian-protocol.md |
| method | `SemVer.zero` | 93 | ❌ | — |
| class | `UpgradeMigration` | 99 | ✅ | docs/modules/upgrade.md |
| method | `UpgradeMigration.semver` | 126 | ❌ | — |
| class | `MigrationReport` | 132 | ✅ | docs/modules/upgrade.md |
| class | `SchemaVersion` | 149 | ✅ | docs/modules/upgrade.md |
| method | `SchemaVersion.initialize` | 162 | ✅ | docs/modules/persistence.md |
| method | `SchemaVersion.get_current` | 167 | ❌ | — |
| method | `SchemaVersion.get_pending` | 185 | ❌ | — |
| method | `SchemaVersion.record` | 202 | ✅ | docs/modules/llm.md |
| method | `SchemaVersion.get_history` | 227 | ❌ | — |
| class | `MigrationRunner` | 255 | ✅ | docs/modules/persistence.md, docs/modules/upgrade.md |
| method | `MigrationRunner.schema_version` | 287 | ❌ | — |
| method | `MigrationRunner.run` | 293 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/data-flow.md, docs/modules/observability.md |
| method | `MigrationRunner.verify_applied` | 337 | ❌ | — |
| method | `MigrationRunner.discover_migrations` | 366 | ❌ | — |

### `src/sovyx/voice/audio.py` — 11 ✅ / 32 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `AudioPlatform` | 58 | ❌ | — |
| function | `detect_platform` | 68 | ❌ | — |
| class | `RingBuffer` | 95 | ❌ | — |
| method | `RingBuffer.capacity` | 117 | ❌ | — |
| method | `RingBuffer.available` | 122 | ❌ | — |
| method | `RingBuffer.write` | 126 | ✅ | docs/modules/plugins.md |
| method | `RingBuffer.read` | 158 | ✅ | docs/modules/plugins.md |
| method | `RingBuffer.clear` | 176 | ✅ | docs/development/testing.md |
| class | `AudioCaptureConfig` | 189 | ✅ | docs/modules/voice.md |
| class | `AudioCapture` | 207 | ✅ | docs/modules/voice.md |
| method | `AudioCapture.sample_rate` | 236 | ❌ | — |
| method | `AudioCapture.chunk_samples` | 241 | ❌ | — |
| method | `AudioCapture.is_running` | 246 | ❌ | — |
| method | `AudioCapture.ring_buffer` | 251 | ❌ | — |
| method | `AudioCapture.start` | 257 | ❌ | — |
| method | `AudioCapture.stop` | 284 | ✅ | docs/modules/llm.md |
| method | `AudioCapture.read_chunk` | 295 | ❌ | — |
| method | `AudioCapture.read_chunk_nowait` | 303 | ❌ | — |
| method | `AudioCapture.get_frame` | 314 | ❌ | — |
| method | `AudioCapture.list_devices` | 345 | ❌ | — |
| method | `AudioCapture.negotiate_sample_rate` | 366 | ❌ | — |
| class | `OutputPriority` | 402 | ❌ | — |
| class | `OutputChunk` | 411 | ❌ | — |
| method | `OutputChunk.duration_ms` | 434 | ❌ | — |
| class | `AudioOutputConfig` | 440 | ✅ | docs/modules/voice.md |
| class | `AudioDucker` | 461 | ✅ | docs/modules/voice.md |
| method | `AudioDucker.duck_gain` | 486 | ❌ | — |
| method | `AudioDucker.is_ducked` | 491 | ❌ | — |
| method | `AudioDucker.duck` | 495 | ❌ | — |
| function | `normalize_lufs` | 525 | ❌ | — |
| class | `AudioOutput` | 555 | ✅ | docs/modules/voice.md |
| method | `AudioOutput.sample_rate` | 586 | ❌ | — |
| method | `AudioOutput.is_playing` | 591 | ❌ | — |
| method | `AudioOutput.ducker` | 596 | ❌ | — |
| method | `AudioOutput.queue_size` | 601 | ❌ | — |
| method | `AudioOutput.start` | 607 | ❌ | — |
| method | `AudioOutput.stop` | 628 | ✅ | docs/modules/llm.md |
| method | `AudioOutput.enqueue` | 639 | ❌ | — |
| method | `AudioOutput.play_immediate` | 664 | ❌ | — |
| method | `AudioOutput.drain` | 683 | ❌ | — |
| method | `AudioOutput.flush` | 699 | ✅ | docs/security/obsidian-protocol.md |
| method | `AudioOutput.apply_fade_out` | 710 | ❌ | — |
| method | `AudioOutput.list_devices` | 743 | ❌ | — |

### `src/sovyx/voice/auto_select.py` — 4 ✅ / 10 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `HardwareTier` | 41 | ✅ | docs/modules/benchmarks.md, docs/modules/voice.md |
| class | `HardwareProfile` | 52 | ✅ | docs/modules/voice.md |
| class | `ModelSelection` | 71 | ✅ | docs/modules/voice.md |
| function | `detect_hardware` | 236 | ❌ | — |
| function | `select_models` | 272 | ❌ | — |
| function | `get_fallback` | 324 | ❌ | — |
| class | `VoiceModelAutoSelector` | 367 | ✅ | docs/modules/voice.md |
| method | `VoiceModelAutoSelector.profile` | 379 | ❌ | — |
| method | `VoiceModelAutoSelector.selection` | 384 | ❌ | — |
| method | `VoiceModelAutoSelector.detect_hardware` | 388 | ❌ | — |
| method | `VoiceModelAutoSelector.select_models` | 397 | ❌ | — |
| method | `VoiceModelAutoSelector.fallback` | 414 | ❌ | — |
| method | `VoiceModelAutoSelector.auto_select` | 426 | ❌ | — |
| method | `VoiceModelAutoSelector.doctor_report` | 435 | ❌ | — |

### `src/sovyx/voice/jarvis.py` — 2 ✅ / 16 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `FillerCategory` | 70 | ❌ | — |
| class | `JarvisConfig` | 131 | ✅ | docs/modules/voice.md |
| function | `validate_jarvis_config` | 159 | ❌ | — |
| class | `JarvisIllusion` | 196 | ✅ | docs/modules/voice.md |
| method | `JarvisIllusion.config` | 218 | ❌ | — |
| method | `JarvisIllusion.beep_cached` | 223 | ❌ | — |
| method | `JarvisIllusion.cached_filler_count` | 228 | ❌ | — |
| method | `JarvisIllusion.history` | 233 | ❌ | — |
| method | `JarvisIllusion.pre_cache` | 239 | ❌ | — |
| method | `JarvisIllusion.play_beep` | 257 | ❌ | — |
| method | `JarvisIllusion.get_beep` | 266 | ❌ | — |
| method | `JarvisIllusion.select_category` | 276 | ❌ | — |
| method | `JarvisIllusion.select_filler` | 295 | ❌ | — |
| method | `JarvisIllusion.get_cached_filler` | 356 | ❌ | — |
| method | `JarvisIllusion.play_filler_after_delay` | 367 | ❌ | — |
| method | `JarvisIllusion.reset_history` | 404 | ❌ | — |
| function | `split_at_boundaries` | 414 | ❌ | — |
| function | `synthesize_beep` | 443 | ❌ | — |

### `src/sovyx/voice/pipeline.py` — 16 ✅ / 19 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `VoicePipelineState` | 52 | ✅ | docs/architecture/data-flow.md, docs/modules/voice.md |
| class | `WakeWordDetectedEvent` | 76 | ✅ | docs/modules/voice.md |
| class | `SpeechStartedEvent` | 83 | ✅ | docs/modules/voice.md |
| class | `SpeechEndedEvent` | 90 | ✅ | docs/modules/voice.md |
| class | `TranscriptionCompletedEvent` | 98 | ✅ | docs/modules/voice.md |
| class | `TTSStartedEvent` | 108 | ✅ | docs/modules/voice.md |
| class | `TTSCompletedEvent` | 115 | ✅ | docs/modules/voice.md |
| class | `BargeInEvent` | 122 | ✅ | docs/modules/voice.md |
| class | `PipelineErrorEvent` | 129 | ✅ | docs/modules/voice.md |
| class | `VoicePipelineConfig` | 142 | ✅ | docs/modules/voice.md |
| function | `validate_config` | 176 | ❌ | — |
| class | `AudioOutputQueue` | 204 | ✅ | docs/modules/voice.md |
| method | `AudioOutputQueue.is_playing` | 218 | ❌ | — |
| method | `AudioOutputQueue.enqueue` | 222 | ❌ | — |
| method | `AudioOutputQueue.play_immediate` | 230 | ❌ | — |
| method | `AudioOutputQueue.drain` | 242 | ❌ | — |
| method | `AudioOutputQueue.interrupt` | 254 | ❌ | — |
| method | `AudioOutputQueue.clear` | 264 | ✅ | docs/development/testing.md |
| class | `BargeInDetector` | 298 | ✅ | docs/modules/voice.md |
| method | `BargeInDetector.check_frame` | 321 | ❌ | — |
| method | `BargeInDetector.monitor` | 336 | ❌ | — |
| class | `VoicePipeline` | 383 | ✅ | docs/architecture/overview.md, docs/modules/llm.md, docs/modules/voice.md +1 |
| method | `VoicePipeline.state` | 453 | ❌ | — |
| method | `VoicePipeline.config` | 458 | ❌ | — |
| method | `VoicePipeline.output` | 463 | ❌ | — |
| method | `VoicePipeline.jarvis` | 468 | ❌ | — |
| method | `VoicePipeline.is_running` | 473 | ❌ | — |
| method | `VoicePipeline.start` | 479 | ❌ | — |
| method | `VoicePipeline.stop` | 493 | ✅ | docs/modules/llm.md |
| method | `VoicePipeline.feed_frame` | 504 | ❌ | — |
| method | `VoicePipeline.speak` | 725 | ❌ | — |
| method | `VoicePipeline.stream_text` | 749 | ❌ | — |
| method | `VoicePipeline.flush_stream` | 780 | ❌ | — |
| method | `VoicePipeline.start_thinking` | 799 | ❌ | — |
| method | `VoicePipeline.reset` | 831 | ✅ | docs/modules/cognitive.md |

### `src/sovyx/voice/stt.py` — 10 ✅ / 7 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `STTState` | 47 | ❌ | — |
| class | `TranscriptionResult` | 57 | ✅ | docs/modules/voice.md |
| class | `TranscriptionSegment` | 78 | ✅ | docs/modules/voice.md |
| class | `PartialTranscription` | 88 | ✅ | docs/modules/voice.md |
| class | `MoonshineConfig` | 102 | ✅ | docs/modules/voice.md |
| class | `STTEngine` | 140 | ✅ | docs/modules/voice.md |
| method | `STTEngine.initialize` | 149 | ✅ | docs/modules/persistence.md |
| method | `STTEngine.transcribe` | 153 | ❌ | — |
| method | `STTEngine.transcribe_streaming` | 161 | ❌ | — |
| method | `STTEngine.close` | 169 | ✅ | docs/modules/cli.md, docs/modules/persistence.md, docs/development/testing.md |
| class | `MoonshineSTT` | 178 | ✅ | docs/modules/voice.md |
| method | `MoonshineSTT.state` | 204 | ❌ | — |
| method | `MoonshineSTT.config` | 209 | ❌ | — |
| method | `MoonshineSTT.initialize` | 213 | ✅ | docs/modules/persistence.md |
| method | `MoonshineSTT.transcribe` | 254 | ❌ | — |
| method | `MoonshineSTT.transcribe_streaming` | 297 | ❌ | — |
| method | `MoonshineSTT.close` | 380 | ✅ | docs/modules/cli.md, docs/modules/persistence.md, docs/development/testing.md |

### `src/sovyx/voice/stt_cloud.py` — 5 ✅ / 5 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `CloudSTTConfig` | 53 | ✅ | docs/modules/voice.md |
| function | `needs_cloud_fallback` | 154 | ❌ | — |
| class | `CloudSTT` | 177 | ✅ | docs/modules/voice.md |
| method | `CloudSTT.state` | 206 | ❌ | — |
| method | `CloudSTT.config` | 211 | ❌ | — |
| method | `CloudSTT.initialize` | 215 | ✅ | docs/modules/persistence.md |
| method | `CloudSTT.transcribe` | 250 | ❌ | — |
| method | `CloudSTT.transcribe_streaming` | 304 | ❌ | — |
| method | `CloudSTT.close` | 355 | ✅ | docs/modules/cli.md, docs/modules/persistence.md, docs/development/testing.md |
| class | `CloudSTTError` | 432 | ✅ | docs/modules/voice.md |

### `src/sovyx/voice/tts_kokoro.py` — 4 ✅ / 6 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `KokoroConfig` | 67 | ✅ | docs/modules/voice.md |
| class | `KokoroTTS` | 118 | ✅ | docs/modules/voice.md |
| method | `KokoroTTS.config` | 155 | ❌ | — |
| method | `KokoroTTS.is_initialized` | 160 | ❌ | — |
| method | `KokoroTTS.sample_rate` | 165 | ❌ | — |
| method | `KokoroTTS.initialize` | 171 | ✅ | docs/modules/persistence.md |
| method | `KokoroTTS.close` | 207 | ✅ | docs/modules/cli.md, docs/modules/persistence.md, docs/development/testing.md |
| method | `KokoroTTS.synthesize` | 249 | ❌ | — |
| method | `KokoroTTS.synthesize_streaming` | 302 | ❌ | — |
| method | `KokoroTTS.list_voices` | 341 | ❌ | — |

### `src/sovyx/voice/tts_piper.py` — 8 ✅ / 9 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `AudioChunk` | 44 | ✅ | docs/modules/voice.md |
| class | `PiperConfig` | 59 | ✅ | docs/modules/voice.md |
| class | `TTSEngine` | 84 | ✅ | docs/modules/voice.md |
| method | `TTSEngine.initialize` | 88 | ✅ | docs/modules/persistence.md |
| method | `TTSEngine.synthesize` | 92 | ❌ | — |
| method | `TTSEngine.synthesize_streaming` | 97 | ❌ | — |
| method | `TTSEngine.close` | 108 | ✅ | docs/modules/cli.md, docs/modules/persistence.md, docs/development/testing.md |
| class | `PiperTTS` | 158 | ✅ | docs/modules/voice.md |
| method | `PiperTTS.config` | 192 | ❌ | — |
| method | `PiperTTS.is_initialized` | 197 | ❌ | — |
| method | `PiperTTS.sample_rate` | 202 | ❌ | — |
| method | `PiperTTS.num_speakers` | 210 | ❌ | — |
| method | `PiperTTS.initialize` | 218 | ✅ | docs/modules/persistence.md |
| method | `PiperTTS.close` | 265 | ✅ | docs/modules/cli.md, docs/modules/persistence.md, docs/development/testing.md |
| method | `PiperTTS.synthesize` | 399 | ❌ | — |
| method | `PiperTTS.synthesize_streaming` | 457 | ❌ | — |
| method | `PiperTTS.list_voices` | 496 | ❌ | — |

### `src/sovyx/voice/vad.py` — 4 ✅ / 6 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `VADState` | 41 | ❌ | — |
| class | `VADEvent` | 55 | ✅ | docs/modules/voice.md |
| class | `VADConfig` | 69 | ✅ | docs/modules/voice.md |
| method | `VADConfig.window_size` | 91 | ❌ | — |
| class | `SileroVAD` | 130 | ✅ | docs/architecture/data-flow.md, docs/architecture/overview.md, docs/modules/voice.md +1 |
| method | `SileroVAD.process_frame` | 194 | ❌ | — |
| method | `SileroVAD.reset` | 241 | ✅ | docs/modules/cognitive.md |
| method | `SileroVAD.state` | 250 | ❌ | — |
| method | `SileroVAD.is_speaking` | 255 | ❌ | — |
| method | `SileroVAD.config` | 260 | ❌ | — |

### `src/sovyx/voice/wake_word.py` — 4 ✅ / 10 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `WakeWordState` | 61 | ❌ | — |
| class | `WakeWordEvent` | 75 | ❌ | — |
| class | `WakeWordConfig` | 89 | ✅ | docs/modules/voice.md |
| method | `WakeWordConfig.frame_samples` | 114 | ❌ | — |
| method | `WakeWordConfig.stage2_window_frames` | 119 | ❌ | — |
| method | `WakeWordConfig.cooldown_frames` | 124 | ❌ | — |
| class | `VerificationResult` | 160 | ✅ | docs/modules/voice.md |
| function | `default_verifier` | 175 | ❌ | — |
| function | `create_stt_verifier` | 192 | ❌ | — |
| class | `WakeWordDetector` | 219 | ✅ | docs/modules/voice.md |
| method | `WakeWordDetector.process_frame` | 282 | ❌ | — |
| method | `WakeWordDetector.reset` | 322 | ✅ | docs/modules/cognitive.md |
| method | `WakeWordDetector.state` | 330 | ❌ | — |
| method | `WakeWordDetector.config` | 335 | ❌ | — |

### `src/sovyx/voice/wyoming.py` — 7 ✅ / 23 ❌

| Kind | Symbol | Line | Status | Documented in |
|---|---|---:|---|---|
| class | `STTResult` | 53 | ❌ | — |
| class | `TTSResult` | 61 | ❌ | — |
| class | `WakeWordResult` | 69 | ❌ | — |
| class | `STTEngineProtocol` | 81 | ❌ | — |
| method | `STTEngineProtocol.transcribe` | 84 | ❌ | — |
| class | `TTSEngineProtocol` | 93 | ❌ | — |
| method | `TTSEngineProtocol.synthesize` | 96 | ❌ | — |
| class | `WakeWordEngineProtocol` | 101 | ❌ | — |
| method | `WakeWordEngineProtocol.process_frame` | 104 | ❌ | — |
| class | `CogLoopProtocol` | 109 | ❌ | — |
| method | `CogLoopProtocol.generate_response` | 112 | ❌ | — |
| class | `WyomingConfig` | 123 | ✅ | docs/modules/voice.md |
| class | `WyomingEvent` | 163 | ✅ | docs/modules/voice.md |
| method | `WyomingEvent.to_bytes` | 176 | ❌ | — |
| method | `WyomingEvent.read_from` | 187 | ❌ | — |
| function | `write_event` | 231 | ❌ | — |
| function | `build_service_info` | 242 | ❌ | — |
| function | `pcm_bytes_to_ndarray` | 348 | ❌ | — |
| function | `ndarray_to_pcm_bytes` | 365 | ❌ | — |
| class | `WyomingClientHandler` | 385 | ✅ | docs/modules/voice.md |
| method | `WyomingClientHandler.closed` | 412 | ❌ | — |
| method | `WyomingClientHandler.run` | 416 | ✅ | docs/architecture/cognitive-loop.md, docs/architecture/data-flow.md, docs/modules/observability.md |
| method | `WyomingClientHandler.close` | 429 | ✅ | docs/modules/cli.md, docs/modules/persistence.md, docs/development/testing.md |
| class | `SovyxWyomingServer` | 634 | ✅ | docs/modules/voice.md |
| method | `SovyxWyomingServer.running` | 674 | ❌ | — |
| method | `SovyxWyomingServer.config` | 679 | ❌ | — |
| method | `SovyxWyomingServer.active_connections` | 684 | ❌ | — |
| method | `SovyxWyomingServer.start` | 688 | ❌ | — |
| method | `SovyxWyomingServer.stop` | 712 | ✅ | docs/modules/llm.md |
| function | `get_local_ip` | 801 | ❌ | — |
