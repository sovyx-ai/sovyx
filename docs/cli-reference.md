# CLI Reference

The `sovyx` command is the single entry point for the daemon, the
interactive REPL, the diagnostic suite, and every subsystem management
surface. All commands are typer-driven; every subcommand accepts
`--help` for usage detail.

```
sovyx [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
|---|---|
| `--version`, `-v` | Show installed Sovyx version. |
| `--install-completion` | Install shell completion for the current shell (bash / zsh / fish / PowerShell). |
| `--show-completion` | Print completion code for the current shell — copy / customize / source manually. |
| `--help` | Show the top-level usage block + command list. |

The default config + data path is `~/.sovyx/`. Override with
`SOVYX_DATA_DIR` (see [`configuration.md`](configuration.md)).

---

## Top-level commands

| Command | Purpose |
|---|---|
| [`init`](#sovyx-init) | Bootstrap a fresh `~/.sovyx/` (config + data dir). |
| [`start`](#sovyx-start) | Start the daemon (REST + WebSocket + cogloop + voice). |
| [`stop`](#sovyx-stop) | Stop a running daemon. |
| [`status`](#sovyx-status) | One-shot daemon status snapshot. |
| [`token`](#sovyx-token) | Show / copy the dashboard authentication token. |
| [`chat`](#sovyx-chat) | Interactive REPL with the active mind. |
| [`logs`](#sovyx-logs) | Query / filter the structured log file. |
| [`brain`](#sovyx-brain) | Brain memory commands (search / stats / analyze scores). |
| [`mind`](#sovyx-mind) | Mind management (list / status / forget / retention). |
| [`dashboard`](#sovyx-dashboard) | Dashboard management — show access info; bundle integrity doctor. |
| [`doctor`](#sovyx-doctor) | Cross-subsystem health checks + auto-fix tools. |
| [`llm`](#sovyx-llm) | LLM provider health doctor + interactive setup wizard. |
| [`plugin`](#sovyx-plugin) | Plugin management (list / install / disable). |
| [`voice`](#sovyx-voice) | Voice setup + voice-data lifecycle (setup / forget / history / train-wake-word / generate-signing-key). |
| [`audit`](#sovyx-audit) | Tamper-evident audit log inspection (verify-chain). |
| [`kb`](#sovyx-kb) | Mixer-profile knowledge base inspector. |

---

## `sovyx init`

Create `~/.sovyx/system.yaml` + `~/.sovyx/logs/` + a default mind directory.
Idempotent — re-running prints dim "already exists" lines for everything
already in place.

```bash
sovyx init
```

After `init`, run `sovyx start` and `sovyx token` to get the dashboard
URL + auth token.

---

## `sovyx start`

Start the daemon. Brings up the bridge channels, registers the cognitive
loop, mounts the FastAPI dashboard at `127.0.0.1:7777`, and (when
configured) starts the voice pipeline.

```bash
sovyx start
```

On Linux, integration with systemd is documented at
[`voice-setup-linux-mint.md`](voice-setup-linux-mint.md).

The first start may download voice ONNX models on demand; the dashboard
banner surfaces download progress.

---

## `sovyx stop`

Stop a running daemon gracefully (drains in-flight TTS, releases audio
devices, closes WebSocket connections, drains the bridge channel queues).

---

## `sovyx status`

One-shot snapshot of daemon state: running / stopped + uptime + mind
summary. Output mirrors `GET /api/status`.

---

## `sovyx token`

Print the dashboard auth token (32 url-safe bytes generated on first
start, stored at `~/.sovyx/token` with `0o600`).

```bash
sovyx token              # print to stdout
sovyx token --copy       # also copy to clipboard (where available); short form -c
```

NEVER paste this token into chat logs / screenshots. It grants full
control of the local daemon.

---

## `sovyx chat`

Open an interactive REPL with the active mind. Uses the LLM provider
selected by the current `MindConfig` (see
[`llm-router.md`](llm-router.md)). Slash-commands inside the REPL:

| Command | Effect |
|---|---|
| `/help` | List commands (`/?` also works). |
| `/status` | Daemon health, uptime, today's LLM cost. |
| `/minds` | List active minds (which one is the default). |
| `/config` | Show the active mind's config (read-only). |
| `/new` | Start a fresh conversation (rotates the conversation id). |
| `/clear` | Clear the screen (and reset the conversation id). |
| `/exit` | Quit the REPL (`/quit` and Ctrl+D also work). |

---

## `sovyx logs`

Query and filter the structured log file. Supports time-range filters,
`key=value` field filters, and Bash-friendly piping.

```bash
sovyx logs --since 30m
sovyx logs --level warning --since 1h
sovyx logs -f module=brain
sovyx logs --json | jq 'select(.event | startswith("voice.frame"))'
```

| Option | Description |
|---|---|
| `--since <duration>`, `-s` | Tail since the given duration (e.g. `30s`, `5m`, `1h`, `2d`). |
| `--level <level>`, `-l` | Minimum level: `debug` / `info` / `warning` / `error`. |
| `--filter <key=value>`, `-f` | Filter by structured field (repeatable). |
| `--limit <N>`, `-n` | Max lines to show (default 50). |
| `--json` | Emit the raw JSON-per-line stream. |
| `--follow`, `-F` | Stream new entries as they arrive (`tail -f` mode). |
| `--file <path>` | Read a specific log file (default `~/.sovyx/logs/sovyx.log`). |

---

## `sovyx brain`

Brain memory inspection. Subcommands:

| Subcommand | Description |
|---|---|
| `sovyx brain search "<query>" [--mind <id>] [--limit N]` | Search concepts in the brain. |
| `sovyx brain stats [--mind <id>]` | Show brain statistics (concept / episode / relation counts). |
| `sovyx brain analyze scores` | Importance + confidence score distribution report. |

All brain subcommands require a running daemon. To bootstrap a new
mind, use `sovyx init <name>` (there is no `mind create` subcommand).

---

## `sovyx mind`

Mind management:

| Subcommand | Description |
|---|---|
| `sovyx mind list` | List active minds. |
| `sovyx mind status <name>` | Show mind status. |
| `sovyx mind forget <id> [--dry-run] [--yes]` | Right-to-erasure (GDPR Art. 17 / LGPD Art. 18 VI) — wipes every per-mind data row (brain + conversations + system stats + voice consent ledger); the mind's configuration is preserved. |
| `sovyx mind retention prune <id> [--dry-run] [--yes]` | Apply the time-based retention policy now (GDPR Art. 5(1)(e) / LGPD Art. 16) — prunes only records older than the configured horizons. |
| `sovyx mind retention status <id>` | Read-only preview of retention horizons + prune-eligible counts. |

---

## `sovyx dashboard`

Dashboard management. The default invocation (no subcommand) prints the
current dashboard URL + token-reveal flag.

```bash
sovyx dashboard                # prints URL + "Token: use --token to reveal"
sovyx dashboard --token        # also prints the token
sovyx dashboard -t             # short form
```

### `sovyx dashboard doctor`

(Mission C5 §T3.3) — Verify the SPA bundle integrity of the installed
dashboard. Runs the four-state classifier (`FULLY_PRESENT` / `PARTIAL` /
`INDEX_HTML_MISSING` / `STATIC_DIR_MISSING` /
`LEGACY_INDEX_HTML_NO_ASSETS`) against
`~/.local/share/pipx/venvs/sovyx/.../sovyx/dashboard/static/` (or the
equivalent install path).

```bash
sovyx dashboard doctor                 # human-readable report
sovyx dashboard doctor --json | jq .   # parseable JSON
```

Exit codes:

| Code | Meaning |
|---|---|
| `0` | `FULLY_PRESENT` — every chunk referenced by `index.html` exists on disk. |
| `1` | Any non-`FULLY_PRESENT` verdict — bundle integrity violated. |

JSON output schema:

```json
{
  "verdict": "fully_present | partial | index_html_missing | static_dir_missing | legacy_index_html_no_assets",
  "static_dir": "<absolute POSIX path>",
  "index_html_path": "<absolute POSIX path>",
  "referenced_count": 42,
  "missing_count": 0,
  "orphan_count": 0,
  "missing_assets": [],
  "orphan_assets": [],
  "scan_duration_ms": 4.213
}
```

Triage workflow when the verdict is not `FULLY_PRESENT`:

1. `PARTIAL` — some referenced chunks are absent. Run
   `pipx reinstall sovyx` (or `npm run build` inside `dashboard/` when
   developing from a checkout).
2. `INDEX_HTML_MISSING` — the SPA entry point is absent. Same fix.
3. `STATIC_DIR_MISSING` — the entire `static/` dir is missing.
   Reinstall is the only path.
4. `LEGACY_INDEX_HTML_NO_ASSETS` — `index.html` exists but `assets/`
   is empty (typically a stale or interrupted developer build). Run
   `npm run build` in `dashboard/`.

See [`docs/modules/dashboard-distribution-integrity.md`](modules/dashboard-distribution-integrity.md)
for the full mission context, the related `dashboard.distribution.*`
OpenTelemetry events, and the tuning knobs under
`EngineConfig.tuning.dashboard`.

---

## `sovyx doctor`

Aggregate health-check command. Runs subsystem-specific diagnostics +
renders the operator-visible composite degraded-banner surfaces alongside
the Phase 1.C dashboard integrity surface (Mission C5 §T3.4).

```bash
sovyx doctor                  # runs the default subcommand suite
sovyx doctor --json           # machine-readable output
```

### Subcommands

| Subcommand | Description |
|---|---|
| `sovyx doctor voice` | Voice subsystem health checks (PortAudio + Linux mixer sanity + APO bypass + capture-integrity probe). `--full-diag` runs the platform-native forensic diagnostic (Linux bash toolkit; native WASAPI/APO/consent probes on Windows; unsupported on macOS). `--fix` and `--calibrate` remain Linux-only. |
| `sovyx doctor cascade` | Run the startup self-diagnosis cascade. |
| `sovyx doctor linux_session_manager_grab` | Detect whether another audio client holds the capture hardware. |
| `sovyx doctor voice_capture_apo` | Scan Windows capture-APO chain for Voice Clarity (Mission F2-M07). |
| `sovyx doctor voice_capture_integrity` | Alias of `voice_capture_apo` (platform-neutral name, Mission H2). |
| `sovyx doctor piper_locale_match` | Check whether a locale has a curated Piper voice (F2-M03↑). |
| `sovyx doctor platform` | Cross-OS platform-diagnostics report. |
| `sovyx doctor resources [--json] [--cohort <name>] [--explain <field>] [--watch] [--tracemalloc-snapshot]` | Render the engine resource-cohort snapshot (Mission H4) — live daemon RPC when reachable, in-process registry otherwise. |
| `sovyx doctor gates [--json]` | Print the Quality Gates registry — STRICT/LENIENT state + sunset target per gate. |

### Composite surfaces rendered by `sovyx doctor voice`

`sovyx doctor voice` (in its default preflight mode, i.e. without
`--fix` / `--calibrate` / `--full-diag`) renders, after the voice
preflight report, the following sections in order (Mission C4 §T3.6 +
Mission C5 §T3.4). `sovyx doctor` without a subcommand runs the
general installation health check (offline checks + daemon RPC), not
these surfaces:

1. **Voice — quarantine surface** — endpoints quarantined by the
   capture-integrity coordinator (Mission C1).
2. **Voice — failover history** — recent runtime-failover ladder runs
   (Mission C3 §T2.9).
3. **Voice — degraded banner** — cross-axis `EngineDegradedStore`
   snapshot + composite severity + per-axis action chips (Mission C4
   §T3.6).
4. **Dashboard — bundle integrity** — SPA bundle verdict + missing-chunk
   sample + remediation hint (Mission C5 §T3.4).
5. **LLM — provider health** — Mission C6 §T3.2 verdict + per-provider
   matrix summary + remediation hint. Full per-provider detail via
   `sovyx llm doctor`.

CLI-only operators see the same picture as the dashboard's composite
banner — no log-grep required.

---

## `sovyx llm`

LLM provider health doctor + interactive setup wizard. Mission C6 §T3.1.

```bash
sovyx llm doctor                  # human-readable provider matrix
sovyx llm doctor --json | jq .    # machine-readable
sovyx llm health                  # alias for doctor
sovyx llm setup                   # interactive wizard
sovyx llm setup --non-interactive --provider anthropic --api-key sk-...
```

### `sovyx llm doctor`

Runs the live discovery scan + per-provider liveness matrix. Returns
exit 0 on `FULLY_AVAILABLE` / `PARTIAL_HEALTH` verdicts and exit 1 on
any other verdict — scriptable for CI / monitoring loops.

Output sections:

1. Top-level verdict (color-coded: green for healthy, yellow for warn,
   red for error/critical).
2. Per-provider matrix (name, env-var, configured, reachable, failure
   reason).
3. Verdict-specific remediation hint.

### `sovyx llm setup`

Interactive wizard for first-time provider onboarding (or rotating
keys). Prompts for provider choice → API key (hidden input for cloud
providers) → validates the key against the provider's API → persists
to `<data_dir>/secrets.env` with `0o600` permissions.

For Ollama (no API key), the wizard just verifies the daemon is
reachable + lists installed models.

Flags:

* `--provider <name>` — skip the choice prompt.
* `--api-key <key>` — provide the key inline (cloud providers only).
* `--non-interactive` — fail-fast on missing inputs (CI / scripted use).
* `--data-dir <path>` — override the secrets.env location (default
  `~/.sovyx`).

See [docs/modules/llm-provider-integrity.md](modules/llm-provider-integrity.md)
for the full verdict taxonomy + REST endpoints reference.

---

## `sovyx plugin`

Plugin management:

| Subcommand | Description |
|---|---|
| `sovyx plugin list` | List installed plugins + entry-point source. |
| `sovyx plugin info <name>` | Show plugin manifest + permissions. |
| `sovyx plugin enable <name>` | Enable a previously-disabled plugin. |
| `sovyx plugin disable <name>` | Disable without uninstalling. |

---

## `sovyx voice`

Voice setup + voice-data lifecycle commands. The lifecycle surface is
GDPR / LGPD compliance — see [`compliance.md`](compliance.md).

| Subcommand | Description |
|---|---|
| `sovyx voice setup [--mind-id <id>] [--input-device <substring>] [--non-interactive]` | Configure the active mind's input device (interactive picker or substring match); persists to `mind.yaml`. |
| `sovyx voice forget --user-id <id> [--yes]` | Purge every ConsentLedger record for the given user id (GDPR Art. 17 / LGPD Art. 18 VI); a `DELETE` tombstone is appended so the audit trail survives the erasure. `--user-id` is required. |
| `sovyx voice history --user-id <id>` | List every ConsentLedger record for the user as JSONL (GDPR Art. 15 / LGPD Art. 18 I) — pipeable to `jq`. |
| `sovyx voice train-wake-word "<word>" [--mind-id <id>] [--language <tag>] [--target-samples N] [--negatives-dir <dir>] …` | Train a custom wake-word ONNX model; hot-reloads into the running daemon on success. |
| `sovyx voice generate-signing-key [--mind-id <id>] [--output <path>] [--force]` | Generate an Ed25519 signing keypair for calibration profiles. |

---

## `sovyx audit`

Tamper-evident audit log inspection:

| Subcommand | Description |
|---|---|
| `sovyx audit verify-chain [--since <ISO date>] [--path <file>] [--audit-dir <dir>]` | Verify the hash chain of every audit log file — exit `0` when every chain is intact, `1` otherwise. |

---

## `sovyx kb`

Inspect the mixer-profile knowledge base (the corpus of audio-mixer
configuration heuristics shipped with the voice health subsystem):

| Subcommand | Description |
|---|---|
| `sovyx kb list [--user-dir <dir>] [--shipped-only]` | List every profile with identity + match-scope + provenance (shipped + user pools). |
| `sovyx kb inspect <profile_id>` | Print a single profile's fields in human-readable form. |
| `sovyx kb validate <path>` | Validate a candidate profile YAML against the KB schema — non-zero exit on failure. |
| `sovyx kb fixtures <profile_id\|all>` | Verify HIL fixture files exist for a profile (CI-friendly with `all`). |

---

## Environment variables

The CLI respects every environment variable consumed by `EngineConfig`
and its sub-configs. Notable for CLI-driven workflows:

| Variable | Effect |
|---|---|
| `SOVYX_DATA_DIR` | Override the `~/.sovyx/` data path. |
| `SOVYX_TUNING__VOICE__*` | Voice tunable knobs (see [`configuration.md`](configuration.md)). |
| `SOVYX_TUNING__DASHBOARD__INTEGRITY_REACTIVE_ENABLED` | Toggle the dashboard bundle integrity reactive on-404 arm (default `True`). Mission C5 §T2.5. |
| `SOVYX_TUNING__DASHBOARD__INTEGRITY_REACTIVE_DEBOUNCE_SEC` | Reactive-arm debounce in seconds (default `60.0`, bounded `[10, 600]`). |
| `SOVYX_TUNING__DASHBOARD__INTEGRITY_ACTION_CHIP_REINSTALL_URL` | Override the operator-action chip reinstall target (default `https://sovyx.dev/docs/install/troubleshooting#reinstall`). |
| `SOVYX_TUNING__DASHBOARD__INTEGRITY_ACTION_CHIP_DOCTOR_URL` | Override the doctor docs URL (default `https://sovyx.dev/docs/cli/doctor#dashboard`). |
| `SOVYX_GATES_MAX_AGE_SEC` | Pre-push hook marker max age (default `1800` = 30 min). See [`contributing.md`](contributing.md) §Quality Gates. |

For the exhaustive variable catalog, see
[`docs/configuration.md`](configuration.md).

---

## Exit code contract

The general contract for `sovyx` commands:

| Code | Meaning |
|---|---|
| `0` | Success — the command's invariants hold. |
| `1` | Subsystem reported a failure (e.g. `sovyx dashboard doctor` on a partial install). |
| `2` | Argument or configuration error (typer's default). |

Two command families overload the codes with richer semantics:

**`sovyx doctor voice`** — without `--fix`, the exit code equals the
number of failing preflight steps (preserving the v0.21.2 contract —
see `docs-internal/missions/MISSION-voice-final-skype-grade-2026.md`
§Phase 1 for the historical rationale; internal doc, not shipped).
With `--fix` (and for `--calibrate` / `--full-diag` where noted) the
command steers into semantic codes:

| Code | Constant | Meaning |
|---|---|---|
| `0` | `EXIT_DOCTOR_OK` | No saturation, `--fix` succeeded, or `--dry-run` printed the plan. |
| `1` | `EXIT_DOCTOR_GENERIC_FAILURE` | Non-fix failure paths (e.g. `--full-diag` script failure). |
| `2` | `EXIT_DOCTOR_SATURATED_NOT_FIXED` | Saturation detected but `--fix` was not requested. |
| `3` | `EXIT_DOCTOR_APPLY_FAILED` | `--fix` attempted but the mixer reset failed, or the re-probe is still saturated. |
| `4` | `EXIT_DOCTOR_USER_ABORTED` | Non-TTY shell without `--yes`, or the interactive prompt was rejected. |
| `5` | `EXIT_DOCTOR_UNSUPPORTED` | Platform mismatch: `--fix` on non-Linux, `amixer` missing, or `--full-diag` on macOS. |
| `6` | `EXIT_DOCTOR_VOICE_NOT_CONFIGURED` | `--calibrate` invoked non-interactively against a mind with no configured mic — run `sovyx voice setup` or pass `--input-device`. |

**`sovyx doctor linux_session_manager_grab`** — verdict-coded:

| Code | Meaning |
|---|---|
| `0` | Detector confirmed no grab (`has_grab=false`). |
| `1` | Detector confirmed a grab — the printed process list names the culprit. |
| `2` | Detector inconclusive (`has_grab=null`), or not applicable on Windows / macOS. |

---

## See also

* [`configuration.md`](configuration.md) — full `EngineConfig` reference.
* [`api-reference.md`](api-reference.md) — HTTP + WebSocket API.
* [`modules/dashboard-distribution-integrity.md`](modules/dashboard-distribution-integrity.md) — Mission C5 operator playbook.
* [`observability.md`](observability.md) — structured logging + OpenTelemetry semconv.
