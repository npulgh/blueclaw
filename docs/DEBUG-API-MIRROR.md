# API Mirror 调试记录

> 2026-03-20 调试 aicodemirror.com / Kimi K2 等第三方 API 接入

## 问题链路

```
User (Telegram) → Host → Container → api_proxy.py → 第三方 API
```

## 根因分析

### Claude Code CLI 的 URL 拼接规则

Claude Agent SDK (`claude_agent_sdk`) 不直接调 HTTP API。它启动 Claude Code CLI（Node.js 子进程），
CLI 自动在 `ANTHROPIC_BASE_URL` 后追加 `/v1/messages`：

```
ANTHROPIC_BASE_URL=https://api.aicodemirror.com/api/claudecode
CLI 请求 →  https://api.aicodemirror.com/api/claudecode/v1/messages  ✓
```

### `api_proxy.py` 的 `/v1` 剥离 BUG

容器内 `api_proxy.py` 拦截请求后，**无条件**剥离 `/v1` 前缀：

```python
if path.startswith("/v1"):
    path = path[3:]  # /v1/messages → /messages
```

对 Kimi（base URL 含 `/v1`）这是正确的：
- upstream: `https://api.kimi.com/coding/v1`
- 剥离后: `/messages`
- 最终: `https://api.kimi.com/coding/v1/messages` ✓

对 aicodemirror（base URL 不含 `/v1`）这是**错误**的：
- upstream: `https://api.aicodemirror.com/api/claudecode`
- 剥离后: `/messages`
- 最终: `https://api.aicodemirror.com/api/claudecode/messages` ❌
- 应该是: `https://api.aicodemirror.com/api/claudecode/v1/messages` ✓

### 修复方案

智能判断：只在 upstream URL 已含版本前缀（如 `/v1`、`/v4`）时才剥离路径中的 `/v1`。

### 额外必需的环境变量

| 变量 | 值 | 说明 |
|------|-----|------|
| `ANTHROPIC_AUTH_TOKEN` | `""` (空字符串) | aicodemirror 要求；Claude Code 会优先检查此变量 |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | `1` | 避免 CLI 尝试访问 `api.anthropic.com`（被 GFW 封锁） |

## 今日修复的其他问题

| 问题 | 根因 | 修复 |
|------|------|------|
| Polling 立即停止 | GFW 封锁 `api.telegram.org`，aiohttp 不走系统代理 | aiogram 配置 `AiohttpSession(proxy=...)` |
| 容器 mount 失败 | `groups/CLAUDE.md`（文件）挂载到目录 | 挂载目标改为 `/workspace/global_memory/CLAUDE.md` |
| 容器无法连 API | `--network none` | 改为 `bridge` |
| 代理导致 ConnectionRefused | 容器内 `HTTPS_PROXY` 让国内 API 走了海外代理 | 不转发代理到容器 |
