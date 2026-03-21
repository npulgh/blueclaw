# Lynxclaw — 系统架构

> 本文件描述 Lynxclaw 的技术架构、组件设计和安全模型。
> AI Agent 在实现功能前应先阅读本文件，理解"系统是怎么建的"。
>
> 关联文档：
> - [TASKS.md](TASKS.md) — 开发任务清单（"做什么"）
> - [adr/](adr/) — 架构决策记录（"为什么这么选"）

---

## 一、项目定位

**Lynxclaw** 是一个轻量级 AI 智能体运行平台，以 **Anthropic Claude Agent SDK（Python）** 为核心，将 Claude Agent 安全地运行在 Docker 容器中，通过 IM（Telegram / 飞书）与用户交互。

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
┌──────────────────────────────────────────────────────────────────────┐
│                   Lynxclaw Host Process (Python / asyncio)            │
│                                                                      │
│  ┌─────────────┐  ┌─────────────┐  ┌────────────────────┐           │
│  │  Telegram    │  │   Feishu    │  │  Future Channel    │           │
│  │  Adapter     │  │   Adapter   │  │  (extensible)      │           │
│  └──────┬──────┘  └──────┬──────┘  └────────┬───────────┘           │
│         └────────────────┼──────────────────┘                        │
│  ┌───────────────────────▼───────────────────────────────────────┐   │
│  │                    Channel Registry                            │   │
│  └───────────────────────┬───────────────────────────────────────┘   │
│  ┌───────────────────────▼───────────────────────────────────────┐   │
│  │   Message Router (dedup → trigger → group queue → dispatch)    │   │
│  └───────────────────────┬───────────────────────────────────────┘   │
│  ┌───────────────────────▼───────────────────────────────────────┐   │
│  │   Container Manager (lifecycle / hardening / concurrency)      │   │
│  └──────────┬────────────────────────────────┬───────────────────┘   │
│             │ ephemeral / resumable           │ persistent            │
│             ▼                                 ▼                      │
│  ┌────────────────────┐           ┌─────────────────────┐           │
│  │ docker run (--rm)  │           │ PersistentContainer  │           │
│  └────────────────────┘           │ (long-running +      │           │
│                                   │  heartbeat + inbox)   │           │
│                                   └─────────────────────┘           │
│                                                                      │
│  ┌──────────┐ ┌──────────────┐ ┌───────────┐ ┌───────────┐         │
│  │ SQLite   │ │ IPC Watcher  │ │ Scheduler │ │ Net Proxy │         │
│  │ (state)  │ │ (watchdog)   │ │ (croniter)│ │ (sidecar) │         │
│  └──────────┘ └──────────────┘ └───────────┘ └───────────┘         │
│                                                                      │
│  ┌──────────────┐ ┌────────────────┐ ┌───────────┐ ┌────────────┐  │
│  │ Stream       │ │ Swarm          │ │ Memory    │ │ CLI        │  │
│  │ Debouncer    │ │ Coordinator    │ │ Manager   │ │ (管理工具) │  │
│  └──────────────┘ └────────────────┘ └───────────┘ └────────────┘  │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │         Observability (structlog + Prometheus + audit)          │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────┬───────────────────────────────────────────┘
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
            │  │  MCP: lynxclaw IPC bridge       │  │
            │  │  Hooks: PreToolUse / PostToolUse│  │
            │  │  API Proxy (第三方镜像时)       │  │
            │  └────────────────────────────────┘  │
            │                                      │
            │  Mounts:                             │
            │  /workspace/group/     (rw)          │
            │  /workspace/project/   (ro)          │
            │  /workspace/global/    (ro)          │
            │  /workspace/ipc/       (rw)          │
            │  /proxy/               (rw, 可选)    │
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
| **Persistent** | 常驻 Agent | 长期运行，心跳保活，通过 inbox 接收 prompt（见 §3.12） |

#### 加固启动参数

```bash
docker run --rm \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --read-only \
  --tmpfs /tmp:rw,nosuid,size=256m \
  --tmpfs /home/agent:rw,nosuid,size=64m,uid=1000,gid=1000 \
  --pids-limit 256 \
  --user 1000:1000 \
  --network none \
  --memory 512m \
  --cpus 1.0 \
  -v {group_dir}:/workspace/group:rw \
  -v {global_dir}:/workspace/global:ro \
  -v {global_memory}:/workspace/global_memory/CLAUDE.md:rw \   # Main Group only
  -v {project_dir}:/workspace/project:ro \
  -v {ipc_dir}:/workspace/ipc:rw \
  -v {proxy_vol}:/proxy:rw \           # 可选，仅启用 Proxy Sidecar 时挂载
  -e ANTHROPIC_API_KEY \
  -e ANTHROPIC_BASE_URL \              # 第三方镜像站时设置
  -e ANTHROPIC_AUTH_TOKEN \            # 部分镜像站要求设为空字符串
  -e CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \  # 第三方镜像时禁止非必要请求
  -e LYNXCLAW_GROUP={group} \
  -e LYNXCLAW_SESSION_ID={session_id} \
  lynxclaw-agent:latest
```

> **加固教训**（详见 [ADR-005-container-hardening-lessons.md](adr/ADR-005-container-hardening-lessons.md)）：
> - `/tmp` tmpfs 省略 `noexec` — Node.js JIT 需要可执行内存映射
> - `/home/agent` tmpfs 必需 — Claude Code CLI 启动时写 `~/.claude.json`
> - 挂载目标必须在 Dockerfile 中 `mkdir -p` + `touch` 预创建
> - 文件不能挂载到目录（`groups/CLAUDE.md` → `/workspace/global_memory/CLAUDE.md`）

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
- **监听**：宿主端 `watchdog` 事件驱动，Spike S2 实测 P50=0.2ms / P99=0.4ms
- **容器端**：MCP stdio server（同进程内运行，无跨容器通信问题）

> **⚠️ 关键实现要求**：宿主 watchdog handler 必须**同时实现 `on_created` 和 `on_moved`**。
> 容器采用 write-tmp + rename 原子写入，在 Windows 和 Linux 上均触发 `FileMovedEvent`（`on_moved`）。
> 仅实现 `on_created` 会导致所有原子写入事件 100% 丢失。`on_moved` 中使用 `event.dest_path`。
>
> ```python
> class IPCHandler(FileSystemEventHandler):
>     def on_created(self, event):   # 直接写入（非原子备用路径）
>         if not event.is_directory:
>             self._process(event.src_path)
>     def on_moved(self, event):     # write-tmp + rename（原子写入主路径）
>         if not event.is_directory:
>             self._process(event.dest_path)   # dest_path，不是 src_path
> ```

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
| `delegate_task` | 容器→宿主 | 跨 Group 任务委派（见 §3.14） |

### 3.6 Agent Runner 与安全钩子

**文件**：`container/agent-runner/main.py`

使用 Claude Agent SDK 的 `hooks` 机制：

| 钩子 | 匹配 | 作用 |
| ---- | ---- | ---- |
| `PreToolUse` | `Bash` | 拦截危险命令（`rm -rf /`、`sudo`、fork bomb） |
| `PostToolUse` | `.*` | 写入审计日志（tool name、input、agent_id、时间戳） |

额外保障：`max_turns=30` 防失控循环，`container.timeout` 秒级硬超时。

Resumable 模式下，runner 从 `SystemMessage(subtype="init")` 提取 `session_id`，宿主存入 `sessions` 表。

**SDK 实现要点（Spike S1 验证）**：

```python
# 必须：在宿主环境（Claude Code 进程内）嵌套运行 SDK 时，需绕过嵌套会话检测
os.environ.pop("CLAUDECODE", None)

# Hook 注册与回调签名
async def hook_callback(input: TypedDict, tool_use_id: str, context) -> dict:
    command = input["tool_input"]["command"]
    if is_dangerous(command):
        return {"decision": "block", "reason": "blocked by policy"}
    return {}

ClaudeAgentOptions(
    hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[hook_callback])]}
)

# MCP 自定义 Tool 必须用 ClaudeSDKClient（不能用 query()）
async with ClaudeSDKClient(options=ClaudeAgentOptions(mcp_servers={...})) as client:
    await client.query(prompt)
```

### 3.7 第三方 API 镜像集成

**文件**：`container/agent-runner/api_proxy.py`、`container/agent-runner/main.py`

Lynxclaw 支持 Anthropic 原生 API 和第三方兼容 endpoint（如 REDACTED、Kimi K2）。

#### 请求链路

```text
Claude Agent SDK (Python)
  └─ 启动 Claude Code CLI (Node.js 子进程)
       └─ 读取 ANTHROPIC_BASE_URL 环境变量
            └─ 发送 GET /v1/models/{id}?beta=true (模型验证)
            └─ 发送 POST /v1/messages (实际对话)
```

> **关键规则**：Claude Code CLI **自动**在 `ANTHROPIC_BASE_URL` 后追加 `/v1/messages`。
> 因此 `ANTHROPIC_BASE_URL` **绝不能**包含 `/v1` 后缀。

#### 容器内 API Proxy

第三方 endpoint 通常不实现 `GET /v1/models/{id}` 验证接口，CLI 会因 404 而中止。
容器内 `api_proxy.py` 解决此问题：

```text
CLI → http://127.0.0.1:9099/v1/models/{id}  → Proxy 返回 fake 200
CLI → http://127.0.0.1:9099/v1/messages      → Proxy 转发到真实 upstream
```

**智能路径处理**：Proxy 根据 upstream URL 是否已含版本前缀（`/v1`、`/v4`）决定是否剥离请求路径中的 `/v1`：

| upstream URL | 含版本前缀？ | 请求 `/v1/messages` | 转发结果 |
| ---- | ---- | ---- | ---- |
| `api.example.com/v1` | 是 | 剥离 `/v1` → `/messages` | `.../coding/v1/messages` ✓ |
| `api.example.com/api` | 否 | 保留 `/v1/messages` | `.../api/claudecode/v1/messages` ✓ |

#### 必需环境变量

| 变量 | 用途 | 示例 |
| ---- | ---- | ---- |
| `ANTHROPIC_BASE_URL` | 第三方 endpoint（不含 `/v1`） | `https://api.example.com/api` |
| `ANTHROPIC_AUTH_TOKEN` | 部分镜像要求设为空字符串 | `""` |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | 禁止 CLI 访问 `api.anthropic.com`（GFW 下必需） | `1` |

### 3.8 GFW 环境网络策略

在中国大陆部署时，`api.telegram.org` 被 GFW 封锁，而国内 API 镜像（如 `api.example.com`、`api.example.com`）可直连。需要分层代理策略：

| 组件 | 目标 | 网络策略 |
| ---- | ---- | ---- |
| Host — Telegram Adapter | `api.telegram.org` | 需代理（`HTTPS_PROXY` → Clash 等） |
| Host — aiogram Session | `api.telegram.org` | `AiohttpSession(proxy=...)` + `aiohttp-socks` |
| Container — API Proxy | 国内 API 镜像 | 直连（`--network bridge`），**不转发** `HTTPS_PROXY` |
| Container — Proxy Sidecar | 外部搜索 | Unix Socket 白名单代理（`--network none` 模式） |

> **陷阱**：Python `aiohttp` / `urllib` 不自动使用系统代理。必须显式配置。
> 将 `HTTPS_PROXY` 转发到容器会导致国内 API 走海外代理节点，反而 `ConnectionRefused`。

### 3.9 流式响应

**文件**：`src/main.py`（`_StreamState`），`src/stream_debouncer.py`

Agent 边生成边推送，宿主收到 `stream_chunk` 后经防抖器合并，再转发到 IM：

1. 首块：`send_message()` 发送"💭 Thinking..."占位消息，拿到平台 msg_id
2. 后续块：`edit_message(msg_id)` 覆盖更新（经 StreamDebouncer 合并）
3. 最终块（`is_final=true`）：最后一次 edit
4. **空响应兜底**：若 API 返回 0 字符，容器端写入 `send_message` IPC 作为兜底

**防抖**：500ms 或 200 字符，取先到者，避免触达 IM 平台 API 限频。详见 §3.13。

| 平台 | 流式实现 |
| ---- | ---- |
| Telegram | `edit_message_text()` 覆盖整条消息 |
| 飞书 | `PATCH /im/v1/messages/:id` 更新 Interactive Card |

### 3.10 网络代理（Proxy Sidecar）

默认 `--network none`。需 Web 搜索时通过 Unix Socket 代理受控出站。

> **Spike S3 确认**：Proxy Sidecar 必须是**容器**（Linux 环境），不能是 Windows 宿主进程。
> Windows Python 无 `socket.AF_UNIX`，宿主进程无法创建 Unix Socket 服务端。

```text
agent 容器 (--network none)
    └─ HTTP_PROXY=http://proxy → /proxy/proxy.sock
             ↑ Docker Volume 共享（proxy_vol:/proxy）
Proxy Sidecar 容器 (lynxclaw-proxy, --network bridge)
    └─ 监听 /proxy/proxy.sock → 域名白名单 + 请求日志 + 速率限制
    └─ 出站请求 → Internet（仅白名单域名）
```

即使 Agent 被 prompt injection 劫持，也无法访问白名单外的域名。
Unix Socket RTT P50=0.060ms / P99=0.064ms（Spike S3 实测），对代理链路性能无影响。

### 3.11 可观测性

**文件**：`src/observability.py`

- **日志**：`structlog` JSON 格式，自动附带 correlation_id
- **指标**（Prometheus）：消息量、容器耗时、token 消耗、活跃容器数、IPC 延迟、工具调用/拦截数（共 7 个 Counter/Gauge）
- **审计**：`tool_audit_log` 表 + `data/audit_log.jsonl` 归档

### 3.12 持久容器模式

**文件**：`src/persistent_container.py`

Persistent 模式下容器长期运行，不随请求销毁。适用于需要保持状态的常驻 Agent。

| 机制 | 说明 |
| ---- | ---- |
| 心跳保活 | 定期检查容器健康状态，异常时自动重启 |
| Inbox 投递 | 新消息通过 `data/ipc/{group}/inbox/` 文件投递给运行中的容器 |
| 生命周期 | `start()` / `stop()` / `send_prompt()` / `health_check()` |

容器配置中 `container_mode: persistent` 启用，安全加固标志与 Ephemeral 模式一致。

### 3.13 流式防抖器

**文件**：`src/stream_debouncer.py`

独立组件，负责合并高频 `stream_chunk` IPC 事件，避免触达 IM 平台 API 限频。

- **刷新策略**：500ms 超时 或 200 字符累积，取先到者
- **每 Group 独立状态**：跟踪 `chat_id`、`channel`、`msg_id`
- **首块 → send，后续 → edit**：自动管理消息创建与更新

### 3.14 Swarm 协调器

**文件**：`src/swarm.py`

支持跨 Group 的 Agent 间任务委派。

- **IPC 方法**：`delegate_task`（容器→宿主→目标 Group 队列）
- **权限检查**：仅允许向已配置的 Group 委派
- **异步执行**：委派任务进入目标 Group 的消息队列，不阻塞发起方

### 3.15 内存管理器

**文件**：`src/memory.py`

负责 Group 目录初始化和 CLAUDE.md 模板播种。

- 首次启动时为每个 Group 创建目录结构（`session/`、`files/`）
- 生成 CLAUDE.md 模板，包含 Group 名称、角色描述、可用工具说明
- Main Group 额外获得全局内存写权限说明

### 3.16 管理 CLI

**文件**：`src/cli.py`

提供运维管理命令行工具：

| 命令 | 说明 |
| ---- | ---- |
| `status` | 显示系统状态（活跃容器、队列深度） |
| `groups` | 列出所有 Group 及其配置 |
| `tasks` | 查看/管理定时任务 |
| `usage` | Token 用量统计 |
| `audit` | 查看工具调用审计日志 |

### 3.17 Token 预算控制

宿主在 `_group_consumer()` 中执行预算检查：

- 每个 Group 可配置 `token_budget`（月度上限）
- 每次容器执行后记录 token 消耗到 `token_usage` 表
- 超出预算时拒绝新请求，向 IM 返回提示消息

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

CREATE TABLE token_usage (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  group_name    TEXT NOT NULL,
  input_tokens  INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  created_at    INTEGER NOT NULL,
  FOREIGN KEY (group_name) REFERENCES groups(name)
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
| 定时任务 | `croniter` | Cron 表达式解析 |
| 指标 | `prometheus-client` | Prometheus 格式指标导出 |

---

## 八、配置结构

**文件**：`lynxclaw.config.yaml`

```yaml
host:
  log_level: info

container:
  image: lynxclaw-agent:latest
  memory: 512m
  cpus: 1.0
  network: none                     # none | bridge | proxy
  timeout: 300
  max_concurrent: 5
  lifecycle: ephemeral              # ephemeral | resumable
  runtime: docker                   # docker | podman

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

groups:                               # 频道 chat_id → Group 映射，启动时写入 DB
  - name: main
    channel: telegram
    chat_id: "123456789"
    is_main: true
    trigger: ""                       # 空字符串 = 所有消息触发
    token_budget: 0                   # 0 = 无限制；设置月度 token 上限
  - name: dev-team
    channel: feishu
    chat_id: "oc_xxx"
    trigger: "@bot"
    token_budget: 500000
```

---

## 九、目录结构

```text
lynxclaw/
├── src/
│   ├── main.py                 # 宿主编排器（graceful shutdown）
│   ├── config.py               # 配置加载
│   ├── router.py               # 消息路由（幂等 + 背压）
│   ├── container_manager.py    # 容器生命周期（加固 + 并发控制）
│   ├── ipc.py                  # IPC Watcher（含流式分发）
│   ├── db.py                   # SQLite + 迁移（7 表，schema v2）
│   ├── scheduler.py            # 定时任务（croniter + 30s 轮询）
│   ├── proxy.py                # Network Proxy Sidecar
│   ├── observability.py        # 指标 + 日志（structlog + Prometheus）
│   ├── types.py                # 全局 dataclass
│   ├── memory.py               # Group 目录播种 + CLAUDE.md 模板
│   ├── stream_debouncer.py     # 流式防抖（500ms / 200 chars）
│   ├── swarm.py                # 跨 Group 任务委派
│   ├── persistent_container.py # 持久容器模式（心跳 + inbox）
│   ├── cli.py                  # 管理 CLI（status / groups / tasks / usage / audit）
│   ├── channels/
│   │   ├── registry.py         # ChannelAdapter ABC + 工厂
│   │   ├── telegram.py         # aiogram v3 + TokenBucket 限频
│   │   ├── feishu.py           # lark-oapi WebSocket + Webhook
│   │   └── example_adapter.py  # Echo 适配器（测试参考）
│   └── server.py               # FastAPI（Webhook + /metrics）
├── container/
│   └── agent-runner/
│       ├── requirements.txt
│       ├── Dockerfile          # python:3.11-slim, non-root user 1000
│       ├── main.py             # Agent 入口（含 hooks + 流式 + 持久模式）
│       ├── ipc_bridge.py       # MCP Server → IPC 文件（原子写入）
│       └── api_proxy.py        # 第三方 API 模型验证拦截 + 智能路径转发
├── groups/
│   ├── CLAUDE.md               # 全局内存（非 Main Group 只读）
│   └── {group-name}/CLAUDE.md  # Group 专属记忆
├── data/
│   ├── store/messages.db
│   ├── ipc/{group}/            # outbox/ inbox/ audit/
│   └── audit_log.jsonl
├── tests/                      # 381 pass, 2 skip（22 个测试文件）
│   └── test_e2e_local.py       # E2E 测试（需 Docker + API key）
├── docs/
│   ├── ARCHITECTURE.md         # 本文件
│   ├── TASKS.md                # 开发任务清单
│   ├── E2E-TESTING.md          # E2E 测试策略
│   ├── DEBUG-API-MIRROR.md     # 第三方 API 镜像调试记录
│   └── adr/                    # 架构决策记录
├── Dockerfile                  # 宿主进程镜像（含 Docker CLI）
├── docker-compose.yml          # 一键部署（host + agent 镜像构建）
├── lynxclaw.config.yaml
├── pyproject.toml
└── .env.example
```

---

## 十、v2 架构升级方案（NanoClaw 借鉴）

> 来源：NanoClaw（[qwibitai/nanoclaw](https://github.com/qwibitai/nanoclaw)）架构分析。
> 分析时间：2026-03-21。
> 原则：只借鉴 Lynxclaw 缺失且收益明确的设计，不照搬。

### 10.1 Credential Proxy——凭证永不入容器（P0）✅

**现状问题**：`ANTHROPIC_API_KEY` 通过 `-e` 直接注入容器环境变量。容器运行期间，Agent 可通过 `env` 或 `/proc/self/environ` 读取明文 key。虽然容器是临时的（`--rm`），但 prompt injection 攻击窗口存在。

**NanoClaw 方案**：宿主运行 HTTP Credential Proxy，容器只收到 `ANTHROPIC_BASE_URL=http://host:port` + placeholder key。Proxy 拦截请求，注入真实 Authorization header，转发到上游。

**实际实现**（2026-03-21）：

```text
容器内 Agent
  └─ ANTHROPIC_BASE_URL=http://host.docker.internal:3001  (宿主侧 credential proxy)
       └─ 注入 x-api-key + anthropic-auth-token
       └─ 转发到真实 upstream (api.anthropic.com 或第三方镜像)
```

- 独立模块 `src/credential_proxy.py`（不复用 Proxy Sidecar，职责更清晰）
- 容器网络从 `none` 切换为 `bridge`（需要访问 host）
- 环境变量白名单 `_ALLOWED_ENV_PREFIXES` 阻止未授权变量泄露
- **默认关闭**（`LYNXCLAW_CREDENTIAL_PROXY=0`），因 Docker Desktop (Windows/WSL2) 网络拓扑不支持 `host.docker.internal`
- Linux 原生 Docker 可通过 `LYNXCLAW_CREDENTIAL_PROXY=1` 启用

**影响范围**：`src/credential_proxy.py`（新）、`src/container_manager.py`、`src/main.py`、`container/agent-runner/main.py`

### 10.2 Skills 扩展系统——无代码扩展 Agent 能力（P0）✅

**现状问题**：Agent 能力完全由 `container/agent-runner/main.py` + hooks 决定。用户无法在不改代码的情况下扩展 Agent 行为（如添加领域知识、自定义工具使用规则）。

**NanoClaw 方案**：`.claude/skills/{skill-name}/SKILL.md` 文件系统，Agent 启动时自动加载。

**实际实现**（2026-03-21）：

```text
groups/
  skills/                          ← 全局技能（所有 Group 可用）
    code-review/SKILL.md
    doc-writer/SKILL.md
  {group-name}/
    skills/                        ← Group 专属技能
      custom-tool/SKILL.md
    CLAUDE.md
```

- Agent Runner 启动时扫描 `/workspace/global/skills/` + `/workspace/group/skills/`
- 将 SKILL.md 内容注入 system prompt（在 CLAUDE.md 之后）
- 技能文件为纯 Markdown，描述 Agent 的额外能力、工具使用规则、领域知识
- 挂载方式：`groups/skills/` → `/workspace/global/skills/:ro`（已被 global_dir 覆盖）

**影响范围**：`container/agent-runner/main.py`（`load_skills` + `_inject_skills`）、`src/memory.py`（目录播种）

### 10.3 安全配置外置（P1）✅

**现状问题**：`blocked_patterns` 和 `blocked_commands` 存在 `lynxclaw.config.yaml` 中（项目根目录），安全策略与业务配置混合。

**NanoClaw 方案**：挂载白名单存储在 `~/.config/nanoclaw/mount-allowlist.json`（项目根目录之外）。

**Lynxclaw 融入设计**：

- 新增 `~/.config/lynxclaw/security.yaml`，包含 `blocked_patterns`、`blocked_commands`、环境变量白名单
- `lynxclaw.config.yaml` 只保留业务配置
- `src/config.py` 加载时合并两个配置源
- 安全配置文件永远不挂入容器

**影响范围**：`src/config.py`、`src/container_manager.py`

### 10.4 环境变量白名单（P1）✅

**现状问题**：`_build_command()` 中 `env_vars` 由调用方决定传什么，ContainerManager 层面无过滤。

**融入设计**：在 `_build_command()` 中增加前缀白名单检查：

```python
_ALLOWED_ENV_PREFIXES = {"ANTHROPIC_", "LYNXCLAW_", "CLAUDE_CODE_DISABLE_"}
```

不匹配的环境变量被拦截并记录 warning。实现 Credential Proxy 后，`ANTHROPIC_API_KEY` 也从白名单中移除。

**影响范围**：`src/container_manager.py`

### 10.5 Channel 自注册（P2）✅

**现状问题**：`discover_adapters()` 工厂函数中每个 Channel 需手动添加 `if config.xxx.enabled` 分支。

**NanoClaw 方案**：Channel 在模块加载时调用 `registerChannel()` 自注册，缺少凭证自动跳过。

**Lynxclaw 融入设计**：

- 每个 adapter 模块定义 `CHANNEL_NAME` 和 `create_adapter(config) -> Optional[ChannelAdapter]`
- `discover_adapters()` 改为扫描 `src/channels/` 目录，动态导入并调用 `create_adapter()`
- 缺少凭证时返回 `None`，自动跳过

**触发条件**：当 Channel 数量增长到 4+ 时实施。当前 2 个 Channel 不值得重构。

**影响范围**：`src/channels/registry.py`、各 adapter 模块

### 10.6 Sender Allowlist——消息预过滤（P2）✅

**NanoClaw 方案**：`src/sender-allowlist.ts` 在消息进入路由前过滤，减少无效容器启动。

**Lynxclaw 融入设计**：

- 在 `lynxclaw.config.yaml` 的 group 配置中增加 `allowed_senders: []`（空 = 不限制）
- Router 在 trigger 匹配后、入队前检查 `sender_id` 是否在白名单中
- 不在白名单的消息返回 `RouteResult.UNAUTHORIZED`

**影响范围**：`src/router.py`、`src/config.py`
