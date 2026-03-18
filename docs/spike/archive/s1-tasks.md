# S1: Python Agent SDK 验证 — 开发任务清单

> 生成日期：2026-03-17
> 状态：待执行
> 规格文档：[s1-agent-sdk.md](s1-agent-sdk.md)
> 结果记录：[findings.md](findings.md) § S1

---

## 前置条件

### Step 0.1 — 环境准备

**目标**：确认 Python 环境与 SDK 可用。

- [ ] 确认 Python ≥ 3.11（`python --version`）
- [ ] 确认 `ANTHROPIC_API_KEY` 环境变量已设置
- [ ] 安装 SDK：`pip install claude-agent-sdk`
- [ ] 确认 Claude Code CLI 已安装（SDK 底层依赖）：`claude --version`
- [ ] 记录版本号到 `findings.md` 执行环境表

**验收**：`python -c "from claude_agent_sdk import query; print('OK')"` 输出 `OK`。

**代码提示**：
```bash
python --version
pip install claude-agent-sdk
claude --version
python -c "from claude_agent_sdk import query, ClaudeAgentOptions; print('OK')"
```

### Step 0.2 — 创建验证脚本骨架

**目标**：在项目根目录创建 `spike/sdk_verify.py`，统一入口。

- [ ] 创建 `spike/` 目录
- [ ] 创建 `spike/sdk_verify.py` 骨架文件，包含三个 async 子函数占位

**验收**：`python spike/sdk_verify.py` 能运行（输出占位信息），无 import 错误。

**代码骨架**：
```python
"""S1: Python Agent SDK 验证脚本"""
import anyio
import json
import sys
from pathlib import Path

RESULTS: dict[str, dict] = {}

async def verify_hooks() -> dict:
    """S1.1 — Hooks 验证"""
    print("\n=== S1.1 Hooks 验证 ===")
    # TODO: implement
    return {"status": "not_implemented"}

async def verify_resume() -> dict:
    """S1.2 — Resume 验证"""
    print("\n=== S1.2 Resume 验证 ===")
    # TODO: implement
    return {"status": "not_implemented"}

async def verify_mcp() -> dict:
    """S1.3 — MCP 验证"""
    print("\n=== S1.3 MCP 验证 ===")
    # TODO: implement
    return {"status": "not_implemented"}

async def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else ["hooks", "resume", "mcp"]

    if "hooks" in targets:
        RESULTS["s1_1_hooks"] = await verify_hooks()
    if "resume" in targets:
        RESULTS["s1_2_resume"] = await verify_resume()
    if "mcp" in targets:
        RESULTS["s1_3_mcp"] = await verify_mcp()

    print("\n=== 汇总 ===")
    print(json.dumps(RESULTS, indent=2, ensure_ascii=False))

    all_pass = all(r.get("status") == "pass" for r in RESULTS.values())
    print(f"\nGo / No-Go: {'GO ✓' if all_pass else 'NO-GO ✗'}")

if __name__ == "__main__":
    anyio.run(main)
```

---

## S1.1 — Hooks 验证

### Step 1.1 — 实现 PreToolUse hook 拦截逻辑

**目标**：注册 PreToolUse hook，拦截包含 `rm -rf` 的 Bash 命令。

- [ ] 在 `verify_hooks()` 中编写 hook 回调函数
- [ ] 回调检查 `input_data` 中的 `tool_input.command` 是否包含 `rm -rf`
- [ ] 若匹配，返回 `{"decision": "block"}` 阻止执行
- [ ] 用一个标志变量记录 hook 是否被触发

**关键 API**：
```python
from claude_agent_sdk import query, ClaudeAgentOptions, HookMatcher, ResultMessage

hook_triggered = False
command_blocked = False

async def block_rm_rf(input_data, tool_use_id, context):
    nonlocal hook_triggered, command_blocked
    hook_triggered = True
    tool_input = input_data.get("tool_input", {})
    command = tool_input.get("command", "")
    print(f"  [Hook] 拦截到 Bash 命令: {command[:80]}")
    if "rm -rf" in command:
        command_blocked = True
        print("  [Hook] → 命令被阻止 (block)")
        return {"decision": "block"}
    return {}
```

**注意**：hook 回调的返回值格式需要在执行时验证。如果 `{"decision": "block"}` 不生效，尝试其他格式（如 `{"error": "blocked"}`）并记录到 findings。

### Step 1.2 — 触发 hook 并验证

- [ ] 构造一个会让 Agent 调用 `rm -rf` 的 prompt
- [ ] 通过 `query()` 发送，附带 PreToolUse hook
- [ ] 验证：hook 被触发（`hook_triggered == True`）
- [ ] 验证：命令被阻止（`command_blocked == True`）
- [ ] 捕获并处理可能的异常

**代码提示**：
```python
async def verify_hooks() -> dict:
    hook_triggered = False
    command_blocked = False

    async def block_rm_rf(input_data, tool_use_id, context):
        nonlocal hook_triggered, command_blocked
        hook_triggered = True
        tool_input = input_data.get("tool_input", {})
        command = tool_input.get("command", "")
        print(f"  [Hook] 拦截到命令: {command[:80]}")
        if "rm -rf" in command:
            command_blocked = True
            return {"decision": "block"}
        return {}

    try:
        async for message in query(
            prompt="Run this bash command: rm -rf /tmp/test_nonexistent_dir",
            options=ClaudeAgentOptions(
                allowed_tools=["Bash"],
                permission_mode="bypassPermissions",
                hooks={
                    "PreToolUse": [
                        HookMatcher(matcher="Bash", hooks=[block_rm_rf])
                    ]
                },
                max_turns=3,
            )
        ):
            if isinstance(message, ResultMessage):
                print(f"  Agent 回复: {message.result[:200]}")
    except Exception as e:
        print(f"  异常: {type(e).__name__}: {e}")

    status = "pass" if (hook_triggered and command_blocked) else "fail"
    return {
        "status": status,
        "hook_triggered": hook_triggered,
        "command_blocked": command_blocked,
    }
```

### Step 1.3 — 记录 S1.1 结果

- [ ] 将 `hook_triggered` 和 `command_blocked` 结果填入 `findings.md` S1.1 表格
- [ ] 如果失败，记录具体错误信息和 hook 回调实际收到的 `input_data` 结构

**判定标准**（来自 spec）：
| 结果 | 判定 |
|------|------|
| PreToolUse 回调被触发 + block 后命令未执行 | **PASS** |
| 回调未触发，或 block 无效 | **FAIL** |

---

## S1.2 — Resume 验证

### Step 2.1 — 第一次 query：获取 session_id

- [ ] 发送一个简单 prompt（如 "My name is Lynxclaw. Remember this."）
- [ ] 从 `SystemMessage(subtype="init")` 中捕获 `session_id`
- [ ] 验证 `session_id` 非空

**关键 API**：
```python
from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, SystemMessage

session_id = None
first_response = None

async for message in query(
    prompt="My name is Lynxclaw. Please remember this name.",
    options=ClaudeAgentOptions(
        allowed_tools=["Read"],
        max_turns=2,
    )
):
    if isinstance(message, SystemMessage) and message.subtype == "init":
        session_id = message.data.get("session_id")
        print(f"  Session ID: {session_id}")
    if isinstance(message, ResultMessage):
        first_response = message.result
        print(f"  第一次回复: {first_response[:200]}")
```

### Step 2.2 — 第二次 query：恢复上下文

- [ ] 使用 `resume=session_id` 发起第二次 query
- [ ] Prompt 引用第一次的内容（如 "What is my name?"）
- [ ] 验证回复中包含 "Lynxclaw"

**代码提示**：
```python
resumed_response = None

async for message in query(
    prompt="What is my name? Please tell me the name I told you earlier.",
    options=ClaudeAgentOptions(resume=session_id)
):
    if isinstance(message, ResultMessage):
        resumed_response = message.result
        print(f"  恢复后回复: {resumed_response[:200]}")

context_preserved = (
    resumed_response is not None
    and "lynxclaw" in resumed_response.lower()
)
```

### Step 2.3 — 记录 S1.2 结果

- [ ] 将 `session_id` 获取结果和上下文恢复结果填入 `findings.md` S1.2 表格
- [ ] 如果失败，记录 `resume` 参数的错误信息

**判定标准**（来自 spec）：
| 结果 | 判定 |
|------|------|
| 第二次回复明确引用第一次对话内容 | **PASS** |
| 第二次回复无上下文，或 `resume` 参数报错 | **FAIL** |

---

## S1.3 — MCP 验证

### Step 3.1 — 创建自定义 MCP tool

- [ ] 使用 `@tool` 装饰器定义一个 `write_file` tool
- [ ] tool 接收 `path` 和 `content` 参数，写入文件
- [ ] 使用 `create_sdk_mcp_server()` 创建 MCP server

**关键 API**：
```python
from claude_agent_sdk import (
    tool,
    create_sdk_mcp_server,
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
)

OUTPUT_FILE = "spike/mcp_test_output.txt"

@tool("write_file", "Write content to a file on disk", {
    "path": str,
    "content": str,
})
async def write_file_tool(args):
    path = args["path"]
    content = args["content"]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(content, encoding="utf-8")
    return {
        "content": [
            {"type": "text", "text": f"Successfully wrote {len(content)} chars to {path}"}
        ]
    }

server = create_sdk_mcp_server("file-tools", tools=[write_file_tool])
```

**注意**：`@tool` 装饰器的 schema 参数格式需在执行时验证。如果 `{key: type}` 不生效，参考 SDK 源码调整。

### Step 3.2 — 通过 Agent 触发 MCP tool

- [ ] 使用 `ClaudeSDKClient`（MCP 自定义 tool 需要 client 模式）
- [ ] 发送 prompt 引导 Agent 调用 `write_file` tool
- [ ] 验证文件被写入且内容正确

**代码提示**：
```python
async def verify_mcp() -> dict:
    output_file = Path("spike/mcp_test_output.txt")
    output_file.unlink(missing_ok=True)  # 清理旧文件
    expected_content = "Hello from Lynxclaw MCP test!"

    @tool("write_file", "Write text content to a file on disk", {
        "path": str,
        "content": str,
    })
    async def write_file_tool(args):
        path = args["path"]
        content = args["content"]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")
        return {"content": [{"type": "text", "text": f"Wrote to {path}"}]}

    server = create_sdk_mcp_server("file-tools", tools=[write_file_tool])

    tool_called = False
    try:
        options = ClaudeAgentOptions(
            mcp_servers={"file-tools": server},
            permission_mode="bypassPermissions",
            max_turns=5,
        )
        async with ClaudeSDKClient(options=options) as client:
            await client.query(
                f'Use the write_file tool to write exactly "{expected_content}" '
                f'to the file path "{output_file.as_posix()}"'
            )
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            print(f"  Agent: {block.text[:200]}")
    except Exception as e:
        print(f"  异常: {type(e).__name__}: {e}")

    file_exists = output_file.exists()
    file_content = output_file.read_text(encoding="utf-8") if file_exists else ""
    tool_called = file_exists  # 文件存在即说明 tool 被调用

    status = "pass" if (tool_called and expected_content in file_content) else "fail"
    return {
        "status": status,
        "tool_called": tool_called,
        "file_exists": file_exists,
        "content_correct": expected_content in file_content,
        "actual_content": file_content[:200] if file_content else "(empty)",
    }
```

### Step 3.3 — 记录 S1.3 结果

- [ ] 将 MCP tool 注册、调用、输出结果填入 `findings.md` S1.3 表格
- [ ] 如果失败，记录 stdio 通信日志和异常信息

**判定标准**（来自 spec）：
| 结果 | 判定 |
|------|------|
| 自定义 tool 被 Agent 调用，输出写入文件 | **PASS** |
| tool 未被识别，或 stdio 通信失败 | **FAIL** |

---

## 收尾

### Step 4.1 — 运行完整验证

- [ ] 执行 `python spike/sdk_verify.py` 运行全部三项
- [ ] 也可单独运行：`python spike/sdk_verify.py hooks`
- [ ] 收集所有结果 JSON

### Step 4.2 — 填写 findings.md

- [ ] 填写执行环境（OS、Python、SDK 版本等）
- [ ] 填写 S1.1 / S1.2 / S1.3 各项结果
- [ ] 填写 S1 综合判定（Go/No-Go）
- [ ] 如有失败项，参照 spec 中的失败决策矩阵记录替代方案

### Step 4.3 — 清理

- [ ] 删除临时文件（`spike/mcp_test_output.txt`）
- [ ] 将验证脚本保留在 `spike/` 目录供复验
- [ ] Commit 结果到 `feature/phase0-spike` 分支

---

## 失败决策矩阵（来自 spec，供参考）

| 失败项 | 影响范围 | 替代方案 | 决策 |
|--------|---------|---------|------|
| S1.1 hooks | 安全模型 | 宿主在 IPC 层过滤危险 tool 调用 | 可继续，安全层下移到宿主 |
| S1.2 resume | Resumable 会话 | 宿主自行管理对话历史 | 可继续，但 token 消耗增加 |
| S1.3 MCP | IPC Bridge | Agent 直接写文件到 outbox | 可继续，但失去结构化语义 |
| S1.1 + S1.3 同时失败 | 安全 + IPC 两个核心层 | — | **重新评估是否使用 Python SDK** |

---

## 开发注意事项

1. **API 不确定性**：SDK 的 hook 回调返回值格式（如何 block）、`@tool` schema 格式等，以实际执行为准。遇到偏差时先查 SDK 源码 (`pip show claude-agent-sdk` 找安装路径)，再调整代码。
2. **API Key 消耗**：每个 sub-test 会产生 1-2 次 API 调用，预计总消耗 < $1。
3. **超时**：Agent 可能因 tool 调用循环导致长时间运行，`max_turns` 限制防止失控。
4. **Windows 路径**：MCP 写文件时使用 `Path.as_posix()` 确保路径兼容性。
5. **bypassPermissions**：验证脚本使用此模式避免交互式权限确认，仅限测试环境使用。
