# Obsidian Protocol — Sovyx Security Engineering Standard

**Status:** active | **Aplica-se a:** v0.5 | **Companheiros:** `threat-model.md`,
`best-practices.md`

> *Obsidiana: vidro vulcânico, mais afiado que aço cirúrgico, forjado sob pressão
> extrema. Negro como a noite. Precisão absoluta.*

Codifica a parcela de **segurança** do Obsidian Protocol aplicada à Sovyx. O
protocolo completo (`vps-brain-dump/memory/nodes/obsidian-protocol.md` v4.5)
cobre arquitetura, testing, CI/CD. Aqui foca-se em invariantes de segurança,
defense-in-depth, e como cada camada está wireada em `src/sovyx/`.

---

## 1. Introdução

Obsidian é o padrão enterprise que Nyx impôs antes da primeira linha. Ativa em
qualquer pedido de código. Inegociáveis: qualidade (ruff + mypy strict +
coverage ≥ 95% por arquivo + property-based), arquitetura (DI, 12-Factor,
threat model explícito), segurança (static analysis, input validation, zero
secrets), gates adversariais por fase, observabilidade (structlog, health
checks, tracing).

**Quando se aplica:** sempre. Toda PR passa pelos gates de `CLAUDE.md`:

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/                          # strict
uv run bandit -r src/sovyx/ --configfile pyproject.toml
uv run pytest tests/ --timeout=20         # 4900+ tests, ≥95% coverage
```

Gate falhando = commit rejeitado. Sem exceção. Sem fast-path.

**Escopo:** princípios (§2), camadas de defesa (§3-§6), crypto (§7), auth (§8),
fronteiras (§9), incident response (§10), compliance (§11), rastreabilidade
(§12). Threat actors e attack vectors: `threat-model.md`. Guias
operacionais: `best-practices.md`.

---

## 2. Princípios

Cinco invariantes, todos testáveis, todos materializados em código.

**Zero Trust.** Nenhum componente confia em outro sem prova. Dashboard
valida Bearer token via `secrets.compare_digest` (`dashboard/server.py:267`).
Plugin sandbox não confia no código do plugin (AST + ImportGuard). Cognitive
loop não confia na saída do LLM (`output_guard.py`, `pii_guard.py`). Bridge
verifica HMAC nos webhooks Stripe.

**Defense-in-Depth.** Nenhuma camada é suficiente. Plugin sandbox tem 7
camadas (5 em v1); cognitive stack tem 5 checkpoints. Registries duplicados
são banidos (lição 25 Obsidian): duas camadas que precisam da mesma lista
importam de uma fonte canônica.

**Local-First.** Default é local. Nenhum dado precisa sair pra funcionalidade
core operar. Cloud é opt-in explícito (tier gating via `cloud/license.py`), e
mesmo em modo cloud a criptografia é zero-knowledge (Argon2id + AES-GCM em
`cloud/crypto.py`).

**Fail-Closed.** Quando um check falha por erro (não bloqueio legítimo), nega.
`sandbox_http._is_local_ip()` retorna `True` em parse error, nunca `False`.
Safety gates fazem redact/escalate em exceção, nunca allow.

**Least-Privilege.** Plugin declara capabilities em `plugin.yaml`. Sem
declaração = sem acesso. `Permission` StrEnum em `plugins/permissions.py` lista
as 13 capabilities suportadas com risco graduado (`PERMISSION_RISK` dict).

---

## 3. Camadas de Defesa

```
Camada 0  AST scanning (static)    — antes do load
Camada 1  ImportGuard (runtime)    — sys.meta_path hook
Camada 2  Permission enforcer      — cada API call
Camada 3  Sandbox FS               — file I/O scoped
Camada 4  Sandbox HTTP             — network scoped
Camada 5  Process-level (v2)       — seccomp, namespaces
Camada 6  Cognitive safety stack   — PII, injection, financial
Camada 7  Data-level               — AES-GCM, JWT, Argon2
Camada 8  Audit + escalation       — trilha + alertas
```

0-5 protegem plugin code. 6-7 protegem dados da Sovyx. 8 é cross-cutting.

---

## 4. Plugin Sandbox (Camadas 0-5)

Spec: `SOVYX-BKD-IMPL-012-PLUGIN-SANDBOX.md`. v0.5 implementa camadas 0-4
(in-process). Camada 5 (kernel-level) deferida para v2 — decisão explícita
em `docs/_meta/gap-analysis.md` linha 72.

### 4.1 Camada 0 — AST Scanner

Arquivo: `src/sovyx/plugins/security.py` — `PluginSecurityScanner`. Roda
ANTES do load. `ast.parse()` + checagem contra registries canônicos:

| Registry | Conteúdo | Linhas em security.py |
|---|---|---|
| `BLOCKED_IMPORTS` | os, subprocess, shutil, sys, importlib, ctypes, pickle, marshal, code, codeop, compileall, multiprocessing, threading, signal, resource, socket, http.server, xmlrpc, webbrowser, turtle, tkinter | 63-87 |
| `BLOCKED_CALLS` | eval, exec, compile, \_\_import\_\_ | 89-96 |
| `BLOCKED_ATTRIBUTES` | \_\_import\_\_, \_\_subclasses\_\_, \_\_bases\_\_, \_\_globals\_\_, \_\_code\_\_, \_\_builtins\_\_ | 98-107 |
| `ALLOWED_IMPORTS` | os.path, pathlib, json, re, datetime, hashlib, hmac, base64, dataclasses, typing, enum, collections, functools, itertools, math, statistics, uuid, logging, asyncio, aiohttp, pydantic | 109-133 |

Resultado: `SecurityFinding(severity, file, line, message)`. `critical`
bloqueia install; `warning` exige aprovação.

### 4.2 Camada 1 — ImportGuard

Runtime hook em `sys.meta_path`, mesmo arquivo. Pega bypasses dinâmicos: string
concat (`getattr(__builtins__, "__imp"+"ort__")`), lazy imports em funções.
Thread-local state não vaza pra threads não-plugin.

### 4.3 Camada 2 — Permission Enforcer

Arquivo: `src/sovyx/plugins/permissions.py`. `Permission` é StrEnum (lição #9
CLAUDE.md, imune a xdist). Capabilities → `PermissionEnforcer.check(Permission.XXX)`
lança `PermissionDeniedError` se não concedida.

| Permission | Risk | Uso |
|---|---|---|
| BRAIN_READ | low | Buscar conceitos/episódios |
| BRAIN_WRITE | medium | Criar/alterar conceitos |
| EVENT_SUBSCRIBE/EMIT | low | EventBus |
| NETWORK_LOCAL | medium | Hosts explicitamente locais |
| NETWORK_INTERNET | high | Allow-list de domínios |
| FS_READ/WRITE | low/medium | Dentro de data_dir |
| SCHEDULER_READ/WRITE | low/medium | Timers |
| VAULT_READ/WRITE | medium | Credenciais |
| PROACTIVE | medium | Iniciar conversa sem prompt |

Mapa: `PERMISSION_RISK: dict[str, str]` (permissions.py:63-77). Dashboard
colore aprovação verde/amarelo/vermelho.

### 4.4 Camada 3 — Sandbox FS

Arquivo: `src/sovyx/plugins/sandbox_fs.py` — `SandboxedFsAccess`.
`_safe_path()` resolve symlinks, verifica prefix em `data_dir`, bloqueia
traversal. Quotas hardcoded (sandbox_fs.py:32-33):

- `_MAX_FILE_BYTES = 50 * 1024 * 1024` (50 MB/arquivo)
- `_MAX_TOTAL_BYTES = 500 * 1024 * 1024` (500 MB/plugin)

Exceder = `PermissionDeniedError`.

### 4.5 Camada 4 — Sandbox HTTP

Arquivo: `src/sovyx/plugins/sandbox_http.py`. Seis controles:

1. Domain allowlist declarada em `plugin.yaml`.
2. Local network blocking (`_is_local_ip`): loopback, RFC 1918, link-local,
   multicast, IPv6 loopback/link-local. Fail-closed em parse error.
3. DNS rebinding protection — `_resolve_hostname` resolve antes do connect;
   se o IP é privado, bloqueia.
4. Rate limit: `_DEFAULT_RATE_LIMIT = 10` req/min, sliding window 60s.
5. Response size: `_DEFAULT_MAX_RESPONSE_BYTES = 5 * 1024 * 1024` (5 MB).
6. Timeout: `_DEFAULT_TIMEOUT_S = 10.0`.

Usa `httpx` já no stack — zero dependência nova.

### 4.6 Camada 5 — Process-level (v2 deferred)

Planejado (`IMPL-012 §1.1 Layers 5-7`): Linux seccomp-BPF + namespaces +
cgroups v2; macOS sandbox-exec SBPL; Windows AppContainer (tracked).
Subprocess IPC por pipe JSON-RPC. **Não implementado em v0.5.** v1 (camadas
0-4) bloqueia os 18 escape vectors catalogados em IMPL-012 §2.

---

## 5. Cognitive Safety Stack (Camada 6)

14 arquivos em `src/sovyx/cognitive/safety_*.py` + módulos
(`injection_tracker.py`, `pii_guard.py`, `financial_gate.py`, `shadow_mode.py`,
`audit_store.py`, `output_guard.py`, `custom_rules.py`, `text_normalizer.py`).

**Injection Tracking** (`injection_tracker.py`, 453 LOC) — jailbreaks
distribuídos em múltiplas mensagens. Sliding window 5 mensagens, score
0.0-1.0 via `_SUSPICION_SIGNALS`. Thresholds: `ESCALATION_THRESHOLD = 1.5`
cumulativo, `HIGH_SUSPICION_THRESHOLD = 0.7` single-message,
`CONSECUTIVE_THRESHOLD = 2`, `ENTRY_TTL_SEC = 1800`. Verdict StrEnum:
`SAFE | SUSPICIOUS | ESCALATE`.

**PII Guard** (`pii_guard.py`, 466 LOC) — **apenas OUTPUT do LLM** (input é
do usuário). Patterns: API keys (sk-…, pk-…), IBAN, SWIFT, cartão, CPF, CNPJ,
RG, SSN, email, telefone, IP, key=value sensível. Redação: `[REDACTED-CPF]`.
Zero overhead quando `pii_protection=False` em `SafetyConfig`.

**Financial Gate** (`financial_gate.py`, 453 LOC) — intercepta tool calls
financeiros. Classifica por `_FINANCIAL_NAME_PATTERNS` (send_payment,
transfer_funds, buy, sell, trade, swap, withdraw, invest, approve, refund…)
OU por `_MIN_FINANCIAL_ARGS = 2` chaves em `_FINANCIAL_ARG_KEYS` (amount,
price, cost, balance, recipient, destination, payment_method…). Exclusão
`_READONLY_PREFIXES`: get_, fetch_, list_, check_, calculate_, validate_.
Confirmação timeout 300s.

**Shadow Mode** (`shadow_mode.py`, 277 LOC) — testa patterns novos em
produção sem afetar tráfego. Matches vão pra audit com
`FilterAction.SHADOW_LOGGED`, nunca bloqueiam. Configurado via
`ShadowPattern` em `mind/config.py:254-272`.

**Escalation** (`safety_escalation.py`, 201 LOC) — tracking por source:
3 blocks em 5 min → WARNING; 5 blocks em 5 min → RATE_LIMITED; 10 blocks em
10 min → ALERTED (callback owner). Cooldown 15 min. Thread-safe (GIL + dict
+ deque).

**Output Guard** (`output_guard.py`, 303 LOC) — pipeline unificada: normalize
(`text_normalizer.py`) → PII → custom_rules → banned_topics → audit. Último
check antes do usuário receber texto.

**Audit Store** (`audit_store.py`, 231 LOC) — SQLite com schema:

```sql
CREATE TABLE safety_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    direction TEXT NOT NULL,        -- INPUT | OUTPUT
    action TEXT NOT NULL,           -- BLOCK | REDACT | SHADOW_LOGGED
    category TEXT NOT NULL,
    tier TEXT NOT NULL,
    pattern_description TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
```

**NENHUM conteúdo original armazenado, apenas metadados.** Write-behind
buffer (`_BUFFER_MAX = 100`, flush `_FLUSH_INTERVAL_SEC = 10`). WAL permite
leituras concorrentes do dashboard.

---

## 6. Data-Level (Camada 7)

**Backup Encryption** — `src/sovyx/cloud/crypto.py` (`BackupCrypto`). Spec:
IMPL-001 §1.1-1.4.

- KDF: Argon2id RFC 9106 SECOND RECOMMENDED — `memory_cost=65536 KiB (64 MiB)`,
  `time_cost=3`, `parallelism=4`, `hash_len=32`, `salt_len=16`
- Cipher: AES-256-GCM — `nonce_len=12` (NIST SP 800-38D), `tag_len=16`
- Wire format: `salt(16) + nonce(12) + ciphertext + tag(16)` (44 B overhead)

XChaCha20-Poly1305 (PyNaCl) rejeitada — não FIPS-compliant; enterprise tier
exige FIPS. **Crypto-shredding:** deletar salt → dados irrecuperáveis (GDPR
Art. 17 criptográfico).

**License Tokens** — `src/sovyx/cloud/license.py`. Spec: SPE-033 §3.3. JWT
EdDSA (Ed25519) com chave pública embutida no binário, validação local
offline. `TOKEN_VALIDITY_DAYS=7`, `GRACE_PERIOD_DAYS=7` (modo degradado),
`REFRESH_INTERVAL_SECONDS=86400`. Tiers: free, starter, sync, cloud, business,
enterprise (`TIER_FEATURES` dict).

**Stripe Webhooks** — HMAC-SHA256 via `stripe-signature`, timestamp tolerance
5 min, `hmac.compare_digest()`.

**Credential Vault (spec v1.0)** — SQLCipher 4 (`cipher_page_size=4096`,
`kdf_iter=256000` PBKDF2-SHA512, `cipher_use_hmac=ON`). Master password via
Argon2id. Em v0.5 usa-se `BackupCrypto` pra field-level encryption.

---

## 7. Crypto — Tabela Canônica

| Uso | Primitiva | Lib | Arquivo |
|---|---|---|---|
| Backup zero-knowledge | AES-256-GCM + Argon2id | cryptography, argon2-cffi | cloud/crypto.py |
| License token | EdDSA (Ed25519) JWT | pyjwt + cryptography | cloud/license.py |
| Stripe webhook | HMAC-SHA256 | hmac stdlib | cloud/billing.py |
| Dashboard token | `secrets.token_urlsafe(32)` | secrets stdlib | dashboard/server.py:59 |
| Bearer compare | Constant-time | `secrets.compare_digest` | dashboard/server.py:267 |
| Vault DB (v1.0) | SQLCipher 4 (AES-256-CBC + HMAC-SHA512) | pysqlcipher3 | cloud/vault.py |
| Relay handshake (v0.6) | X25519 + XSalsa20-Poly1305 (NaCl Box) | pynacl | bridge/relay_client.py |
| Request ID | UUID4 | uuid stdlib | dashboard middleware |

Regras inegociáveis (Obsidian §7 Cryptographic Hygiene): `hmac.compare_digest`
sempre; constant-time decrypt path (sem early-exit MAC vs pad); random IV
per-encryption; key length validation no construtor; truncation resistance.

---

## 8. Autenticação

**Dashboard Token** — `dashboard/server.py:52-63, 207-272`. 32 bytes
`secrets.token_urlsafe(32)` gerado no primeiro start, `~/.sovyx/dashboard_token`
com `chmod 0o600`. `HTTPBearer` + `secrets.compare_digest`. **Teste-friendly:**
`create_app(token="literal")` — CLAUDE.md #10: nunca monkeypatchar
`_ensure_token` ou `_server_token`. Middleware: `SecurityHeadersMiddleware`
(CSP, X-Frame-Options), `RequestIdMiddleware`.

**Rate Limit** — `dashboard/rate_limit.py`. Sliding window 60s: GET 120/min,
POST/PUT/PATCH/DELETE 30/min, `/api/chat` 20/min, `/api/export` 5/min,
`/api/import` 10/min. Headers `X-RateLimit-*`. Cleanup a cada 5 min.

**CLI Daemon** — `engine/rpc_server.py`. JSON-RPC 2.0 via Unix socket (Linux/
macOS) / named pipe (Windows). Auth por filesystem ACL (`chmod 0o600` no
socket). Trust boundary = filesystem local.

**Plugin Manifest Signing (v1.0)** — plugins marketplace assinados Ed25519
pela Sovyx Foundation. Manifest: `publisher_key_id` + `signature`. v0.5:
plugins oficiais bundled na release; signing ativa em v1.0 com marketplace.

**SSO (business/enterprise, v1.0)** — SAML 2.0 + OIDC + LDAP. Spec IMPL-013.
Gate via `TIER_FEATURES["business"] = [..., "sso"]`. Claims → user_id →
mind ownership (DB-per-Mind isolation).

---

## 9. Fronteiras de Ameaça

**Protegido (in-scope):**

| Ameaça | Camada |
|---|---|
| Plugin code malicioso (estático/dinâmico) | 0, 1 |
| Plugin acessando recurso não declarado | 2 |
| Plugin path traversal | 3 |
| Plugin SSRF / DNS rebinding | 4 |
| Prompt injection single/multi-turn | 6 |
| PII em resposta LLM | 6 |
| Tool financeiro não autorizado | 6 |
| Backup cloud lido pelo provider | 7 (zero-knowledge) |
| Token dashboard interceptado | 7 + 8 (HTTPS + Bearer constant-time) |
| License forgery | 7 (Ed25519) |
| Stripe webhook spoof | 7 (HMAC-SHA256) |
| Abuso por volume | 8 (rate_limit + escalation) |
| Detecção pós-incidente | 8 (audit_store) |

**Out-of-scope (documentar, não iludir):**

- Acesso físico ao device (v1.0 mitiga parcial com SQLCipher vault)
- OS-level privilege escalation (responsabilidade do OS)
- Kernel-level exploits (v0.5 sem mitigação; v2 adiciona seccomp)
- Hardware side-channel (Spectre, Rowhammer) — fora de app-layer
- Supply-chain comprometida (lockfile + pip-audit mitiga parcial, não elimina)
- Malware no host (Sovyx não é AV)
- LLM provider leakando queries (mitigação: Ollama local)
- Coercion do owner (crypto-shredding via salt; plausible deniability v2+)

---

## 10. Incident Response

**Detecção (3 sinais):**
1. Audit store — eventos BLOCK/REDACT em SQLite
2. Safety escalation — callback `on_alert` em `ALERT_THRESHOLD = 10`
3. Observability — `AlertManager` + SLO burn rate em `observability/`

**Triagem — Degradation.** `engine/degradation.py` `DegradationManager`:
`HEALTHY → DEGRADED → FAILED` por componente. Engine opera com subset; fallback
chains configuradas.

**Conter — Fail-safe Defaults.** Todos os gates fazem fail-closed em
exceção: `pii_guard` → REDACT; `financial_gate` → NEEDS_CONFIRMATION;
`injection_tracker` → ESCALATE; `sandbox_fs` → PermissionDeniedError;
`sandbox_http` → True (bloqueia).

**Recuperar.** Backup diário (tier starter+) com GFS retention (7 daily, 4
weekly, 12 monthly) via `cloud/backup.py` + `cloud/scheduler.py`. Restore
via `sovyx doctor --restore`. Blue-green deploy (`upgrade/blue_green.py`)
pra upgrade sem downtime.

**Pós-mortem.** Obsidian §8: blameless, em `docs/postmortems/YYYY-MM-DD-<slug>.md`,
SEV-1/SEV-2 obrigatório. Template: timeline, root cause (5 Whys), impacto,
action items preventivos. Regra: **foca no sistema, não na pessoa.**

---

## 11. Compliance Hooks

**GDPR / LGPD:**

| Artigo | Mecanismo | Status |
|---|---|---|
| Art. 15 access | `/api/export` | v0.5 parcial |
| Art. 16 rectification | Edit dashboard | v0.5 ✓ |
| Art. 17 erasure | `sovyx mind delete` + crypto-shred | v0.5 ✓ |
| Art. 20 portability | SMFExporter (IMPL-SUP-015) | v0.6 planned |
| Art. 21 object | Opt-out telemetry (default off) | v0.5 ✓ |
| Art. 25 by design | Local-first | ✓ |
| Art. 32 security | Este documento | ✓ |
| Art. 33 breach | Audit + AlertManager | v0.5 ✓ |

**Retention.** Cognitive audit: 90 dias (`audit_store.flush()` purge por
timestamp). Conversations: indefinido (feature — companion persistente).
Owner configura via `mind.yaml: brain.conversation_retention_days`. Backups:
GFS via `cloud/scheduler.py`. Logs: rotação 10 MB × 10 arquivos.

**Logging Redaction.** `httpx` suprimido para WARNING (CLAUDE.md #6) —
evita URL tokens. `output_guard` roda antes do log. Structlog JSON;
campos PII marcados `redacted: bool`.

**ISO 27001 (essência).** Secrets via env (`SOVYX_` prefix, `SettingsConfigDict`).
TLS em prod via Caddy/Nginx. Least-privilege = §4.3. Audit trail = §5.7.
Incident response = §10. DR testado = gate de release.

---

## 12. Rastreabilidade

**Docs-fonte:**
- `vps-brain-dump/memory/nodes/obsidian-protocol.md` v4.5
- `vps-brain-dump/memory/nodes/obsidian-stack-decisions.md`
- `vps-brain-dump/memory/nodes/sovyx-coding-protocol.md`
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/specs/SOVYX-BKD-IMPL-001-CRYPTO.md`
- `.../SOVYX-BKD-IMPL-012-PLUGIN-SANDBOX.md`
- `.../SOVYX-BKD-IMPL-013-SSO-SECURITY.md`
- `.../SOVYX-BKD-IMPL-SUP-007-ANTIABUSE-CRASH.md`
- `.../SOVYX-BKD-IMPL-SUP-009-COMPLIANCE.md`
- `.../SOVYX-BKD-IMPL-SUP-010-SECURITY-TOOLCHAIN.md`
- `.../SOVYX-BKD-IMPL-SUP-011-DIFFERENTIAL-PRIVACY.md`
- `.../SOVYX-BKD-SPE-024-SECURITY-CREDENTIAL-VAULT.md`
- `.../SOVYX-BKD-SPE-032-PRIVACY-COMPLIANCE.md`
- `docs/_meta/gap-analysis.md`, `docs/_meta/gap-inputs/analysis-{A,B}-*.md`
- `CLAUDE.md` — anti-patterns #1–#12

**Código de referência:**
- Plugin sandbox: `src/sovyx/plugins/security.py`, `permissions.py`,
  `sandbox_fs.py`, `sandbox_http.py`
- Cognitive: `src/sovyx/cognitive/injection_tracker.py`, `pii_guard.py`,
  `financial_gate.py`, `shadow_mode.py`, `safety_escalation.py`,
  `audit_store.py`, `output_guard.py`
- Crypto: `src/sovyx/cloud/crypto.py`, `cloud/license.py`
- Auth: `src/sovyx/dashboard/server.py`, `dashboard/rate_limit.py`
- Resilience: `src/sovyx/engine/degradation.py`
- Config: `src/sovyx/mind/config.py:275-289`

**Companheiros:**
- `docs/security/threat-model.md` — threat actors, attack vectors, STRIDE
- `docs/security/best-practices.md` — guias operacionais, checklists,
  playbook incident response

---

*Forjado sob o Obsidian Protocol. Auditável por qualquer engenheiro Staff+.
This is our standard. Non-negotiable.*
