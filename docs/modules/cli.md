# Module: cli

## What it does

`sovyx.cli` is the Typer-based command-line interface. It manages the daemon lifecycle (`start` / `stop` / `status`), exposes brain queries, controls plugins, and provides an interactive REPL (`sovyx chat`) over the daemon's JSON-RPC endpoint (Unix domain socket on POSIX; TCP loopback on Windows).

## Key classes

| Name | Responsibility |
|---|---|
| `app` | Typer root with nested sub-apps (brain, mind, logs, dashboard, doctor, plugin, audit, kb, llm, voice). |
| `DaemonClient` | JSON-RPC 2.0 client over the daemon RPC endpoint (UDS on POSIX, TCP loopback on Windows). |
| `chat` | Interactive REPL with prompt_toolkit (history, slash commands). |

## Commands

Top-level commands and sub-app groups are listed below. For full
syntax of any command, run `sovyx <command> --help`. The CLI uses
Typer; auto-completion is available via
`sovyx --install-completion`.

### Root commands

| Command | What it does |
|---|---|
| `sovyx init <name>` | Create `~/.sovyx/<name>/` with `mind.yaml`. Since v0.39.0 the command invokes `sovyx voice setup` inline after mind creation so the operator can configure the input device interactively; pass `--skip-voice-setup` to preserve the pre-v0.39.0 non-interactive flow (useful for CI / scripted installs). |
| `sovyx start [--mind-id <id>]` | Launch the daemon + dashboard (`:7777`). Resolves the active mind via the shared resolver (`--mind-id` flag / auto-detect when exactly one mind exists). Runs in the foreground; use the OS service manager for backgrounded execution (the old `--foreground` flag was removed 2026-05-02). |
| `sovyx stop` | Stop the daemon. |
| `sovyx status` | Daemon health summary. |
| `sovyx token [--copy]` | Print or copy the dashboard bearer token. |
| `sovyx chat` | Interactive REPL with prompt_toolkit (history + slash commands). |

### `sovyx doctor` — diagnostic checks (sub-app)

| Command | What it does |
|---|---|
| `sovyx doctor` | Run the 10+ default diagnostic check matrix. |
| `sovyx doctor voice [--calibrate] [--non-interactive] [--input-device <name>] [--mind-id <id>] [--full-diag]` | Voice subsystem checks. `--calibrate` runs the full slow-path calibration; the prereq gate is **STRICT since v0.40.0** — exits with code `EXIT_DOCTOR_VOICE_NOT_CONFIGURED=6` when no input device is configured on the resolved mind. `--input-device "<name>"` is the escape hatch: inline-configures the named device (substring match against the PortAudio list; non-interactive sessions require exactly one match), persists it to `mind.yaml`, then continues. |
| `sovyx doctor cascade` | Probe the Linux device cascade planner against the operator's audio stack. |
| `sovyx doctor linux_session_manager_grab` | Verify PipeWire / PulseAudio session-manager grab semantics. |
| `sovyx doctor voice_capture_apo` | Detect Windows capture-side APOs (Voice Clarity etc.) per anti-pattern #21. |
| `sovyx doctor voice_capture_integrity` | Platform-neutral alias of `voice_capture_apo` (Mission H2). |
| `sovyx doctor piper_locale_match` | Flag drift between the operator's spoken language and the auto-selected Piper voice (F2-M03). |
| `sovyx doctor stt_language_match [--language <tag>] [--json]` | Check whether a language has a Moonshine STT model (ENGINES-9 — the STT sibling of `piper_locale_match`). Exit 0 = PASS; exit 1 = WARN (no model — STT coerces to English at pipeline start). |
| `sovyx doctor platform` | Cross-platform parity summary (Linux / Windows / macOS detection + delta to baseline). |
| `sovyx doctor resources [--json] [--cohort <name>] [--explain <field>] [--watch]` | Engine resource-cohort snapshot (Mission H4) — live daemon RPC when reachable. |
| `sovyx doctor gates [--json]` | Quality Gates registry: STRICT/LENIENT state + sunset target per gate. |

Note on `--full-diag`: the forensic diagnostic is cross-platform —
Linux runs the bundled bash toolkit; Windows dispatches to a native
WASAPI/APO/mic-consent producer (W3.2); macOS is not yet supported.
`--fix` and `--calibrate` remain Linux-only.

### `sovyx voice` — voice data lifecycle (sub-app)

| Command | What it does |
|---|---|
| `sovyx voice setup [--mind-id <id>] [--input-device <substring>] [--non-interactive]` | Configure the active mind's input device. Renders an interactive picker over the PortAudio device list (or applies `--input-device` substring match). Persists the choice to `mind.yaml` under `voice_input_device_name`. Shipped v0.39.0 as part of MISSION-voice-config-calibrate-enterprise Phase 2. |
| `sovyx voice forget --user-id <id> [--yes]` | Purge every ConsentLedger record for the given user id (GDPR Art. 17 / LGPD Art. 18 VI). A `DELETE` tombstone is appended so the audit trail survives the erasure. |
| `sovyx voice history --user-id <id>` | List every ConsentLedger record for the user as JSONL (GDPR Art. 15 / LGPD Art. 18 I). |
| `sovyx voice train-wake-word <wake_word> [--mind-id <id>] [--unattached] [--language <tag>] [--target-samples N] [--negatives-dir <dir>] [--output <path>] [--voices <ids>] [--variants <phrases>]` | Train a sub-second ONNX wake-word model. The wake word is a positional argument; `--unattached` trains globally (no per-mind hot-reload; mutually exclusive with `--mind-id`); `--negatives-dir` is required. |
| `sovyx voice generate-signing-key [--mind-id <id>] [--output <path>] [--force]` | Generate an Ed25519 signing keypair for the calibration / KB profile signing flow (per anti-pattern #26). |

### `sovyx brain` — brain memory queries (sub-app)

| Command | What it does |
|---|---|
| `sovyx brain search <query>` | Hybrid (KNN + FTS5 + RRF) search across the brain graph. |
| `sovyx brain stats` | Concept / episode / relation counts. |
| `sovyx brain analyze scores <mind_id> [--json] [--db <path>]` | Importance + confidence score distribution (reads the mind's SQLite directly — no daemon required). |

### `sovyx mind` — mind management (sub-app)

| Command | What it does |
|---|---|
| `sovyx mind list` | List configured minds. |
| `sovyx mind status` | Active mind details. |
| `sovyx mind forget <id>` | Delete a mind (concepts + episodes + relations + voice data). |
| `sovyx mind retention prune <mind_id> [--dry-run] [--yes]` | Apply the retention policy now (delete records older than the configured horizons). `mind_id` is a positional argument. |
| `sovyx mind retention status <mind_id>` | Read-only preview of retention horizons + prune-eligible counts (equivalent to `prune <mind_id> --dry-run`). |

### `sovyx plugin` — plugin management (sub-app)

| Command | What it does |
|---|---|
| `sovyx plugin list` | Installed plugins with state. |
| `sovyx plugin info <name>` | Manifest, permissions, tools, risk levels. |
| `sovyx plugin install <path> [--yes]` | AST-scan + copy to `data_dir/plugins`. `--yes` skips the permission-consent prompt. |
| `sovyx plugin enable <name>` / `disable <name>` | Toggle. |
| `sovyx plugin remove <name>` | Uninstall. |
| `sovyx plugin validate <path>` | Run quality gates (manifest, AST, permissions) without installing. |
| `sovyx plugin create <name> [--output <dir>, -o]` | Scaffold a new plugin skeleton (default output: current directory). |

### `sovyx kb` — mixer-profile Knowledge Base inspection (sub-app)

Read-only inspection + validation of the voice mixer-profile KB
(shipped pool + user pool at `~/.sovyx/mixer_kb/user/`). No
PortAudio / ALSA dependency — contributors on any OS can validate a
Linux-targeted profile. Exit codes: `0` success, `1`
validation/lookup failure, `2` filesystem/argument error.

| Command | What it does |
|---|---|
| `sovyx kb list [--user-dir <dir>] [--shipped-only]` | List every profile (shipped + user pools) with identity, match scope, and provenance. |
| `sovyx kb inspect <profile_id> [--user-dir <dir>]` | Print a single profile's fields (driver family, codec glob, match threshold, attestations) in human-readable form. |
| `sovyx kb validate <path>` | Validate a candidate profile YAML with the same loader the daemon uses at boot — the authoritative "will this parse?" check before opening a PR. |
| `sovyx kb fixtures <profile_id\|all> [--fixtures-root <dir>]` | Verify the three HIL attestation fixtures (before/after `amixer` dumps + validation capture WAV) exist for a profile; `all` checks every shipped profile (CI-friendly). |

### `sovyx audit` — tamper-evident audit log (sub-app)

| Command | What it does |
|---|---|
| `sovyx audit verify-chain [--since <ISO date>] [--path <file>] [--audit-dir <dir>]` | Walk the audit chain and verify hashes from genesis to head. Non-zero exit if any entry tampered. |

### `sovyx llm` — LLM provider health + setup (sub-app)

| Command | What it does |
|---|---|
| `sovyx llm doctor [--json]` | Live provider discovery scan + per-provider liveness matrix; exit 0 on healthy verdicts, 1 otherwise (Mission C6 §T3.1). |
| `sovyx llm health` | Alias for `sovyx llm doctor`. |
| `sovyx llm setup [--provider <name>] [--api-key <key>] [--non-interactive] [--data-dir <path>]` | Interactive wizard for provider onboarding — validates the key against the provider API + persists to `<data_dir>/secrets.env` (`0o600`). |

### `sovyx logs` + `sovyx dashboard`

| Command | What it does |
|---|---|
| `sovyx logs [--level] [--follow]` | Tail / filter daemon logs. |
| `sovyx dashboard [--token]` | Print the dashboard URL; `--token` (`-t`) also reveals the auth token. |

## Interactive REPL

`sovyx chat` opens a prompt_toolkit session that talks to the daemon over JSON-RPC (not HTTP). Works even when the dashboard is disabled.

Features:
- Persistent history at `~/.sovyx/history` (chmod 0600).
- Word-completer over the slash-command vocabulary.
- History search (Ctrl+R).
- Seven slash commands: `/help`, `/status`, `/minds`, `/config`, `/new`, `/clear`, `/exit` — plus the aliases `/quit` (for `/exit`) and `/?` (for `/help`).

## RPC protocol

The daemon listens on a Unix domain socket (`~/.sovyx/sovyx.sock`) on POSIX platforms; on Windows it binds TCP `127.0.0.1` on an ephemeral port persisted to a `.port` file. `DaemonClient` sends JSON-RPC 2.0 requests and reads responses. Stale socket detection via probe (connect + immediate close).

Methods currently wired: `status` and `shutdown` (registered in `main.py`), plus the 14-method `register_cli_handlers` set in `engine/_rpc_handlers.py` — `chat`, `brain.search`, `brain.stats`, `mind.list`, `mind.status`, `mind.forget`, `mind.retention.prune`, `config.get`, `wake_word.register_mind`, `wake_word.unregister_mind`, `engine.resources.snapshot`, `engine.resources.tracemalloc_snapshot`, `voice.health.snapshot`, `doctor`. A registration-parity test (`tests/unit/cli/test_rpc_method_parity.py`) pins every `DaemonClient.call` literal to a registered method (anti-pattern #74).

## Configuration

No dedicated CLI config — reads `EngineConfig` from `system.yaml` and env vars. The RPC endpoint defaults to `~/.sovyx/sovyx.sock` (`DEFAULT_SOCKET_PATH` in `engine/rpc_server.py`); on Windows the sibling `sovyx.port` file carries the TCP loopback port.

## Roadmap

- Admin utilities (DB inspect, config reset, user/mind management).
- Migrate remaining brain/plugin commands from HTTP to RPC.

## See also

- Source: `src/sovyx/cli/main.py`, `src/sovyx/cli/commands/`, `src/sovyx/cli/chat.py`, `src/sovyx/cli/rpc_client.py`.
- Tests: `tests/unit/cli/`.
- Related: [`engine`](./engine.md) (RPC server side), [`dashboard`](./dashboard.md) (HTTP fallback for some commands).
