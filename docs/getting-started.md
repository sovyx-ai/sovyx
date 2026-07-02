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

#### Voice setup runs inline (new in v0.39.0)

When `sovyx init` is invoked on an interactive shell, it pauses after
creating the mind to let you pick a microphone. A table of capture
devices is shown; type the index or a substring of the name to
persist that choice to `mind.yaml`.

```text
Capture devices
┌───┬──────────────────────────────┬─────────────────┬──────────┬─────────────────┐
│ # │ Name                         │ Host API        │ Channels │ Sample rate (Hz)│
├───┼──────────────────────────────┼─────────────────┼──────────┼─────────────────┤
│ 0 │ Built-in Microphone          │ Windows WASAPI  │ 2        │ 44100           │
│ 1 │ Razer BlackShark V2 Pro      │ Windows WASAPI  │ 1        │ 48000           │
└───┴──────────────────────────────┴─────────────────┴──────────┴─────────────────┘
Enter device # or name (or 'q' to abort): 1
```

You can:

* **Skip the picker** with `sovyx init my-mind --skip-voice-setup` —
  useful for scripted installs that configure the mic separately.
* **Configure later** with `sovyx voice setup` (interactive) or
  `sovyx voice setup --input-device "Razer" --non-interactive`
  (scripted).
* **Non-TTY shells** (CI / systemd / cron) auto-skip the picker and
  print a hint pointing at the non-interactive flag.

The same mic choice that the dashboard's voice-enable wizard
persists is what `sovyx voice setup` writes — both paths share the
same atomic config-write helper (`voice_input_device_name` +
`voice_input_device_host_api` paired in `mind.yaml`).

> **Why this matters:** `sovyx doctor voice --calibrate` reads
> `voice_input_device_name` to know which mic to tune. Pre-v0.39.0 an
> empty value silently fell into a heuristic ("first attenuated
> ALSA card") which could pick the wrong device on multi-mic
> machines. v0.39.0 makes the choice explicit; v0.40.0 will require
> it as a hard prereq.

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

#### Configure providers and plugins (setup wizard)

The plugin and provider setup wizards walk you through API key entry,
connection testing, and voice hardware selection without leaving the
dashboard. The flows are identical across providers — only the form
fields change.

**Configuring a plugin or provider**

Open the **Plugins** page and click the **Configure** button on any
plugin card.

The setup wizard opens as a modal. Pick the provider you want to wire
up; the form below changes to match the provider's required fields.

Form fields are typed (text, password, dropdown, toggle) and the
wizard validates each one as you type — required fields are flagged
inline before you submit.

Use the **Test connection** button to verify the credentials before
saving. A green panel confirms latency and the responding model. A
red panel surfaces the structured error from the provider — common
failures are bad API key, expired credentials, or the provider being
unreachable from your network.

**Configuring voice**

The voice setup wizard enumerates every microphone PortAudio sees and
runs a short diagnostic on each so you can pick the one that actually
captures audio (not the silent default many laptops ship with).

If the Python voice dependencies (`moonshine-voice`, `sounddevice`)
or the underlying system library (PortAudio on Linux/macOS) are
missing, the wizard refuses to proceed and shows the install command
verbatim.

If the dependencies are present but the device can't be opened (driver
busy, USB unplugged, permission denied), the wizard surfaces the
PortAudio error code with a remediation hint per platform.

> Screenshots for these flows are not yet bundled with the docs. The
> capture conventions are documented in
> [`docs/_assets/setup-wizard/README.md`](_assets/setup-wizard/README.md)
> if you want to contribute them.

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

### Voice not working? Auto-fix it (Linux)

**Prerequisite:** the calibrate flow reads `voice_input_device_name`
from your `mind.yaml` to know which mic to tune. If `sovyx init`
already ran the inline picker, you're set. If not, configure the
mic via ONE of:

* **Interactive:** `sovyx voice setup --mind-id <X>` (picker
  prompts).
* **Scripted:** `sovyx voice setup --mind-id <X> --input-device "Razer" --non-interactive`.
* **Inline:** pass `--input-device "Razer"` directly to the
  calibrate command on a non-interactive shell.

On an interactive shell, `--calibrate` runs the picker for you when
the mic isn't yet configured. On a non-interactive shell with an
unconfigured mic AND no `--input-device` flag, the command exits
with `EXIT_DOCTOR_VOICE_NOT_CONFIGURED` (code 6) + an error
message listing the three remediation paths.

If your microphone is silent or voice capture isn't working, run:

```bash
sovyx doctor voice --calibrate --non-interactive
```

This is an automatic 8-12 minute hardware tune-up. It detects mic/mixer
issues, applies safe fixes, and saves the result so the next run replays
the cached profile in ~5 seconds. **You don't need any technical
knowledge to run it** — the dashboard's onboarding flow mounts the
calibration wizard automatically with a "Start calibration" button on
the Voice step (one click, then it runs unattended). The CLI command
above is a manual entry point for the same flow.

**Linux only** — the slow-path uses bundled bash diag tools that aren't
available on Windows or macOS. On those platforms the dashboard skips
the auto-fix wizard and shows the simple device-test setup instead, so
you still get a guided path; the CLI command above will print a
"Linux-only" message.

The dashboard's Settings → Voice section also has a "Recalibrate" button
that re-runs the same flow any time without leaving the UI (also
disabled on non-Linux daemons).

#### What if the auto-fix didn't help?

Three options, in order of operator-friendliness:

1. **Click "Recalibrate"** in Settings → Voice. Re-runs the auto-fix
   from scratch. The system rotates the previous calibration into a
   multi-generation backup chain (up to 3 prior calibrations
   retained), so you can roll back repeatedly if a re-calibration
   didn't help.
2. **Run `sovyx doctor voice --calibrate --rollback`** from the CLI to
   restore the most-recent prior calibration. Each rollback consumes
   one generation; you can repeat up to 3 times before needing to
   re-run `--calibrate` to repopulate the chain. The CLI prints how
   many rollback steps remain after each invocation.
3. **Switch to the simple device-test wizard** if calibration ended in
   "fallback" — the dashboard surfaces this option automatically with
   a "Use simple setup" button when calibration can't proceed.

#### Other voice-doctor commands

```bash
sovyx doctor voice                       # quick read-only health check (cross-platform)
sovyx doctor voice --full-diag           # detailed audio test only, no auto-fix
                                         # (Linux: interactive bash toolkit, 8-12 min;
                                         #  Windows: native WASAPI/APO/mic-consent probes,
                                         #  no speech prompts; macOS: not yet supported)
sovyx doctor voice --calibrate --show    # show what was applied (read-only, Linux)
sovyx doctor voice --calibrate --explain # also list which detection rules fired (Linux)
```

#### For developers + power users

The calibration system is documented in depth at
[modules/voice-calibration.md](modules/voice-calibration.md): rule
registry, profile schema, signing model (LENIENT default / STRICT
opt-in via Ed25519), telemetry namespace, fallback reasons, and the
data flow between bash diag → triage → rule engine → applier. Most
users never need to read this; it's reference for contributors and
ops teams running Sovyx in regulated environments.

### Custom wake words

By default Sovyx listens for "Sovyx". If you want each mind to respond to its own name (e.g. "Lúcia", "Jonny"), you have two paths: a no-training STT-fallback path (just edit `mind.yaml`) or a sub-second ONNX-trained path (one `sovyx voice train-wake-word` invocation). Both, with quality-gate thresholds and the OpenWakeWord training hand-off, are documented in [contributing/voice-wake-words.md](contributing/voice-wake-words.md).

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
