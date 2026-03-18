# Lynxclaw T0 Spike — 综合报告

> 执行日期：2026-03-17 / 18
> 覆盖范围：S1（Python Agent SDK）、S2（watchdog IPC）、S3（Unix Socket）
> 结论：**全部 GO ✓** — 三项核心技术假设均已验证，Phase 1 开发可启动

---

## 一、总体判定

| Spike | 核心问题 | 判定 |
|-------|---------|------|
| S1 Agent SDK | Python SDK 的 hooks / resume / MCP 是否可用且行为符合预期？ | **GO ✓** |
| S2 watchdog IPC | Docker Volume 文件事件在 Windows Docker Desktop 下是否可靠？ | **GO ✓** |
| S3 Unix Socket | `--network none` 容器能否通过共享卷 Unix Socket 通信？ | **GO ✓** |

无 No-Go 项，无需修改技术路线。有三处实现细节（见第三节）需要在开发时注意。

---

## 二、工程发现（可直接用于实现）

### 2.1 Agent SDK — 精确 API 用法

#### Hooks

```python
# 注册方式
ClaudeAgentOptions(
    hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[callback])]}
)

# 回调签名
async def callback(input: TypedDict, tool_use_id: str, context) -> dict:
    command = input["tool_input"]["command"]   # Bash 命令完整字符串
    if "rm -rf" in command:
        return {"decision": "block", "reason": "blocked"}
    return {}

# 必须：在 Claude Code 内运行时绕过嵌套会话检测
os.environ.pop("CLAUDECODE", None)
```

#### Resume（会话恢复）

```python
# 第一次：从 SystemMessage 获取 session_id
async for msg in query(prompt, options):
    if isinstance(msg, SystemMessage) and msg.subtype == "init":
        session_id = msg.data["session_id"]   # UUID 字符串

# 第二次：恢复上下文（无需重新指定 allowed_tools，自动继承）
options = ClaudeAgentOptions(resume=session_id)
```

#### MCP 自定义 Tool

```python
# 注册（json_schema_dict 是完整的 JSON Schema）
@tool("write_file", "Write content to a file", {
    "type": "object",
    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
    "required": ["path", "content"]
})
async def write_file_tool(args: dict) -> dict:
    Path(args["path"]).write_text(args["content"])
    return {"content": [{"type": "text", "text": "ok"}]}   # MCP 标准返回格式

server = create_sdk_mcp_server("file-tools", tools=[write_file_tool])

# 必须用 ClaudeSDKClient，不能用 query()
async with ClaudeSDKClient(options=ClaudeAgentOptions(mcp_servers={"file-tools": server})) as client:
    await client.query(prompt)
```

---

### 2.2 IPC watchdog 处理器 — 关键平台差异

**核心发现**：Docker 容器使用 `write-tmp + rename` 原子写入时，Windows Docker Desktop 触发
`FileMovedEvent`（`on_moved`），而非 `FileCreatedEvent`（`on_created`）。Linux 行为相同。

**正确实现**（`src/ipc.py` 的 watchdog handler 必须如此）：

```python
class IPCHandler(FileSystemEventHandler):
    def on_created(self, event):          # 直接写入触发（非原子，备用路径）
        if not event.is_directory:
            self._process(event.src_path)

    def on_moved(self, event):            # write-tmp + rename 触发（原子写入主路径）
        if not event.is_directory:
            self._process(event.dest_path)   # ← dest_path，不是 src_path
```

**若仅实现 `on_created`，原子写入事件 100% 丢失**（在 Windows 和 Linux 均如此）。

**性能数据**（Windows Docker Desktop WSL2，100 样本）：

| 指标 | 直接写入 | write-tmp + rename |
|------|---------|-------------------|
| P50  | 0.2 ms  | 同量级             |
| P99  | 0.4 ms  | 同量级             |
| 丢失率 | 0%    | 0%（100 并发文件，5 容器）|

---

### 2.3 Unix Socket 跨容器通信

**结论**：`--network none` 容器可通过 Docker Volume 共享的 Unix Socket 与另一容器双向通信。
性能远超文件 IPC：

| 指标 | Unix Socket (P50/P99) | 文件 IPC (P99) |
|------|----------------------|---------------|
| RTT  | 0.060 ms / 0.064 ms  | 0.4 ms（事件延迟）|

**关键平台限制**：

```python
# Windows Python 3.12.10 (win32) 无 AF_UNIX
hasattr(socket, "AF_UNIX")  # → False

# 宿主进程无法作为 Unix Socket 服务端
# Proxy Sidecar 必须是容器（Linux 环境）
```

**工作模式**（Phase 3 Proxy Sidecar 的实现基础）：

```
agent 容器 (--network none)
    └─ 连接 /socket/proxy.sock
         ↑ Docker Volume 共享
proxy sidecar 容器 (有网络)
    └─ 监听 /socket/proxy.sock
```

---

## 三、需要落地到代码的实现要点

| 任务 | 要点 | 影响文件 |
|------|------|---------|
| **T1.7 IPC Watcher** | 必须同时实现 `on_created` 和 `on_moved`；`on_moved` 用 `dest_path` | `src/ipc.py` |
| **T1.9 Agent Runner** | `os.environ.pop("CLAUDECODE", None)` 才能在宿主环境内嵌套运行 SDK | `container/agent-runner/main.py` |
| **Phase 3 Proxy Sidecar** | Sidecar 必须是容器（不能是 Windows 宿主进程）；socket 通过共享 volume 暴露 | `src/proxy.py` + ADR-004 |

---

## 四、对 ADR 的补充说明

> 以下内容需在对应 ADR 文件中以脚注形式补充，不修改原决策。

**ADR-001（文件 IPC）**：宿主端 watchdog handler 须同时监听 `FileCreatedEvent` 和
`FileMovedEvent`。容器的原子写入（write-tmp + rename）仅触发后者。

**ADR-004（Proxy Sidecar）**：Sidecar 进程必须运行在 Linux 容器中（非 Windows 宿主进程），
因为 Windows Python 不提供 `socket.AF_UNIX`。推荐实现为独立容器，与 agent 容器共享 IPC volume，
通过 `proxy.sock` 提供 HTTP 代理。

---

## 五、原始数据与可复现资产

| 资产 | 用途 |
|------|------|
| [findings.md](findings.md) | 三项 Spike 完整执行记录（表格 + 数据 + 备注） |
| [`spike/ipc_verify.py`](../../spike/ipc_verify.py) | S2 watchdog 测试脚本，可在新环境复现 |
| [`spike/s3_socket_verify.py`](../../spike/s3_socket_verify.py) | S3 Unix Socket 测试脚本，可在新环境复现 |
| [`spike/s2_results.json`](../../spike/s2_results.json) | S2 机器可读结果 |
| [`spike/s3_results.json`](../../spike/s3_results.json) | S3 机器可读结果 |
| [archive/](archive/) | Spike 规格和任务清单（过程性文档，已存档） |
