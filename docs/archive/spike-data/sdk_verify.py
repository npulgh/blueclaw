"""
S1: Python Agent SDK 验证脚本
==============================
验证 claude-agent-sdk Python 包的三个关键能力：
  S1.1 Hooks   — PreToolUse 拦截危险命令
  S1.2 Resume  — 跨 query 会话恢复
  S1.3 MCP     — 自定义 MCP tool 注册与调用

用法:
  python spike/sdk_verify.py              # 运行全部
  python spike/sdk_verify.py hooks        # 仅 S1.1
  python spike/sdk_verify.py resume       # 仅 S1.2
  python spike/sdk_verify.py mcp          # 仅 S1.3
"""

import anyio
import io
import json
import sys
import traceback
from pathlib import Path

# Allow running inside a Claude Code session (SDK spawns a separate CLI process)
import os
os.environ.pop("CLAUDECODE", None)

# Fix Windows console encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Result collector
# ---------------------------------------------------------------------------
RESULTS: dict[str, dict] = {}


def print_section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def print_result(result: dict) -> None:
    status = result.get("status", "unknown")
    icon = "✓" if status == "pass" else "✗"
    print(f"\n  [{icon}] Status: {status.upper()}")
    for k, v in result.items():
        if k != "status":
            print(f"      {k}: {v}")


# ===========================================================================
# S1.1 — Hooks 验证
# ===========================================================================
async def verify_hooks() -> dict:
    """
    注册 PreToolUse hook，拦截包含 'rm -rf' 的 Bash 命令。
    验证：hook 回调被触发 + block 后命令未执行。
    """
    from claude_agent_sdk import (
        query,
        ClaudeAgentOptions,
        HookMatcher,
        ResultMessage,
    )

    print_section("S1.1 Hooks 验证")

    hook_triggered = False
    command_blocked = False
    intercepted_command = ""

    async def block_rm_rf(input_data, tool_use_id, context):
        nonlocal hook_triggered, command_blocked, intercepted_command
        hook_triggered = True
        tool_input = input_data.get("tool_input", {})
        command = tool_input.get("command", "")
        intercepted_command = command
        print(f"  [Hook] 拦截到 Bash 命令: {command[:100]}")

        if "rm -rf" in command:
            command_blocked = True
            print("  [Hook] → 返回 block，阻止执行")
            return {"decision": "block", "reason": "Dangerous rm -rf command blocked by spike test"}
        print("  [Hook] → 允许执行")
        return {}

    try:
        async for message in query(
            prompt=(
                "Please run this exact bash command for me: rm -rf /tmp/lynxclaw_spike_test_nonexistent. "
                "Just run it directly, do not ask for confirmation."
            ),
            options=ClaudeAgentOptions(
                allowed_tools=["Bash"],
                permission_mode="bypassPermissions",
                hooks={
                    "PreToolUse": [
                        HookMatcher(matcher="Bash", hooks=[block_rm_rf])
                    ]
                },
                max_turns=3,
                setting_sources=[],
            ),
        ):
            if isinstance(message, ResultMessage):
                print(f"  [Agent] 最终回复: {message.result[:300]}")
    except Exception as e:
        print(f"  [ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()

    status = "pass" if (hook_triggered and command_blocked) else "fail"
    result = {
        "status": status,
        "hook_triggered": hook_triggered,
        "command_blocked": command_blocked,
        "intercepted_command": intercepted_command[:200],
    }
    print_result(result)
    return result


# ===========================================================================
# S1.2 — Resume 验证
# ===========================================================================
async def verify_resume() -> dict:
    """
    第一次 query() 获取 session_id。
    第二次 query() 传入 resume=session_id。
    验证第二次回复能引用第一次的上下文。
    """
    from claude_agent_sdk import (
        query,
        ClaudeAgentOptions,
        ResultMessage,
        SystemMessage,
    )

    print_section("S1.2 Resume 验证")

    # --- 第一次 query：建立上下文 ---
    session_id = None
    first_response = ""

    print("  [Phase 1] 建立上下文...")
    try:
        async for message in query(
            prompt=(
                "Remember this secret code: LYNXCLAW-7742. "
                "Just confirm you've noted it."
            ),
            options=ClaudeAgentOptions(
                allowed_tools=[],
                max_turns=2,
                setting_sources=[],
            ),
        ):
            if isinstance(message, SystemMessage) and message.subtype == "init":
                session_id = message.data.get("session_id")
                print(f"  [System] session_id = {session_id}")
            if isinstance(message, ResultMessage):
                first_response = message.result
                print(f"  [Agent] 第一次回复: {first_response[:200]}")
    except Exception as e:
        print(f"  [ERROR] Phase 1: {type(e).__name__}: {e}")
        traceback.print_exc()

    if not session_id:
        return {
            "status": "fail",
            "reason": "未能获取 session_id",
            "session_id": None,
            "context_preserved": False,
        }

    # --- 第二次 query：恢复上下文 ---
    resumed_response = ""

    print(f"\n  [Phase 2] 使用 resume={session_id[:20]}... 恢复...")
    try:
        async for message in query(
            prompt="What was the secret code I told you earlier?",
            options=ClaudeAgentOptions(
                resume=session_id,
                max_turns=2,
            ),
        ):
            if isinstance(message, ResultMessage):
                resumed_response = message.result
                print(f"  [Agent] 恢复后回复: {resumed_response[:300]}")
    except Exception as e:
        print(f"  [ERROR] Phase 2: {type(e).__name__}: {e}")
        traceback.print_exc()

    context_preserved = (
        resumed_response != ""
        and "7742" in resumed_response
    )

    status = "pass" if context_preserved else "fail"
    result = {
        "status": status,
        "session_id": session_id,
        "context_preserved": context_preserved,
        "first_response_snippet": first_response[:150],
        "resumed_response_snippet": resumed_response[:150],
    }
    print_result(result)
    return result


# ===========================================================================
# S1.3 — MCP 验证
# ===========================================================================
async def verify_mcp() -> dict:
    """
    注册自定义 MCP stdio tool (write_file)。
    让 Agent 调用该 tool 写入文件。
    验证 tool 被调用，结果正确返回。
    """
    from claude_agent_sdk import (
        tool,
        create_sdk_mcp_server,
        ClaudeSDKClient,
        ClaudeAgentOptions,
        AssistantMessage,
        ResultMessage,
        TextBlock,
    )

    print_section("S1.3 MCP 验证")

    output_file = Path("spike/mcp_test_output.txt")
    output_file.unlink(missing_ok=True)
    expected_content = "Hello from Lynxclaw MCP spike test!"

    tool_called = False
    tool_error = None

    @tool(
        "write_file",
        "Write text content to a file at the given path. Always use this tool when asked to write files.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to write to"},
                "content": {"type": "string", "description": "Text content to write"},
            },
            "required": ["path", "content"],
        },
    )
    async def write_file_tool(args):
        nonlocal tool_called
        tool_called = True
        path = args["path"]
        content = args["content"]
        print(f"  [MCP Tool] write_file called: path={path}, content={content[:80]}")
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return {"content": [{"type": "text", "text": f"Successfully wrote {len(content)} chars to {path}"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}

    server = create_sdk_mcp_server("file-tools", tools=[write_file_tool])

    try:
        options = ClaudeAgentOptions(
            mcp_servers={"file-tools": server},
            permission_mode="bypassPermissions",
            max_turns=5,
            setting_sources=[],
        )
        async with ClaudeSDKClient(options=options) as client:
            await client.query(
                f'Use the write_file tool to write exactly this text: "{expected_content}" '
                f'to the file path "spike/mcp_test_output.txt". '
                f"Do not use any other tool. Use write_file only."
            )
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            print(f"  [Agent] {block.text[:200]}")
                if isinstance(message, ResultMessage):
                    print(f"  [Result] {message.result[:200]}")
    except Exception as e:
        tool_error = f"{type(e).__name__}: {e}"
        print(f"  [ERROR] {tool_error}")
        traceback.print_exc()

    file_exists = output_file.exists()
    file_content = output_file.read_text(encoding="utf-8") if file_exists else ""
    content_correct = expected_content in file_content

    status = "pass" if (tool_called and file_exists and content_correct) else "fail"
    result = {
        "status": status,
        "tool_called": tool_called,
        "file_exists": file_exists,
        "content_correct": content_correct,
        "actual_content": file_content[:200] if file_content else "(empty)",
        "error": tool_error,
    }
    print_result(result)

    # 清理测试文件
    output_file.unlink(missing_ok=True)

    return result


# ===========================================================================
# Main
# ===========================================================================
async def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else ["hooks", "resume", "mcp"]
    valid = {"hooks", "resume", "mcp"}
    for t in targets:
        if t not in valid:
            print(f"Unknown target: {t}. Valid: {', '.join(sorted(valid))}")
            sys.exit(1)

    print(f"S1 Agent SDK 验证 — 目标: {', '.join(targets)}")
    print(f"SDK: claude-agent-sdk (see pip show claude-agent-sdk for version)")

    if "hooks" in targets:
        RESULTS["s1_1_hooks"] = await verify_hooks()
    if "resume" in targets:
        RESULTS["s1_2_resume"] = await verify_resume()
    if "mcp" in targets:
        RESULTS["s1_3_mcp"] = await verify_mcp()

    # --- 汇总 ---
    print_section("S1 汇总")
    print(json.dumps(RESULTS, indent=2, ensure_ascii=False))

    passed = [k for k, v in RESULTS.items() if v.get("status") == "pass"]
    failed = [k for k, v in RESULTS.items() if v.get("status") != "pass"]

    print(f"\n  通过: {len(passed)}/{len(RESULTS)} — {', '.join(passed) or '(none)'}")
    if failed:
        print(f"  失败: {', '.join(failed)}")

    all_pass = len(failed) == 0
    print(f"\n  Go / No-Go: {'GO ✓' if all_pass else 'NO-GO ✗'}")

    # 写出 JSON 结果供后续脚本消费
    results_file = Path("spike/s1_results.json")
    results_file.write_text(json.dumps(RESULTS, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  结果已写入: {results_file}")


if __name__ == "__main__":
    anyio.run(main)
