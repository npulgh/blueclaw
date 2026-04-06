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

**SDK caching the event loop at module level**
Some SDKs (e.g. `lark-oapi`) cache `asyncio.get_event_loop()` as a module-level
variable at import time.  If your adapter runs the SDK in a daemon thread with a
fresh event loop, `asyncio.new_event_loop()` + `set_event_loop()` alone won't
work — you must also monkey-patch the SDK's cached reference.  Diagnose with
`grep "get_event_loop\|run_until_complete"` in the SDK source.

**Message type consistency for streaming edits**
Some platforms (e.g. Feishu) reject `edit_message` calls that change the message
type.  If your adapter uses streaming edits, the **first** `send_message` must
already use the target format (e.g. Interactive Card), so that subsequent edits
are same-type patches.

**Platform permissions ≠ event subscriptions**
Some platforms (e.g. Feishu) treat API permissions and event subscriptions as
separate configurations.  "Permission granted" does not mean "events will be
delivered."  After changing either, you may need to re-publish the app version
for changes to take effect.

---

## 6. Third-Party API Mirror Integration

When using Anthropic-compatible API mirrors (e.g. for cost or latency reasons),
be aware of URL path handling:

**Golden rule: `ANTHROPIC_BASE_URL` must NOT include `/v1`.**
Claude Code CLI automatically appends `/v1/messages` to the base URL.

If the agent container uses `api_proxy.py` to relay requests, the proxy must
handle upstream URLs that may or may not already contain a version prefix:

| Upstream URL | Has version suffix? | `/v1/messages` handling | Final path |
| --- | --- | --- | --- |
| `api.example.com/coding/v1` | Yes | Strip `/v1` → `/messages` | `.../coding/v1/messages` ✓ |
| `api.example.com/api/claudecode` | No | Keep `/v1/messages` | `.../api/claudecode/v1/messages` ✓ |

Pattern for smart path forwarding:

```python
import re
has_version_suffix = bool(re.search(r'/v\d+/?$', upstream.rstrip('/')))
if has_version_suffix and path.startswith("/v1"):
    path = path[3:]
```

### Debugging methodology for API mirror issues

1. Add `[PROXY]` stderr logging to `api_proxy.py` — trace actual URLs and response codes
2. Increase consumer stderr capture length — default may truncate full tracebacks
3. Manual `docker run` reproduction — bypass host process, see full container output
4. Layer-by-layer network validation — `docker run python:3.11-slim python3 -c "urllib..."` to confirm container→target connectivity
5. Local vs container comparison — same API key + URL, works locally but not in container → network/path issue

---

## 7. IM 通道可行性评估

> 调研时间：2026-03-21。为后续 Channel 扩展提供决策参考。

### 已实现

| 通道 | SDK | 连接方式 | 状态 |
|------|-----|----------|------|
| Telegram | aiogram v3 | Long Polling | ✅ 生产可用 |
| 飞书 | lark-oapi | WebSocket | ✅ 生产可用 |

### 评估结论

| 通道 | 可行性 | 推荐方案 | 工作量 | 备注 |
|------|--------|----------|--------|------|
| Discord | 高 | discord.py | 1 天 | 官方 API，WebSocket 长连接 |
| Slack | 高 | slack-bolt | 1 天 | 官方 API，WebSocket/Events API |
| 企业微信 | 高 | wechatpy | 1-2 天 | 官方 API，HTTP 回调（需公网 IP） |
| 个人微信 | 不推荐 | — | — | 无官方 API，所有方案均为逆向工程，封号风险高 |

### 企业微信（WeCom）详细评估

**推荐方案：自建应用（Custom App）**

架构适配：
```
用户在企业微信发消息
  → 企业微信服务器 POST AES 加密 XML 到回调 URL
  → WeComAdapter 解密 + 解析
  → 路由到 Lynxclaw agent 容器
  → Agent 生成回复
  → Adapter 调用企业微信 send message API
  → 用户看到回复
```

关键特性：
- 官方 REST API，零封号风险
- 双向消息（发送 + 接收回调）
- 支持文本、图片、语音、视频、文件、Markdown、模板卡片
- 消息 AES 加密（wechatpy SDK 透明处理）
- 国内服务，无 GFW 问题
- 注册免费，无需企业认证即可开发（限 200 人）
- 注册人自动成为超级管理员，拥有创建自建应用权限

所需凭证：
| 凭证 | 来源 |
|------|------|
| `corpid` | 管理后台 → 我的企业 → 企业ID |
| `secret` | 管理后台 → 应用管理 → 自建应用 → Secret |
| `agentid` | 管理后台 → 应用管理 → 自建应用 → AgentId |
| `token` + `encoding_aes_key` | 管理后台 → 应用管理 → 接收消息 → API 接收设置 |

与现有 Channel 的差异：
| 维度 | Telegram | 飞书 | 企业微信 |
|------|----------|------|----------|
| 连接方式 | Long Polling | WebSocket | HTTP 回调 |
| 流式支持 | edit_message | edit_message | edit_message（同模式） |
| GFW | 需代理 | 不需要 | 不需要 |
| 消息加密 | 无 | 无 | AES（SDK 处理） |

注意事项：
- HTTP 回调模式需要公网可达的 endpoint（与 ADR-003 长连接优先原则不同）
- 可复用 `src/server.py`（FastAPI）接收回调
- 开发阶段可用 ngrok/frp 做内网穿透

Python SDK：[wechatpy](https://github.com/wechatpy/wechatpy)（4.2k stars，`wechatpy.enterprise` 模块）

成功案例：
- [AstrBot](https://github.com/AstrBotDevs/AstrBot)（17k stars）— 多平台 AI 聊天框架，支持企业微信
- [chatgpt-on-wechat](https://github.com/zhayujie/chatgpt-on-wechat) — 最活跃的 AI+微信集成项目
- [Dify + WeCom](https://github.com/luolin-ai/Dify-Enterprise-WeChat-bot) — 零代码 AI 工作流对接企业微信

### 个人微信——为什么不推荐

| 方案 | 原理 | 现状 | 风险 |
|------|------|------|------|
| itchat | Web 协议逆向 | 已死，微信关闭大部分账号 Web 登录 | — |
| wechaty (web) | 同上 | 不可用 | — |
| WeChatFerry | Windows 客户端 DLL 注入 | 活跃，需固定微信版本 | 高，且需 Windows |
| GeWe | iPad 协议逆向（商业） | 提供 Docker + REST API | 中高，商业依赖 |

核心问题：微信从 2019 年起持续封杀第三方自动化，先警告 → 限制登录 → 永久封号。所有方案都是灰色地带，不符合 Lynxclaw 安全优先的设计哲学。
