# Configuration

Sovyx reads configuration from three sources, in order of priority:
environment variables (prefix `SOVYX_`, nesting delimiter `__`), YAML files
(`system.yaml` for the engine, `mind.yaml` per Mind), and built-in defaults.
Higher-priority sources override lower ones field by field.

## Files Layout

```
~/.sovyx/
├── system.yaml              # Engine-wide config (optional)
├── logs/
│   └── sovyx.log            # JSON log file (always on)
├── sovyx.sock               # Daemon RPC socket
└── <mind-name>/
    ├── mind.yaml            # Mind config — personality, LLM, brain, channels
    ├── brain.db             # SQLite brain (WAL mode)
    └── ...
```

The default data dir is `~/.sovyx`. Override it with `SOVYX_DATA_DIR` or the
`data_dir` key in `system.yaml`.

## Environment Variables

Prefix: `SOVYX_`. Nested fields use `__` (two underscores) as the delimiter.

| Variable | Maps to | Example |
|---|---|---|
| `SOVYX_DATA_DIR` | `data_dir` | `/var/lib/sovyx` |
| `SOVYX_LOG__LEVEL` | `log.level` | `DEBUG` |
| `SOVYX_LOG__CONSOLE_FORMAT` | `log.console_format` | `json` |
| `SOVYX_DATABASE__WAL_MODE` | `database.wal_mode` | `true` |
| `SOVYX_API__PORT` | `api.port` | `7777` |
| `SOVYX_HARDWARE__TIER` | `hardware.tier` | `pi` / `n100` / `gpu` / `auto` |

Provider credentials use their native names (not `SOVYX_`-prefixed):
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `XGROK_API_KEY`,
`DEEPSEEK_API_KEY`, `MISTRAL_API_KEY`, `TOGETHER_API_KEY`, `GROQ_API_KEY`,
`FIREWORKS_API_KEY`. Channel tokens use the variable named in
`channels.<channel>.token_env` (default `SOVYX_TELEGRAM_TOKEN` for Telegram).

## Engine Config — `system.yaml`

`system.yaml` is optional. When absent, defaults are used. Full schema:

```yaml
data_dir: ~/.sovyx          # Base directory for everything

log:
  level: INFO               # DEBUG | INFO | WARNING | ERROR
  console_format: text      # text | json (file handler always writes JSON)
  log_file: null            # Resolved to <data_dir>/logs/sovyx.log

database:
  data_dir: ~/.sovyx        # Where SQLite files live
  wal_mode: true            # Write-Ahead Logging
  mmap_size: 268435456      # 256 MB
  cache_size: -64000        # 64 MB (negative = KB)
  read_pool_size: 3         # Reader connections

hardware:
  tier: auto                # auto | pi | n100 | gpu
  mmap_size_mb: 128

api:
  enabled: true
  host: 127.0.0.1
  port: 7777
  cors_origins:
    - http://localhost:7777

telemetry:
  enabled: false            # Opt-in anonymous telemetry

relay:
  enabled: false            # Cloud relay for multi-device

socket:
  path: ""                  # Auto: /run/sovyx/sovyx.sock or ~/.sovyx/sovyx.sock

llm:
  routing_strategy: auto    # auto | always-local | always-cloud
  providers: []             # Engine-level provider defaults (Mind can override)
  degradation_message: |
    I'm having trouble thinking clearly right now — my language
    models are unavailable. I can still remember things and listen
    to you.
```

### Hardware tiers

`hardware.tier` controls voice model selection and memory sizing.

| Tier | Target | Voice models |
|---|---|---|
| `pi` | Raspberry Pi 5, N100 under load | Moonshine tiny, Piper (ARM-friendly) |
| `n100` | Mini-PC, laptop | Moonshine base, Piper |
| `gpu` | Workstation with CUDA/Metal | Moonshine base, Kokoro TTS |
| `auto` | Detect at start-up from CPU count, RAM, and GPU presence | |

### Voice calibration wizard

The dashboard's onboarding flow mounts a calibration wizard that runs
the full forensic diag (8-12 min) and applies the matching rule
automatically. **Default ON since v0.31.0-rc.10** — fresh installs see
the auto-fix wizard on the onboarding Voice step. Flip OFF to keep the
legacy `<HardwareDetection />` flow.

```yaml
voice:
  calibration_wizard_enabled: true   # default since v0.31.0-rc.10; flip to false to disable
```

Or via env:

```bash
export SOVYX_VOICE__CALIBRATION_WIZARD_ENABLED=false
```

Or runtime via the dashboard: **Settings → Voice → Calibration wizard**
toggle (in-memory override; restart re-reads the persisted config).

When ON, the dashboard onboarding mounts `<VoiceCalibrationStep />`;
when OFF, it falls through to the legacy hardware-detection + optional
setup wizard. Both flows persist `voice_id` + `language` to
`mind.yaml`. The recalibrate button in **Settings → Voice** is always
visible regardless of this flag (disabled with a tooltip when off).

**Cross-platform behaviour** — the underlying bash diag toolkit is
Linux-only (`voice/diagnostics/_runner.py:_check_prerequisites`
raises on non-Linux). On Windows / macOS daemons, the dashboard's
`GET /api/voice/calibration/feature-flag` response carries
`platform_supported: false`; the frontend gates the wizard mount on
`enabled AND platform_supported`, so non-Linux operators get the
legacy flow plus a banner explaining the limitation. Flipping
`calibration_wizard_enabled` on a non-Linux daemon has no visible
effect on the onboarding step (still gated by `platform_supported`).

CLI operators don't need this flag — `sovyx doctor voice --calibrate`
runs the same calibration pipeline end-to-end without the dashboard
mount (also Linux-only; non-Linux callers get a friendly
DiagPrerequisiteError message pointing at `sovyx doctor voice` for
cross-platform health checks).

## Mind Config — `mind.yaml`

Each Mind has its own YAML at `~/.sovyx/<name>/mind.yaml`. Generated by
`sovyx init`.

### Top-level fields

| Field | Type | Default |
|---|---|---|
| `name` | string | required |
| `id` | string | derived from name (lowercased, spaces → hyphens) |
| `language` | string | `en` |
| `timezone` | string | `UTC` |
| `template` | string | `assistant` |

### Personality

Two parallel personality models. `personality` drives conversational style;
`ocean` is the Big Five trait model.

```yaml
personality:
  tone: warm            # warm | neutral | direct | playful
  formality: 0.5        # 0.0 – 1.0
  humor: 0.4
  assertiveness: 0.6
  curiosity: 0.7
  empathy: 0.8
  verbosity: 0.5

ocean:
  openness: 0.7
  conscientiousness: 0.6
  extraversion: 0.5
  agreeableness: 0.7
  neuroticism: 0.3
```

### LLM

```yaml
llm:
  default_provider: anthropic              # "" for auto-detect
  default_model: claude-sonnet-4-20250514  # "" for auto-detect
  fast_model: claude-3-5-haiku-20241022    # "" for auto-detect
  local_model: llama3.2:1b                 # Ollama fallback
  temperature: 0.7
  streaming: true
  budget_daily_usd: 2.0
  budget_per_conversation_usd: 0.5
```

See [LLM Router](llm-router.md) for routing behavior.

### Brain

```yaml
brain:
  consolidation_interval_hours: 6       # 1 – 168
  dream_time: "02:00"                   # HH:MM for the nightly DREAM phase (mind tz)
  dream_lookback_hours: 24              # 1 – 168 — how far back DREAM looks at episodes
  dream_max_patterns: 5                 # 0 – 50 — set 0 to disable DREAM entirely
  max_concepts: 50000                   # 100 – 1,000,000
  forgetting_enabled: true
  decay_rate: 0.1                       # Ebbinghaus rate, 0.0 – 1.0
  min_strength: 0.01                    # Prune threshold, 0.0 – 1.0

  scoring:
    # Importance weights — MUST sum to 1.0
    importance_category: 0.15
    importance_llm: 0.35
    importance_emotional: 0.10
    importance_novelty: 0.15
    importance_explicit: 0.25
    # Confidence weights — MUST sum to 1.0
    confidence_source: 0.35
    confidence_llm: 0.30
    confidence_explicitness: 0.20
    confidence_richness: 0.15
```

Weight sums are validated at startup — a typo surfaces immediately.

### Channels

```yaml
channels:
  telegram:
    token_env: SOVYX_TELEGRAM_TOKEN      # Env var NAME, not the token itself
    allowed_users: []                    # Empty = anyone
  signal:
    enabled: true
```

Tokens are always read from environment variables. `token_env` is the
**name** of the variable Sovyx should read, never the token value.

### Safety

```yaml
safety:
  child_safe_mode: false
  financial_confirmation: true           # Require confirm for money actions
  content_filter: standard               # none | standard | strict
  pii_protection: true
  shadow_mode: false                     # Log-only mode for new rules
  guardrails:                            # Injected into system prompt
    - id: honesty
      rule: "Always be truthful. Never fabricate facts."
      severity: critical
      builtin: true
  custom_rules:
    - name: no_crypto_advice
      pattern: "(?i)(buy|sell).*(bitcoin|eth)"
      action: block                      # block | log
      message: "I don't give investment advice."
  banned_topics: []
```

### Plugins

```yaml
plugins:
  enabled: []                # Whitelist; empty = allow all discovered
  disabled: []               # Blacklist
  tool_timeout_s: 30.0

  plugins_config:
    weather:
      enabled: true
      config:
        default_unit: celsius
      permissions:
        - network.outbound
    home-assistant:
      base_url: "http://homeassistant.local:8123"  # or http://192.168.x.x:8123
      token: "<long-lived access token from HA Profile page>"
    caldav:
      base_url: "https://caldav.fastmail.com/dav/calendars/user/me@example.com/"
      username: "me@example.com"
      password: "<app-specific password>"   # iCloud + Fastmail require this
      verify_ssl: true                      # default true
      default_calendar: "Personal"          # optional
      allow_local: false                    # set true for self-hosted Nextcloud on LAN
      timezone: "America/Sao_Paulo"         # optional, defaults to UTC
```

> Google Calendar is **not** supported — Google discontinued CalDAV in 2023.
> Use Nextcloud, iCloud, Fastmail, Radicale, SOGo, or Baikal instead.

## Complete Example

A working `mind.yaml`:

```yaml
name: Aria
language: en
timezone: America/Sao_Paulo

personality:
  tone: warm
  humor: 0.5
  empathy: 0.85
  verbosity: 0.6

llm:
  default_provider: anthropic
  default_model: claude-sonnet-4-20250514
  fast_model: claude-3-5-haiku-20241022
  temperature: 0.7
  budget_daily_usd: 5.0
  budget_per_conversation_usd: 1.0

brain:
  consolidation_interval_hours: 6
  max_concepts: 100000
  forgetting_enabled: true
  decay_rate: 0.1

channels:
  telegram:
    token_env: SOVYX_TELEGRAM_TOKEN
    allowed_users: ["123456789"]

safety:
  child_safe_mode: false
  financial_confirmation: true
  content_filter: standard
  pii_protection: true

plugins:
  enabled: [calculator, weather, knowledge]
  tool_timeout_s: 30.0
```

## Tuning knobs (`SOVYX_TUNING__*`)

Thresholds, timeouts, URLs, and SHA-256 pins that used to be hardcoded
constants live on `EngineConfig.tuning`, grouped into three sub-models.
Each field is overridable at runtime via a `SOVYX_TUNING__<GROUP>__<FIELD>`
environment variable (nesting delimiter is two underscores).

```yaml
tuning:
  safety:
    classifier_budget_per_hour: 20        # LLM classifier calls per hour
    classifier_cache_ttl_seconds: 300     # memoization window
    escalation_decay_minutes: 60
    # … full list in src/sovyx/engine/config.py

  brain:
    model_url: "https://…/all-MiniLM-L6-v2.onnx"   # embedding model URL
    model_sha256: "…"                              # pinned SHA, refuses wrong file
    model_download_retries: 3
    consolidation_levenshtein_threshold: 0.85
    # …

  voice:
    auto_select_min_gpu_vram_mb: 4000     # GPU VRAM for auto-selecting Kokoro
    kokoro_model_url: "…"
    device_test_frame_rate_hz: 30
    device_test_peak_hold_ms: 1500
    # voice-linux-cascade-root-fix (T5, T10) — session-manager
    # contention detection + native-rate cascade prepend:
    cascade_native_rate_min_hz: 8000     # lower bound for hw: native-rate prepend
    cascade_native_rate_max_hz: 192000   # upper bound
    detector_pactl_timeout_s: 2.0        # pactl wall-clock cap
    detector_proc_timeout_s: 1.5         # /proc scan wall-clock cap
    detector_proc_max_scan: 5000         # /proc scan PID count cap
    detector_evidence_max_chars: 2048    # report payload truncation cap
    # … see voice-device-test module for the full device-test family
```

Examples:

```bash
# Crank the safety classifier budget for a load test.
export SOVYX_TUNING__SAFETY__CLASSIFIER_BUDGET_PER_HOUR=200

# Pin a different embedding model for an offline environment.
export SOVYX_TUNING__BRAIN__MODEL_URL=file:///opt/models/embedding.onnx
export SOVYX_TUNING__BRAIN__MODEL_SHA256=<sha256>

# Lower device-test frame rate on slow displays.
export SOVYX_TUNING__VOICE__DEVICE_TEST_FRAME_RATE_HZ=15
```

Tuning fields are **not** documented individually in this page — the
canonical source is `src/sovyx/engine/config.py` (`SafetyTuning`,
`BrainTuning`, `VoiceTuning`). Overriding them is unsupported for
deployment; change them only for benchmarks, debugging, or constrained
environments.

## Validation

Every startup validates config through Pydantic: types and ranges
(`temperature` must be `0.0–2.0`), weight sums (importance and confidence
each sum to `1.0`), required fields, and enum values (`tone`,
`content_filter`, `hardware.tier`). If validation fails, the daemon refuses
to start and prints the exact field and error.

Run a full check with `sovyx doctor`. It validates config, checks
disk/RAM/CPU, tests database connectivity, pings each configured LLM
provider, and verifies channel tokens. Use `--json` for machine-readable
output.
