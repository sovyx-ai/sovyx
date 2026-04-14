# Módulo: benchmarks

## Objetivo

Infraestrutura de *performance budgets* e *baseline tracking* do Sovyx. Define limites duros por tier de hardware (Pi5 / N100 / GPU) e compara medições contra um baseline armazenado para detectar regressões em CI. É o alicerce dos *quality gates* de performance: junto com o runner de benchmarks em `benchmarks/bench_*.py`, fornece a "linha vermelha" para startup, footprint de memória, latência de brain search, context assembly e throughput de working memory.

**Estado atual: Aligned.** Implementação mínima completa (2 módulos, 483 LOC) — orquestração dos benchmarks é feita por `pytest` + scripts em `benchmarks/` na raiz do repo, e este módulo provê o *harness* de validação.

## Responsabilidades

- **Performance budgets** — `PerformanceBudget` valida `BenchmarkResult` contra `TierLimits` por hardware tier; retorna `BudgetCheck` por métrica.
- **Hardware tiers** — 3 perfis (`PI5`, `N100`, `GPU`) com limites calibrados em SPE-031 / ADR-002.
- **Baseline tracking** — `BaselineManager` salva resultados como JSON timestamped em `baselines/`; mantém `latest.json` para comparação rápida.
- **Regression detection** — `compare()` calcula `change_pct` por métrica; eleva `RegressionDetected` se ultrapassar a `tolerance` (default 10%).
- **Higher-is-better awareness** — métricas de throughput (`working_memory_ops_per_sec`, `tokens_per_sec`, `ops_per_sec`) são tratadas inversas (diminuição é regressão).
- **Serialização** — `ComparisonReport.to_dict()` e `BenchmarkResult.to_dict()` para dumps JSON em CI.

## Arquitetura

```
benchmarks/
  ├── __init__.py    re-exports públicos
  ├── budgets.py     HardwareTier + TierLimits + PerformanceBudget + BenchmarkResult + BudgetCheck
  └── baseline.py    BaselineManager + ComparisonReport + MetricComparison + RegressionDetected
```

Fluxo típico em CI:

```
bench_*.py (pytest-benchmark/script)
      │
      ▼
  list[BenchmarkResult]  ──► PerformanceBudget.check_all(results)  ──► list[BudgetCheck]
      │                                                                     │
      │                                                                     ▼
      │                                                            all_passed() → gate PASS/FAIL
      ▼
  BaselineManager.compare(results)  ──►  ComparisonReport
                                          │
                                          ▼
                                  has_regressions → RegressionDetected
```

## Código real (exemplos curtos)

**`src/sovyx/benchmarks/budgets.py`** — limites por tier (SPE-031 §2 / ADR-002):

```python
class HardwareTier(StrEnum):
    PI5 = "pi5"
    N100 = "n100"
    GPU = "gpu"

_TIER_LIMITS: dict[HardwareTier, TierLimits] = {
    HardwareTier.PI5: TierLimits(
        startup_ms=5000,          # cold start
        rss_mb=650,               # memória residente
        brain_search_ms=100,      # FTS5 p95
        context_assembly_ms=200,
        working_memory_ops_per_sec=10_000,
    ),
    HardwareTier.N100: TierLimits(
        startup_ms=3000, rss_mb=1024,
        brain_search_ms=50, context_assembly_ms=100,
        working_memory_ops_per_sec=50_000,
    ),
    HardwareTier.GPU: TierLimits(
        startup_ms=2000, rss_mb=2048,
        brain_search_ms=20, context_assembly_ms=50,
        working_memory_ops_per_sec=100_000,
    ),
}
```

**`src/sovyx/benchmarks/budgets.py`** — check per métrica com awareness de throughput:

```python
class PerformanceBudget:
    def check(self, result: BenchmarkResult) -> BudgetCheck | None:
        mapping: dict[str, tuple[float, bool]] = {
            "startup_ms":                  (self._limits.startup_ms, False),
            "rss_mb":                      (self._limits.rss_mb, False),
            "brain_search_ms":             (self._limits.brain_search_ms, False),
            "context_assembly_ms":         (self._limits.context_assembly_ms, False),
            "working_memory_ops_per_sec":  (self._limits.working_memory_ops_per_sec, True),
        }
        lookup = mapping.get(result.name)
        if lookup is None:
            return None
        limit_val, higher_is_better = lookup
        passed = (result.value >= limit_val) if higher_is_better else (result.value <= limit_val)
        return BudgetCheck(name=result.name, measured=result.value, limit=limit_val,
                           unit=result.unit, passed=passed, higher_is_better=higher_is_better)
```

**`src/sovyx/benchmarks/baseline.py`** — regression detection:

```python
_REGRESSION_TOLERANCE = 0.10  # 10% threshold

class BaselineManager:
    def compare(self, current: list[BenchmarkResult],
                baseline: list[BenchmarkResult] | None = None) -> ComparisonReport:
        # ... compute change_pct por métrica ...
        if has_regressions:
            regressed_names = [c.name for c in comparisons if c.regressed]
            msg = f"Performance regression detected: {', '.join(regressed_names)}"
            logger.warning("regression_detected", metrics=regressed_names)
            raise RegressionDetected(msg)
        return report
```

## Specs-fonte

- **SPE-031-PERFORMANCE-BUDGETS** — definição dos tiers, calibração dos limites, política de regression tolerance.
- **ADR-002** — hardware tiers adotados pelo Sovyx (Pi5 mínimo, N100 target, GPU power-user).

## Status de implementação

| Item | Status |
|---|---|
| 3 HardwareTier (PI5, N100, GPU) + TierLimits | Aligned |
| PerformanceBudget.check / check_all / all_passed | Aligned |
| BaselineManager.save_baseline / load_baseline | Aligned |
| `latest.json` symlink-equivalente | Aligned |
| Higher-is-better awareness (throughput metrics) | Aligned |
| Regression tolerance configurável (default 10%) | Aligned |
| ComparisonReport serialization (`to_dict`) | Aligned |
| RegressionDetected exception | Aligned |
| Integração com `pytest-benchmark` harness | Aligned — scripts em `benchmarks/` (raiz) emitem `BenchmarkResult` |
| Mapa de benchmarks exaustivo (todas as métricas SPE-026) | Partial — 9 métricas mapeadas hoje (startup, rss, brain_search, context_assembly, working_memory) |

## Divergências

**Cobertura de métricas parcial** — `PerformanceBudget.check` mapeia 9 nomes de benchmark (startup_ms, create_app_cold, rss_mb, rss_after_import, rss_after_create_app, brain_search_ms, context_assembly_ms, budget_allocation_6_slots, working_memory_ops_per_sec). As 30+ métricas do catálogo SPE-026-OBSERVABILITY-METRICS não têm budgets definidos aqui — budgets de latência LLM, custo/msg, embedding throughput etc. vivem em SLOs (`observability/slo.py`), não em tier limits. Decisão consciente: tier limits são para **garantias de hardware**; SLOs são para **garantias de experiência**.

## Dependências

- `sovyx.observability.logging` — `get_logger(__name__)`.
- stdlib: `dataclasses`, `enum.StrEnum`, `pathlib.Path`, `json`, `datetime`.

Consumidores:

- `tests/unit/test_benchmarks.py` — valida thresholds chamando funções `bench_*` de `benchmarks/bench_brain.py` e `benchmarks/bench_cogloop.py` (diretório fora de `src/`).
- CI pipeline (`.github/workflows/`) — roda benchmarks, salva baseline em artifact, chama `BaselineManager.compare()` para gate de regressão.

## Testes

- `tests/unit/test_benchmarks.py` — assertions diretas em thresholds por benchmark (ex: `working_memory_100_under_10ms`).
- `benchmarks/.benchmarks/` — diretório de cache do `pytest-benchmark` (ignored pelo ruff, ver `pyproject.toml`).
- Regression tests usam `tmp_path` para isolar baselines.
- Regra crítica: nunca commitar `baselines/*.json` — são artefatos de CI por-runner, variáveis por hardware.

## Public API reference

### Public API
| Classe | Descrição |
|---|---|
| `PerformanceBudget` | Valida `BenchmarkResult` contra `TierLimits` — retorna `BudgetCheck` por métrica. |
| `BaselineManager` | Salva/carrega baselines JSON; compara current vs. baseline; detecta regressões. |
| `TierLimits` | Dataclass imutável com 5 limites (startup, rss, brain_search, context_assembly, wm_ops). |
| `BenchmarkResult` | Medição (`name`, `value`, `unit`) — input do budget check e do compare. |
| `BudgetCheck` | Resultado do check (`name`, `measured`, `limit`, `passed`, `higher_is_better`). |
| `HardwareTier` | StrEnum — `pi5` / `n100` / `gpu`. |
| `ComparisonReport` | Agregado de `MetricComparison` com `has_regressions` e `timestamp`. |
| `MetricComparison` | Comparison de uma métrica (`current`, `baseline`, `change_pct`, `regressed`). |

*(`TierLimits`, `BudgetCheck` e `MetricComparison` são classes públicas mas não estão em `sovyx.benchmarks.__all__`. Acesse direto: `from sovyx.benchmarks.budgets import TierLimits, BudgetCheck` / `from sovyx.benchmarks.baseline import MetricComparison`.)*

### Errors
| Exception | Quando é raised |
|---|---|
| `RegressionDetected` | `BaselineManager.compare()` detectou ao menos uma métrica com `change_pct > tolerance`. |

### Events
| Event | Payload / trigger |
|---|---|

*(sem events — benchmarks são síncronos e puramente CI-side; regressões geram logs estruturados + raise)*

### Configuration
| Config | Campo/Finalidade |
|---|---|
| `TierLimits` | Limites por hardware tier (inmutável — `@dataclass(frozen=True, slots=True)`). |

## Referências

- `src/sovyx/benchmarks/__init__.py` — re-exports públicos.
- `src/sovyx/benchmarks/budgets.py` — HardwareTier, TierLimits, PerformanceBudget, BenchmarkResult, BudgetCheck.
- `src/sovyx/benchmarks/baseline.py` — BaselineManager, ComparisonReport, MetricComparison, RegressionDetected.
- `benchmarks/bench_brain.py` (raiz do repo) — benchmarks de Brain (search, retrieval, scoring).
- `benchmarks/bench_cogloop.py` — benchmarks do loop cognitivo.
- `benchmarks/bench_context.py` — benchmarks de Context Assembly.
- `benchmarks/bench_memory.py` — benchmarks de working memory + consolidation.
- `benchmarks/bench_startup.py` — benchmarks de boot time do daemon.
- `tests/unit/test_benchmarks.py` — validação dos thresholds.
- SPE-031-PERFORMANCE-BUDGETS — spec dos tiers e política de regressão.
- ADR-002 — decisão sobre hardware tiers suportados.
- `docs/modules/observability.md` §SLO — comparação com limites de experiência (SLOs) vs. limites de hardware (budgets).
