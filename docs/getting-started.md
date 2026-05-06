# Getting Started

Sovyx is a self-hosted AI companion engine with persistent memory. It runs as a
Python daemon on your own machine, stores its brain in SQLite, and talks to any
LLM provider you point it at (Anthropic, OpenAI, Google, or a local Ollama).

This guide gets you from zero to a running daemon and a working chat in about
five minutes.

## Requirements

| Requirement | Version / note |
|---|---|
| Python | 3.11 or newer (3.12 recommended) |
| SQLite | 3.35+ with FTS5 compiled in (default on modern distros) |
| RAM | 512 MB minimum (runs on Raspberry Pi 5) |
| Disk | ~200 MB for the engine and ONNX models |
| LLM access | One of: Anthropic, OpenAI, or Google API key, **or** a local Ollama install |

Sovyx runs on Linux, macOS, and Windows.

## Install

Using pip:

```bash
pip install sovyx
```

Or with [uv](https://github.com/astral-sh/uv):

```bash
uv pip install sovyx
```

Verify the install:

```bash
sovyx --version
```

## First Run

### 1. Initialize a mind

```bash
sovyx init my-mind
```

This creates:

```
~/.sovyx/
├── system.yaml           # engine config (optional, has defaults)
├── logs/                 # daemon logs (JSON)
└── my-mind/
    └── mind.yaml         # mind config — personality, LLM, brain, channels
```

The name is required (1–64 chars, letters/digits/`_`/`-`, starts with a
letter) and is lowercased for the filesystem path. `sovyx init MyMind`
creates `~/.sovyx/mymind/mind.yaml`.

### 2. Set an API key

Pick one provider and export its key. Sovyx auto-detects which one is present
at start-up:

```bash
# Anthropic (default if present)
export ANTHROPIC_API_KEY=sk-ant-...

# or OpenAI
export OPENAI_API_KEY=sk-...

# or Google
export GOOGLE_API_KEY=...
```

No cloud key? Install [Ollama](https://ollama.ai) and pull a model:

```bash
curl -fsSL https://ollama.ai/install.sh | sh
ollama pull llama3.2:1b
```

Sovyx will detect Ollama on `http://localhost:11434` automatically.

### 3. Start the daemon

```bash
sovyx start
```

You should see:

```
Starting Sovyx daemon...
Sovyx daemon started
```

By default the dashboard binds to `http://127.0.0.1:7777`.

## Talk to It

### Via the dashboard

1. Open `http://localhost:7777` in your browser.
2. Get your auth token:

   ```bash
   sovyx token
   ```

3. Paste the token on the login page.

The dashboard has a chat page, a live brain graph, conversation history, logs,
and plugin management.

### Via Telegram

Add your bot token to `~/.sovyx/my-mind/mind.yaml`:

```yaml
channels:
  telegram:
    token_env: SOVYX_TELEGRAM_TOKEN
```

Set the environment variable and restart:

```bash
export SOVYX_TELEGRAM_TOKEN=123456:ABC-DEF...
sovyx stop
sovyx start
```

Then message your bot on Telegram.

## Check It's Healthy

```bash
sovyx status
sovyx doctor
```

`doctor` runs a tiered health check: disk, RAM, CPU, model files, and
configuration (offline), plus database, brain, LLM connectivity, channels, and
cost budget (online, requires the daemon). Use `sovyx doctor --json` for
machine-readable output.

### Voice diagnostics + calibration (Linux)

If voice capture isn't working, the same `doctor` command has a voice subsurface:

```bash
sovyx doctor voice                       # quick health check (cross-platform)
sovyx doctor voice --full-diag           # 8-12 min forensic diag + triage (Linux)
sovyx doctor voice --fix --yes           # apply known mixer remediation
sovyx doctor voice --calibrate           # full pipeline: fingerprint + diag + rules
sovyx doctor voice --calibrate --show    # render last persisted profile (read-only)
sovyx doctor voice --calibrate --rollback  # restore prior profile from .bak slot
```

`--calibrate` produces a `CalibrationProfile` at
`<data_dir>/<mind_id>/calibration.json` recording every applicable decision
(set / advise / preserve) with full provenance. By default the profile
is **unsigned** (LENIENT-loadable; STRICT mode rejects); pass
`--signing-key <pem-path>` to sign with an Ed25519 private key. The
verdict renderer surfaces signed/unsigned status so you know at a glance.
See [modules/voice-calibration.md](modules/voice-calibration.md) for the
rule registry, profile schema, telemetry namespace, signing model, and
rollback semantics.

The dashboard onboarding wizard can host the calibration step. The mount is
gated by `EngineConfig.voice.calibration_wizard_enabled` (default `False`
during the v0.30.x soak). To opt in:

```bash
SOVYX_VOICE__CALIBRATION_WIZARD_ENABLED=true sovyx start
```

Or in `system.yaml`:

```yaml
voice:
  calibration_wizard_enabled: true
```

The Settings → Voice → Advanced section also exposes a runtime toggle that
flips the in-memory copy on the running daemon (the env / yaml change is
still required for the value to survive a daemon restart).

#### Calibration FAQ

**Q: How long does the first calibration take?**

8–12 minutes on first run. The pipeline captures a hardware fingerprint
(~1 s), runs the bundled forensic diagnostic (8–12 min, includes 3 short
speech windows), triages the result, evaluates the rule engine, and
persists a `CalibrationProfile` (unsigned by default; pass
`--signing-key` to sign). Subsequent runs on the same hardware replay
the cached profile in ~5 seconds via the fast path.

**Q: My calibration ended in `fallback`. What happened?**

The orchestrator hit a precondition it can't satisfy: missing `bash 4+`,
non-Linux platform, the diag selftest aborted, or the hypothesis triage
couldn't crown a winner with confidence ≥ 0.7. The dashboard renders the
specific reason in the FALLBACK banner; common values are
`diag_prerequisite_unmet`, `diag_run_failed`, `triage_failed`. Click
"Use simple setup" to fall back to the v0.30.x device-test wizard.

**Q: Can I roll back a calibration I disagree with?**

Yes — `sovyx doctor voice --calibrate --rollback` restores the prior
profile from `<data_dir>/<mind_id>/calibration.json.bak`. Single-step
only; the .bak slot is consumed by the swap. To regenerate, re-run
`--calibrate` after rollback.

**Q: How do I see what the engine decided without applying anything?**

`sovyx doctor voice --calibrate --dry-run` runs the full pipeline but
skips persistence + state mutation. Pair with `--explain` to render the
rule trace (which rules fired, what conditions matched, what they
produced).

**Q: Where do `voice.diagnostics.*` and `voice.calibration.*` events go?**

To `<data_dir>/logs/sovyx.log` (file handler always JSON) and to the
configured OTel collector if observability is enabled. Closed-enum
fields keep the cardinality bounded: `mode ∈ {full, skip_captures,
surgical}`, `path ∈ {fast, slow, fallback, unknown}`, `step ∈ {probe,
fast_path, slow_path, review, fallback, unknown}`.

#### Calibration data flow

```text
operator                bash diag                triage             engine             applier
   │                        │                       │                  │                  │
   ├─ --calibrate ─────────▶│                       │                  │                  │
   │                        │── 8-12min (W1..W3) ──▶│                  │                  │
   │                        │  result.tar.gz ──────▶│                  │                  │
   │                        │                       │── HypothesisVerdict ───────────────▶│
   │ ◀────────────────────────────────────── voice.calibration.*       │                  │
   │                                              ▲                    │                  │
   │                                              │                    ▼                  │
   │                                              │           CalibrationProfile          │
   │                                              │           (frozen + signed)           │
   │                                              │                    │                  │
   │                                              │                    ▼                  │
   │                                              │     <data_dir>/<mind_id>/calibration.json
   │ ◀──────────────── advised_actions [ "sovyx doctor voice --fix --yes" ] ──────────────│
```

## Common Commands

| Command | Does |
|---|---|
| `sovyx init <name>` | Create a new mind (name required) |
| `sovyx start` | Start the daemon and dashboard |
| `sovyx stop` | Stop the daemon |
| `sovyx status` | Show daemon status |
| `sovyx doctor` | Run health checks |
| `sovyx token` | Print the dashboard auth token |
| `sovyx logs --follow` | Follow the daemon log stream |
| `sovyx brain search "query"` | Search concepts in the brain |
| `sovyx brain stats` | Brain counts and growth |
| `sovyx mind list` | List active minds |
| `sovyx plugin list` | List installed plugins |

## Next Steps

- **[Configuration](configuration.md)** — everything you can set in
  `mind.yaml` and `system.yaml`, plus the `SOVYX_*` env vars.
- **[Architecture](architecture.md)** — how the cognitive loop, brain graph,
  and router fit together.
- **[LLM Router](llm-router.md)** — how Sovyx picks a model for each message.
- **[Plugin Developer Guide](plugin-developer-guide.md)** — write your own
  tools the LLM can call.
