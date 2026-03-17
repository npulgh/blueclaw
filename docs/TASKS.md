# Blueclaw — 开发任务清单

> 本文件是 AI Agent 和人类开发者的实施指南。
> 每个任务是一个可独立实现、独立测试的工作单元。
>
> 关联文档：
> - [ARCHITECTURE.md](ARCHITECTURE.md) — 系统架构（"怎么建"）
> - [adr/](adr/) — 架构决策记录（"为什么这么选"）

---

## 使用说明

- 每个任务标注 **文件**（要创建/修改的文件）和 **验收标准**（怎样算做完了）
- 任务间有依赖关系时用 `depends:` 标注
- AI Agent 应一次只做一个任务，完成后运行验收标准中的测试再继续

---

## Phase 1 — MVP（Telegram 端到端）

> 目标：一条 Telegram 消息进来，Agent 在容器里处理，回复发回 Telegram。

### T1.1 项目脚手架

**文件**：`pyproject.toml`, `src/__init__.py`, `src/main.py`, `.env.example`

- 初始化 Python 项目（pyproject.toml，Python 3.11+）
- 依赖：`aiosqlite`, `aiogram`, `watchdog`, `structlog`, `pyyaml`, `python-dotenv`
- `src/main.py` 包含 `async def main()` 空壳 + SIGTERM/SIGINT handler
- `.env.example` 列出所有环境变量占位

**验收**：`python -m src.main` 启动后打印 "Blueclaw starting..."，Ctrl+C 优雅退出。

### T1.2 配置系统

**文件**：`src/config.py`, `blueclaw.config.yaml`

- 加载 YAML 配置 + `.env` 环境变量
- 配置 dataclass 类型提示
- 缺必填项时抛出明确错误

**验收**：单元测试——加载示例配置，验证必填/默认值/类型。

**depends**: T1.1

### T1.3 数据库

**文件**：`src/db.py`, `tests/test_db.py`

- aiosqlite 异步访问
- 建表脚本（messages, groups, sessions, tasks, cursors, tool_audit_log）
- 启动时自动迁移（检查 schema 版本）
- messages 表 `UNIQUE(channel, chat_id, message_id)` 幂等约束

**验收**：
- 测试：建表 → 插入 → 查询 → 重复插入触发 IGNORE
- `data/store/messages.db` 自动创建

**depends**: T1.2

### T1.4 Channel Registry + 抽象基类

**文件**：`src/types.py`, `src/channels/registry.py`, `src/channels/__init__.py`

- `IncomingMessage`, `OutgoingMessage`, `ChannelCapabilities` dataclass（见 ARCHITECTURE.md 3.2）
- `ChannelAdapter` ABC
- `ChannelRegistry`：注册、查找、start_all、stop_all

**验收**：用 Mock Adapter 测试注册/启动/停止生命周期。

**depends**: T1.2

### T1.5 Telegram Adapter

**文件**：`src/channels/telegram.py`, `tests/test_telegram.py`

- 实现 `ChannelAdapter` 接口
- Long Polling 模式（aiogram v3 `Dispatcher.start_polling`）
- `send_message()` 返回平台 message_id
- `edit_message()` 用于后续流式更新

**验收**：
- 配置真实 Bot Token → 启动 → 手动发消息 → 回显（echo adapter）
- 或：aiogram mock 测试 `IncomingMessage` 解析

**depends**: T1.4

### T1.6 消息路由器

**文件**：`src/router.py`, `tests/test_router.py`

- 幂等检查（DB INSERT OR IGNORE）
- Group 查找
- Trigger 匹配（直接消息=始终；群聊=@Bot 前缀）
- Group Queue（`asyncio.Queue(maxsize=10)`）
- 背压：队列满时返回排队提示

**验收**：
- 单元测试：重复 message_id → 只处理一次
- 单元测试：queue 满 → 返回背压信号

**depends**: T1.3, T1.4

### T1.7 IPC 通信层

**文件**：`src/ipc.py`, `container/agent-runner/ipc_bridge.py`, `tests/test_ipc.py`

- 宿主端：watchdog 监听 `data/ipc/{group}/outbox/`
- 消费 JSON-RPC 文件 → 解析 → 分发 → 删除
- 容器端：MCP stdio server，将 tool 调用写入 outbox/
- 支持方法：`send_message`, `stream_chunk`

**验收**：
- 手动写入 JSON 文件到 outbox → Watcher 拾取并打印
- ipc_bridge 单元测试：tool 调用 → 生成文件

**depends**: T1.2

### T1.8 Container Manager（Ephemeral）

**文件**：`src/container_manager.py`, `tests/test_container.py`

- `docker run` 加固启动（见 ARCHITECTURE.md 3.4.2 完整参数）
- `asyncio.Semaphore(max_concurrent)` 并发控制
- 超时自动 `docker kill`
- 挂载验证：白名单/黑名单检查

**验收**：
- 启动测试容器 → 运行 `echo hello` → 收到输出 → 容器自动销毁
- 并发测试：同时 spawn 超过上限 → 排队等待

**depends**: T1.2

### T1.9 Agent Runner 容器镜像

**文件**：`container/agent-runner/Dockerfile`, `container/agent-runner/requirements.txt`, `container/agent-runner/main.py`

- Dockerfile（python:3.11-slim，非 root 用户）
- `requirements.txt`：claude-agent-sdk
- `main.py`：读取环境变量 → 调用 `query()` → 通过 MCP 发送响应
- PreToolUse 钩子：拦截危险 Bash 命令
- PostToolUse 钩子：写审计日志到 `/workspace/ipc/audit/`

**验收**：
- `docker build` 成功
- 手动运行容器（传入 test prompt）→ IPC outbox 中出现响应文件

**depends**: T1.7

### T1.10 端到端集成

**文件**：`src/main.py`（整合所有组件）

- 在 `main()` 中连接：Registry → Router → Container Manager → IPC Watcher
- Telegram 消息 → Router → Container → Agent → IPC → Router → Telegram 回复

**验收**：
- 启动 Blueclaw → 给 Telegram Bot 发消息 → 收到 Agent 回复
- 日志中可见完整消息流

**depends**: T1.5, T1.6, T1.7, T1.8, T1.9

---

## Phase 2 — 飞书 + 流式 + 安全

> 目标：飞书接入、流式响应、审计日志、Resumable 会话。

### T2.1 飞书 Adapter

**文件**：`src/channels/feishu.py`, `tests/test_feishu.py`

- 实现 `ChannelAdapter` 接口
- WebSocket 长连接模式（`lark.ws.Client`）
- `send_message()` 调用飞书 Send Message API v2
- `edit_message()` 更新 Interactive Card

**验收**：飞书群聊 @Bot → 收到 Agent 回复。

**depends**: T1.10

### T2.2 流式响应

**文件**：`src/ipc.py`（新增 stream_chunk 处理），`src/channels/telegram.py`（edit_message），`src/channels/feishu.py`（card update）

- IPC Watcher 收到 `stream_chunk` → 防抖（500ms / 200字符）→ edit_message
- 首块 → send_message 拿 msg_id → 后续块 → edit_message

**验收**：Agent 处理较长任务时，Telegram/飞书中消息实时增长更新。

**depends**: T2.1

### T2.3 Resumable 会话

**文件**：`src/container_manager.py`（传 session_id），`container/agent-runner/main.py`（resume 参数），`src/db.py`（sessions 表操作）

- Agent Runner 返回 session_id → 宿主存入 sessions 表
- 下次消息：从 DB 读 session_id → 传入容器环境变量 → Agent SDK `resume=`

**验收**：发两条消息（前后相隔 > 容器生命周期），第二条能引用第一条的上下文。

**depends**: T1.10

### T2.4 挂载安全验证

**文件**：`src/container_manager.py`（validate_mount 函数完善）

- 路径穿越检测（`..` / 绝对路径注入）
- 符号链接解析后再校验
- 白名单/黑名单匹配
- Non-Main Group 强制只读

**验收**：单元测试——尝试挂载 `.ssh` 目录 → 拒绝；正常目录 → 通过。

**depends**: T1.8

### T2.5 多 Group 支持

**文件**：`src/router.py`（多 Group 路由），`src/db.py`（groups 表操作），CLI 或配置注册 Group

- 多个 chat_id 映射不同 Group
- 每个 Group 独立 Queue、独立 IPC 目录、独立 CLAUDE.md

**验收**：两个不同 Telegram 群各有独立 Agent 上下文，互不干扰。

**depends**: T1.10

### T2.6 Webhook HTTP Server（可选模式）

**文件**：`src/server.py`

- FastAPI 应用，接收 Telegram/飞书 Webhook 回调
- 配置 `mode: webhook` 时启用，替代 Long Polling / WebSocket

**验收**：配置 webhook 模式 → Telegram 设置 webhook URL → 消息正常收发。

**depends**: T2.1

---

## Phase 3 — 网络代理 + 运维

> 目标：受控联网、可观测性、定时任务、生产就绪。

### T3.1 Network Proxy Sidecar

**文件**：`src/proxy.py`

- Unix Socket 监听
- 域名白名单（`proxy.allowed_domains`）
- 请求日志（URL + 大小 + 耗时）
- 速率限制

**验收**：容器通过 proxy 访问 google.com → 成功；访问 evil.com → 被拒。

### T3.2 可观测性

**文件**：`src/observability.py`, `src/server.py`（/metrics 端点）

- structlog JSON 日志 + correlation_id
- Prometheus 指标：7 个核心指标（见 ARCHITECTURE.md 3.9）
- /metrics 端点

**验收**：`curl /metrics` 返回 Prometheus 格式；structlog 输出包含 correlation_id。

### T3.3 定时任务调度

**文件**：`src/scheduler.py`

- 从 tasks 表读取 active 任务
- croniter 解析 cron 表达式
- 到期任务 → 通过 Container Manager spawn 执行

**验收**：创建 cron 任务 → 到期时自动触发 Agent → 响应发到对应 Group。

### T3.4 分层内存系统

**文件**：`groups/CLAUDE.md`（模板），文档说明

- 全局 CLAUDE.md 模板
- Group 级 CLAUDE.md 模板
- Main Group 写全局、Non-Main 只读的权限检查

**验收**：Main Group Agent 修改全局 CLAUDE.md → 其他 Group 下次启动能读到变更。

### T3.5 崩溃恢复

**文件**：`src/router.py`（recover_pending），`src/db.py`

- 启动时扫描 status='processing' 的消息 → 重新入队
- cursors 表记录各 Channel 最后处理的 message_id

**验收**：模拟宕机（kill -9）→ 重启 → 未处理消息自动恢复。

---

## Phase 4 — 扩展

> 目标：Channel 插件化、多 Agent 协作、管理界面。

### T4.1 Channel 扩展接口

文档化 `ChannelAdapter` 接口，提供第三方开发指南。

### T4.2 Agent Swarms

多 Agent 协作 + 跨 Group 路由。

### T4.3 管理 CLI / Web Dashboard

健康检查、Group 管理、任务管理、审计日志查看。

### T4.4 Persistent 容器模式

长驻 Agent 容器 + 心跳保活 + 进程内多任务隔离。

---

## DO NOT CHANGE 清单

> 以下设计已在 ADR 中确认，修改前必须先更新对应 ADR。

| 不可随意变更的决策 | 对应 ADR |
| ---- | ---- |
| IPC 使用文件系统 JSON-RPC（不换 gRPC/Socket） | [ADR-001](adr/001-file-ipc.md) |
| Telegram SDK 使用 aiogram（不换 python-telegram-bot） | [ADR-002](adr/002-aiogram.md) |
| 长连接优先于 Webhook | [ADR-003](adr/003-long-connection.md) |
| 联网走 Proxy Sidecar（不用 --network bridge） | [ADR-004](adr/004-proxy-sidecar.md) |
| 默认 Ephemeral 容器 + 可选 Resumable（不做 Persistent） | [ADR-005](adr/005-resumable-containers.md) |
| 容器加固参数（cap-drop ALL 等）不可删减 | ARCHITECTURE.md §3.4.2 |
| 数据库使用 SQLite（不换 PostgreSQL 等） | 设计哲学——零部署依赖 |
