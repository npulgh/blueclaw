# 飞书机器人接入调试记录

> 创建时间：2026-03-21 | 状态：已解决 ✅
> 飞书 + Telegram 双通道同时运行，私聊模式，流式卡片回复

## 遇到的问题及解决方案（按时间线）

### BUG-1：`RuntimeError: This event loop is already running`

**现象**：`lark_oapi` WebSocket 线程启动时崩溃。

**根因**：`lark_oapi/ws/client.py` 在**模块加载时**（第 26 行）缓存了 `loop = asyncio.get_event_loop()` 为模块级变量。当 daemon thread 调用 `loop.run_until_complete()` 时，这个 loop 是主线程的——已经在运行中。

**修复**：在 daemon thread 启动前 monkey-patch 模块级变量：

```python
def _run_ws_in_new_loop(self) -> None:
    import lark_oapi.ws.client as ws_mod
    fresh_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(fresh_loop)
    ws_mod.loop = fresh_loop  # override cached module-level loop
    self._ws_client.start()
```

**教训**：第三方 SDK 缓存 event loop 是常见陷阱。`asyncio.new_event_loop()` + `set_event_loop()` 不够，还需要替换 SDK 内部缓存的引用。

### BUG-2：`This message is NOT a card`（edit_message 400 错误）

**现象**：流式编辑 PATCH 请求返回 `code=230001`。

**根因**：`send_message()` 发送纯文本消息，`edit_message()` 尝试用 Interactive Card 格式 PATCH。飞书不允许把文本消息改为卡片。

**修复**：`_build_content()` 始终返回 Interactive Card 格式：

```python
def _build_content(content):
    text = content.text or ""
    card = {
        "config": {"wide_screen_mode": True},
        "elements": [{"tag": "markdown", "content": text}],
    }
    return "interactive", json.dumps(card)
```

**教训**：飞书的 PATCH 消息 API 只能修改同类型消息。要支持流式编辑，首条消息必须是 Interactive Card。

### BUG-3：WebSocket 连接正常但消息事件不到达（间歇性）

**现象**：Handshake OK，ping/pong 正常，但发消息后日志中没有 `INSERT OR IGNORE INTO messages`。

**根因（多因素）**：

1. **事件订阅未生效**：飞书后台添加权限 ≠ 事件订阅已启用。必须在"事件与回调"页面**手动添加** `im.message.receive_v1`，且选择"长连接"模式
2. **修改后未重新发布**：每次改权限/事件订阅后，必须在"版本管理与发布"中**创建新版本并发布**，否则不生效
3. **飞书 WebSocket 事件投递间歇性延迟**：平台已知问题，重启 bot 通常能恢复

**教训**：飞书的权限系统和事件订阅是**两个独立配置**，必须分别完成。每次配置变更后必须重新发布版本。

### BUG-4：`git checkout` 误还原工作配置

**现象**：用户说"config 不提交"，执行 `git checkout -- lynxclaw.config.yaml` 还原了飞书配置（enabled: false，group 消失）。

**修复**：`git update-index --assume-unchanged lynxclaw.config.yaml` 保护本地配置不被 git 操作覆盖。

**教训**："不提交"和"还原"是两回事。保护本地配置用 `--assume-unchanged`，不要用 `checkout`。

### BUG-5：Agent 回复泄露内部推理

**现象**：回复开头出现 `The user asked "天津在哪里"... Please provide a brief, accurate answer`。

**修复**：加强 system prompt，明确禁止元评论：

```
NEVER include phrases like 'The user asked', 'Please provide', or any meta-commentary.
Just answer the question directly.
```

同时需要**重建 Docker 镜像**：`docker build -t lynxclaw-agent:latest -f container/agent-runner/Dockerfile .`

**教训**：修改容器内代码后必须重建镜像。`docker images --format "{{.CreatedAt}}"` 检查镜像时间。

## 飞书接入排查清单

### 飞书开放平台配置

| # | 检查项 | 如何检查 |
|---|--------|----------|
| 1 | 事件订阅已启用 | 事件与回调 → 确认"启用" |
| 2 | 已添加 `im.message.receive_v1` | 事件列表中包含"接收消息 v2.0" |
| 3 | 接收方式选择"长连接" | 请求方式 = WebSocket |
| 4 | 应用已发布 | 版本管理 → 状态 = 已上线 |
| 5 | 机器人能力已添加 | 应用能力 → 机器人 ✅ |
| 6 | API 权限完整 | 见下表 |

### 必需的 API 权限

| 权限 | 用途 |
|------|------|
| `im:message` | 基础消息权限 |
| `im:message:send_as_bot` | 以机器人身份发消息 |
| `im:message.p2p_msg:readonly` | 私聊消息 |
| `im:message.group_at_msg:readonly` | 群聊 @消息 |
| `im:message.group_msg` | 群聊所有消息（敏感权限） |

> **关键陷阱**：权限批量导入**不会**自动配置事件订阅。每次修改后**必须重新发布版本**。

### 代码层面

| # | 检查项 |
|---|--------|
| 7 | `lark_oapi` ws.Client 使用 monkey-patch 独立 event loop |
| 8 | `_build_content()` 始终返回 Interactive Card（非纯文本） |
| 9 | `EventDispatcherHandler` 初始化两个空字符串 `("", "")` |
| 10 | 容器镜像包含最新代码（`docker images` 检查时间） |

## 飞书平台已知问题

| 问题 | 影响 | 来源 |
|------|------|------|
| WebSocket 事件投递间歇性延迟 | 消息偶尔不到达，重启恢复 | 实测确认 |
| `code: 1000040351, system busy` | 连接建立失败 | [GitHub #42354](https://github.com/openclaw/openclaw/issues/42354) |
| 国际版不支持 WebSocket 长连接 | 必须用 Webhook | [LangBot Docs](https://docs.langbot.app/en/deploy/platforms/lark) |

## 参考资料

- [飞书事件订阅配置](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case)
- [接收消息事件 API](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive?lang=zh-CN)
- [长连接接收事件指南](https://feishu.apifox.cn/doc-7518429)
- [n8n 集成飞书踩坑](https://blog.csdn.net/warkcod/article/details/151836437)
- [飞书接入 OpenClaw 的坑](https://zhuanlan.zhihu.com/p/2011609135314126574)
- [lark-oapi Python SDK](https://github.com/larksuite/oapi-sdk-python)
