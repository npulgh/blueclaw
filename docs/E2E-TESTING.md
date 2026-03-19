# End-to-End Integration Testing Guide

> Lynxclaw 端到端集成测试策略与操作手册

## 1. 测试现状

项目已有 **383 个单元/集成测试**（381 pass, 2 skip），覆盖全部 4 个阶段的功能模块。

| 指标 | 值 |
|------|-----|
| 测试文件 | 21 个（`tests/` 目录） |
| 总行数 | 7,273 行 |
| 运行时间 | ~17 秒 |
| 跳过项 | 2（Windows 下 symlink 测试） |

**当前局限**：所有测试基于 mock——Docker、IM 平台、文件系统 IPC 均为模拟。`tests/test_integration.py`（1,170 行）是最接近 E2E 的，但仍 mock 了 Docker 和 IM。

## 2. 核心消息流

端到端测试需要验证的完整链路：

```
IM (Telegram/Feishu)
  → ChannelAdapter.on_message()
  → MessageRouter (dedup / trigger matching / queue)
  → GroupConsumer
  → ContainerManager (spawn ephemeral/persistent container)
  → AgentRunner (SDK hooks + streaming)
  → IPC outbox (JSON-RPC file)
  → IPCWatcher (watchdog on_created + on_moved)
  → StreamDebouncer (500ms / 200 chars)
  → ChannelAdapter.send_message() / edit_message()
  → IM
```

## 3. 分层测试策略

### 3.1 Layer 1 — 本地 E2E（无 IM，推荐优先实施）

用 `ExampleAdapter`（已有的 mock adapter）+ **真实 Docker** + **真实 IPC**，不需要 IM token。

**前置条件**：

```bash
# 构建 agent 镜像
docker build -t lynxclaw-agent:latest container/agent-runner/

# 设置 API Key（或使用 mock SDK）
export ANTHROPIC_API_KEY=sk-ant-...
```

**测试骨架**：

```python
# tests/test_e2e_local.py
import shutil
import pytest

@pytest.mark.skipif(not shutil.which("docker"), reason="Docker not available")
class TestLocalE2E:
    """ExampleAdapter → Router → real Container → real IPC → back to adapter"""

    async def test_full_message_flow(self, tmp_path):
        # 1. 准备真实 config，IPC 目录指向 tmp_path
        # 2. 启动 Main orchestrator（用 ExampleAdapter 替代 Telegram）
        # 3. 通过 ExampleAdapter.inject_message() 注入消息
        # 4. 等待 IPC outbox 出现响应文件
        # 5. 验证 ExampleAdapter.sent_messages 包含预期回复
        # 6. 验证 DB 中 messages 表有对应记录
        pass

    async def test_streaming_edit_flow(self, tmp_path):
        # 验证流式响应：多次 edit_message 调用，最终 send_message
        pass

    async def test_idempotency(self, tmp_path):
        # 发送相同 message_id 两次，DB 只有一条记录
        pass

    async def test_graceful_shutdown(self, tmp_path):
        # 发送 SIGTERM，验证容器停止、IPC 清理完毕
        pass
```

**覆盖范围**：核心链路的 ~80%，是性价比最高的 E2E 层。

### 3.2 Layer 2 — IPC 层真实测试（无需 Docker）

单独验证 watchdog + debouncer 在真实文件系统上的行为。

**终端 1 — 启动 IPC Watcher**：

```bash
python -c "
import asyncio
from src.ipc import IPCWatcher
from src.config import load_config

async def main():
    config = load_config('config.yaml')
    watcher = IPCWatcher(config)
    watcher.on_method('send_message', lambda params: print('GOT:', params))
    await watcher.start()
    await asyncio.Event().wait()  # block forever

asyncio.run(main())
"
```

**终端 2 — 模拟容器写 IPC 文件**：

```bash
# 原子写入（先写 .tmp 再 rename，触发 on_moved）
cat > /tmp/msg_001.json.tmp << 'EOF'
{
  "jsonrpc": "2.0",
  "method": "send_message",
  "params": {"chat_id": "123", "content": "hello from fake agent"},
  "id": "1"
}
EOF
mv /tmp/msg_001.json.tmp data/ipc/test-group/outbox/msg_001.json
```

**验证点**：终端 1 应打印 `GOT: {'chat_id': '123', 'content': 'hello from fake agent'}`。

### 3.3 Layer 3 — 带 Telegram 的完整 E2E

需要真实 Telegram Bot Token。

**步骤**：

1. 通过 `@BotFather` 创建测试 bot（`/newbot`）
2. 配置 `config.yaml`：

```yaml
channels:
  telegram:
    token: "${TELEGRAM_BOT_TOKEN}"
    allowed_chats: [-100xxxxxxxxxx]  # 测试群组 ID

groups:
  test-group:
    trigger: "/ask"
    channel: telegram
    chat_id: -100xxxxxxxxxx
    budget:
      monthly_tokens: 100000
```

3. 启动系统：

```bash
python -m src.main
```

4. 在 Telegram 测试群发送 `/ask 你好`，观察：
   - structlog 日志（JSON 格式，含 `correlation_id`）
   - `data/ipc/test-group/outbox/` 产生 JSON-RPC 文件
   - Bot 回复（流式编辑，可见多次消息更新）
   - `data/store/messages.db` 中记录消息和 token 用量

### 3.4 Layer 4 — Feishu E2E

需要飞书应用凭证。

**配置**：

```yaml
channels:
  feishu:
    app_id: "${FEISHU_APP_ID}"
    app_secret: "${FEISHU_APP_SECRET}"
```

**验证方式**：同 Layer 3，在飞书群中 @bot 发送消息。

## 4. 关键验证矩阵

| 验证项 | Layer 1 | Layer 2 | Layer 3/4 | 验证方法 |
|--------|:-------:|:-------:|:---------:|----------|
| 消息去重 | ✅ | — | ✅ | 发送相同 `message_id` 两次，DB 只有一条 |
| 流式推送 | ✅ | — | ✅ | 观察 `edit_message` 调用次数（500ms/200char） |
| 容器安全 | ✅ | — | ✅ | `docker inspect` 确认 cap-drop、read-only、pids-limit |
| IPC 原子写入 | ✅ | ✅ | ✅ | `on_moved` 事件被正确捕获 |
| 优雅关闭 | ✅ | — | ✅ | `kill -TERM <pid>`，容器全部停止、IPC 清理 |
| 崩溃恢复 | ✅ | — | ✅ | `kill -9` 后重启，status='processing' 消息重新入队 |
| Token 预算 | ✅ | — | ✅ | 超月度额度后容器拒绝启动 |
| 网络隔离 | — | — | ✅ | 容器内 `curl` 失败（`--network none`） |
| Proxy 白名单 | — | — | ✅ | 仅白名单域名可访问 |
| 可观测性 | ✅ | — | ✅ | `/metrics` 端点返回 Prometheus 格式 |

## 5. 冒烟测试脚本

```bash
#!/bin/bash
# scripts/smoke_test.sh
set -e

echo "=== 1. Unit tests ==="
python -m pytest tests/ -x -q

echo "=== 2. Docker image build ==="
docker build -t lynxclaw-agent:latest container/agent-runner/

echo "=== 3. Container security flags ==="
# 验证容器以非 root 用户运行
docker run --rm lynxclaw-agent:latest whoami | grep -q "^agent$"
echo "  Container runs as non-root user: OK"

echo "=== 4. Container hardening ==="
CONTAINER_ID=$(docker run -d --rm \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  --pids-limit 256 \
  --user 1000:1000 \
  --network none \
  --memory 512m \
  --cpus 1.0 \
  lynxclaw-agent:latest sleep 10)

# 验证安全配置
INSPECT=$(docker inspect "$CONTAINER_ID")
echo "$INSPECT" | python3 -c "
import sys, json
c = json.load(sys.stdin)[0]
hc = c['HostConfig']
assert hc['CapDrop'] == ['ALL'], 'cap-drop ALL missing'
assert hc['ReadonlyRootfs'] == True, 'read-only missing'
assert hc['PidsLimit'] == 256, 'pids-limit wrong'
assert hc['NetworkMode'] == 'none', 'network not none'
print('  All hardening flags verified: OK')
"
docker stop "$CONTAINER_ID" > /dev/null 2>&1 || true

echo "=== 5. IPC roundtrip (if e2e test exists) ==="
if [ -f tests/test_e2e_local.py ]; then
  timeout 60 python -m pytest tests/test_e2e_local.py -v --timeout=30
else
  echo "  Skipped (tests/test_e2e_local.py not found)"
fi

echo ""
echo "=== All smoke tests passed ==="
```

## 6. docker-compose 测试环境

```yaml
# docker-compose.test.yml
version: "3.8"

services:
  host:
    build: .
    command: python -m src.main
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - ./groups:/app/groups
      - ./data:/app/data
      - /var/run/docker.sock:/var/run/docker.sock
    env_file: .env
    ports:
      - "8080:8080"   # FastAPI /metrics
    depends_on:
      - proxy

  proxy:
    build: container/proxy/
    networks:
      - agent-net

networks:
  agent-net:
    driver: bridge
```

**启动**：

```bash
docker compose -f docker-compose.test.yml up --build
```

## 7. CI 集成建议

```yaml
# .github/workflows/e2e.yml
name: E2E Tests
on: [push, pull_request]

jobs:
  unit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -e ".[dev]"
      - run: python -m pytest tests/ -x --timeout=30

  e2e:
    runs-on: ubuntu-latest
    needs: unit
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -e ".[dev]"
      - run: docker build -t lynxclaw-agent:latest container/agent-runner/
      - run: bash scripts/smoke_test.sh
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

## 8. 实施优先级

| 优先级 | 任务 | 预期收益 |
|:------:|------|----------|
| P0 | Layer 1 — ExampleAdapter + 真实 Docker + 真实 IPC | 覆盖核心链路 80%，无需外部凭证 |
| P0 | 冒烟测试脚本 (`scripts/smoke_test.sh`) | CI 基线保障 |
| P1 | Layer 2 — IPC 手动验证 | 调试 watchdog 边界行为 |
| P1 | docker-compose.test.yml | 一键启动完整环境 |
| P2 | Layer 3 — Telegram E2E | 验证真实 IM 交互 |
| P2 | CI workflow | 自动化回归 |
| P3 | Layer 4 — Feishu E2E | 完整平台覆盖 |
