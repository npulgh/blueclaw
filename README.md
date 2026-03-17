# Blueclaw

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
- Telegram Bot Token 或 飞书应用凭证
- Anthropic API Key

### 安装

```bash
git clone https://github.com/yourname/blueclaw.git
cd blueclaw

# 创建并激活虚拟环境
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
.venv\Scripts\activate           # Windows

# 安装依赖（项目脚手架完成后）
pip install -e ".[dev]"

# 配置环境变量
cp .env.example .env
# 编辑 .env，填入 ANTHROPIC_API_KEY、TELEGRAM_BOT_TOKEN 等
```

### 启动

```bash
# 构建 Agent 容器镜像
docker build -t blueclaw-agent:latest container/agent-runner/

# 启动宿主进程
python -m src.main
```

### 运行测试

```bash
pytest tests/
pytest tests/test_db.py          # 单个测试文件
```

## 配置

主配置文件：`blueclaw.config.yaml`（参考 `docs/ARCHITECTURE.md §8`）

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

当前处于 **Phase 1（MVP）** 开发阶段。实施顺序见 [docs/TASKS.md](docs/TASKS.md)。

## License

MIT
