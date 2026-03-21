# 飞书机器人接入调试记录

> 创建时间：2026-03-21
> 状态：已解决 ✅

## 问题现象

WebSocket 连接成功（Handshake-Status: 0，ping/pong 正常），但新消息不触发事件回调。
首次启动时能收到一条消息（`你好啊，做个自我介绍`），之后再发消息无响应。

## 排查清单

### 飞书开放平台配置（最可能的原因）

| # | 检查项 | 如何检查 | 状态 |
|---|--------|----------|------|
| 1 | **事件订阅已启用** | 飞书开放平台 → 应用详情 → 事件与回调 → 确认"启用"状态 | ❓ |
| 2 | **已添加 `im.message.receive_v1` 事件** | 事件与回调 → 事件列表中包含"接收消息 v2.0" | ❓ |
| 3 | **接收方式选择"长连接"** | 事件与回调 → 请求方式 = WebSocket（不是 Webhook） | ❓ |
| 4 | **应用已发布（不是"开发中"）** | 版本管理与发布 → 当前版本状态 = 已上线 | ❓ |
| 5 | **机器人能力已添加** | 应用能力 → 机器人 ✅ | ❓ |
| 6 | **API 权限完整** | 权限管理 → 以下权限已开启 | ❓ |

### 必需的 API 权限

| 权限 | 用途 | 说明 |
|------|------|------|
| `im:message` | 获取与发送消息 | 基础权限 |
| `im:message:send_as_bot` | 以机器人身份发消息 | 发送回复需要 |
| `im:message.p2p_msg:readonly` | 获取单聊消息 | 私聊场景 |
| `im:message.group_at_msg:readonly` | 获取群聊 @机器人消息 | 群聊 @触发 |
| `im:message.group_msg` | 获取群组所有消息 | **敏感权限**，群聊免@需要 |
| `im:chat` | 获取群组信息 | chat_id 解析 |

> **关键陷阱**：权限批量导入**不会**自动配置事件订阅，必须在"事件与回调"页面**手动单独添加**。

### 应用发布流程

飞书应用必须经过发布流程才能正常接收事件：

1. 左侧菜单 → **版本管理与发布**
2. 创建新版本
3. 提交审核（个人开发者可自审）
4. 确认状态为"已上线"

> **注意**：每次修改权限或事件订阅后，需要**重新发布**一个新版本才能生效。

### 代码层面

| # | 检查项 | 状态 |
|---|--------|------|
| 7 | `lark_oapi` ws.Client 使用独立 event loop | ✅ 已修复（monkey-patch `ws_mod.loop`） |
| 8 | `EventDispatcherHandler` 注册了 `p2_im_message_receive_v1` | ✅ |
| 9 | 回调初始化两个空字符串 `("", "")` | ✅ |
| 10 | WebSocket 连接成功 | ✅（`Handshake-Status: 0`） |

### 已知平台问题

| 问题 | 影响 | 来源 |
|------|------|------|
| WebSocket 长连接间歇性失败 `code: 1000040351, system busy` | 连接建立失败 | [GitHub Issue #42354](https://github.com/openclaw/openclaw/issues/42354) |
| 国际版飞书不支持 WebSocket 长连接 | 必须用 Webhook 模式 | [LangBot Docs](https://docs.langbot.app/en/deploy/platforms/lark) |

## 调试步骤

### Step 1：确认飞书后台配置

登录 [飞书开放平台](https://open.feishu.cn/) → 进入应用 → 逐一核对上述清单项 1-6。

### Step 2：确认应用已发布

版本管理与发布 → 创建版本 → 发布 → 确认"已上线"。

### Step 3：重启服务测试

```bash
python -m src.main
```

发送**新消息**（不是之前发过的），观察日志中是否出现：
- `INSERT OR IGNORE INTO messages ... feishu` — 消息到达
- `router.queued` — 路由成功
- `container.spawn.start` — 容器启动

### Step 4：如果仍无消息

在飞书开放平台 → 应用详情 → **日志查询**，查看是否有事件推送记录。
如果平台侧有推送但 bot 没收到，说明 WebSocket 连接有问题。

## 参考资料

- [飞书事件订阅配置](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case)
- [接收消息事件 API](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive?lang=zh-CN)
- [长连接接收事件指南](https://feishu.apifox.cn/doc-7518429)
- [n8n 集成飞书踩坑](https://blog.csdn.net/warkcod/article/details/151836437)
- [飞书接入 OpenClaw 的坑](https://zhuanlan.zhihu.com/p/2011609135314126574)
- [lark-oapi Python SDK](https://github.com/larksuite/oapi-sdk-python)
