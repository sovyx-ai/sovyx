# Module: bridge

## What it does

`sovyx.bridge` connects external communication channels — Telegram, Signal, and anything that speaks the internal channel protocol — to the cognitive loop. It normalizes inbound messages into a common `InboundMessage` type, resolves the sender to a persistent `PersonId`, tracks conversations, and sends responses back through the originating channel. It also hosts the financial confirmation flow: when a tool call requires explicit user approval, the bridge sends inline buttons and routes the callback back to the `FinancialGate`.

## Key components

| Name | Responsibility |
|---|---|
| `BridgeManager` | Orchestrator: routes inbound traffic to the cognitive loop, serializes by conversation, dispatches financial confirmation callbacks. |
| `InboundMessage` | Normalized message coming in from a channel. |
| `OutboundMessage` | Normalized message going out to a channel, optionally with inline buttons. |
| `InlineButton` | Single inline button. Enforces the 64-byte `callback_data` limit. |
| `PersonResolver` | Maps a `(channel_type, channel_user_id)` pair to a persistent `PersonId`. |
| `ConversationTracker` | Tracks active conversations with TTL and cleanup. |
| `TelegramChannel` | Adapter for the Telegram Bot API via `aiogram 3.x`. |
| `SignalChannel` | Adapter for Signal via `signal-cli-rest-api`. |

Only `InboundMessage`, `OutboundMessage`, and `InlineButton` are re-exported from `sovyx.bridge`. Other types are imported from their submodule (e.g. `from sovyx.bridge.manager import BridgeManager`).

## Flow

```
channel webhook / long-poll
         │
         ▼
  channel adapter            (telegram.py | signal.py)
         │
         ▼
  InboundMessage ─► PersonResolver ─► ConversationTracker
         │
         ▼
  BridgeManager ─► Perception ─► CogLoopGate ─► think → act
         │                                          │
         ▼                                          ▼
  channel.send(OutboundMessage) ◄──────── response from loop
```

Locks are kept per `conversation_id` so two messages in the same chat are processed in order, with an LRU cap of 500 entries to avoid unbounded growth.

## Example — protocol

```python
# src/sovyx/bridge/protocol.py
@dataclasses.dataclass(frozen=True, slots=True)
class InlineButton:
    text: str
    callback_data: str    # max 64 bytes (Telegram limit)

    def __post_init__(self) -> None:
        if len(self.callback_data.encode()) > 64:
            raise ValueError(f"callback_data exceeds 64 bytes")


@dataclasses.dataclass
class InboundMessage:
    channel_type: ChannelType
    channel_user_id: str
    channel_message_id: str
    chat_id: str
    text: str
    person_id: PersonId | None = None
    display_name: str = ""
    metadata: dict[str, object] = dataclasses.field(default_factory=dict)
    timestamp: datetime = dataclasses.field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_callback(self) -> bool:
        return "callback_data" in self.metadata
```

## Example — sending an outbound message

```python
from sovyx.bridge.protocol import OutboundMessage, InlineButton

await bridge_manager.send(
    OutboundMessage(
        chat_id=msg.chat_id,
        channel_type=msg.channel_type,
        text="Confirm transferring $100 to Alice?",
        inline_buttons=[
            InlineButton(text="Yes", callback_data=f"fin_confirm:{tool_call_id}"),
            InlineButton(text="No",  callback_data=f"fin_cancel:{tool_call_id}"),
        ],
    )
)
```

When the user presses a button, the channel adapter receives a callback query, builds an `InboundMessage` with `metadata["callback_data"]` populated, and hands it to the `BridgeManager`. Callbacks whose `callback_data` starts with `fin_confirm:` or `fin_cancel:` are routed directly to the `FinancialGate` and edit the original message to remove the buttons — they do not re-enter the cognitive loop.

## Telegram adapter

```python
# src/sovyx/bridge/channels/telegram.py
class TelegramChannel:
    """Telegram channel adapter via aiogram 3.x."""

    @property
    def channel_type(self) -> ChannelType:
        return ChannelType.TELEGRAM

    @property
    def capabilities(self) -> set[str]:
        return {"send", "inline_buttons", "edit"}

    @property
    def format_capabilities(self) -> dict[str, object]:
        return {
            "markdown": True,
            "max_length": 4096,
            "parse_mode": "MarkdownV2",
        }
```

Formatting goes through `telegramify-markdown` and uses `MessageEntity` lists instead of `parse_mode` to dodge MarkdownV2 escaping issues. Reconnect uses exponential backoff capped at 60 seconds.

## Signal adapter

`SignalChannel` talks to a local `signal-cli-rest-api` process over HTTP via `httpx`. The same `InboundMessage` / `OutboundMessage` contracts apply; inline buttons degrade to a numbered text menu because Signal does not have a native equivalent.

## Configuration

```yaml
bridge:
  conversation_lock_cache: 500   # LRU lock cap per BridgeManager

channels:
  telegram:
    enabled: true
    token: "${TELEGRAM_BOT_TOKEN}"
    poll_timeout_s: 30
  signal:
    enabled: false
    api_url: http://127.0.0.1:8080
    phone_number: "+1555..."
```

Credentials can come from environment variables (`SOVYX_CHANNELS__TELEGRAM__TOKEN=...`) or from the file. Nothing is stored in the repo.

## Events

| Event | Emitted when |
|---|---|
| `ChannelConnected` | A channel adapter completes its handshake. |
| `ChannelDisconnected` | A channel drops (includes `reason`). |
| `PerceptionReceived` | An `InboundMessage` was turned into a `Perception` for the cognitive loop. |
| `ResponseSent` | A response was delivered back through a channel. |

`ChannelConnectionError` is raised from `engine.errors` when a channel cannot be initialized (missing token, bad credentials, etc.).

## Roadmap

- **Relay client** — WebSocket audio streaming with Opus (24 kbps, 20 ms frames, DTX/FEC), a 60 ms ring buffer, 16 ↔ 48 kHz resampling, offline queue, and exponential backoff with jitter. Required for the mobile app and the cloud relay.
- **Home Assistant bridge** — entity registry for 10 domains, an `ActionSafety` framework (`SAFE` / `CONFIRM` / `DENY`), mDNS discovery, and WebSocket reconnect.
- **CalDAV** — incremental sync (`ctag` + `etag`), `RRULE` expansion via `dateutil`, timezone handling (DATE vs DATE-TIME, DST), and conflict resolution.

## See also

- Source: `src/sovyx/bridge/protocol.py`, `manager.py`, `identity.py`, `sessions.py`, `channels/telegram.py`, `channels/signal.py`.
- Tests: `tests/unit/bridge/`, `tests/integration/bridge/`.
- Related modules: [`cognitive`](./engine.md) for the `CogLoopGate` and `FinancialGate`, [`dashboard`](./dashboard.md) for `/api/channels`.
