# Lynxclaw

轻量级 AI 智能体运行平台。以 Anthropic Claude Agent SDK（Python）为核心，将 Claude Agent 安全地运行在 Docker 容器中，通过 Telegram / 飞书与用户交互。

## 设计哲学

| 原则 | 说明 |
|------|------|
| 容器即安全边界 | Agent 运行在最小权限 Docker 容器中，OS 级隔离 |
| 纵深防御 | 容器隔离 + 网络代理 + 工具钩子 + 挂载白名单，多层叠加 |
| 流式优先 | Agent 响应边生成边推送到 IM，用户无需等待完整回复 |
| 小而可审计 | 核心代码 ≤ 5,000 行 |
| IPC 解耦 | 宿主与容器通过文件系统 JSON-RPC 通信，不耦合 SDK 版本 |

## 架构概览

```
Telegram / 飞书
      ↓
  Channel Adapter
      ↓
  Message Router  ──→  SQLite (messages, groups, sessions, tasks)
      ↓
Container Manager  (asyncio.Semaphore, max 5 并发)
      ↓
Docker Container  (--cap-drop ALL / --read-only / --network none)
      │
  Agent Runner  (Claude Agent SDK + PreToolUse/PostToolUse hooks)
      │
  IPC Bridge  (MCP stdio → /workspace/ipc/outbox/ 文件)
      ↓
  IPC Watcher  (watchdog 事件驱动)
      ↓
  Channel send_message / edit_message (流式更新)
```

## 快速开始

### 前置条件

- Python 3.11+
- Docker Engine
- Anthropic API Key（或兼容 endpoint，如 Kimi K2）
- （可选）Telegram Bot Token / 飞书应用凭证

### 安装

```bash
git clone https://github.com/yourname/lynxclaw.git
cd lynxclaw

# 创建并激活虚拟环境
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
.venv\Scripts\activate           # Windows

# 安装依赖
pip install -e ".[dev]"

# 配置环境变量
cp .env.example .env
# 编辑 .env，填入 ANTHROPIC_API_KEY 等凭证
```

### 构建 Agent 镜像

所有运行路径都需要先构建 Agent 容器镜像：

```bash
docker build -t lynxclaw-agent:latest -f container/agent-runner/Dockerfile .
```

---

### 路径 A：接 Telegram Bot（推荐体感最好）

Step 1 — 创建 Bot：在 Telegram 找 `@BotFather`，发 `/newbot`，获取 token。

Step 2 — 配置 `.env`：

```
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=你的token
```

也支持 Anthropic 兼容 endpoint（如 Kimi K2）：

```
ANTHROPIC_API_KEY=sk-kimi-...
ANTHROPIC_BASE_URL=https://api.kimi.com/coding/v1
```

Step 3 — 获取 `chat_id`。先给 bot 发一条任意消息，然后在浏览器打开：

```
https://api.telegram.org/bot<你的TOKEN>/getUpdates
```

在返回的 JSON 中找到 `result[0].message.chat.id`，即为你的 `chat_id`。私聊是正数（如 `7994661284`），群聊是负数（如 `-1001234567890`）。

> **常见问题**：如果返回 404，检查 URL 中 `bot` 前缀是否存在（格式必须是 `bot123456:AAH...`，`bot` 和 token 之间无空格）。如果 `result` 为空数组，先给 bot 发一条消息再刷新。

Step 4 — 启用 Telegram + 配置 group，编辑 `lynxclaw.config.yaml`：

```yaml
telegram:
  enabled: true
  mode: polling

groups:
  - name: main
    channel: telegram
    chat_id: "你的chat_id"   # Step 3 中获取的值
    is_main: true
    trigger: ""              # 私聊建议设为空（所有消息触发）；群聊可设为 "@bot"
    token_budget: 0
```

> **trigger 说明**：`trigger: "@bot"` 要求消息必须以 `@bot` 开头才会触发 agent。私聊场景建议设为 `""`（空字符串），让所有消息都触发。群聊场景建议保留前缀，避免 bot 响应无关对话。

Step 5 — 启动：

```bash
python -m src.main
```

Step 6 — 体验：

在 Telegram 群里发 `@bot 你好`（或私聊），观察：

- 终端日志显示：消息路由 → 容器启动 → Agent 调用 API → IPC 响应
- Bot 流式回复：先发一条消息，然后不断 edit 更新内容
- 数据持久化：`data/store/messages.db` 中可查看消息记录和 token 用量

---

### 路径 B：本地 E2E（不需要 IM token）

使用内置的 `ExampleAdapter` + 真实 Docker + 真实 API，跑通完整链路：

```bash
# 确保 .env 中 API key 有效
python -m pytest tests/test_e2e_local.py -v -x
```

这会走完整消息流：注入消息 → Router → 启动容器 → Agent 调用 API → IPC 写文件 → Watcher 回调 → Adapter 收到回复。

---

### 路径 C：Docker Compose 一键部署

```bash
docker compose up -d --build
```

需要先配好 `.env` 和 `lynxclaw.config.yaml`。详见 [docker-compose.yml](docker-compose.yml)。

---

### 运行测试

```bash
python -m pytest tests/                                         # 全部测试（381 pass，~17s）
python -m pytest tests/ --ignore=tests/test_e2e_local.py        # 仅单元/集成（不需要 Docker）
python -m pytest tests/test_e2e_local.py                        # E2E（需要 Docker + API key）
```

### 常见调整

| 需求 | 配置项 |
|------|--------|
| Agent 需要联网（搜索等） | `container.network: proxy` + `proxy.enabled: true` |
| 使用兼容 API endpoint | `.env` 中设置 `ANTHROPIC_BASE_URL` |
| 容器保持运行（多轮对话复用） | `container.lifecycle: resumable` |
| 调整流式刷新频率 | `streaming.debounce_ms` / `streaming.debounce_chars` |
| 限制月度 token 用量 | `groups[].token_budget: 500000` |

## 配置

主配置文件：`lynxclaw.config.yaml`（参考 `docs/ARCHITECTURE.md §8`）

```yaml
container:
  lifecycle: ephemeral    # ephemeral | resumable
  max_concurrent: 5

telegram:
  enabled: true
  mode: polling           # polling | webhook

feishu:
  enabled: false
  mode: websocket         # websocket | webhook
```

敏感凭证通过 `.env` 传入（不进配置文件）：

```
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=...
FEISHU_APP_ID=...
FEISHU_APP_SECRET=...
```

## 文档

| 文档 | 内容 |
|------|------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 系统架构、组件设计、安全模型、数据模型 |
| [docs/TASKS.md](docs/TASKS.md) | 分阶段开发任务清单与验收标准 |
| [docs/adr/](docs/adr/) | 架构决策记录（为什么这么选） |

## 安全模型

每个 Agent 调用在独立 Docker 容器中运行，硬化参数：

- `--cap-drop ALL` — 移除所有 Linux Capabilities
- `--read-only` — 只读根文件系统
- `--network none` — 默认无网络（联网走 Proxy Sidecar）
- `--user 1000:1000` — 非 root 用户
- `--pids-limit 256` — 进程数上限
- `--memory 512m --cpus 1.0` — 资源限制

IM 凭证永远不进容器，仅 `ANTHROPIC_API_KEY` 通过环境变量注入。

## 开发状态

**MVP 已完成**（2026-03-19）。4 个开发阶段全部实现并测试通过，381 个单元/集成测试 pass。详见 [docs/TASKS.md](docs/TASKS.md)。

## License

MIT
