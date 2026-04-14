# Módulo: cli

## Objetivo

Interface de linha de comando do Sovyx. Um app Typer com subcomandos para gerenciar o daemon (start/stop/status), autenticação (token), diagnóstico (doctor), Brain (search/stats/analyze), Mind (list/status), plugins (list/install/enable/disable/validate/create), dashboard (start/token) e logs (tail/search). Comunica com o daemon via **JSON-RPC 2.0 sobre Unix socket**; Rich fornece output colorido e todas as saídas críticas suportam `--json`.

**Estado atual: ~61% completo.** Framework + RPC client + comandos core prontos; REPL interativo e utilidades admin não foram implementados. `DaemonRPCServer` existe em esqueleto, sem registry completo de métodos.

## Responsabilidades

- **CLI framework** — `typer.Typer` aninhado com subcomandos (`brain`, `mind`, `plugin`, `dashboard`, `logs`).
- **Daemon client** — `DaemonClient` fala JSON-RPC 2.0 com o daemon via Unix socket (`~/.sovyx/sovyx.sock`).
- **Token management** — `sovyx token` lê `~/.sovyx/token` e opcionalmente copia para clipboard.
- **Rich output** — tabelas, cores, progress bars via `rich.console.Console`.
- **`--json` switch** — saída estruturada para scripting/CI em todos os comandos de listagem.
- **Stale socket detection** — `is_daemon_running()` valida conectividade, não apenas existência do arquivo.

## Arquitetura

```
cli/
  ├── main.py                  Typer root app + comandos core
  │                            (init, start, stop, status, token, doctor, version)
  ├── rpc_client.py            DaemonClient (JSON-RPC 2.0 over Unix socket)
  └── commands/
        ├── brain_analyze.py   sovyx brain analyze scores
        ├── dashboard.py       sovyx dashboard (callback com flags --token/--url)
        ├── logs.py            sovyx logs (callback com flags --follow/--level/--since)
        └── plugin.py          sovyx plugin {list|install|enable|disable|
                                            remove|validate|create}
```

## Código real (exemplos curtos)

**`src/sovyx/cli/main.py`** — composição do Typer:

```python
app = typer.Typer(
    name="sovyx",
    help="Sovyx — Sovereign Minds Engine",
    no_args_is_help=True,
)
brain_app = typer.Typer(name="brain", help="Brain memory commands")
mind_app = typer.Typer(name="mind", help="Mind management commands")
brain_app.add_typer(analyze_app)
app.add_typer(brain_app)
app.add_typer(mind_app)
app.add_typer(logs_app)
app.add_typer(dashboard_app, name="dashboard")
app.add_typer(plugin_app, name="plugin")
```

**`src/sovyx/cli/rpc_client.py`** — socket padrão + probe robusto:

```python
DEFAULT_SOCKET_PATH = Path.home() / ".sovyx" / "sovyx.sock"

class DaemonClient:
    def is_daemon_running(self) -> bool:
        """Probe real (não apenas existência do arquivo)."""
        if not self._socket_path.exists():
            return False
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(str(self._socket_path))
            sock.close()
        except (ConnectionRefusedError, OSError, TimeoutError):
            return False
        return True
```

**`src/sovyx/cli/main.py`** — `sovyx token` com Rich:

```python
@app.command()
def token(copy: bool = typer.Option(False, "--copy", "-c")) -> None:
    """Show the dashboard authentication token."""
    if not TOKEN_FILE.exists():
        console.print("[yellow]Token not generated yet.[/yellow]\n"
                      "[dim]Start Sovyx first: [bold]sovyx start[/bold][/dim]")
        raise typer.Exit(1)
    token_value = TOKEN_FILE.read_text().strip()
    console.print(f"\n[bold]🔑 Dashboard Token[/bold]\n\n  {token_value}\n")
```

## Comandos disponíveis

| Comando | Função | Status |
|---|---|---|
| `sovyx init` | Inicializa `~/.sovyx/` | Aligned |
| `sovyx start` | Starta daemon | Aligned |
| `sovyx stop` | Stop graceful | Aligned |
| `sovyx status` | Status + healthchecks | Aligned |
| `sovyx token` | Mostra/copia token | Aligned |
| `sovyx doctor` | 10+ diagnósticos (via `observability.health.HealthRegistry`; com daemon, delega RPC `doctor`) | Aligned |
| `sovyx brain search` | Busca hybrid na Brain | Aligned |
| `sovyx brain stats` | Métricas de Brain | Aligned |
| `sovyx brain analyze scores` | Análise de importance/confidence/evolution (subcomando `scores`) | Aligned |
| `sovyx mind list` | Lista Minds | Aligned |
| `sovyx mind status` | Status de Mind | Aligned |
| `sovyx plugin list` | Plugins carregados | Aligned |
| `sovyx plugin install` | Instala de fonte | Aligned |
| `sovyx plugin info` | Detalhes de um plugin instalado | Aligned |
| `sovyx plugin enable/disable/remove` | Gerencia | Aligned |
| `sovyx plugin validate` | AST scanner offline | Aligned |
| `sovyx plugin create` | Scaffold novo plugin | Aligned |
| `sovyx dashboard` | Info do dashboard (callback; `--token`, `--url`, `--open`) — não há subcomandos `start`/`stop` | Aligned |
| `sovyx logs` | Filtra/segue logs JSON do daemon (callback; flags `--follow`, `--level`, `--since`, `--filter`) — não há subcomandos `tail`/`search` | Aligned |

## Specs-fonte

- **SPE-015-CLI-TOOLS** — Typer + Rich, JSON-RPC 2.0, REPL, admin utilities.

## Status de implementação

| Item | Status |
|---|---|
| Typer app + subcomandos aninhados | Aligned |
| DaemonClient (Unix socket JSON-RPC 2.0) | Aligned |
| Stale socket detection via probe | Aligned |
| Token management (`sovyx token` + `--copy`) | Aligned |
| Rich output colorido + `--json` switch | Aligned |
| `sovyx doctor` (integra `observability.health.HealthRegistry`; RPC quando daemon ativo) | Aligned |
| Comandos core (init/start/stop/status) | Aligned |
| Comandos brain/mind/plugin (com subcomandos) + dashboard/logs (callbacks com flags) | Aligned |
| `DaemonRPCServer` com registry completo | Partial — sketch only |
| REPL interativo (multi-line, auto-complete, history) | Not Implemented |
| Admin utilities (DB inspection, config reset, user/mind mgmt) | Not Implemented |

## Divergências

**DaemonRPCServer incompleto** — SPE-015 §2.1-2.2 descreve o servidor JSON-RPC com registry de métodos tipados e dispatch. O código atual contém apenas esqueleto (métodos `status`, `shutdown`); não há registry completo nem validação de schema de params. Hoje os comandos CLI que precisam do daemon funcionam via endpoints do dashboard. Prioridade: média.

**REPL (SPE-015 §3.1) não implementado** — shell interativo com multi-line input, auto-complete (via `prompt_toolkit`), history (`~/.sovyx/history`). Útil para exploração e scripts; não bloqueia uso básico.

**Admin utilities (SPE-015 §3.2) não implementadas** — `sovyx admin`: `db inspect` (tables/rows), `config reset`, `user/mind` management. Complementa doctor.

## Dependências

- `typer>=0.12` — framework CLI.
- `rich>=13` — console output.
- `asyncio` + `socket` (stdlib) — client RPC.
- `sovyx.engine.rpc_protocol` — `rpc_send`, `rpc_recv` (wire format JSON-RPC 2.0).
- `sovyx.engine.errors.ChannelConnectionError` — socket falhou.
- `sovyx.dashboard.server.TOKEN_FILE` — leitura do token.

## Testes

- `tests/unit/cli/` — parsing de comandos via `typer.testing.CliRunner`, fixtures de socket falso.
- Testes do `DaemonClient` usam socket em `tmp_path` com server dummy.
- Comandos que dependem do daemon têm testes com e sem daemon rodando (erro exit codes).
- Testes de `--json` validam schema de saída.

## Public API reference

### Public API
| Classe | Descrição |
|---|---|
| `DaemonClient` | Client JSON-RPC 2.0 sobre Unix socket — `~/.sovyx/sovyx.sock`. |
| `DaemonRPCServer` | Servidor JSON-RPC (em `engine/rpc_server.py`) — sketch only, registry incompleto. |

*(o CLI é composto por `typer.Typer` apps, não classes: `app` root em `cli/main.py`; sub-apps para `brain`, `mind`, `plugin`, `dashboard`, `logs` agregados via `app.add_typer`)*

### Errors
| Exception | Quando é raised |
|---|---|

*(sem exceptions dedicadas — `DaemonClient` propaga `ChannelConnectionError` de `sovyx.engine.errors`; `typer.Exit` encerra com exit code)*

### Events
| Event | Payload / trigger |
|---|---|

*(CLI não emite events — consome apenas status do daemon via RPC e logs via file tail)*

### Configuration
| Config | Campo/Finalidade |
|---|---|

*(sem dataclass `CLIConfig` — CLI lê `EngineConfig` via YAML/env; path do socket vem de `SocketConfig` em `engine/config.py`)*

## Referências

- `src/sovyx/cli/main.py` — Typer root, comandos core, `sovyx token`.
- `src/sovyx/cli/rpc_client.py` — DaemonClient, JSON-RPC 2.0.
- `src/sovyx/cli/commands/brain_analyze.py` — `brain analyze scores` subcommand.
- `src/sovyx/cli/commands/dashboard.py` — `dashboard` callback (flags).
- `src/sovyx/cli/commands/logs.py` — `logs` callback (follow/level/since/filter flags).
- `src/sovyx/cli/commands/plugin.py` — `plugin list/install/validate/...`.
- SPE-015-CLI-TOOLS — spec Typer + Rich + JSON-RPC + REPL + admin.
- `docs/_meta/gap-inputs/analysis-C-integration.md` §cli — 61% completion.
- `docs/_meta/gap-analysis.md` §cli — divergências.
