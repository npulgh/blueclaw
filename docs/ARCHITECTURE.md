# Blueclaw — 系统架构

> 本文件描述 Blueclaw 的技术架构、组件设计和安全模型。
> AI Agent 在实现功能前应先阅读本文件，理解"系统是怎么建的"。
>
> 关联文档：
> - [TASKS.md](TASKS.md) — 开发任务清单（"做什么"）
> - [adr/](adr/) — 架构决策记录（"为什么这么选"）

---

## 一、项目定位

**Blueclaw** 是一个轻量级 AI 智能体运行平台，以 **Anthropic Claude Agent SDK（Python）** 为核心，将 Claude Agent 安全地运行在 Docker 容器中，通过 IM（Telegram / 飞书）与用户交互。

### 设计哲学

| 原则 | 说明 |
| ---- | ---- |
| **容器即安全边界** | Agent 运行在最小权限 Docker 容器中，OS 级隔离 |
| **纵深防御** | 容器隔离 + 网络代理 + 工具钩子 + 挂载白名单，多层叠加 |
| **流式优先** | Agent 响应边生成边推送到 IM，用户无需等待完整回复 |
| **长连接优先** | 飞书 WebSocket / Telegram Long Polling 为首选，Webhook 可选 |
| **小而可审计** | 核心代码 ≤5,000 行 |
| **IM 原生** | Telegram + 飞书为一等公民，不做通用网关 |
| **IPC 解耦** | 宿主与容器通过文件系统 JSON-RPC 通信，不耦合 SDK 版本 |

---

## 二、整体架构

```text
┌──────────────────────────────────────────────────────────────────┐
│                     Blueclaw Host Process (Python / asyncio)      │
│                                                                  │
│  ┌─────────────┐  ┌─────────────┐  ┌────────────────────┐       │
│  │  Telegram    │  │   Feishu    │  │  Future Channel    │       │
│  │  Adapter     │  │   Adapter   │  │  (extensible)      │       │
│  └──────┬──────┘  └──────┬──────┘  └────────┬───────────┘       │
│         └────────────────┼──────────────────┘                    │
│  ┌───────────────────────▼───────────────────────────────────┐   │
│  │                  Channel Registry                          │   │
│  └───────────────────────┬───────────────────────────────────┘   │
│  ┌───────────────────────▼───────────────────────────────────┐   │
│  │   Message Router (dedup → trigger → group queue → dispatch)│   │
│  └───────────────────────┬───────────────────────────────────┘   │
│  ┌───────────────────────▼───────────────────────────────────┐   │
│  │   Container Manager (lifecycle / hardening / circuit break) │   │
│  └───────────────────────┬───────────────────────────────────┘   │
│                           │                                      │
│  ┌──────────┐  ┌─────────▼────┐  ┌───────────┐  ┌───────────┐  │
│  │ SQLite   │  │ IPC Watcher  │  │ Scheduler  │  │ Net Proxy │  │
│  │ (state)  │  │ (watchdog)   │  │ (asyncio)  │  │ (sidecar) │  │
│  └──────────┘  └──────────────┘  └───────────┘  └───────────┘  │
└──────────────────────────┬───────────────────────────────────────┘
                           │ docker run (hardened)
            ┌──────────────▼──────────────────────┐
            │        Docker Container              │
            │  cap-drop ALL / read-only rootfs     │
            │                                      │
            │  ┌────────────────────────────────┐  │
            │  │  Agent Runner                   │  │
            │  │  (Claude Agent SDK - Python)    │  │
            │  │                                 │  │
            │  │  Built-in: Bash Read Write Edit │  │
            │  │  MCP: blueclaw IPC bridge       │  │
            │  │  Hooks: PreToolUse / PostToolUse│  │
            │  └────────────────────────────────┘  │
            │                                      │
            │  Mounts:                             │
            │  /workspace/group/     (rw)          │
            │  /workspace/project/   (ro)          │
            │  /workspace/global/    (ro)          │
            │  /workspace/ipc/       (rw)          │
            │  /run/proxy.sock       (rw, 可选)    │
            └──────────────────────────────────────┘
```

---

## 三、核心组件

### 3.1 宿主编排器（Host Orchestrator）

**文件**：`src/main.py`

- 单 Python asyncio 进程，管理全局生命周期
- SIGTERM/SIGINT 优雅关停
- `asyncio.gather` 并发运行：Channel 长连接 + IPC Watcher + Task Scheduler

### 3.2 Channel 适配层

**文件**：`src/channels/`

适配器 + 工厂注册模式。每个 Channel 实现 `ChannelAdapter` 抽象基类：

```python
class ChannelAdapter(ABC):
    async def init(config) -> None
    async def start() -> None
    async def stop() -> None
    def on_message(handler) -> None
    async def send_message(chat_id, content) -> str     # 返回平台 msg_id
    async def edit_message(chat_id, msg_id, content)     # 流式更新需要
    def capabilities() -> ChannelCapabilities
```

**Telegram Adapter** (`src/channels/telegram.py`)：aiogram v3，默认 Long Polling。
**飞书 Adapter** (`src/channels/feishu.py`)：lark-oapi，默认 WebSocket 长连接。

关键数据结构：

```python
@dataclass
class IncomingMessage:
    message_id: str       # 平台原始 ID（幂等用）
    chat_id: str          # 会话/群组标识
    sender_id: str
    sender_name: str
    text: str
    attachments: list
    timestamp: int
    raw: Any

@dataclass
class OutgoingMessage:
    text: Optional[str]
    rich_text: Optional[dict]
    attachments: Optional[list]
```

### 3.3 消息路由器（Message Router）

**文件**：`src/router.py`

流程：幂等检查 → Group 查找 → Trigger 匹配 → 背压检查 → Group Queue → Container Dispatch

- **幂等**：`(channel, chat_id, message_id)` 唯一约束，INSERT OR IGNORE
- **背压**：`asyncio.Queue(maxsize=group_queue_max)`，满时返回排队提示
- **串行**：同 Group 内消息排队处理，避免并发上下文冲突

### 3.4 容器管理器（Container Manager）

**文件**：`src/container_manager.py`

#### 生命周期模式

| 模式 | 适用场景 | 行为 |
| ---- | ---- | ---- |
| **Ephemeral**（默认） | 一次性问答 | `--rm`，用完即毁 |
| **Resumable** | 多轮对话 | 销毁容器，session_id 持久化到 DB，下次传 `resume=` |
| **Persistent**（Phase 4） | 常驻 Agent | 长期运行，心跳保活 |

#### 加固启动参数

```bash
docker run --rm \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  --pids-limit 256 \
  --user 1000:1000 \
  --network none \
  --memory 512m \
  --cpus 1.0 \
  -v {group_dir}:/workspace/group:rw \
  -v {global_dir}:/workspace/global:ro \
  -v {project_dir}:/workspace/project:ro \
  -v {ipc_dir}:/workspace/ipc:rw \
  -e ANTHROPIC_API_KEY \
  -e BLUECLAW_GROUP={group} \
  -e BLUECLAW_SESSION_ID={session_id} \
  blueclaw-agent:latest
```

#### 并发控制

`asyncio.Semaphore(max_concurrent=5)`——超出上限的请求排队等待。

#### 容器镜像

```dockerfile
FROM python:3.11-slim
RUN useradd -m -u 1000 agent
WORKDIR /workspace/group
COPY container/agent-runner/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY container/agent-runner/ ./runner/
USER agent
ENTRYPOINT ["python", "runner/main.py"]
```

### 3.5 IPC 通信层

**文件**：`src/ipc.py`（宿主端），`container/agent-runner/ipc_bridge.py`（容器端）

- **传输**：文件系统 JSON-RPC 2.0，通过 Docker Volume 共享
- **监听**：宿主端 `watchdog` 事件驱动，毫秒级
- **容器端**：MCP stdio server（同进程内运行，无跨容器通信问题）

#### IPC 目录

```text
data/ipc/{group}/
  ├── outbox/     # 容器 → 宿主
  ├── inbox/      # 宿主 → 容器
  └── audit/      # 工具调用审计日志
```

#### IPC 方法

| 方法 | 方向 | 说明 |
| ---- | ---- | ---- |
| `stream_chunk` | 容器→宿主 | 流式文本块（含 `is_final` 标志） |
| `send_message` | 容器→宿主 | 完整消息 |
| `schedule_task` | 容器→宿主 | 创建定时任务 |
| `list_tasks` | 容器→宿主 | 列出任务 |
| `cancel_task` | 容器→宿主 | 取消任务 |
| `read_context` | 容器→宿主 | 读取共享上下文 |

### 3.6 Agent Runner 与安全钩子

**文件**：`container/agent-runner/main.py`

使用 Claude Agent SDK 的 `hooks` 机制：

| 钩子 | 匹配 | 作用 |
| ---- | ---- | ---- |
| `PreToolUse` | `Bash` | 拦截危险命令（`rm -rf /`、`sudo`、fork bomb） |
| `PostToolUse` | `.*` | 写入审计日志（tool name、input、agent_id、时间戳） |

额外保障：`max_turns=30` 防失控循环，`container.timeout` 秒级硬超时。

Resumable 模式下，runner 返回 `session_id`，宿主存入 `sessions` 表。

### 3.7 流式响应

Agent 边生成边推送，宿主收到 `stream_chunk` 后转发到 IM：

1. 首块：`send_message()` 发送，拿到平台 msg_id
2. 后续块：`edit_message(msg_id)` 覆盖更新
3. 最终块（`is_final=true`）：最后一次 edit

**防抖**：500ms 或 200 字符，取先到者，避免触达 IM 平台 API 限频。

| 平台 | 流式实现 |
| ---- | ---- |
| Telegram | `edit_message_text()` 覆盖整条消息 |
| 飞书 | `PATCH /im/v1/messages/:id` 更新 Interactive Card |

### 3.8 网络代理（Proxy Sidecar）

默认 `--network none`。需 Web 搜索时通过 Unix Socket 代理受控出站：

```text
容器内 Agent → HTTP_PROXY=socks5h://proxy → /run/proxy.sock
  ↓ (Unix Socket)
宿主 blueclaw-proxy → 域名白名单 + 请求日志 + 速率限制
```

即使 Agent 被 prompt injection 劫持，也无法访问白名单外的域名。

### 3.9 可观测性

**文件**：`src/observability.py`

- **日志**：`structlog` JSON 格式，自动附带 correlation_id
- **指标**（Prometheus）：消息量、容器耗时、token 消耗、活跃容器数、IPC 延迟、工具调用/拦截数
- **审计**：`tool_audit_log` 表 + `data/audit_log.jsonl` 归档

---

## 四、分层内存系统

```text
groups/
  CLAUDE.md                  ← 全局内存（所有 Group 只读）
  {group-name}/
    CLAUDE.md                ← Group 专属记忆（Agent 可读写）
    session/                 ← Agent SDK 会话数据
    files/                   ← Agent 产出文件
```

Main Group 可写全局 CLAUDE.md；非 Main Group 只读。

---

## 五、安全模型

### 5.1 容器加固基线

| 层级 | 机制 |
| ---- | ---- |
| Capabilities | `--cap-drop ALL` |
| 提权防护 | `--security-opt no-new-privileges` |
| 根文件系统 | `--read-only` + `--tmpfs /tmp` |
| 进程限制 | `--pids-limit 256` |
| 用户隔离 | `--user 1000:1000` |
| 文件系统 | 白名单挂载，项目目录只读 |
| 网络隔离 | `--network none`（联网走 Proxy） |
| 资源限制 | `--memory 512m --cpus 1.0` |

### 5.2 权限分级

| 能力 | Main Group | Non-Main |
| ---- | ---- | ---- |
| 项目目录 | 只读 | 无 |
| Group 目录 | 读写 | 读写 |
| 全局内存 | 可写 | 只读 |
| 网络（Proxy） | 可配 | 默认禁止 |

### 5.3 凭证安全

- `ANTHROPIC_API_KEY` 注入环境变量（容器即销毁，泄露面有限）
- 高安全模式（可选）：Key 由 Proxy 注入 Authorization header，不进环境变量
- IM 凭证永远不进容器
- 默认黑名单：`.ssh`、`.aws`、`.gnupg`、`.env`、`*.pem`、`*.key`、`credentials*`

### 5.4 Prompt Injection 六层防御

| # | 防线 |
| ---- | ---- |
| 1 | 触发词过滤——非 @Bot 不触发 |
| 2 | System Prompt 隔离——CLAUDE.md 声明用户消息不可信 |
| 3 | PreToolUse 钩子——拦截危险 Bash 命令 |
| 4 | Network Proxy 白名单——无法外传数据 |
| 5 | `--read-only` 根文件系统——无法篡改运行时 |
| 6 | `max_turns` 限制——防对抗性循环 |

---

## 六、数据模型

```sql
CREATE TABLE messages (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  channel       TEXT NOT NULL,
  chat_id       TEXT NOT NULL,
  message_id    TEXT NOT NULL,
  group_name    TEXT,
  sender_id     TEXT NOT NULL,
  sender_name   TEXT,
  content       TEXT NOT NULL,
  direction     TEXT NOT NULL,              -- 'inbound' | 'outbound'
  status        TEXT DEFAULT 'pending',
  created_at    INTEGER NOT NULL,
  processed_at  INTEGER,
  UNIQUE(channel, chat_id, message_id)      -- 幂等约束
);

CREATE TABLE groups (
  name          TEXT PRIMARY KEY,
  channel       TEXT NOT NULL,
  chat_id       TEXT NOT NULL,
  is_main       INTEGER DEFAULT 0,
  trigger       TEXT DEFAULT '@bot',
  created_at    INTEGER NOT NULL,
  UNIQUE(channel, chat_id)
);

CREATE TABLE sessions (
  group_name    TEXT PRIMARY KEY,
  session_id    TEXT,
  last_active   INTEGER,
  FOREIGN KEY (group_name) REFERENCES groups(name)
);

CREATE TABLE tasks (
  id            TEXT PRIMARY KEY,
  group_name    TEXT NOT NULL,
  type          TEXT NOT NULL,
  schedule      TEXT NOT NULL,
  prompt        TEXT NOT NULL,
  status        TEXT DEFAULT 'active',
  last_run      INTEGER,
  next_run      INTEGER,
  created_at    INTEGER NOT NULL,
  FOREIGN KEY (group_name) REFERENCES groups(name)
);

CREATE TABLE cursors (
  channel       TEXT NOT NULL,
  chat_id       TEXT NOT NULL,
  last_msg_id   TEXT NOT NULL,
  updated_at    INTEGER NOT NULL,
  PRIMARY KEY (channel, chat_id)
);

CREATE TABLE tool_audit_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  group_name    TEXT NOT NULL,
  session_id    TEXT,
  tool_name     TEXT NOT NULL,
  agent_id      TEXT,
  input_summary TEXT,
  blocked       INTEGER DEFAULT 0,
  created_at    INTEGER NOT NULL
);
```

---

## 七、技术栈

| 类别 | 技术 | 理由 |
| ---- | ---- | ---- |
| 运行时 | Python 3.11+ | Agent SDK 官方支持 |
| AI SDK | `claude-agent-sdk` | 含 hooks / resume / MCP |
| 容器 | Docker Engine | 跨平台 |
| 数据库 | SQLite + `aiosqlite` | 零依赖，async |
| Telegram | `aiogram` v3 | 原生 asyncio |
| 飞书 | `lark-oapi` | 官方 SDK，WebSocket 内置 |
| 文件监听 | `watchdog` | 跨平台 FS watcher |
| 日志 | `structlog` | 结构化 JSON |
| 配置 | `pyyaml` + `python-dotenv` | YAML + .env |
| HTTP | `fastapi` + `uvicorn` | Webhook / metrics（可选） |
| 调度 | `croniter` + `asyncio` | cron 解析 + 异步 |

---

## 八、配置结构

**文件**：`blueclaw.config.yaml`

```yaml
host:
  log_level: info

container:
  image: blueclaw-agent:latest
  memory: 512m
  cpus: 1.0
  network: none                     # none | proxy
  timeout: 300
  max_concurrent: 5
  lifecycle: ephemeral              # ephemeral | resumable

telegram:
  enabled: true
  mode: polling                     # polling | webhook

feishu:
  enabled: true
  mode: websocket                   # websocket | webhook

router:
  default_trigger: "@bot"
  history_limit: 50
  group_queue_max: 10

proxy:
  enabled: false
  allowed_domains:
    - api.anthropic.com
    - "*.google.com"
    - "*.bing.com"

streaming:
  enabled: true
  debounce_ms: 500
  debounce_chars: 200

security:
  blocked_commands: ["rm -rf /", "sudo", "chmod 777", ":(){:|:&};:"]
  blocked_patterns: [".ssh", ".aws", ".gnupg", ".env", "*.pem", "*.key"]
```

---

## 九、目录结构

```text
blueclaw/
├── src/
│   ├── main.py                 # 宿主编排器（graceful shutdown）
│   ├── config.py               # 配置加载
│   ├── router.py               # 消息路由（幂等 + 背压）
│   ├── container_manager.py    # 容器生命周期（加固 + 并发控制）
│   ├── ipc.py                  # IPC Watcher（含流式分发）
│   ├── db.py                   # SQLite + 迁移
│   ├── scheduler.py            # 定时任务
│   ├── proxy.py                # Network Proxy Sidecar
│   ├── observability.py        # 指标 + 日志
│   ├── types.py                # 全局 dataclass
│   ├── channels/
│   │   ├── registry.py
│   │   ├── telegram.py
│   │   └── feishu.py
│   └── server.py               # FastAPI（Webhook + /metrics）
├── container/
│   └── agent-runner/
│       ├── requirements.txt
│       ├── Dockerfile
│       ├── main.py             # Agent 入口（含 hooks）
│       └── ipc_bridge.py       # MCP Server → IPC 文件
├── groups/
│   ├── CLAUDE.md
│   └── {group-name}/CLAUDE.md
├── data/
│   ├── store/messages.db
│   ├── ipc/{group}/
│   └── audit_log.jsonl
├── docs/
│   ├── ARCHITECTURE.md         # 本文件
│   ├── TASKS.md                # 开发任务清单
│   └── adr/                    # 架构决策记录
├── blueclaw.config.yaml
├── docker-compose.yml
├── pyproject.toml
└── .env.example
```
