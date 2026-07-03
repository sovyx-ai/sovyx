# LLM Provider Integrity

Mission C6 ships the LLM-provider discovery + health-monitoring contract.
This page is the operator playbook for the surface the daemon now
exposes: refined verdict taxonomy, banner remediation chips,
`sovyx llm doctor` triage, `sovyx llm setup` wizard, and the
`/api/llm/health` + `/api/llm/test-connection` REST endpoints.

## Why this exists

Before Mission C6, an operator who started `sovyx` with no cloud API
keys AND no Ollama daemon running saw exactly one structured log line
(`no_llm_provider_detected`) buried 374 lines into the boot log. The
composite dashboard banner shipped by Mission C4 v0.46.0 surfaced this
state as a single reason (`no_llm_provider`) with the chip "Install
Ollama" — wrong remediation for the (very common) case of "Ollama is
already installed but the daemon isn't running."

Mission C6 refines the LLM-axis taxonomy into seven distinct reason
tokens, each with verdict-specific action chips that match the actual
operator action required.

## Verdict taxonomy

`scan_llm_provider_health()` returns one of eight `DiscoveryVerdict`
values. The first match in the table below wins (precedence is
top-to-bottom).

| Verdict | Severity | Triggered when | Banner chips |
|---|---|---|---|
| `OLLAMA_UNREACHABLE` | error | `mind.yaml` declares `default_provider: ollama` AND Ollama ping fails (regression from known-good state) — checked FIRST, before `NO_PROVIDER_CONFIGURED`, so the "Start Ollama" chip surfaces | Start Ollama, Run sovyx llm doctor |
| `NO_PROVIDER_CONFIGURED` | critical | No provider configured at all (no cloud keys AND Ollama not reachable) | Run sovyx llm setup, Install Ollama |
| `OLLAMA_NO_MODELS` | warn | No cloud providers configured AND Ollama running + reachable + `list_models()` returns empty | Browse model library, Run sovyx llm doctor |
| `CLOUD_KEY_INVALID` | error | Every configured cloud key has been validated as invalid AND Ollama is not reachable | Open provider settings, Test connection |
| `ALL_PROVIDERS_UNHEALTHY` | error | At least one provider configured but none currently available | View provider health, Run sovyx llm doctor |
| `DEFAULT_MODEL_UNAVAILABLE` | error | A provider is available but `mind_config.llm.default_model` cannot be served by any available provider | Open provider settings |
| `PARTIAL_HEALTH` | warn | One or more providers available + one or more configured but unhealthy | View provider health |
| `FULLY_AVAILABLE` | (none) | Every configured provider available (fallthrough — no higher rule matched) | — |

## CLI surface

### `sovyx llm doctor`

Runs the discovery scanner against the live process environment + a
fresh Ollama ping. Prints a verdict + per-provider matrix +
verdict-specific remediation. Exits with code 0 on healthy /
partial-health verdicts; 1 on degraded verdicts. Use `--json` for
machine-readable output (pipes cleanly to `jq`).

```text
$ sovyx llm doctor

Sovyx LLM — provider health
  NO_PROVIDER_CONFIGURED  (configured=0, available=0)

  ┏━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━┓
  ┃ Provider  ┃ Env var           ┃ Configured ┃ Reachable ┃ Failure      ┃
  ┡━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━┩
  │ anthropic │ ANTHROPIC_API_KEY │ no         │ no        │ no_key       │
  ...
  │ ollama    │ (local)           │ no         │ no        │ ping_failed  │
  └───────────┴───────────────────┴────────────┴───────────┴──────────────┘

  No LLM provider is configured. Run 'sovyx llm setup' to onboard a cloud
  key OR install Ollama (https://ollama.ai) for a local fallback.
```

The alias `sovyx llm health` is identical to `doctor` — provided so
operators discover the command via either entry point.

### `sovyx llm setup`

Interactive wizard. Prompts for provider choice → API key (hidden
input for cloud providers) → validates the key against the provider's
API → persists `ENV_VAR=value` into `~/.sovyx/secrets.env` with `0o600`
permissions.

```text
$ sovyx llm setup

Choose an LLM provider:
  [1] anthropic (ANTHROPIC_API_KEY)
  [2] openai (OPENAI_API_KEY)
  ...
  [10] ollama (local)
Provider number [1]: 1
API key for anthropic:
[probing...]
✓ anthropic configured. Key persisted to /home/me/.sovyx/secrets.env.
```

For scripted onboarding (CI, fleet provisioning), use
`--non-interactive` with `--provider <name> --api-key <key>`:

```text
$ sovyx llm setup --non-interactive --provider anthropic --api-key sk-...
```

Setup for Ollama (no API key required) just verifies the daemon is
reachable and that at least one model is installed.

## Composite banner

When any non-healthy verdict fires, the daemon records a
`DegradedEntry(axis="llm", reason=<verdict-token>, …)` into the
`EngineDegradedStore`. The dashboard composite banner (Mission C4)
renders this alongside the voice / dashboard axes. Operators on a
CLI-only deployment see the same state in the `sovyx doctor` aggregate
surface (the LLM section appears alongside voice + dashboard).

The store is dual-emitted with the legacy `no_llm_provider_detected`
WARN through the v0.49.x cycle (ADR-D14 LENIENT discipline). Phase 3
v0.50.0 drops the legacy event.

## Liveness probe

`LLMLivenessProbe` is a single asyncio background task (anti-pattern
\#15 — bounded cardinality) that re-runs the discovery scan at the
cadence configured by `SOVYX_TUNING__LLM__LIVENESS_CHECK_INTERVAL_SEC`
(default 60 s, bounded [10, 600]). On verdict transition the probe
dispatches through `dispatch_llm_discovery_verdict` to refresh the
composite-store entry + emit `llm.liveness_probe.transition`.

Transitions from healthy to unhealthy are filtered by a grace period
(`SOVYX_TUNING__LLM__PROVIDER_UNHEALTHY_GRACE_PERIOD_SEC`, default
30 s) — transient blips shorter than the grace window don't flap the
banner. Recovery transitions (unhealthy → healthy) promote
immediately.

To opt out of the periodic probe set
`SOVYX_TUNING__LLM__LIVENESS_CHECK_ENABLED=false`. The boot-time scan
still runs; mid-session transitions just won't be detected without it.

## REST endpoints

### `GET /api/llm/health`

Returns the cached `LLMRouterDiscoveryReport` snapshot. The report is
primed at boot by `bootstrap.py` and refreshed by `LLMLivenessProbe` on
every tick — the endpoint is cheap and idempotent.

```json
{
  "verdict": "fully_available",
  "configured_count": 2,
  "available_count": 2,
  "default_provider": "anthropic",
  "default_model": "claude-sonnet-4-6",
  "scan_duration_ms": 0.04,
  "per_provider": [
    {"name": "anthropic", "env_var": "ANTHROPIC_API_KEY", "configured": true, "reachable": null, "failure_reason": null},
    {"name": "ollama", "env_var": "", "configured": true, "reachable": true, "failure_reason": null}
  ]
}
```

The response model is forward-additive (`extra="allow"`) per
anti-pattern \#40 — future fields don't break consumers.

### `POST /api/llm/test-connection`

Probes a candidate provider WITHOUT persisting or hot-registering.
Used by the dashboard provider-settings page to validate
operator-pasted keys before commit.

```bash
$ curl -X POST -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"provider": "anthropic", "api_key": "sk-..."}' \
    http://127.0.0.1:7777/api/llm/test-connection

{"ok": true, "message": "OK", "latency_ms": 412.83}
```

Returns `{ok: false, message: …, latency_ms: …}` on validation
failure. Never modifies process state.

## Quality Gate 12

`scripts/dev/check_llm_provider_discipline.py` enforces wire-discipline
across five consumer surfaces:

1. `engine/bootstrap.py` — every `LLMProviderKey` member registered.
2. `dashboard/routes/onboarding.py` — env-var map covers every cloud
   member.
3. `dashboard/src/locales/{en,pt-BR,es}/voice.json` —
   `degraded.llm.providers.<key>.{label,envVar}` keys for every member.
4. `cli/_provider_setup_shared._DEFAULT_MODEL_BY_PROVIDER` — default-model
   mapping covers every member.
5. `docs/configuration.md` — env-var documented for every cloud member.

LENIENT through v0.49.x; STRICT-promoted in `verify_gates.sh` at
v0.50.0 (Mission C6 Phase 3 §T5.1). The `# c6-allowlist: <surface_id>`
inline comment escape hatch on a `_provider_registry.LLMProviderKey`
member exempts that member from the specified surface check.

## Tuning knobs

All under `SOVYX_TUNING__LLM__*`:

| Knob | Default | Bounds | Description |
|---|---|---|---|
| `LIVENESS_CHECK_ENABLED` | True | bool | Kill-switch for the background probe. |
| `LIVENESS_CHECK_INTERVAL_SEC` | 60.0 | [10.0, 600.0] | Re-scan cadence. |
| `BOOT_KEY_VALIDATION_ENABLED` | False | bool | Probe each cloud key at boot. Opt-in (cloud probes cost money). |
| `BOOT_KEY_VALIDATION_TIMEOUT_SEC` | 5.0 | [1.0, 30.0] | Per-key timeout when validation is enabled. |
| `PROVIDER_UNHEALTHY_GRACE_PERIOD_SEC` | 30.0 | [0.0, 300.0] | Transient-blip filter on healthy→unhealthy transitions. |
| `COGNITIVE_DEGRADED_MODE_FAIL_FAST` | True | bool | Short-circuit `CognitiveLoop.process_request` on missing LLM (Phase 1.D). |

Plus the pre-existing circuit-breaker tunables
(`CIRCUIT_BREAKER_FAILURES`, `CIRCUIT_BREAKER_RESET_SECONDS`) which
gate per-provider call retries.

## OTel semantic conventions

The Mission C6 events join the existing voice + dashboard taxonomy:

| Event | Severity | Fields |
|---|---|---|
| `llm.discovery.report` | INFO | `verdict`, `configured_count`, `available_count`, `default_provider`, `default_model`, `scan_duration_ms` |
| `llm.liveness_probe.disabled` | INFO | `reason` |
| `llm.liveness_probe.started` | INFO | `interval_sec`, `grace_period_sec` |
| `llm.liveness_probe.stopped` | INFO | — |
| `llm.liveness_probe.tick_failed` | WARN | `error`, `error_type` |
| `llm.liveness_probe.transition` | INFO | `from_verdict`, `to_verdict` |
| `llm.liveness_probe.unhealthy_grace_armed` | INFO | `verdict`, `grace_period_sec` |
| `no_llm_provider_detected` | WARN | `hint`, `proximate_cause` (LEGACY — dropped at v0.50.0) |

Phase 1.D (v0.49.3) extends with `cognitive.loop.started_in_degraded_mode`
+ `cognitive.loop.short_circuit_degraded` + `cognitive.loop.gate.dependency_check_failed`.

## Anti-pattern \#44

The structural lesson of Mission C6 — "dependency-gated workers MUST
verify their dependency at startup, emit a structured
`started_in_degraded_mode` signal AND a composite-store entry when the
dependency is absent, AND gate every iteration on the dependency" —
ships as anti-pattern \#44 at v0.50.0 alongside the Quality Gate 12
STRICT-promotion.
