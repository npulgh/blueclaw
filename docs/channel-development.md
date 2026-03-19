# Channel Development Guide

How to add a new IM channel to Lynxclaw.

---

## 1. The ChannelAdapter Interface

Every channel is a Python class that inherits from `ChannelAdapter`
(`src/channels/registry.py`).  The ABC enforces seven methods:

| Method | Sync/Async | Purpose |
|---|---|---|
| `init(config)` | async | Validate config, create SDK clients |
| `start()` | async | Open connection, begin polling/listening |
| `stop()` | async | Graceful shutdown, release resources |
| `on_message(handler)` | sync | Register the host's message callback |
| `send_message(chat_id, content) -> str` | async | Send a message, return platform msg ID |
| `edit_message(chat_id, msg_id, content)` | async | Update an existing message (streaming) |
| `capabilities() -> ChannelCapabilities` | sync | Declare supported features |

### Method contracts

**`init(config)`**
- Called once before `start()`.
- Raise `ValueError` for missing required credentials.
- Do not start background tasks here.

**`start()`**
- Must not block the event loop.
- Start background asyncio tasks or daemon threads here.
- Raise `RuntimeError` if `init()` was not called first.

**`stop()`**
- Must be idempotent.
- Cancel tasks, close SDK clients, flush buffers.
- Should not raise; log exceptions instead.

**`on_message(handler)`**
- Store the handler; call `await handler(incoming)` for every message.
- The handler is `Callable[[IncomingMessage], Awaitable[None]]`.

**`send_message(chat_id, content) -> str`**
- Returns a non-empty string that uniquely identifies the sent message.
- This ID is passed back to `edit_message()` for streaming updates.

**`edit_message(chat_id, msg_id, content)`**
- If the platform does not support editing, raise `NotImplementedError`
  and set `capabilities().supports_edit = False`.

**`capabilities()`**
- Returns `ChannelCapabilities(supports_edit, supports_rich_text, supports_attachments)`.
- The host uses `supports_edit` to decide whether to stream via edits or
  send a new message for each chunk.

### Key types

```python
# src/types.py

@dataclass
class IncomingMessage:
    message_id: str    # platform-native ID (idempotency key)
    chat_id: str       # conversation / group identifier
    sender_id: str
    sender_name: str
    text: str
    channel: str       # e.g. "telegram", "discord"
    attachments: list
    timestamp: int     # Unix epoch seconds
    raw: Any           # original platform payload

@dataclass
class OutgoingMessage:
    text: Optional[str] = None
    rich_text: Optional[dict] = None
    attachments: Optional[list] = None

@dataclass
class ChannelCapabilities:
    supports_edit: bool = False
    supports_rich_text: bool = False
    supports_attachments: bool = False
```

---

## 2. Step-by-step: Creating a New Adapter

### Step 1 — Create the adapter file

```
src/channels/discord.py
```

Start from the example adapter (`src/channels/example_adapter.py`), which
is a fully-documented echo implementation with no external dependencies.

### Step 2 — Define a config dataclass

```python
# src/channels/discord.py
from dataclasses import dataclass
from typing import Optional

@dataclass
class DiscordConfig:
    enabled: bool = False
    bot_token: Optional[str] = None
    guild_id: Optional[str] = None
```

### Step 3 — Implement the adapter class

```python
import structlog
from src.channels.registry import ChannelAdapter, MessageHandler
from src.types import ChannelCapabilities, IncomingMessage, OutgoingMessage

logger = structlog.get_logger(__name__)

class DiscordAdapter(ChannelAdapter):

    def __init__(self) -> None:
        self._client = None
        self._handler = None

    async def init(self, config: DiscordConfig) -> None:
        if not config.bot_token:
            raise ValueError("DiscordConfig.bot_token is required")
        # import discord  # pip install discord.py
        # self._client = discord.Client(intents=discord.Intents.default())
        logger.info("discord_adapter_initialized")

    async def start(self) -> None:
        if self._client is None:
            raise RuntimeError("Call init() before start()")
        # asyncio.create_task(self._client.start(self._config.bot_token))
        logger.info("discord_adapter_started")

    async def stop(self) -> None:
        # await self._client.close()
        logger.info("discord_adapter_stopped")

    def on_message(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def send_message(self, chat_id: str, content: OutgoingMessage) -> str:
        # channel = self._client.get_channel(int(chat_id))
        # msg = await channel.send(content.text or "")
        # return str(msg.id)
        raise NotImplementedError

    async def edit_message(self, chat_id: str, msg_id: str, content: OutgoingMessage) -> None:
        # channel = self._client.get_channel(int(chat_id))
        # msg = await channel.fetch_message(int(msg_id))
        # await msg.edit(content=content.text or "")
        raise NotImplementedError

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            supports_edit=True,
            supports_rich_text=False,
            supports_attachments=False,
        )
```

### Step 4 — Add config section to `src/config.py`

```python
# src/config.py

@dataclass
class DiscordConfig:
    enabled: bool = False
    bot_token: Optional[str] = None

@dataclass
class Config:
    ...
    discord: DiscordConfig = field(default_factory=DiscordConfig)
```

Add it to `section_map` in `load_config()`:

```python
section_map = {
    ...
    "discord": cfg.discord,
}
```

And add env var override:

```python
if token := os.environ.get("DISCORD_BOT_TOKEN"):
    cfg.discord.bot_token = token
```

### Step 5 — Register in `discover_adapters()`

Open `src/channels/registry.py` and add your adapter to `discover_adapters()`:

```python
from src.channels.discord import DiscordAdapter

def discover_adapters(config: Config) -> dict[str, ChannelAdapter]:
    adapters: dict[str, ChannelAdapter] = {}
    ...
    if config.discord.enabled:
        adapters["discord"] = DiscordAdapter()
    return adapters
```

### Step 6 — Wire the config YAML

```yaml
# lynxclaw.config.yaml
discord:
  enabled: true

groups:
  - name: my-discord-group
    channel: discord
    chat_id: "123456789"
```

That's it. `main.py` calls `discover_adapters()` at startup and the new
adapter is live.

---

## 3. Registration via `discover_adapters()`

`src/channels/registry.py` exports:

```python
def discover_adapters(config: Config) -> dict[str, ChannelAdapter]:
    """Return a dict of {channel_name: uninitialised_adapter} for all enabled channels."""
```

`main.py` calls this, then calls `adapter.init(channel_config)` for each
entry.  Adding a new adapter requires only:

1. Creating the adapter class.
2. Adding a config section.
3. Adding one `if config.X.enabled` block in `discover_adapters()`.

No other files need to change.

---

## 4. Testing Strategy

### Unit test the adapter in isolation

Use `ExampleAdapter` as a reference.  The key pattern is:

```python
import pytest
from src.channels.your_adapter import YourAdapter, YourConfig

@pytest.mark.asyncio
async def test_send_message():
    adapter = YourAdapter()
    config = YourConfig(enabled=True, ...)
    await adapter.init(config)
    await adapter.start()

    sent_ids = []
    async def handler(msg):
        sent_ids.append(msg.message_id)

    adapter.on_message(handler)
    msg_id = await adapter.send_message("chat-1", OutgoingMessage(text="hello"))
    assert msg_id  # must be non-empty
    await adapter.stop()
```

### Mock the platform SDK

Patch the SDK client so tests run without network access:

```python
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_send_calls_sdk():
    adapter = DiscordAdapter()
    mock_channel = AsyncMock()
    mock_msg = AsyncMock()
    mock_msg.id = 42
    mock_channel.send.return_value = mock_msg

    with patch.object(adapter, "_client") as mock_client:
        mock_client.get_channel.return_value = mock_channel
        adapter._running = True
        msg_id = await adapter.send_message("999", OutgoingMessage(text="hi"))

    assert msg_id == "42"
    mock_channel.send.assert_called_once_with("hi")
```

### What to test

| Scenario | What to assert |
|---|---|
| `init()` with missing credentials | Raises `ValueError` |
| `start()` before `init()` | Raises `RuntimeError` |
| `send_message()` returns non-empty string | `assert msg_id` |
| `edit_message()` calls SDK with correct args | Mock assertion |
| `stop()` is idempotent | Call twice, no exception |
| `capabilities()` returns correct flags | Field assertions |
| Incoming message dispatched to handler | Handler called with correct `IncomingMessage` |

### Integration test with `simulate_incoming()`

If your adapter exposes a `simulate_incoming()` helper (like `ExampleAdapter`),
you can drive the full message lifecycle without a real connection:

```python
@pytest.mark.asyncio
async def test_echo_roundtrip():
    adapter = ExampleAdapter()
    await adapter.init(ExampleConfig(enabled=True))
    await adapter.start()

    received = []
    adapter.on_message(lambda msg: received.append(msg) or asyncio.sleep(0))
    await adapter.simulate_incoming("hello", chat_id="c1")

    assert len(received) == 1
    assert received[0].text == "hello"
    await adapter.stop()
```

---

## 5. Common Pitfalls

**Blocking the event loop in `start()`**
If your SDK's connection method is blocking (like `lark-oapi`'s `ws.Client.start()`),
run it in a daemon thread and bridge back with `asyncio.run_coroutine_threadsafe()`.
See `FeishuAdapter._on_feishu_message()` for the pattern.

**Forgetting to check `self._handler is None`**
Always guard the handler call:
```python
if self._handler is None:
    return
```
The host calls `on_message()` after `init()` but before `start()`.  If a
message arrives before the handler is wired (race condition), silently drop it.

**Returning an empty string from `send_message()`**
The host stores the returned ID and passes it to `edit_message()` for streaming.
An empty string will cause the debouncer to send a new message on every chunk
instead of editing the placeholder.

**Not handling `edit_message()` when `supports_edit=False`**
If your platform cannot edit messages, set `supports_edit=False` in
`capabilities()`.  The host will then send a new message for each streaming
chunk.  You still need to implement `edit_message()` (raise `NotImplementedError`
is fine) because the ABC requires it.

**Swallowing exceptions in the message handler**
Wrap `await self._handler(incoming)` in a try/except and log the exception.
An unhandled exception in the handler will crash the background task.

**Using `asyncio.run()` inside an async context**
If your SDK fires callbacks in a thread, use `asyncio.run_coroutine_threadsafe()`
to schedule coroutines on the running event loop.  Never call `asyncio.run()`
from within an already-running loop.

**Storing IM tokens in containers**
Lynxclaw's security model keeps IM credentials in the host process only.
Never pass `bot_token`, `app_secret`, or similar values into container env vars.
Only `ANTHROPIC_API_KEY` enters containers.
