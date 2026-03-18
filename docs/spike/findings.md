# Lynxclaw — Spike 执行发现记录

> 本文件在 Spike 执行过程中填写，记录每项验证的实际结果。
> 设计文档见 [README.md](README.md)。

---

## 执行环境

| 项目 | 值 |
| ---- | ---- |
| 日期 | 2026-03-17 / S2: 2026-03-18 |
| OS | Windows 11 Pro 10.0.26200 (MINGW64/bash) |
| Docker 版本 | 29.2.1 (Docker Desktop WSL2) |
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

> 执行脚本：`spike/ipc_verify.py` | 运行环境：Windows 11 Pro / Docker Desktop 29.2.1 (WSL2) / watchdog 6.0.0

### 关键发现（先读）

**事件类型差异**：`write-tmp + rename` 模式在 Windows Docker Desktop 下触发 `FileMovedEvent`（`on_moved`），而非 `FileCreatedEvent`（`on_created`）。宿主端 `ipc.py` 的 watchdog 处理器**必须同时实现 `on_created` 和 `on_moved`**，才能可靠检测 Docker 容器的原子写入操作。

直接写入（无 rename）正常触发 `on_created`，但不具备原子性，不适合 IPC。

### 基础事件

| 项目 | 结果 |
| ---- | ---- |
| 状态 | **PASS** ✓ |
| 容器写入 → 宿主收到事件 | 是 — `FileCreatedEvent` 在 Docker volume 挂载下正常传递 |
| 备注 | 直接写入 `.json` 文件触发 `on_created`；write-tmp+rename 触发 `on_moved`（见关键发现） |

### 延迟测量

| 项目 | 结果 |
| ---- | ---- |
| 状态 | **PASS** ✓ |
| 样本数 | 100 |
| P50 延迟 | 0.2 ms |
| P99 延迟 | 0.4 ms |
| 最大延迟 | 0.6 ms |
| 备注 | 远低于 100 ms 阈值。Windows Docker Desktop WSL2 延迟优秀，无需降级为轮询模式。 |

### 高频并发

| 项目 | 结果 |
| ---- | ---- |
| 状态 | **PASS** ✓ |
| 总文件数 | 100（5 容器 × 20 文件） |
| 收到事件数 | 100 |
| 丢失率 | 0% |
| 顺序是否正确 | 是 — 各容器内部写入顺序完全保持 |
| 备注 | `on_moved` 处理器正确捕获所有并发 rename 事件；零丢失零乱序。 |

### 原子性

| 项目 | 结果 |
| ---- | ---- |
| 状态 | **PASS** ✓ |
| write-tmp + rename 是否原子 | 是 — 50 个文件全部完整写入 |
| 是否读到半写内容 | 否 — 零个 JSON parse error |
| 备注 | rename 在 NTFS 上保证原子性；watchdog `on_moved` 仅在 rename 完成后触发，确保宿主不会读到半写文件。 |

### 平台差异（Windows Docker Desktop WSL2）

| 项目 | 结果 |
| ---- | ---- |
| 状态 | **PASS** ✓（即此测试本身即 Windows 平台） |
| 与 Linux 行为是否一致 | 实质一致 — 延迟、可靠性均达标 |
| 差异描述 | 事件类型：Windows 下 rename 触发 `FileMovedEvent`（Linux 亦如此，行为相同） |
| 备注 | Windows Docker Desktop WSL2 不是问题；需要处理 `on_moved` 这一点与 Linux 行为完全一致，无平台分叉。 |

### S2 综合判定

| 判定 | 值 |
| ---- | ---- |
| Go / No-Go | **GO** ✓ — 四项全部通过 |
| 是否需要定时扫描兜底 | 可选保留（T1.7 已设计），但非必须 — 事件零丢失 |
| 是否需要回退到轮询模式 | 否 |
| 对 `ipc.py` 的影响 | **必须**：watchdog handler 须同时实现 `on_created`（直接写）和 `on_moved`（write-tmp+rename）；仅 `on_created` 会导致 100% 丢失原子写入事件 |
| 对 TASKS.md 的影响 | T1.7 IPC Watcher 实现时添加注释：`on_moved` 为原子写入的主要触发事件 |

---

## S3: Unix Socket 跨容器验证（可选）

> 执行脚本：`spike/s3_socket_verify.py` | 执行日期：2026-03-18

### S3 关键发现

**容器间 Unix Socket 完全可用**：`--network none` 容器通过 Docker Volume 挂载的 Unix Socket，可与另一个普通容器（服务端）通信。RTT P50=0.060ms，性能远超文件 IPC。

**宿主 Windows Python 无 AF_UNIX**：Python 3.12.10 (win32) 的 `socket` 模块无 `AF_UNIX` 属性，宿主进程无法作为 Unix Socket 服务端。**代理 Sidecar 必须是容器，不能是 Windows 宿主进程**。

### S3.1 Container-to-Container

| 项目 | 结果 |
| ---- | ---- |
| 状态 | **PASS** ✓ |
| 容器 → 宿主 Socket 通信 | — (改测容器间) |
| 容器 → 容器 Socket 通信 | 是 — `--network none` 客户端成功连接服务端 socket |
| RTT | 0.27 ~ 1.1 ms |
| 备注 | 两个容器共享 Docker Volume，服务端创建 Unix socket，客户端以 `--network none` 通过 volume 路径连接，通信完全成功 |

### S3.2 Host-to-Container（Windows 宿主）

| 项目 | 结果 |
| ---- | ---- |
| 状态 | **SKIP / 平台不支持** |
| Windows Python 3.12.10 AF_UNIX 支持 | 否 — `socket.AF_UNIX` 属性缺失（win32 平台） |
| 容器能否连到宿主 Windows socket | 无法验证（宿主无法创建 AF_UNIX socket） |
| 备注 | 宿主侧 AF_UNIX 在当前 Python/Windows 安装中不可用，代理 Sidecar 必须作为容器而非 Windows 进程部署 |

### S3.3 RTT 延迟（--network none → 服务端容器）

| 项目 | 结果 |
| ---- | ---- |
| 状态 | **PASS** ✓ |
| 样本数 | 20 |
| P50 延迟 | **0.060 ms** |
| P99 延迟 | **0.064 ms** |
| 最大延迟 | 0.132 ms |
| 备注 | Unix Socket 延迟极低，比文件 IPC（P99 0.4ms）快约 6×，适合高频 stream_chunk 代理转发 |

### S3 综合判定

| 判定 | 值 |
| ---- | ---- |
| Go / No-Go | **GO** ✓ — 容器间 Unix Socket 验证通过 |
| Proxy Sidecar 实现约束 | **必须**：Sidecar 是容器，不能是 Windows 宿主进程（无 AF_UNIX） |
| 对 ADR-004 的影响 | 补充说明：Proxy Sidecar 必须是容器（Linux 环境），agent 容器与 sidecar 通过共享 volume 的 Unix socket 通信 |
| 延迟结论 | Unix socket P99 0.064ms，适合作为 stream_chunk 代理链路 |

---

## 总结决策

> S1 + S2 + S3 均已完成。

| 项目 | 决策 |
| ---- | ---- |
| 整体 Go / No-Go | **GO** ✓ — S1（Agent SDK）三项 PASS，S2（watchdog IPC）四项 PASS，S3（Unix Socket）两项 PASS |
| 需要修改的 TASKS.md 任务 | T1.7 IPC Watcher：handler 须同时实现 `on_created` 和 `on_moved`（原子写入用后者） |
| 需要更新的 ADR | `docs/adr/001-file-ipc.md`：补充说明宿主端需监听 `FileMovedEvent`；`docs/adr/004-proxy-sidecar.md`：Sidecar 必须是容器（非宿主进程） |
| 其他发现 | watchdog P99 延迟 0.4 ms；write-tmp+rename 零丢失；Unix socket RTT P99 0.064 ms；Windows 宿主 Python 无 AF_UNIX（win32 限制） |
