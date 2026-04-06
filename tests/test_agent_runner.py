# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for container/agent-runner/main.py.

Does NOT require a real API key or Docker. SDK imports are mocked.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Inject a minimal mock for claude_agent_sdk before importing main
# ---------------------------------------------------------------------------

def _make_sdk_mock():
    sdk = types.ModuleType("claude_agent_sdk")

    class HookMatcher:
        def __init__(self, matcher: str, hooks: list):
            self.matcher = matcher
            self.hooks = hooks

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    sdk.HookMatcher = HookMatcher
    sdk.ClaudeAgentOptions = ClaudeAgentOptions
    sdk.query = AsyncMock(return_value=iter([]))
    return sdk


sys.modules.setdefault("claude_agent_sdk", _make_sdk_mock())

# Now we can import from main safely
sys.path.insert(0, str(Path(__file__).parent.parent / "container" / "agent-runner"))
import main as runner  # noqa: E402


# ---------------------------------------------------------------------------
# is_dangerous
# ---------------------------------------------------------------------------

class TestIsDangerous:
    def test_blocks_rm_rf(self):
        assert runner.is_dangerous("rm -rf /") is True

    def test_blocks_sudo(self):
        assert runner.is_dangerous("sudo apt-get install something") is True

    def test_blocks_chmod_777(self):
        assert runner.is_dangerous("chmod 777 /etc/passwd") is True

    def test_blocks_fork_bomb(self):
        assert runner.is_dangerous(":(){:|:&};:") is True

    def test_allows_safe_command(self):
        assert runner.is_dangerous("ls -la /workspace") is False

    def test_allows_echo(self):
        assert runner.is_dangerous("echo hello world") is False

    def test_allows_python(self):
        assert runner.is_dangerous("python script.py") is False


# ---------------------------------------------------------------------------
# write_audit
# ---------------------------------------------------------------------------

class TestWriteAudit:
    def test_creates_json_file(self, tmp_path):
        audit_dir = tmp_path / "audit"
        runner.write_audit(
            audit_dir=audit_dir,
            tool_name="Bash",
            tool_input={"command": "ls"},
            tool_use_id="test-id-123",
            blocked=False,
            group="main",
            session_id="sess-abc",
        )
        files = list(audit_dir.glob("*.json"))
        assert len(files) == 1

        data = json.loads(files[0].read_text())
        assert data["tool_name"] == "Bash"
        assert data["tool_use_id"] == "test-id-123"
        assert data["blocked"] is False
        assert data["group"] == "main"
        assert data["session_id"] == "sess-abc"
        assert "timestamp" in data

    def test_blocked_flag_recorded(self, tmp_path):
        audit_dir = tmp_path / "audit"
        runner.write_audit(
            audit_dir=audit_dir,
            tool_name="Bash",
            tool_input={"command": "rm -rf /"},
            tool_use_id="block-id",
            blocked=True,
            group="main",
            session_id="",
        )
        files = list(audit_dir.glob("*.json"))
        data = json.loads(files[0].read_text())
        assert data["blocked"] is True

    def test_creates_audit_dir_if_missing(self, tmp_path):
        audit_dir = tmp_path / "deep" / "nested" / "audit"
        assert not audit_dir.exists()
        runner.write_audit(audit_dir, "Read", {}, "id", False, "g", "s")
        assert audit_dir.exists()

    def test_multiple_entries_separate_files(self, tmp_path):
        audit_dir = tmp_path / "audit"
        for i in range(3):
            runner.write_audit(audit_dir, f"Tool{i}", {}, f"id-{i}", False, "g", "s")
        assert len(list(audit_dir.glob("*.json"))) == 3


# ---------------------------------------------------------------------------
# run_agent wrapper
# ---------------------------------------------------------------------------

class TestRunAgent:
    def test_returns_text_from_sdk(self):
        """run_agent should collect text blocks and return joined string."""

        # Build a fake event with content blocks
        block = MagicMock()
        block.text = "Hello from agent"
        event = MagicMock()
        event.content = [block]
        # No subtype attribute → no session_id capture
        del event.subtype

        async def fake_query(prompt, options):
            yield event

        with patch.dict(sys.modules, {"claude_agent_sdk": _make_sdk_mock()}):
            import importlib
            importlib.reload(runner)
            sdk_mock = sys.modules["claude_agent_sdk"]
            sdk_mock.query = fake_query

            text, session_id, _in, _out = asyncio.run(
                runner.run_agent("test prompt", "", {}, "fake-key")
            )
        assert "Hello from agent" in text
        assert session_id == ""  # no init event → falls back to input session_id

    def test_empty_response_returns_empty_string(self):
        async def fake_query(prompt, options):
            return
            yield  # make it an async generator

        with patch.dict(sys.modules, {"claude_agent_sdk": _make_sdk_mock()}):
            import importlib
            importlib.reload(runner)
            sdk_mock = sys.modules["claude_agent_sdk"]
            sdk_mock.query = fake_query

            text, session_id, _in, _out = asyncio.run(
                runner.run_agent("test", "", {}, "fake-key")
            )
        assert text == ""
        assert session_id == ""

    def test_captures_session_id_from_init_event(self):
        """run_agent captures session_id from SystemMessage(subtype='init')."""

        init_event = MagicMock()
        init_event.subtype = "init"
        init_event.data = {"session_id": "2a9917c8-b197-4952-a186-cdae7bbfdcda"}
        # No content on init event
        del init_event.content

        async def fake_query(prompt, options):
            yield init_event

        with patch.dict(sys.modules, {"claude_agent_sdk": _make_sdk_mock()}):
            import importlib
            importlib.reload(runner)
            sdk_mock = sys.modules["claude_agent_sdk"]
            sdk_mock.query = fake_query

            text, session_id, _in, _out = asyncio.run(
                runner.run_agent("test", "", {}, "fake-key")
            )
        assert session_id == "2a9917c8-b197-4952-a186-cdae7bbfdcda"
        assert text == ""


# ---------------------------------------------------------------------------
# Pre/PostToolUse hook behaviour (integration-style, no real SDK)
# ---------------------------------------------------------------------------

class TestHooks:
    def _make_hooks_and_audit(self, tmp_path):
        """Return (pre_hook, post_hook, audit_dir) using runner internals."""
        audit_dir = tmp_path / "audit"
        group = "main"
        session_id = "sess-1"

        async def pre_tool_use(input: dict, tool_use_id: str, context) -> dict:
            tool_name = input.get("tool_name", "")
            tool_input = input.get("tool_input", {})
            command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
            blocked = tool_name == "Bash" and runner.is_dangerous(command)
            runner.write_audit(audit_dir, tool_name, tool_input, tool_use_id, blocked, group, session_id)
            if blocked:
                return {"decision": "block", "reason": "blocked by policy"}
            return {}

        async def post_tool_use(input: dict, tool_use_id: str, context) -> dict:
            tool_name = input.get("tool_name", "")
            tool_input = input.get("tool_input", {})
            runner.write_audit(audit_dir, tool_name, tool_input, tool_use_id, False, group, session_id)
            return {}

        return pre_tool_use, post_tool_use, audit_dir

    def test_pre_hook_blocks_dangerous_bash(self, tmp_path):
        pre, _, audit_dir = self._make_hooks_and_audit(tmp_path)
        result = asyncio.run(
            pre({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}, "uid-1", None)
        )
        assert result == {"decision": "block", "reason": "blocked by policy"}
        files = list(audit_dir.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["blocked"] is True

    def test_pre_hook_allows_safe_bash(self, tmp_path):
        pre, _, audit_dir = self._make_hooks_and_audit(tmp_path)
        result = asyncio.run(
            pre({"tool_name": "Bash", "tool_input": {"command": "ls /workspace"}}, "uid-2", None)
        )
        assert result == {}
        data = json.loads(list(audit_dir.glob("*.json"))[0].read_text())
        assert data["blocked"] is False

    def test_post_hook_writes_audit(self, tmp_path):
        _, post, audit_dir = self._make_hooks_and_audit(tmp_path)
        asyncio.run(
            post({"tool_name": "Read", "tool_input": {"file_path": "/workspace/group/CLAUDE.md"}}, "uid-3", None)
        )
        files = list(audit_dir.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["tool_name"] == "Read"
        assert data["blocked"] is False


# ---------------------------------------------------------------------------
# Session ID file written by main()
# ---------------------------------------------------------------------------

class TestSessionIdFile:
    def test_session_id_file_written_after_run(self, tmp_path):
        """main() writes session_id.txt to the IPC dir after a successful run."""
        group = "main"
        ipc_dir = tmp_path / group
        ipc_dir.mkdir(parents=True)

        init_event = MagicMock()
        init_event.subtype = "init"
        init_event.data = {"session_id": "test-session-uuid"}
        del init_event.content

        async def fake_query(prompt, options):
            yield init_event

        # Patch ipc_bridge.send_message and stream_chunk so they don't fail
        ipc_bridge_mock = types.ModuleType("ipc_bridge")
        ipc_bridge_mock.send_message = MagicMock()
        ipc_bridge_mock.stream_chunk = MagicMock()

        env = {
            "ANTHROPIC_API_KEY": "fake-key",
            "LYNXCLAW_GROUP": group,
            "LYNXCLAW_SESSION_ID": "",
            "LYNXCLAW_PROMPT": "hello",
            "LYNXCLAW_CHAT_ID": "chat-1",
            "IPC_BASE_DIR": str(tmp_path),
            # Use non-streaming mode so main() calls send_message (simpler test)
            "LYNXCLAW_STREAMING": "0",
        }

        with (
            patch.dict("os.environ", env),
            patch.dict(sys.modules, {"claude_agent_sdk": _make_sdk_mock(), "ipc_bridge": ipc_bridge_mock}),
        ):
            import importlib
            importlib.reload(runner)
            sys.modules["claude_agent_sdk"].query = fake_query

            asyncio.run(runner.main())

        session_file = ipc_dir / "session_id.txt"
        assert session_file.exists(), "session_id.txt was not written"
        assert session_file.read_text().strip() == "test-session-uuid"
