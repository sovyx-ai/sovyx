# Module: voice setup

`sovyx voice setup` is the headless / non-dashboard CLI command that
configures the per-mind capture device. It commits the operator's mic
choice to `<data_dir>/<mind_id>/mind.yaml` as the paired
`voice_input_device_name` + `voice_input_device_host_api` scalars —
the same fields the dashboard's voice-enable wizard writes.

Introduced in v0.39.0 (Phase 2 of
`MISSION-voice-config-calibrate-enterprise-2026-05-13`). Before this
command shipped, the only production write site for these fields was
the dashboard endpoint — operators on server-only installs had to
hand-edit `mind.yaml`.

## Operator-facing CLI

```text
sovyx voice setup                               # Interactive picker (TTY)
sovyx voice setup --mind-id <id>                # Pick target mind explicitly
sovyx voice setup --input-device "Razer"        # Substring match (case-insensitive)
sovyx voice setup --input-device 2              # Enumeration index
sovyx voice setup --input-device "Razer" --non-interactive  # Scripted / CI
```

## Match precedence for `--input-device`

| Precedence | Match | Example |
|---|---|---|
| 1 | Integer enumeration index | `--input-device 2` |
| 2 | Exact case-sensitive name | `--input-device "Razer BlackShark V2 Pro"` |
| 3 | Case-insensitive normalised substring (single hit) | `--input-device razer` |

An ambiguous substring (>1 hit) errors with the full match list — pass a more specific name or the index.

## Interactive picker

When invoked without `--input-device` on a TTY, the command renders a Rich table of capture devices (filtered to `max_input_channels > 0`) and prompts for a selection. Accepts either the device index or the name; the same matching rules apply as the flag.

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

Type `q` (or hit Ctrl+C / Ctrl+D) to abort. Aborting returns a non-zero exit code; nothing is persisted.

## Non-interactive use (CI / systemd / cron)

Pass `--non-interactive` + `--input-device "NAME"`. Without the flag the command refuses to prompt and exits with `VoiceSetupRequiredError` (exit code 2) and an actionable message listing the enumerated devices.

```bash
sovyx voice setup --mind-id jonny --input-device "Razer" --non-interactive
```

## Exit codes

| Code | Cause |
|---|---|
| 0 | Success — `mind.yaml` updated |
| 1 | `VoiceSetupError` — no capture devices, ambiguous `--input-device` match, persist failure |
| 2 | `VoiceSetupRequiredError` — `--non-interactive` without `--input-device`. Also covers `FileNotFoundError` when `<data_dir>/<mind_id>/mind.yaml` does not exist (operator must run `sovyx init` first) |

## Shared function (entry points)

`sovyx.cli.commands.voice_setup.run_voice_setup` is the async orchestrator shared by **three** callers:

1. `sovyx voice setup` CLI command (this module).
2. `sovyx init` (Phase 2.T2.2) — invokes setup inline when stdin is a TTY and `--skip-voice-setup` was not passed.
3. `sovyx doctor voice --calibrate` prereq gate (Phase 4.T4.1) — invokes setup before step 1 of the calibration pipeline when `voice_input_device_name` is empty on an interactive shell.

All three callers go through the same picker / validation / persistence path so behaviour stays consistent across entry points.

## Persistence semantics

Persistence delegates to `voice/calibration/_persist_device.persist_voice_input_device` which uses `engine/config_editor.ConfigEditor.set_scalar`:

* **Atomic** — temp-file + rename; partial writes never reach disk.
* **Lock-per-path** — concurrent writes to the same `mind.yaml` serialise via `LRULockDict`.
* **Comment-preserving** — the YAML editor preserves comments + key order in the existing file.
* **Paired write** — `voice_input_device_name` is always written; `voice_input_device_host_api` is written when the enumeration source provides it (some platforms / older PortAudio builds do not).
* **In-memory mirror** — when the caller holds a live `MindConfig`, the helper updates the matching attributes so the running process sees the new values without a daemon restart.

## Related

* `docs/modules/voice-calibration.md` — describes the calibrate prereq that reads what this command persists.
* `docs/getting-started.md` — the first-run flow that ties `sovyx init` to the inline picker.
