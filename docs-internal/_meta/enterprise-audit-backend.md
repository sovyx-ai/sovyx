# Enterprise-Grade Audit — Sovyx Backend

**Gerado em**: 2026-04-14 (FASE 1 de 4)
**Escopo**: 165 arquivos `.py` em `src/sovyx/` (16 módulos, ~46k LOC)
**Método**: 5 agents paralelos, cada um auditando 2-4 módulos, avaliando 10 critérios por arquivo, brutalmente honesto.

**Detalhe por grupo**: `enterprise-audit-part-{A,B,C,D,E}.md`.

---

## Score global do backend

| Classificação | Arquivos | % | Score médio |
|---|---:|---:|---:|
| **ENTERPRISE** (8-10/10) | 129 | **78.2%** | — |
| **DEVELOPED-NOT-ENTERPRISE** (5-7/10) | 36 | 21.8% | — |
| **NOT-ENTERPRISE** (0-4/10) | 0 | 0% | — |
| **TOTAL** | **165** | 100% | **8.5/10** |

**Veredicto**: Sovyx backend é **78% enterprise-grade**. Zero arquivos abaixo de 5/10. Os 22% "developed" têm fundamentos sólidos mas carregam dívidas específicas catalogadas abaixo — majoritariamente god files, bare excepts, e superfícies externas sem hardening.

---

## Por módulo

| Módulo | Files | Avg | ENTERPRISE | DEVELOPED | NOT-ENT |
|---|---:|---:|---:|---:|---:|
| engine | 12 | 8.6 | — | — | 0 |
| cognitive | 23 | 8.0 | — | — | 0 |
| brain | 13 | 7.8 | — | — | 0 |
| context | 4 | 9.5 | — | — | 0 |
| mind | 2 | 9.25 | — | — | 0 |
| llm | ~10 | 8.4 | — | — | 0 |
| voice | ~10 | 8.09 | — | — | 0 |
| persistence | 8 | 9.2 | 8 | 0 | 0 |
| observability | 7 | 9.1 | 6 | 1 | 0 |
| plugins | 21 | 8.4 | 15 | 6 | 0 |
| bridge | 7 | 9.0 | — | — | 0 |
| cloud | 10 | 9.2 | — | — | 0 |
| upgrade | 7 | 9.1 | — | — | 0 |
| cli | 7 | 7.5 | — | — | 0 |
| dashboard | 17 | 8.6 | 14 | 3 | 0 |
| benchmarks | 3 | 9.3 | 3 | 0 | 0 |

**Módulos mais fortes**: `context` (9.5), `benchmarks` (9.3), `cloud` (9.2), `persistence` (9.2), `mind` (9.25). Estes estão prontos pra referência externa.

**Módulos com dívida maior**: `cli` (7.5), `brain` (7.8), `cognitive` (8.0), `voice` (8.09). Todos ainda passam de 7, mas carregam padrões específicos que bloqueiam classificação enterprise.

---

## Top 10 issues sistêmicos (ordem de impacto)

### 1. God files (8 arquivos >500 LOC com múltiplas responsabilidades)

| Arquivo | LOC | Problema |
|---|---:|---|
| `dashboard/server.py` | **2134** | `create_app()` inlines ~40 endpoints em 1 função; deve ser `APIRouter` per domain |
| `cognitive/safety_patterns.py` | 1165 | 8 categorias de patterns + dispatch + compilation cache em 1 arquivo |
| `cognitive/reflect.py` | 1021 | Concept extraction + episode encoding + emotional update + spreading all em Reflect phase |
| `voice/pipeline.py` | 840 | Output queue + barge-in + state machine + orchestrator colocados |
| `plugins/manager.py` | 819 | Discovery + loading + execution + health + emission de 4 events duplicados |
| `brain/service.py` | 712 | BrainService como façade excessiva — mistura retrieval, persistence, consolidation |
| `brain/embedding.py` | 705 | ONNX Runtime orchestration + model loading + caching + device selection |
| `cognitive/safety_classifier.py` | 704 | LLM classifier + heuristic fallback + budget + normalization |

**Impacto**: testabilidade, revisão de diff, coupling.

### 2. BLE001 (bare `except Exception:`) — pervasivo

~40+ ocorrências identificadas em:
- `cognitive/` (20+): safety classifier, PII NER, audit store, escalation notifier
- `plugins/`: manager `_emit_*` methods com `except Exception: pass` sem logger
- `cloud/`: vários com `# noqa: BLE001` + `logger.debug` (silent failure mode)
- `cli/`: quase todos os commands
- `dashboard/`: 10+ com `# noqa: BLE001` hiding registry-wiring bugs

**Pattern comum**: `except Exception: logger.debug(...)` ou `except Exception: pass`. Mascara bugs reais em produção.

### 3. Security gaps (críticos)

**3a. Wyoming server unauthenticated na LAN**
- `voice/wyoming.py`: escuta `0.0.0.0:10700` sem token, sem allowlist, sem rate limit, sem payload size cap, sem read timeout
- Qualquer peer na LAN pode invocar `cogloop.generate_response` (custa créditos LLM do dono) ou DoS via slow-loris
- **Maior gap de segurança identificado no audit**

**3b. Official plugins bypassam o sandbox que shippam**
- `plugins/official/weather.py`, `web_intelligence.py` importam `httpx.AsyncClient` diretamente, nunca roteiam via `SandboxedHttpClient`
- Domain allowlist e local-IP check simplesmente não se aplicam
- Arquitetura de sandbox vira teatro se as referências oficiais a ignoram

**3c. DNS rebinding TOCTOU em `sandbox_http.py`**
- `socket.getaddrinfo()` chamado (bloqueia event loop) → httpx resolve DE NOVO no connect
- DNS hostil pode servir `8.8.8.8` ao check e `127.0.0.1` à request real
- Fix: pin IP via custom httpx transport

**3d. AST scanner incompleto em `plugins/security.py`**
- Faltam: `builtins`, `tempfile`, `gc`, `inspect`, `mmap`, `pty`
- Faltam atributos: `__class__`, `__mro__`, `__base__`, `f_back`, `gi_frame`
- Escape clássico `().__class__.__base__.__subclasses__()` **não é bloqueado**

**3e. Mind-name path traversal**
- `cli/main.py` `init`: `name.lower()` concatenado a path sem validação
- `sovyx init --name ../../etc` escapa `~/.sovyx/`

**3f. Plugin install sem checksum/allowlist**
- `cli/commands/plugin.py install` aceita pip/git arbitrário sem verify

**3g. Dashboard import endpoint sem size cap**
- 10GB POST → daemon crash (OOM)

**3h. Dashboard chat endpoint sem max message length**
- 10MB payload queima contexto e custo LLM em 1 hit

**3i. Google provider API key na URL**
- `?key=...` — qualquer httpx error que stringifica URL vaza o key

### 4. Sync IO em async context (event-loop starvation)

- `voice/`: Piper, Kokoro, Silero VAD, OpenWakeWord, Moonshine — todos rodam ONNX síncrono direto em `async def`. Sem `asyncio.to_thread()`. TTS trava dashboard/HTTP por segundos
- `cloud/backup.py`: boto3 sync calls em hot path async; upload grande trava event loop
- `dashboard/logs.py`: `time.sleep(0.1)` em async function durante rotation race

### 5. asyncio primitives construídas em `__init__`

- `voice/pipeline.py` e `voice/audio.py`: `asyncio.Queue()` / `Event()` instanciados em `__init__`
- Anti-pattern Python 3.10+: bind à current running loop no momento de construção
- Bug latente em tests/multi-loop — violaria a regra de instanciar dentro de async context

### 6. Hardcoded config constants (fora do `EngineConfig`)

- `cognitive/safety_classifier.py`: budgets/timeouts module-level
- `cognitive/safety_patterns.py`: catalog de regex inteiro hardcoded
- `brain/embedding.py`: model URLs, SHA, dimensions hardcoded
- `brain/learning.py`: learning rates hardcoded
- `brain/consolidation.py`: thresholds hardcoded
- Vários: `shutil.disk_usage("/")` — errado em Windows e em data_dirs não-root

### 7. Unbounded asyncio primitives

- `cloud/flex.py`, `cloud/usage.py`: `defaultdict(asyncio.Lock)` sem eviction — memory leak por usuário distinto
- `bridge/manager.py` **tem** LRU eviction (pattern a copiar pra cloud)

### 8. Encapsulation leak via private attribute access

- `plugins/context.py` BrainAccess lê 7+ BrainService privates: `_embedding`, `_concepts`, `_relations._pool`, `_memory._activations`, etc.
- `dashboard/server.py` lê 10+ service privates: `router._providers`, `bridge._adapters`, `brain._embedding._loaded`, `guard._daily_budget`, `scheduler._running`, `counters._tz`
- Quebra na primeira refactor — contrato implícito

### 9. Duplicação de pricing table

- Tabela de preços LLM duplicada em 5 arquivos: `llm/router.py` + `llm/providers/{anthropic,openai,google,ollama}.py`
- Drift entre pre-call estimation e post-call recording é iminente

### 10. Portability gaps (Windows)

- `voice/auto_select.py` usa `os.sysconf` — Linux-only, quebra em Windows
- `cli/rpc_client.py`: `AF_UNIX` only — não suporta Windows apesar de projeto documentar suporte
- `shutil.disk_usage("/")` hardcoded em checks de disco

### 11. Outros achados relevantes

- **persistence/pool.py**: contador `_read_index` round-robin não-protegido (race em high contention)
- **observability/alerts.py**: mutação de `_metrics`/`_states` dicts sem lock (funciona hoje por asyncio single-thread, frágil)
- **dashboard rate limiter**: bucket key usa raw path — `/api/conversations/{id}` cria buckets unbounded
- **dashboard /ws**: sem connect rate limit (explicitamente skipped)
- **cli init**: path validation ausente; combina com sanitização de name fraca
- **cli RpcServer (engine/rpc_server.py)**: 7/10, no auth beyond `0o600`, no rate limit, swallows all connection errors silently, unpacks arbitrary kwargs do socket para registered handlers
- **STT timeout silently returns empty string** — pipeline trata "STT hung" como "silence"
- **License key**: carregado once at startup, sem runtime rotation

---

## Pontos fortes (worth calling out)

Nem tudo é crítica. Vários sub-sistemas são de qualidade de referência:

- **`cloud/billing.py` webhook handling**: raw-body HMAC, 300s replay tolerance, multi-sig parse, `compare_digest`, idempotent EventStore, per-event error isolation — **textbook**
- **`cloud/crypto.py`**: RFC 9106 Argon2id + AES-256-GCM + crypto-shredding pra GDPR — exemplar
- **`upgrade/blue_green.py`**: 6-phase upgrade com rollback em qualquer falha — design robusto
- **`bridge/manager.py`**: per-conversation `asyncio.Lock` com LRU-500 eviction — **melhor pattern de concurrency no codebase**
- **`dashboard/events.py` ConnectionManager.broadcast()**: snapshot under lock → release → send → prune stale — slow consumer não bloqueia outros
- **`persistence/manager.py`**: DB-per-Mind isolation via `str(mind_id)` keying — zero cross-mind leaks
- **`persistence/migrations.py`**: forward-only, checksummed, transactional BEGIN/END aninhado
- **`dashboard` auth**: `secrets.compare_digest` (timing-safe), aplicado em HTTP e WebSocket
- **`dashboard` security headers**: CSP, X-Frame-Options DENY, Referrer-Policy, Permissions-Policy completos
- **Test coverage**: ~1.4:1 test LOC / prod LOC (16.4k tests vs 12k prod); property-based em billing invariants; `tests/dashboard/` com 30+ arquivos

---

## Roadmap de hardening (prioridade)

### P0 — security blockers (antes de abrir a plataforma)

1. **Wyoming server**: adicionar token auth + rate limit + payload size cap + read timeout
2. **Official plugins**: refatorar `weather.py` e `web_intelligence.py` pra usar `SandboxedHttpClient`
3. **AST scanner**: adicionar patterns faltantes (`__class__.__base__`, `builtins`, `tempfile`, `gc`, `inspect`, `mmap`, `pty`)
4. **CLI init**: validar mind-name (regex `^[a-z0-9_-]{1,64}$`) antes de path join
5. **Dashboard import endpoint**: size cap (ex: 100MB) + streaming parse
6. **Dashboard chat endpoint**: max message length (ex: 10k chars)
7. **Google provider**: mover API key pra header `x-goog-api-key` em vez de query string
8. **DNS rebinding fix em sandbox_http**: pin IP via custom transport
9. **Plugin install**: checksum obrigatório + pip install bloqueado por default (só local path / git SHA)

### P1 — concurrency e reliability

10. Mover ONNX inference de `voice/` pra `asyncio.to_thread()`
11. Mover boto3 calls em `cloud/backup.py` pra `aioboto3` ou `to_thread`
12. `asyncio.Queue/Event` em `__init__` → lazy init no primeiro uso async
13. `cloud/flex.py` + `cloud/usage.py`: copiar LRU eviction pattern do `bridge/manager.py`
14. `observability/alerts.py`: wrap mutações em `asyncio.Lock`
15. `persistence/pool.py`: proteger `_read_index` com lock
16. Dashboard rate limiter: bucket key por path pattern, não por URI exata

### P2 — debt estrutural

17. Quebrar `dashboard/server.py` em `APIRouter` per domain (brain, conversations, chat, settings, etc.)
18. Quebrar `cognitive/safety_patterns.py` em `patterns/{pii,financial,injection,...}.py`
19. Quebrar `voice/pipeline.py` em `state_machine.py` + `output_queue.py` + `barge_in.py`
20. Quebrar `plugins/manager.py` em `discovery.py` + `loader.py` + `executor.py` + `health.py`
21. Consolidar `except Exception` em 3 padrões tipados: `except SovyxError`, `except (TimeoutError, ConnectionError)`, `except Exception: logger.exception(...)` (não `.debug`)
22. Mover hardcoded constants de `cognitive/`, `brain/`, `voice/` pra `EngineConfig`
23. Unificar pricing table em 1 fonte de verdade (`llm/pricing.py`)
24. Encapsular access a brain privates em interface explícita (`BrainContextProtocol`)
25. Windows gaps: `os.sysconf` → `psutil`, `AF_UNIX` → TCP loopback fallback

---

## Gaps não reportados (out of scope desta fase)

- Frontend (React dashboard) — FASE 2 próxima
- Tests (unit/integration/property/security) — FASE 3 próxima
- Infra (Docker, CI, deploy) — FASE 4 próxima

---

## Output files

- `docs-internal/_meta/enterprise-audit-part-A.md` — engine/cognitive/brain (460 linhas)
- `docs-internal/_meta/enterprise-audit-part-B.md` — context/mind/llm/voice (195 linhas)
- `docs-internal/_meta/enterprise-audit-part-C.md` — persistence/observability/plugins (197 linhas)
- `docs-internal/_meta/enterprise-audit-part-D.md` — bridge/cloud/upgrade/cli (337 linhas)
- `docs-internal/_meta/enterprise-audit-part-E.md` — dashboard/benchmarks (122 linhas)
- `docs-internal/_meta/enterprise-audit-backend.md` — este arquivo (consolidado)
