# API Mirror 调试记录

> 2026-03-20 调试 aicodemirror.com / Kimi K2 等第三方 API 接入
> 最终状态：✅ 已修复，端到端验证通过

## 问题链路

```text
User (Telegram) → Host Process → Docker Container → api_proxy.py → 第三方 API
                    ↑                    ↑                ↑              ↑
                 需代理(GFW)        需 bridge 网络    智能路径处理    需正确 URL
```

## 根因分析

### 1. Claude Code CLI 的 URL 拼接规则

Claude Agent SDK (`claude_agent_sdk`) 不直接调 HTTP API。它启动 Claude Code CLI（Node.js 子进程），
CLI **自动**在 `ANTHROPIC_BASE_URL` 后追加 `/v1/messages`：

```
ANTHROPIC_BASE_URL=https://api.aicodemirror.com/api/claudecode
CLI 请求 →  https://api.aicodemirror.com/api/claudecode/v1/messages  ✓
```

**黄金规则：`ANTHROPIC_BASE_URL` 绝不能包含 `/v1` 后缀。**

### 2. `api_proxy.py` 的 `/v1` 剥离 BUG

容器内 `api_proxy.py` 拦截请求后，原实现**无条件**剥离 `/v1` 前缀：

```python
# BUG: 无条件剥离
if path.startswith("/v1"):
    path = path[3:]  # /v1/messages → /messages
```

对 Kimi（base URL 含 `/v1`）这是正确的：

- upstream: `https://api.kimi.com/coding/v1` + `/messages` → `✓`

对 aicodemirror（base URL 不含 `/v1`）这是**错误**的：

- upstream: `https://api.aicodemirror.com/api/claudecode` + `/messages` → `❌`
- 应该是: `.../api/claudecode/v1/messages` → `✓`

### 3. 修复：智能路径处理

用正则检测 upstream URL 是否已含版本前缀，按需决定是否剥离：

```python
import re
has_version_suffix = bool(re.search(r'/v\d+/?$', upstream.rstrip('/')))
if has_version_suffix and path.startswith("/v1"):
    path = path[3:]
```

| upstream URL | 含版本前缀？ | `/v1/messages` 处理 | 最终路径 |
| ---- | ---- | ---- | ---- |
| `api.kimi.com/coding/v1` | 是 | 剥离 → `/messages` | `.../coding/v1/messages` ✓ |
| `api.aicodemirror.com/api/claudecode` | 否 | 保留 → `/v1/messages` | `.../api/claudecode/v1/messages` ✓ |
| `open.bigmodel.cn/api/paas/v4` | 是 | 剥离 → `/messages` | `.../paas/v4/messages` ✓ |

### 4. 额外必需的环境变量

| 变量 | 值 | 说明 |
| ---- | ---- | ---- |
| `ANTHROPIC_AUTH_TOKEN` | `""` (空字符串) | aicodemirror 要求；Claude Code 优先检查此变量 |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | `1` | 禁止 CLI 访问 `api.anthropic.com`（GFW 下必需） |

## 同日修复的其他问题

| # | 问题 | 根因 | 修复 |
| ---- | ---- | ---- | ---- |
| 1 | Telegram Polling 静默失败 | GFW 封锁 `api.telegram.org`，aiohttp 不走系统代理 | aiogram `AiohttpSession(proxy=...)` + `aiohttp-socks` |
| 2 | 容器 mount 失败 | `groups/CLAUDE.md`（文件）挂载到目录路径 | 挂载目标改为 `/workspace/global_memory/CLAUDE.md`，Dockerfile 预创建 |
| 3 | 容器无法连 API | `--network none` 阻断所有出站 | 改为 `--network bridge` |
| 4 | 国内 API ConnectionRefused | 容器内 `HTTPS_PROXY` 让国内 API 走了海外代理节点 | 不转发 `HTTPS_PROXY` 到容器 |
| 5 | 空响应无 IPC 文件 | API 返回空内容时不写 outbox | 写 fallback `send_message` IPC |
| 6 | asyncio 异常丢失 | polling task 异常无人 await | `_safe_polling()` 包装 + 全局异常处理器 |

## 可复用的调试方法论

1. **给 proxy 加 `[PROXY]` stderr 日志** — 追踪实际发送的 URL 和响应码
2. **增大 consumer stderr 截取长度** — 默认 500 字符不够看完整 traceback
3. **手动 `docker run` 复现** — 绕过 host 进程，直接看容器完整输出
4. **分层验证网络** — 先 `docker run python:3.11-slim python3 -c "urllib..."` 确认容器能连目标
5. **对比本机 vs 容器** — 同一个 API key + URL，本机能用但容器不行 → 网络/路径问题

## 关联文档

- [ARCHITECTURE.md](ARCHITECTURE.md) §3.7 第三方 API 镜像集成
- [ARCHITECTURE.md](ARCHITECTURE.md) §3.8 GFW 环境网络策略
- [ADR-005-container-hardening-lessons.md](adr/ADR-005-container-hardening-lessons.md)
