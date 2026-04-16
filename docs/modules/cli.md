# Module: cli

## What it does

`sovyx.cli` is the Typer-based command-line interface. It manages the daemon lifecycle (`start` / `stop` / `status`), exposes brain queries, controls plugins, and provides an interactive REPL (`sovyx chat`) over the existing JSON-RPC Unix socket.

## Key classes

| Name | Responsibility |
|---|---|
| `app` | Typer root with nested sub-apps (brain, mind, plugin, dashboard, logs). |
| `DaemonClient` | JSON-RPC 2.0 client over Unix socket (`~/.sovyx/sovyx.sock`). |
| `chat` | Interactive REPL with prompt_toolkit (history, slash commands). |

## Commands

```
sovyx init [name]              # create ~/.sovyx/<name>/ with mind.yaml
sovyx start [--foreground]     # launch daemon + dashboard (:7777)
sovyx stop                     # stop the daemon
sovyx status                   # daemon health summary
sovyx doctor                   # 10+ diagnostic checks (runs without daemon)
sovyx token [--copy]           # print or copy dashboard bearer token
sovyx chat                     # interactive REPL with slash commands
sovyx brain search <query>     # search the brain graph
sovyx brain stats              # concept / episode / relation counts
sovyx brain analyze            # importance score distribution
sovyx mind list                # list configured minds
sovyx mind status              # active mind details
sovyx plugin list              # installed plugins
sovyx plugin install <path>    # install a plugin from disk
sovyx plugin enable|disable    # toggle a plugin
sovyx plugin remove <name>     # uninstall
sovyx plugin validate <path>   # run quality gates (manifest, AST, permissions)
sovyx plugin create <name>     # scaffold a new plugin
sovyx dashboard [--open]       # open the dashboard URL
sovyx logs [--level] [--follow] # tail daemon logs
```

## Interactive REPL

`sovyx chat` opens a prompt_toolkit session that talks to the daemon over JSON-RPC (not HTTP). Works even when the dashboard is disabled.

Features:
- Persistent history at `~/.sovyx/history` (chmod 0600).
- Word-completer over the slash-command vocabulary.
- History search (Ctrl+R).
- Seven slash commands: `/help`, `/exit`, `/quit`, `/new`, `/clear`, `/status`, `/minds`, `/config`.

## RPC protocol

The daemon listens on a Unix socket (`~/.sovyx/sovyx.sock`). `DaemonClient` sends JSON-RPC 2.0 requests and reads responses. Stale socket detection via probe (connect + immediate close).

5 methods currently wired: `status`, `shutdown`, `chat`, `mind.list`, `config.get`. Brain and plugin subcommands fall back to dashboard HTTP endpoints when the RPC method is not registered.

## Configuration

No dedicated CLI config — reads `EngineConfig` from `system.yaml` and env vars. Socket path from `EngineConfig.socket.socket_path`.

## Roadmap

- Admin utilities (DB inspect, config reset, user/mind management).
- Migrate remaining brain/plugin commands from HTTP to RPC.

## See also

- Source: `src/sovyx/cli/main.py`, `src/sovyx/cli/commands/`, `src/sovyx/cli/chat.py`, `src/sovyx/cli/rpc_client.py`.
- Tests: `tests/unit/cli/`.
- Related: [`engine`](./engine.md) (RPC server side), [`dashboard`](./dashboard.md) (HTTP fallback for some commands).
