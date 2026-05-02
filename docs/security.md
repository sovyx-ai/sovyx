# Security

This document describes Sovyx's security posture, the plugin sandbox model, the
cognitive safety stack, authentication, data-at-rest guarantees, and the threat
boundaries. It is aimed at operators, integrators, and plugin authors.

## Security posture

Sovyx is built on five invariants: **zero-trust** (no component trusts another
without proof), **defense-in-depth** (no single layer is considered sufficient),
**local-first** (core functionality works entirely offline — cloud is opt-in),
**fail-closed** (when a check errors rather than explicitly allows, it denies),
and **least-privilege** (plugins only receive the capabilities they declare in
their manifest). Every invariant is materialized in code and exercised by the
test suite.

---

## Plugin sandbox — five layers

Sovyx plugins run in-process and are constrained by five independent layers.
Each layer is enforced separately; bypassing one does not bypass the others.

### Layer 0 — AST scanner

Runs at install time and in CI. `src/sovyx/plugins/security.py` parses every
Python file in the plugin directory and rejects dangerous constructs against
three registries.

`BLOCKED_IMPORTS` (root module names):

```
os, subprocess, shutil, sys, importlib, ctypes, pickle, marshal,
code, codeop, compileall, multiprocessing, threading, signal,
resource, socket, http.server, xmlrpc, webbrowser, turtle, tkinter
```

`BLOCKED_CALLS`:

```
eval, exec, compile, __import__
```

`BLOCKED_ATTRIBUTES`:

```
__import__, __subclasses__, __bases__, __globals__, __code__, __builtins__
```

A curated `ALLOWED_IMPORTS` set covers the safe stdlib and `pydantic`/`aiohttp`
explicitly, so submodules like `os.path` still work. Any `critical` finding
blocks installation; `warning` findings require explicit user approval.

### Layer 1 — ImportGuard

`ImportGuard` is a `sys.meta_path` hook installed while a plugin executes. It
catches dynamic bypasses the AST cannot see: string concatenation into
`__import__`, lazy imports inside function bodies, `importlib` after load. The
guard is a per-plugin `MetaPathFinder` that raises `ImportError` on blocked
modules and uninstalls cleanly when the plugin finishes setup.

### Layer 2 — Permission enforcer

Every `PluginContext` access goes through `PermissionEnforcer.check(...)`.
Thirteen capability-based permissions are declared by plugins in `plugin.yaml`
and must be approved by the user on install:

| Permission           | Risk   | Description                                       |
| -------------------- | ------ | ------------------------------------------------- |
| `brain:read`         | low    | Search concepts and episodes                      |
| `brain:write`        | medium | Create new concepts                               |
| `event:subscribe`    | low    | Listen to engine events                           |
| `event:emit`         | low    | Publish custom events                             |
| `network:local`      | medium | Connect to LAN services                           |
| `network:internet`   | high   | Connect to allowlisted internet domains           |
| `fs:read`            | low    | Read within the plugin's data dir                 |
| `fs:write`           | medium | Write within the plugin's data dir                |
| `scheduler:read`     | low    | List timers and reminders                         |
| `scheduler:write`    | medium | Create, modify, or cancel timers                  |
| `vault:read`         | medium | Read plugin-scoped credentials                    |
| `vault:write`        | medium | Store plugin-scoped credentials                   |
| `proactive`          | medium | Send messages without being prompted              |

After ten consecutive permission denials a plugin is auto-disabled
(`PluginAutoDisabledError`) to prevent brute-force probing.

### Layer 3 — Sandboxed filesystem

`SandboxedFsAccess` restricts I/O to the plugin's own data directory. Paths are
fully resolved (symlinks included) before every access and must remain inside
the allowed root. Traversal attempts and symlinks escaping the root are
rejected. Hard quotas:

- `50 MB` per file
- `500 MB` total per plugin

Exceeding either raises `PermissionDeniedError`.

### Layer 4 — Sandboxed HTTP

`SandboxedHttpClient` wraps `httpx` with six controls:

1. Domain allowlist declared in `plugin.yaml`.
2. Local-network blocking — loopback, RFC 1918, link-local, multicast, IPv6
   loopback and link-local. Fail-closed on parse error.
3. DNS rebinding protection — hostnames are resolved before the connection is
   opened; a private resolved IP aborts the request.
4. Rate limit — 10 requests per minute by default, sliding 60 s window.
5. Response size cap — 5 MB by default.
6. Connection timeout — 10 s by default.

---

## Cognitive safety

Sovyx treats the LLM as an untrusted component. Four checkpoints sit on the
cognitive loop:

- **Injection tracker** — sliding window over the last five messages with a
  cumulative suspicion score. Verdicts are `SAFE`, `SUSPICIOUS`, or `ESCALATE`.
- **PII guard** — scans LLM output only (input is the user's own) for API
  keys, IBAN, SWIFT, card numbers, emails, phone numbers, national IDs, and
  common secret-shaped key-value pairs. Matches are redacted before the user
  sees them.
- **Financial gate** — intercepts tool calls that look financial (by name or
  argument shape) and requires explicit confirmation. Read-only prefixes
  (`get_`, `fetch_`, `list_`, `check_`, `calculate_`, `validate_`) are
  exempt.
- **Output guard** — the last stage before the response reaches the user:
  normalize, PII, custom rules, banned topics, audit log.

All safety events are written to a local SQLite audit store with metadata only;
original content is never persisted.

---

## Authentication and tokens

**Dashboard token.** `dashboard/server.py` generates a 32-byte URL-safe token
on first start and writes it to `~/.sovyx/token` with `0o600` permissions.
The token is compared with `secrets.compare_digest`. Tests should construct
the app with an explicit token via `create_app(token=...)` — never
monkeypatch the token helpers.

```python
from sovyx.dashboard.server import create_app
app = create_app(token="your-test-token")
```

Requests carry the token as `Authorization: Bearer <token>`. WebSocket clients
pass it via a query parameter (`/ws?token=<token>`).

**Rate limiting.** A per-route sliding window is enforced at the middleware
layer. Mutating endpoints and `/api/chat` have lower ceilings than read-only
routes. Headers `X-RateLimit-Limit` and `X-RateLimit-Remaining` are returned
on every response.

**CLI daemon.** The daemon exposes JSON-RPC 2.0 over a Unix socket (Linux and
macOS) or a named pipe (Windows). The trust boundary is the local filesystem
ACL (`0o600`). There is no network listener.

---

## Data at rest

**Backup encryption.** Sovyx Cloud provides zero-knowledge backups with:

- KDF: **Argon2id** (RFC 9106 second recommendation) — memory cost 64 MiB,
  time cost 3, parallelism 4, 32-byte hash, 16-byte salt.
- Cipher: **AES-256-GCM** — 12-byte nonce, 16-byte tag.
- Wire format: `salt(16) || nonce(12) || ciphertext || tag(16)` — 44 bytes of
  overhead.

Crypto-shredding is supported by design: deleting the salt renders the
ciphertext unrecoverable, which satisfies GDPR Article 17 cryptographically.

**License tokens.** Cloud tiers use Ed25519 JWTs validated locally against an
embedded public key. Tokens are valid for seven days with a seven-day grace
period during which the daemon runs in a degraded mode rather than disabling
paid features abruptly.

**Optional SQLite encryption.** Per-mind databases can be opened against
SQLCipher 4 (AES-256-CBC + HMAC-SHA512) when a master password is set; the
default build uses plain SQLite with WAL and the data directory's OS-level
permissions.

---

## Threat boundaries

**In scope.** Malicious or buggy plugins (static and dynamic), plugins
attempting unauthorized resource access, path traversal, SSRF, DNS rebinding,
prompt injection (single and multi-turn), PII leakage through LLM output,
unauthorized financial tool calls, backup exfiltration by a cloud provider,
Bearer-token interception, license forgery, abuse by volume (rate limit plus
escalation), and post-incident detection (audit log).

**Out of scope.** Physical access to the device, OS-level privilege
escalation, kernel exploits, hardware side channels (Spectre, Rowhammer),
supply-chain compromise beyond what `pip-audit` catches, malware already
resident on the host, LLM providers logging or training on cloud queries
(mitigate by running Ollama locally), and coercion of the device owner.

---

## Security audit summary

> Phase 7 / T7.47. Latest run: 2026-05-02.

Static-analysis + dependency-policy snapshot of the codebase at
the v0.30.0 GA candidate point.

### Static analysis (Bandit)

```
Code scanned:        126,513 lines of code
Issues identified:   0
Severity:            Undefined: 0  Low: 0  Medium: 0  High: 0
Confidence:          Undefined: 0  Low: 0  Medium: 0  High: 0
Files scanned:       468 Python source files
Specifically suppressed (#nosec): 2 entries
```

The 2 specifically-suppressed `#nosec` entries are for the
`subprocess` module in `voice/_phonetic_matcher.py` (Phase 8 / T8.12)
where the binary is `shutil.which`-resolved + arguments are bounded
+ no shell. Both annotations cite the rationale inline and have
matching test coverage that exercises the subprocess timeout +
non-zero-exit + OSError paths.

Run command (CI-equivalent):

```bash
uv run bandit -r src/sovyx/ --configfile pyproject.toml
```

### Dependency security policy

Sovyx pins exact dependency versions in `uv.lock` (committed). CI
enforces `uv lock --check` so a drift between `pyproject.toml` and
`uv.lock` fails the build. Dependency upgrades happen via deliberate
PRs that touch both files — never silent.

Vulnerability scans are run via `pip-audit` against the locked
dependency tree (operator-side, not yet wired into CI as of
v0.30.0 — tracked as a v0.30.x patch item). Operators running
their own deployment SHOULD run:

```bash
uv export --no-dev | pip-audit --requirement /dev/stdin --strict
```

before each tag bump. A `--strict` exit failure should be triaged
before deployment; non-strict (informational) findings should be
documented in the operator's compliance log.

### Plugin sandbox audit posture

The five-layer sandbox documented above is the **architectural**
defense; the **operational** evidence is:

* `tests/security/` exercises every layer's reject path (50+ tests).
* `tests/unit/plugins/test_sandbox_*.py` covers the SandboxedHttpClient
  + filesystem-sandbox positive + negative paths.
* `tests/property/` includes Hypothesis-based tests pinning the
  AST scanner against arbitrary attacker-generated import graphs.

No bypass has been reported as of v0.30.0. Re-audits at every minor
version bump are operator-side per
[`OPERATOR-DEBT-MASTER-2026-05-02.md`](../docs-internal/OPERATOR-DEBT-MASTER-2026-05-02.md).

### Test coverage (T7.46 evidence)

Run on Windows 11 dev hardware, 2026-05-02:

```
13,512 tests passed, 26 skipped, 0 failed in 446 s
```

Coverage spans:

* `tests/unit/` — 12k+ unit tests across every subpackage.
* `tests/integration/` — cross-component flow tests.
* `tests/dashboard/` — 1,040 backend API tests.
* `tests/property/` — Hypothesis property-based tests including
  cross-mind isolation invariants (T8.20).
* `tests/security/` — sandbox + auth + plugin permission tests.
* `tests/stress/` — load / contention / soak tests.

CI runs on a self-hosted `sovyx-4core` runner with Linux + Python
3.11 + 3.12 matrix; local Windows runtime is ~50% of total time
because of psutil's Windows-specific handle-iteration cost
(anti-pattern #30). CI runtime stays under the 5-minute T7.46
target via parallelisation across the matrix.

### Audit log integrity

The `audit/audit.jsonl` log uses `HashChainHandler` for tamper
evidence. Each record carries a SHA-256 of the previous record's
hash, forming a chain that detects insertion, deletion, or
modification. `sovyx audit verify-chain` exits non-zero on any
break. Operators run this before submitting any audit extract to
a regulator or third party — a passing verification is the only
evidence that the extract is integrity-protected.

---

## Reporting vulnerabilities

Please email **security@sovyx.ai** with a minimal reproduction and the
affected version. Do not open a public GitHub issue for security-sensitive
findings. We aim to acknowledge reports within 72 hours and to publish a
fixed release within 30 days for critical and high-severity issues.
