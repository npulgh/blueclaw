"""Lynxclaw Agent Runner — container entry point.

Reads env vars, runs the Claude Agent SDK, and sends the response back to the
host via IPC (ipc_bridge). Hooks intercept dangerous Bash commands and write
audit logs.

run_agent() is the single abstraction point for SDK interaction — future
backend changes only touch this function.

Streaming: when streaming is enabled (LYNXCLAW_STREAMING=1), run_agent()
emits stream_chunk IPC calls as text events arrive from the SDK, with
is_final=True on the last chunk. The final send_message call is omitted
when streaming, since the is_final chunk replaces it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

import structlog

# Must bypass nested session detection when running inside Claude Code
os.environ.pop("CLAUDECODE", None)

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Security policy
# ---------------------------------------------------------------------------

BLOCKED_COMMANDS: list[str] = [
    "rm -rf /",
    "sudo",
    "chmod 777",
    ":(){:|:&};:",
]


def is_dangerous(command: str) -> bool:
    """Return True if the command matches any blocked pattern."""
    for pattern in BLOCKED_COMMANDS:
        if pattern in command:
            return True
    return False


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def write_audit(
    audit_dir: Path,
    tool_name: str,
    tool_input: Any,
    tool_use_id: str,
    blocked: bool,
    group: str,
    session_id: str,
) -> None:
    """Append a JSON audit entry to the audit directory."""
    audit_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": time.time(),
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "input_summary": str(tool_input)[:500],
        "blocked": blocked,
        "group": group,
        "session_id": session_id,
    }
    entry_path = audit_dir / f"{uuid.uuid4()}.json"
    entry_path.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# SDK wrapper — single abstraction point
# ---------------------------------------------------------------------------

async def run_agent(
    prompt: str,
    session_id: str,
    hooks: dict,
    api_key: str,
    *,
    stream_cb: "AsyncIterator[tuple[str, bool]] | None" = None,
    on_chunk: "Any | None" = None,
) -> tuple[str, str]:
    """Run the Claude Agent SDK and return (response_text, new_session_id).

    This is the single point of SDK interaction. Changing the backend
    (e.g., swapping SDK versions or providers) only requires editing here.

    Args:
        prompt: User message to send to the agent.
        session_id: Session ID for resumable conversations (empty = new session).
        hooks: Hook dict passed to ClaudeAgentOptions.
        api_key: Anthropic API key.
        on_chunk: Optional async callable(text: str, is_final: bool) invoked for
                  each text block as it arrives. When provided, the caller is
                  responsible for delivering chunks to the IM platform; the
                  final send_message call is skipped.

    Returns:
        Tuple of (agent's final text response, session_id from this run).
    """
    from claude_agent_sdk import ClaudeAgentOptions, query  # type: ignore

    options = ClaudeAgentOptions(
        api_key=api_key,
        max_turns=30,
        hooks=hooks,
    )
    if session_id:
        options.resume = session_id

    response_parts: list[str] = []
    captured_session_id: str = session_id
    last_text_index: int = -1  # tracks which response_parts index was last emitted

    async for event in query(prompt=prompt, options=options):
        # Capture session_id from SDK init event
        if (
            hasattr(event, "subtype")
            and event.subtype == "init"
            and hasattr(event, "data")
            and isinstance(event.data, dict)
            and "session_id" in event.data
        ):
            captured_session_id = event.data["session_id"]

        # Collect text from assistant messages
        if hasattr(event, "content"):
            for block in event.content:
                if hasattr(block, "text"):
                    response_parts.append(block.text)
                    if on_chunk is not None:
                        # Emit as non-final chunk; is_final is set after the loop
                        await on_chunk(block.text, False)
                        last_text_index = len(response_parts) - 1

    full_text = "".join(response_parts)

    # If streaming, emit a final chunk to signal completion
    if on_chunk is not None and last_text_index >= 0:
        # Send an empty final chunk to close the stream cleanly
        await on_chunk("", True)
    elif on_chunk is not None:
        # No text was produced; still signal is_final so the host cleans up
        await on_chunk("", True)

    return full_text, captured_session_id


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    group = os.environ.get("LYNXCLAW_GROUP", "main")
    session_id = os.environ.get("LYNXCLAW_SESSION_ID", "")
    prompt = os.environ.get("LYNXCLAW_PROMPT", "")
    chat_id = os.environ.get("LYNXCLAW_CHAT_ID", "")
    ipc_base = os.environ.get("IPC_BASE_DIR", "/workspace/ipc")
    streaming_enabled = os.environ.get("LYNXCLAW_STREAMING", "1") == "1"

    audit_dir = Path(ipc_base) / group / "audit"

    if not api_key:
        log.error("missing ANTHROPIC_API_KEY")
        sys.exit(1)
    if not prompt:
        log.error("missing LYNXCLAW_PROMPT")
        sys.exit(1)

    # --- Hook: PreToolUse — block dangerous Bash commands ---
    async def pre_tool_use(input: dict, tool_use_id: str, context: Any) -> dict:
        tool_name = input.get("tool_name", "")
        tool_input = input.get("tool_input", {})
        command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""

        blocked = tool_name == "Bash" and is_dangerous(command)
        write_audit(audit_dir, tool_name, tool_input, tool_use_id, blocked, group, session_id)

        if blocked:
            log.warning("blocked dangerous command", command=command)
            return {"decision": "block", "reason": "blocked by policy"}
        return {}

    # --- Hook: PostToolUse — write audit log for every tool ---
    async def post_tool_use(input: dict, tool_use_id: str, context: Any) -> dict:
        tool_name = input.get("tool_name", "")
        tool_input = input.get("tool_input", {})
        write_audit(audit_dir, tool_name, tool_input, tool_use_id, False, group, session_id)
        return {}

    from claude_agent_sdk import HookMatcher  # type: ignore

    hooks = {
        "PreToolUse": [HookMatcher(matcher="Bash", hooks=[pre_tool_use])],
        "PostToolUse": [HookMatcher(matcher=".*", hooks=[post_tool_use])],
    }

    # Import ipc_bridge from the same runner directory
    runner_dir = Path(__file__).parent
    sys.path.insert(0, str(runner_dir))
    from ipc_bridge import send_message, stream_chunk  # type: ignore

    try:
        log.info("agent starting", group=group, session_id=session_id or "new",
                 streaming=streaming_enabled)

        if streaming_enabled:
            # Streaming path: emit stream_chunk IPC calls as text arrives
            async def on_chunk(text: str, is_final: bool) -> None:
                stream_chunk(
                    group=group,
                    chat_id=chat_id,
                    text=text,
                    is_final=is_final,
                    ipc_base=ipc_base,
                )

            response, new_session_id = await run_agent(
                prompt, session_id, hooks, api_key, on_chunk=on_chunk
            )
            log.info("agent done (streaming)", chars=len(response))
        else:
            # Non-streaming path: collect full response, then send_message
            response, new_session_id = await run_agent(
                prompt, session_id, hooks, api_key
            )
            send_message(group=group, chat_id=chat_id, text=response, ipc_base=ipc_base)
            log.info("agent done", chars=len(response))

        # Write session_id so the host can persist it for the next turn
        if new_session_id:
            session_file = Path(ipc_base) / group / "session_id.txt"
            session_file.parent.mkdir(parents=True, exist_ok=True)
            session_file.write_text(new_session_id, encoding="utf-8")
            log.info("session_id written", session_id=new_session_id)
    except Exception as exc:
        log.error("agent error", error=str(exc))
        error_text = f"[Agent error: {exc}]"
        try:
            send_message(group=group, chat_id=chat_id, text=error_text, ipc_base=ipc_base)
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
