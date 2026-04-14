# Sovyx Security — Best Practices

**Status:** active | **Audiência:** plugin devs, operadores, devs internos,
reviewers, compliance | **Companheiros:** `obsidian-protocol.md`,
`threat-model.md`

Guias práticos derivados do Obsidian Protocol, do código em `src/sovyx/`, e
das 42 lições de stress tests/audits adversariais registradas no protocolo.

---

## 1. Para Desenvolvedores de Plugin

### 1.1 Manifest (`plugin.yaml`)

- Declare **todas** as capabilities que o plugin usa — nem mais, nem menos.
- Nome + versão SemVer estrito.
- `permissions:` lista de strings casando com `Permission` StrEnum em
  `src/sovyx/plugins/permissions.py:24-58`.
- `network.allowed_domains:` lista explícita (wildcards `*.example.com` ok,
  `*` sozinho não).
- `manifest_version` sincronizado com SDK.

### 1.2 `@tool` Decorator

Use `from sovyx.plugins.sdk import tool`. Cada função declara schema de args +
retorno. **Nome importa:** `financial_gate.py` bloqueia tools cujo nome casa
com `_FINANCIAL_NAME_PATTERNS`. Tools read-only: comece com `get_*`, `list_*`,
`check_*`, `calculate_*`, `validate_*` (lista em `_READONLY_PREFIXES`) para
evitar falso positivo. Erros levantam exceção tipada; core traduz em
`PluginError`.

### 1.3 Sandbox Awareness

Plugin NÃO tem acesso a: `os`, `subprocess`, `sys`, `importlib`, `ctypes`,
`pickle`, `marshal`, threading, multiprocessing, signal, socket, webbrowser,
tkinter (lista completa em `plugins/security.py:63-87`). File I/O via
`PluginContext.fs` (`SandboxedFsAccess`, limites 50 MB/arquivo + 500 MB total,
paths relativos a `data_dir`). HTTP via `PluginContext.http` (allowlist; rate
limit 10/min; timeout 10s; cap 5 MB). Sem threads ou asyncio tasks
não-awaited.

### 1.4 Testing em Dry-Run

```bash
sovyx plugin validate ./my-plugin           # manifest + AST scan
sovyx plugin install ./my-plugin --dry-run  # permissions sem calls reais
cd my-plugin && pytest
```

Use `src/sovyx/plugins/testing.py` — `MockBrain`, `MockHttp`, `MockFs` pra
tests isolados. Se usar o real, `SandboxedFsAccess` em test exige `tmp_path`.

### 1.5 Self-Checklist Antes de Submeter

Nenhum import bloqueado (ruff + manual); nenhum `eval`/`exec`/`compile`/
`__import__`; nenhum `__subclasses__`/`__bases__`/`__globals__`/`__code__`;
todo network call declara domínio; input validado (Obsidian §2 Input
Validation Symmetry); sem secrets no código (use `PluginContext.vault`);
`__all__` definido; README com intent + data flow; coverage ≥ 95%.

---

## 2. Para Operadores

### 2.1 Token Rotation

```bash
sovyx token rotate             # dashboard token: gera novo, invalida antigo
sovyx token show               # exibe (uma vez; guarde no password manager)
# License token: refreshed automaticamente a cada 24h; grace 7d pós-expiry
```

Rotacione ≤ 90 dias; após suspicion event; após offboarding de admin com
acesso ao host.

### 2.2 Backup Encryption

Passphrase NUNCA no host. Em `system.yaml`:

```yaml
cloud:
  backup:
    enabled: true
    destination: r2   # ou s3, b2, local
    # passphrase: NUNCA aqui
```

Export via env: `export SOVYX_CLOUD__BACKUP__PASSPHRASE="..."` (20+ chars).
Argon2id torna brute-force impossível com passphrase decente. **Teste restore
mensalmente** (Obsidian §4 "Backup/restore TESTADO").

### 2.3 Log Redaction

- Files: `~/.sovyx/logs/sovyx.log` (rotação 10 MB × 10)
- Console: `text` (dev) ou `json` (prod, CI, systemd)
- Audit query: `sovyx brain audit --since 30d --category injection`
- `/api/chat` body nunca loga cru (output_guard faz o trabalho)

### 2.4 SSO Setup (business/enterprise, v1.0)

Spec: `IMPL-013` + `VR-086-SSO-LDAP.md`. Ativa via `TIER_FEATURES["business"] =
[..., "sso"]`.

```yaml
auth:
  sso:
    provider: saml  # ou oidc, ldap
    idp_metadata_url: "https://idp.company.com/metadata"
    sp_entity_id: "sovyx-prod"
    attribute_mapping:
      user_id: NameID
      email: "urn:oid:0.9.2342.19200300.100.1.3"
      groups: "http://schemas.xmlsoap.org/claims/Group"
```

Claims → `user_id` → `mind_id` (DB-per-Mind isolation). Grupos viram roles
(owner, viewer, auditor).

### 2.5 Monitorando o Audit Store

```sql
-- Bloqueios por categoria, 7 dias
SELECT category, COUNT(*) FROM safety_events
WHERE timestamp > strftime('%s','now','-7 days')
GROUP BY category;

-- Padrões com mais bloqueios
SELECT pattern_description, COUNT(*) AS hits FROM safety_events
WHERE action = 'BLOCK'
GROUP BY pattern_description ORDER BY hits DESC LIMIT 20;
```

Dashboard `/security` page expõe sem SQL direto.

### 2.6 SLO / Alerting

SLOs default: dashboard availability 99.5% (30d); cognitive loop p99 < 5s;
LLM provider graceful degrade (`engine/degradation.py` tem fallback chains).
Configurado em `observability/alerts.py` + Prometheus exporter.
`HealthChecker` expõe `/health` e `/ready` pros probes.

---

## 3. Para Devs Internos Sovyx

CLAUDE.md governa. Destaques de segurança:

### 3.1 Logging — Nunca `print()`

```python
# ERRADO
print(f"User {user_id} logged in")

# CERTO
from sovyx.observability.logging import get_logger
logger = get_logger(__name__)
logger.info("user_login", user_id=user_id)
```

Structured, redaction automática, rotação, filtro httpx (CLAUDE.md #6),
mockable em tests.

### 3.2 Tests — `create_app(token=...)`, não monkeypatch

CLAUDE.md #10:

```python
# ERRADO
def test_api(monkeypatch):
    monkeypatch.setattr("sovyx.dashboard.server._server_token", "test-token")

# CERTO
_TOKEN = "test-token-fixo"

@pytest.fixture()
def app(): return create_app(token=_TOKEN)
```

Monkeypatch vaza entre testes sob xdist; `_server_token` é implementation
detail.

### 3.3 Patch — `patch.object`, não string path (CLAUDE.md #11)

```python
# ERRADO — resolve a módulo diferente sob xdist
with patch("sovyx.cognitive.pii_guard.PII_PATTERNS", new_patterns):

# CERTO
from sovyx.cognitive import pii_guard
with patch.object(pii_guard, "PII_PATTERNS", new_patterns):
```

### 3.4 Enums — Sempre `StrEnum` + `@unique` (CLAUDE.md #9)

```python
from enum import StrEnum, unique

@unique
class Action(StrEnum):
    BLOCK = "block"
    REDACT = "redact"
```

Value-based, imune a xdist namespace duplication. Ver `Permission`
(`plugins/permissions.py:24`), `InjectionVerdict`, `EscalationLevel`.

### 3.5 Nunca `sys.modules` stubs (CLAUDE.md #2)

`sys.modules["anthropic"] = MagicMock()` **poisona a suite inteira**. Use DI
(provider recebe client via parâmetro) ou `importlib` com context manager.

### 3.6 Exception Hygiene (Obsidian §2)

```python
# ERRADO — silencioso, bug invisível
try: fetch_remote()
except Exception: pass

# CERTO — tipado, logger com contexto, re-raise como domain exc
try:
    fetch_remote()
except (ConnectionError, TimeoutError) as exc:
    logger.warning("fetch_remote_failed", error=str(exc), exc_info=True)
    raise FetchUnavailableError("remote down") from exc
```

Ruff S110 pega `except: pass`. Nunca `except BaseException`.

### 3.7 NaN Poison

`NaN < x` é sempre `False` → NaN passa por qualquer comparação. Check antes:

```python
import math
if math.isnan(score) or math.isinf(score):
    return Verdict.ESCALATE            # fail-closed
if score > THRESHOLD: return Verdict.BLOCK
```

### 3.8 Constant-Time Compare + Path Validation + Defense-in-Depth

- Secrets (tokens, MACs, signatures): `secrets.compare_digest` sempre (ver
  `dashboard/server.py:267`). Nunca `==`.
- FS: pattern canônico `plugins/sandbox_fs.py::_safe_path()` — resolve
  symlinks, prefix check em `data_dir`, `PermissionDeniedError` em escape.
- Input → Query (Obsidian §7): (1) validate na boundary; (2) parameterize
  query com `?` (nunca string concat). Ambas obrigatórias.

---

## 4. Para Revisores de PR

**PR em endpoint:** `Depends(verify_token)`; rate limit (default ou `_LIMITS`
override); erros sem traceback (HTTPException com `detail` humano); Request
ID tracing; resposta com schema em `types/`; input validado antes do handler;
PII passa por guard.

**PR adiciona plugin oficial:** `plugin.yaml` completo; permissions mínimas;
`ALLOWED_IMPORTS` suficiente (se não, discutir antes); coverage ≥ 95%; teste
adversarial (`eval("...")` → AST scanner pega?); doc do data flow.

**PR adiciona dep externa:** justificada no commit body (Obsidian §2);
`pip-audit`/`npm audit` passa; pinned no lockfile com hash; não substitui
stdlib sem razão; maintainer ativo (< 6 meses); license compatível (MIT/
Apache/BSD ok; GPL/AGPL exige ADR).

**PR adiciona permission type:** novo membro em `Permission` StrEnum;
`PERMISSION_RISK` + `PERMISSION_DESCRIPTIONS` dicts atualizados; dashboard UI
de aprovação inclui; sandbox component implementado; tests (sem permission →
`PermissionDeniedError`; com permission → passa); `obsidian-protocol.md §4.3`
atualizado.

**PR mexe em crypto:** nenhum downgrade (MD5/SHA1/RSA-1024 nunca); parâmetros
seguem spec (Argon2id IMPL-001; Ed25519 JWT); constant-time compare em
secrets; nonce/IV aleatório por encryption; key não reusada cross-purpose;
test vectors (NIST, RFC); `obsidian-protocol.md §7` atualizado.

---

## 5. Compliance Workflow

**GDPR Data Export (Art. 20).** `Dashboard "Export" → /api/export → SMFExporter
→ ZIP (mind.yaml sanitized, brain.jsonl, conversations.jsonl, audit.jsonl
metadata-only, manifest.json com schema+checksum) → assinatura Ed25519 →
download`. SLA: ≤ 30 dias corridos (Art. 12(3)); target: segundos pra mind
típico.

**Right to Erasure (Art. 17).**

```bash
sovyx mind delete <name>
# 1. Stop processos; 2. Delete ~/.sovyx/minds/<name>/ recursive;
# 3. Delete backup entries + salt (crypto-shred); 4. Audit log
```

Crypto-shredding: salt deletado = backups R2 irrecuperáveis mesmo se bucket
comprometido. Garantia criptográfica de Art. 17.

**Data Retention Policy** (em `mind.yaml`):

```yaml
brain:
  conversation_retention_days: 365   # null = forever (default)
  episode_retention_days: null
  low_importance_prune: true         # Ebbinghaus + scoring
  consolidation_schedule: nightly
safety:
  audit_retention_days: 90
```

Automática via `cognitive/consolidation.py` (quando integrada) + cron em
`cloud/scheduler.py`. Audit purge: `audit_store.purge(before_timestamp)`.

**DPA (enterprise).** Template em `docs/compliance/dpa-template.md` (v1.0):
Controller/Processor roles; LLM providers = Sub-Processors; 30d aviso em
sub-processor changes; data residency (EU, US, BR); breach notification 24h;
annual pentest + SOC 2 Type II.

---

## 6. Incident Response Playbook

**Detecção:** AlertManager (`observability/alerts.py`); escalation → ALERTED
(`cognitive/safety_escalation.py`); audit store spike (dashboard `/security`);
user report; CVE em dep.

**Triagem (5 min):** Severidade (Obsidian §8 — SEV-1 down/data loss < 15 min;
SEV-2 degradação severa < 30 min; SEV-3 parcial < 2h; SEV-4 cosmético próximo
business day). Escopo. Público/privado.

**Conter (15-30 min):**

```bash
sovyx plugin disable --mind <name> --all   # parar plugins
sovyx token revoke                          # dashboard token
sovyx pause --mind <name>                  # pausar cognitive
# snapshot forense
tar czf /tmp/sovyx-incident-$(date +%s).tar.gz \
    ~/.sovyx/minds/<name>/ ~/.sovyx/logs/ ~/.sovyx/safety_audit.db
```

**Recuperar (1-4h):** DB corrompida → restore backup (tested monthly); plugin
problemático → `sovyx plugin uninstall` + upstream issue; credential vazado →
rotate (dashboard, API keys, Stripe secret); bug Sovyx → hotfix main + tag
+ CI + comunicado.

**Pós-mortem (1 semana).** SEV-1/SEV-2 obrigatório. Blameless. Em
`docs/postmortems/YYYY-MM-DD-<slug>.md`. Template: timeline por minuto, root
cause (5 Whys), impacto (users, data, SLA), action items **preventivos** (não
paliativos), lessons learned (cross-ref a `obsidian-protocol.md §11` se
generalizável).

**Disclosure (CVE em Sovyx):** reservar CVE ID (MITRE/GitHub Security
Advisory); fix em branch privada + coordenar com reporters externos; release
simultânea patch + advisory; notificar (GitHub Security watch, Discord, email
biz/enterprise); post-mortem público após 90d (ou imediato se baixo risco
ativo).

---

## 7. Rastreabilidade

**Docs-fonte:**
- `vps-brain-dump/memory/nodes/obsidian-protocol.md` §2, §6, §7, §8, §11
- `vps-brain-dump/memory/nodes/sovyx-coding-protocol.md`
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/specs/SOVYX-BKD-IMPL-001-CRYPTO.md`
- `.../SOVYX-BKD-IMPL-012-PLUGIN-SANDBOX.md`
- `.../SOVYX-BKD-IMPL-013-SSO-SECURITY.md`
- `.../SOVYX-BKD-IMPL-SUP-009-COMPLIANCE.md`
- `.../SOVYX-BKD-IMPL-SUP-010-SECURITY-TOOLCHAIN.md`
- `.../SOVYX-BKD-IMPL-SUP-011-DIFFERENTIAL-PRIVACY.md`
- `CLAUDE.md` — anti-patterns #1–#12, Quality Gates

**Código de referência:**
- `src/sovyx/observability/logging.py` — `get_logger`
- `src/sovyx/dashboard/server.py:52-272` — token + verify
- `src/sovyx/dashboard/rate_limit.py` — sliding window
- `src/sovyx/plugins/permissions.py` — `Permission` StrEnum
- `src/sovyx/plugins/security.py` — AST + ImportGuard
- `src/sovyx/plugins/sandbox_fs.py` — FS sandbox + quotas
- `src/sovyx/plugins/sandbox_http.py` — HTTP allowlist + rebinding
- `src/sovyx/plugins/testing.py` — MockBrain/Http/Fs
- `src/sovyx/cognitive/pii_guard.py` — PII patterns
- `src/sovyx/cognitive/injection_tracker.py` — multi-turn
- `src/sovyx/cognitive/financial_gate.py` — tool classification
- `src/sovyx/cognitive/safety_escalation.py` — rate escalation
- `src/sovyx/cognitive/audit_store.py` — SQLite trail
- `src/sovyx/cloud/crypto.py` — Argon2id + AES-GCM
- `src/sovyx/cloud/license.py` — EdDSA + grace period

**Companheiros:**
- `docs/security/obsidian-protocol.md` — camadas de defesa
- `docs/security/threat-model.md` — threat actors + attack vectors

---

*"Cada linha de código representa o Guipe publicamente. Enterprise ou nada."*
— `sovyx-coding-protocol.md`, Regra 6
