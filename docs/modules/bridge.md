# Módulo: bridge

## Objetivo

Integra canais de comunicação externos (Telegram, Signal, e futuramente voz, Home Assistant e CalDAV) com o loop cognitivo. Responsável por normalizar mensagens *inbound*, rotear ao `CogLoopGate`, aplicar *financial confirmation flow* (dupla confirmação para ações financeiras) e enviar respostas *outbound* ao canal apropriado.

**Estado atual: ~38% completo.** Texto multi-canal funciona; áudio, smart home e calendário não foram implementados.

## Responsabilidades

- **Normalização de mensagens** — `InboundMessage`/`OutboundMessage` desacoplam formato de canal do cognitivo.
- **Person + Conversation resolution** — `PersonResolver` mapeia `channel_user_id` para `PersonId` persistente; `ConversationTracker` gerencia conversas ativas.
- **Routing** — cada `InboundMessage` vira uma `Perception` e é submetida ao `CogLoopGate`.
- **Financial confirmation flow** — quando `ActPhase` retorna `pending_confirmation=True`, `BridgeManager` envia mensagem com botões inline (`fin_confirm:<id>` / `fin_cancel:<id>`), espera callback, resolve via `FinancialGate` e edita a mensagem original (remove botões).
- **LRU lock management** — locks por `conversation_id` com `_LRULockDict(maxsize=500)` para evitar crescimento unbounded.

## Arquitetura

```
Telegram/Signal webhook
       │
       ▼
  ChannelAdapter (telegram.py | signal.py)
       │
       ▼
  InboundMessage  ─► PersonResolver ─► ConversationTracker
       │
       ▼
  BridgeManager ─► Perception ─► CogLoopGate
       │                              │
       │                              ▼
       │                         (think → act)
       │                              │
       │                              ▼
       │◄──────── OutboundMessage ────┘
       │
       ▼
  Channel.send(text, [inline_buttons])
```

## Código real (exemplos curtos)

**`src/sovyx/bridge/protocol.py`** — contratos normalizados:

```python
@dataclasses.dataclass(frozen=True, slots=True)
class InlineButton:
    text: str
    callback_data: str  # max 64 bytes (Telegram); "fin_confirm:<tool_call_id>"

    def __post_init__(self) -> None:
        if len(self.callback_data.encode()) > 64:
            raise ValueError(f"callback_data exceeds 64 bytes")
```

**`src/sovyx/bridge/manager.py`** — LRU lock para evitar vazamento:

```python
class _LRULockDict(Generic[_K]):
    """Bounded dict de asyncio.Lock com LRU eviction.

    Previne crescimento unbounded quando conversation_ids são
    gerados por-sessão. Ao atingir maxsize, evicta o LRU.
    """
    def __init__(self, maxsize: int = 500) -> None: ...
```

**`src/sovyx/bridge/channels/telegram.py`** — adapter aiogram 3.x:

```python
class TelegramChannel:
    """Telegram channel adapter.

    v0.1: text + reply apenas. Sem media; inline keyboards; grupos complexos.
    """
```

**Financial confirmation** — convenção de callback:

```python
# BridgeManager emite:
# OutboundMessage(text="Confirma transferir $100?", buttons=[
#     InlineButton(text="Sim", callback_data=f"fin_confirm:{tool_call_id}"),
#     InlineButton(text="Não",  callback_data=f"fin_cancel:{tool_call_id}"),
# ])
#
# Na próxima inbound com callback_data = "fin_confirm:...":
#   1. resolve via FinancialGate
#   2. edita a mensagem original (remove botões)
#   3. NÃO submete ao cognitive loop
```

## Specs-fonte

- **SPE-014-COMMUNICATION-BRIDGE** — normalização multi-canal, protocolo, person resolver.
- **IMPL-007-RELAY-CLIENT** — audio streaming via WebSocket + Opus (NOT IMPLEMENTED).
- **IMPL-008-HOME-ASSISTANT** — HA integration, entity registry, ActionSafety (NOT IMPLEMENTED).
- **IMPL-009-CALDAV** — calendar sync com CalDAV, RRULE, timezones (NOT IMPLEMENTED).

## Status de implementação

| Item | Status |
|---|---|
| `InboundMessage` / `OutboundMessage` / `InlineButton` | Aligned |
| `BridgeManager` + routing | Aligned |
| `PersonResolver` / `ConversationTracker` | Aligned |
| `TelegramChannel` (aiogram 3.x) | Aligned |
| `SignalChannel` (signal-cli-rest-api) | Aligned |
| Financial confirmation flow | Aligned |
| LRU lock por conversation_id (500) | Aligned |
| RelayClient (Opus 24 kbps, ring buffer 60 ms) | Not Implemented |
| Audio resampling 16 ↔ 48 kHz | Not Implemented |
| Offline message queue + exponential backoff | Not Implemented |
| HomeAssistantBridge (10 domains, ActionSafety) | Not Implemented |
| mDNS discovery para HA | Not Implemented |
| CalDAV (ctag + etag, RRULE, timezones) | Not Implemented |

## Divergências

**RelayClient (IMPL-007) não implementado** — WebSocket streaming de áudio com Opus codec (24 kbps, frames 20 ms, modo VOIP, DTX/FEC), ring buffer de 60 ms para compensar jitter, resampling 16↔48 kHz, offline queue e reconnect com exponential backoff + jitter. **Impacto comercial: bloqueia mobile app + cloud relay** (ver gap-analysis Top 10 #1). Estimativa: 3-5 dias.

**HomeAssistantBridge (IMPL-008) não implementado** — integração smart home com entity registry de 10 domains, framework `ActionSafety` (`SAFE` / `CONFIRM` / `DENY`), mDNS discovery, reconnect WebSocket. Bloqueia *smart home positioning*.

**CalDAV (IMPL-009) não implementado** — sync incremental via `ctag+etag`, expansão de `RRULE` via `dateutil`, tratamento de timezones (DATE vs DATE-TIME, DST), resolução de conflito. Bloqueia feature de calendário.

## Dependências

- `aiogram>=3.x` — cliente Telegram Bot API (`TelegramChannel`).
- `httpx` — cliente REST (`SignalChannel` contra `signal-cli-rest-api`).
- `sovyx.cognitive.gate.CogLoopGate` — submissão de requests cognitivos.
- `sovyx.cognitive.perceive.Perception` — tipo de entrada no loop.
- `sovyx.cognitive.financial_gate.FinancialGate` — resolução de confirmações financeiras.
- `sovyx.engine.protocols.ChannelAdapter` — protocol dos canais.
- `sovyx.engine.types` — `ChannelType`, `ConversationId`, `MindId`, `PersonId`, `PerceptionType`.

## Testes

- `tests/unit/bridge/` — contratos (`InlineButton` 64-byte check), manager routing, LRU eviction.
- `tests/integration/bridge/` — Telegram adapter com fixtures aiogram, financial confirmation end-to-end.
- Nunca usar Telegram real em CI — usar mock do aiogram Dispatcher.

## Referências

- `src/sovyx/bridge/protocol.py` — InboundMessage, OutboundMessage, InlineButton.
- `src/sovyx/bridge/manager.py` — BridgeManager, LRULockDict, financial confirmation.
- `src/sovyx/bridge/identity.py` — PersonResolver.
- `src/sovyx/bridge/sessions.py` — ConversationTracker.
- `src/sovyx/bridge/channels/telegram.py` — TelegramChannel (aiogram).
- `src/sovyx/bridge/channels/signal.py` — SignalChannel (REST).
- SPE-014-COMMUNICATION-BRIDGE — spec de normalização.
- IMPL-007-RELAY-CLIENT — gap crítico (mobile/cloud relay).
- IMPL-008-HOME-ASSISTANT — gap (smart home).
- IMPL-009-CALDAV — gap (calendar).
- `docs/_meta/gap-inputs/analysis-C-integration.md` §bridge — 38% completion.
- `docs/_meta/gap-analysis.md` Top 10 #1, #6, #7.
