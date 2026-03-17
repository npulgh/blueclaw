# Lynxclaw — Spike 执行发现记录

> 本文件在 Spike 执行过程中填写，记录每项验证的实际结果。
> 设计文档见 [README.md](README.md)。

---

## 执行环境

| 项目 | 值 |
| ---- | ---- |
| 日期 | 2026-03-17 |
| OS | Windows 11 Pro 10.0.26200 (MINGW64/bash) |
| Docker 版本 | （S1 不需要 Docker，S2 时填写） |
| Python 版本 | 3.12.10 |
| claude-agent-sdk 版本 | 0.1.48 |
| Claude Code CLI 版本 | 2.1.63 |

---

## S1: Python Agent SDK 验证

### S1.1 Hooks

| 项目 | 结果 |
| ---- | ---- |
| 状态 | **PASS** ✓ |
| PreToolUse 回调触发 | 是 — `HookMatcher(matcher="Bash")` 成功匹配，回调收到完整 `tool_input.command` |
| block 返回后命令是否被阻止 | 是 — 返回 `{"decision": "block", "reason": "..."}` 后命令未执行 |
| 备注 | 回调签名为 `async def(input: TypedDict, tool_use_id, context)`，`input["tool_input"]["command"]` 包含完整命令字符串。需要 `os.environ.pop("CLAUDECODE", None)` 绕过嵌套会话检测。 |

### S1.2 Resume

| 项目 | 结果 |
| ---- | ---- |
| 状态 | **PASS** ✓ |
| 第一次 query 返回 session_id | 是 — `SystemMessage(subtype="init")` 中 `data["session_id"]` 返回 UUID |
| 第二次 query 带 resume= 是否恢复上下文 | 是 — 传入 `ClaudeAgentOptions(resume=session_id)` 后，Agent 完整回忆出第一次对话中的 secret code |
| 备注 | session_id 格式为 UUID（如 `2a9917c8-b197-4952-a186-cdae7bbfdcda`）。resume 后无需重新指定 `allowed_tools`，SDK 自动继承原会话配置。 |

### S1.3 MCP

| 项目 | 结果 |
| ---- | ---- |
| 状态 | **PASS** ✓ |
| 自定义 MCP tool 注册成功 | 是 — `@tool(name, desc, json_schema_dict)` + `create_sdk_mcp_server()` 正常工作 |
| Agent 调用 tool 成功 | 是 — Agent 自动识别并调用了 `write_file` tool，传入正确参数 |
| tool 输出正确写入文件 | 是 — 文件内容与预期完全一致 |
| 备注 | `input_schema` 接受 JSON Schema dict（`{"type": "object", "properties": {...}}`）。MCP 自定义 tool 需要 `ClaudeSDKClient`（不能用 `query()`）。回调签名 `async def(args: dict) -> dict`，返回 MCP 标准格式 `{"content": [{"type": "text", "text": "..."}]}`。 |

### S1 综合判定

| 判定 | 值 |
| ---- | ---- |
| Go / No-Go | **GO** ✓ — 三项全部通过 |
| 需要启用的替代方案 | 无 — 所有能力均可直接使用 |
| 对 TASKS.md 的影响 | 无需修改。T1.9 Agent Runner 可直接使用 hooks 安全模型、resume 会话恢复、MCP IPC bridge。 |

---

## S2: watchdog + Docker Volume 验证

### 基础事件

| 项目 | 结果 |
| ---- | ---- |
| 状态 | 待执行 |
| 容器写入 → 宿主收到事件 | — |
| 备注 | — |

### 延迟测量

| 项目 | 结果 |
| ---- | ---- |
| 状态 | 待执行 |
| 样本数 | — |
| P50 延迟 | — |
| P99 延迟 | — |
| 最大延迟 | — |
| 备注 | — |

### 高频并发

| 项目 | 结果 |
| ---- | ---- |
| 状态 | 待执行 |
| 总文件数 | — |
| 收到事件数 | — |
| 丢失率 | — |
| 顺序是否正确 | — |
| 备注 | — |

### 原子性

| 项目 | 结果 |
| ---- | ---- |
| 状态 | 待执行 |
| write-tmp + rename 是否原子 | — |
| 是否读到半写内容 | — |
| 备注 | — |

### 平台差异（Windows Docker Desktop）

| 项目 | 结果 |
| ---- | ---- |
| 状态 | 待执行 |
| 与 Linux 行为是否一致 | — |
| 差异描述 | — |
| 备注 | — |

### S2 综合判定

| 判定 | 值 |
| ---- | ---- |
| Go / No-Go | — |
| 是否需要定时扫描兜底 | — |
| 是否需要回退到轮询模式 | — |
| 对 TASKS.md 的影响 | — |

---

## S3: Unix Socket 跨容器验证（可选）

| 项目 | 结果 |
| ---- | ---- |
| 状态 | 待执行 |
| 容器 → 宿主 Socket 通信 | — |
| Windows Docker Desktop 兼容性 | — |
| 备注 | — |

---

## 总结决策

| 项目 | 决策 |
| ---- | ---- |
| 整体 Go / No-Go | — |
| 需要修改的 TASKS.md 任务 | — |
| 需要更新的 ADR | — |
| 其他发现 | — |
