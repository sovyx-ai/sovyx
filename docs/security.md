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

**Thread-safety (v0.32.0):** `sys.meta_path` is a process-global mutable list.
`ImportGuard.install` and `uninstall` mutate it under a module-level
`threading.Lock` so two plugins running concurrent tools cannot corrupt the
list — the worst-case pre-fix outcome was plugin A's guard being removed by
plugin B's exit, leaving plugin A executing without import enforcement.

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

### Plugin discovery — entry-point supply-chain gate (v0.32.0)

Sovyx discovers plugins from three sources: programmatic registration,
local directories under `~/.sovyx/plugins/`, and pip-installed packages
that register a `sovyx.plugins` entry point. The third path is a
classic supply-chain risk: any pip package could ship arbitrary code in
its module body and have it auto-execute on the next daemon boot —
**before** the AST scanner could even run, because importing the entry
point's module is what triggers the AST-violating code.

The fix in v0.32.0 is **default-deny** for third-party packages:

* **First-party** plugins (`ep.dist.name == "sovyx"`) always load.
  These ship in the same wheel as the engine and are covered by the
  same release-signing posture.
* **Third-party** plugins are skipped without ever calling `ep.load()`
  unless the operator has explicitly opted in:

  1. `EngineConfig.plugins.allow_third_party_plugins = true` — master
     gate that enables the allowlist check.
  2. `EngineConfig.plugins.trusted_plugin_packages` — list of exact
     pip package names the operator has audited.

Operators flip the gates via env vars or `system.yaml`:

```bash
SOVYX_PLUGINS__ALLOW_THIRD_PARTY_PLUGINS=true \
SOVYX_PLUGINS__TRUSTED_PLUGIN_PACKAGES='["sovyx-finance","my-org-plugin"]' \
    sovyx start
```

Every skip emits a structured `plugin.entry_point.skipped_third_party`
event with the package name and reason (`default_deny` /
`not_in_allowlist`) so operators have an audit trail of what a daemon
COULD have loaded but didn't.

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

## Cross-mind isolation

Sovyx supports multiple Minds running concurrently in one daemon, each with its
own SQLite database, voice identity, retention policy, and configuration. The
isolation contract is enforced both at storage layer (database-per-mind) and at
runtime — every request that touches per-mind state MUST resolve the active
`mind_id` at the route boundary, never accept a sentinel default.

Anti-pattern #35 in [`CLAUDE.md`](https://github.com/sovyx-ai/sovyx/blob/main/CLAUDE.md) documents the failure mode: a
low-level config field with `mind_id: str = "default"` propagating as a
sentinel through layers that fail to overwrite it, causing voice / brain /
calibration state to land under a phantom `"default"` mind even when the
operator has created `"meu-mind"`. The structural mitigations:

- **Backend:** `dashboard/_shared.resolve_active_mind_id_for_request` queries
  `MindManager.get_active_minds()` at the route boundary; `voice/factory/__init__.py`
  emits a `voice.factory.mind_id_default_sentinel` structured WARN when the
  sentinel reaches the factory (lenient detection, with telemetry).
- **Frontend:** `useResolvedMindId` hook is the only sanctioned source of
  `mind_id` in any component. An ESLint rule (`no-restricted-syntax`) blocks
  literal `"default"` assignments to `mind_id` props.
- **Property tests:** `tests/property/test_cross_mind_isolation_t820.py`
  pins cross-mind state isolation under Hypothesis-generated message
  interleavings.

Operator-set strings (mind names, device names, friendly names) are hashed
or removed in telemetry per the calibration retention contract below.

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
version bump are operator-side per the operator-debt master ledger
(`docs-internal/OPERATOR-DEBT-MASTER-2026-05-03.md`, gitignored — internal
reference only).

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

## Calibration telemetry retention

The voice calibration subsystem (Layer 2 + Layer 3 — wizard + applier
+ persistence + KB cache) emits structured events under two prefixes:

* `voice.calibration.*` — engine, applier, persistence, wizard,
  KB cache (~25 distinct events).
* `voice.diagnostics.*` — full-diag runner + cancellation
  (~5 events).

### Where events go

* **Local file:** `<data_dir>/logs/sovyx.log` (rotated per Sovyx's
  standard logging policy; default 50 MB per file, 5-file retention
  via `RotatingFileHandler`).
* **OTel collector** (when configured via env per the standard
  Sovyx tracing setup): events ship to whatever sink the operator
  configured.

### What's hashed vs raw (post-P0 + P1)

Per the privacy contract enforced by
`tests/integration/test_telemetry_privacy_audit.py`:

* **Hashed** (16-hex SHA256 prefix via `sovyx.observability.privacy.short_hash`):
  - `mind_id_hash` — replaces operator-set `mind_id`.
  - `job_id_hash` — replaces calibration job IDs.
  - `profile_id_hash` — replaces UUID4 calibration profile IDs.
  - `cached_mind_id_hash` — KB cache lookups.
* **Raw** (closed-enum fields with bounded cardinality):
  - `status`, `step`, `path`, `mode`, `signature_status`, `verdict`,
    `prompt_type`, `rule_id`, `triage_winner_hid`, `audio_stack`,
    `system_vendor`, `system_product`, `failure_reason`, `trigger`,
    `rollback_reason`.
* **Removed** (P1 v0.30.29): no filesystem `path` fields. The pre-P0
  loader emitted absolute paths to operator-host filesystem; those
  fields were deprecated in P0 and dropped in P1.

### Voice telemetry — `host_api` label (v0.31.7 LOW.6)

The voice subsystem emits a `host_api` field on `audio.apo.scan`
(legacy) / `audio.capture_chain.scan` (Mission H2 v0.49.9 neutral
sibling — dual-emitted through v0.51.0 STRICT per ADR-D14),
`voice_apo_detected`, `voice.health.cold_probe`, and other
audio-stack-tagging events. The value is a closed-enum bounded-
cardinality label identifying the audio host API in use:

* Windows: `WASAPI`, `WASAPI Exclusive`, `MME`, `DirectSound`, `WDM-KS`.
* Linux: `ALSA`, `PipeWire`, `PulseAudio`, `JACK`.
* macOS: `Core Audio`.

The label is logged verbatim (no hashing) because it carries low
entropy (~6 distinct values across the operator fleet), it's a
mechanical property of the OS audio stack rather than an
operator-set string, and self-hosted operators audit their own logs
— there is no cross-operator privacy boundary the field could
breach. Operators reviewing log exports for sharing with third
parties (vendor support, community triage) should be aware that the
field reveals the audio stack but nothing about the operator
identity, the mind, the device serial, or any user-set name.

This is the only voice-telemetry label with low cardinality that is
intentionally NOT redacted. All operator-set strings (mind names,
device names, endpoint names, friendly names) are either hashed or
removed entirely per the rules above.

### Cryptographic verdicts (P4 v0.30.32+)

Calibration profile signatures are verified against
`src/sovyx/voice/calibration/_trusted_keys/v1.pub` (Ed25519). The
`voice.calibration.profile.signature.invalid{verdict}` event carries
the closed-enum verdict for forensic triage. See the
"Cryptographic primitives" section above for the algorithm contract.

### How operators audit

Grep `<data_dir>/logs/sovyx.log` for `voice.calibration.` to see all
calibration events. Sample queries:

```bash
# All signature verdicts the loader emitted on startup
jq 'select(.event | startswith("voice.calibration.profile.signature"))' sovyx.log

# Apply-rollback occurrences
jq 'select(.event == "voice.calibration.applier.apply_failed_with_rollback")' sovyx.log

# Migration failures (if a future schema bump runs)
jq 'select(.event == "voice.calibration.profile.migration_failed")' sovyx.log
```

### CI gate

`tests/integration/test_telemetry_privacy_audit.py` walks every
`voice.calibration.*` and `voice.diagnostics.*` emission in three
end-to-end scenarios (slow-path DONE / FALLBACK / CANCELLED) plus
direct module emissions (persistence, KB cache, progress) and
asserts ZERO field values match the raw-mind-id heuristic
(non-hex string > 16 chars) or the filesystem-path heuristic
(starts with `/`, `\`, `C:`, `D:`). The gate fails CI on any new
emission that leaks an operator-set string. The complete exempt
list (closed enums, dynamic exception text, deprecated aliases) is
documented in the test file itself.

---

## Reporting vulnerabilities

Please email **security@sovyx.ai** with a minimal reproduction and the
affected version. Do not open a public GitHub issue for security-sensitive
findings. We aim to acknowledge reports within 72 hours and to publish a
fixed release within 30 days for critical and high-severity issues.
