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
) -> tuple[str, str, int, int]:
    """Run the Claude Agent SDK and return (response_text, new_session_id, input_tokens, output_tokens).

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
        Tuple of (agent's final text response, session_id, input_tokens, output_tokens).
    """
    from claude_agent_sdk import ClaudeAgentOptions, query  # type: ignore

    options = ClaudeAgentOptions(
        max_turns=30,
        hooks=hooks,
    )
    if session_id:
        options.resume = session_id

    response_parts: list[str] = []
    captured_session_id: str = session_id
    last_text_index: int = -1  # tracks which response_parts index was last emitted
    total_input_tokens: int = 0
    total_output_tokens: int = 0

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

        # Collect token usage from usage events
        # TaskProgressMessage / TaskNotificationMessage: usage is TaskUsage TypedDict
        #   with "total_tokens" (no input/output breakdown)
        # ResultMessage: usage is dict[str, Any], may have detailed breakdown
        if hasattr(event, "usage") and event.usage is not None:
            usage = event.usage
            if isinstance(usage, dict):
                # ResultMessage.usage — prefer input/output breakdown if available
                if "input_tokens" in usage:
                    total_input_tokens = int(usage["input_tokens"])
                if "output_tokens" in usage:
                    total_output_tokens = int(usage["output_tokens"])
                # Fallback: split total_tokens evenly as rough estimate
                if "total_tokens" in usage and total_input_tokens == 0 and total_output_tokens == 0:
                    total_input_tokens = int(usage["total_tokens"])

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

    return full_text, captured_session_id, total_input_tokens, total_output_tokens


async def _run_with_resume_fallback(
    prompt: str,
    session_id: str,
    hooks: dict,
    api_key: str,
    *,
    on_chunk: "Any | None" = None,
) -> tuple[str, str, int, int]:
    """Try run_agent with resume; on failure, retry as new session.

    Third-party API mirrors (Kimi, aicodemirror, etc.) may not support the
    SDK's resume feature. When resume fails, we fall back to a fresh session
    so the user still gets a response instead of an error.
    """
    if not session_id:
        return await run_agent(prompt, "", hooks, api_key, on_chunk=on_chunk)
    try:
        return await run_agent(prompt, session_id, hooks, api_key, on_chunk=on_chunk)
    except Exception as exc:
        log.warning("resume failed, retrying as new session", session_id=session_id, error=str(exc))
        return await run_agent(prompt, "", hooks, api_key, on_chunk=on_chunk)


# ---------------------------------------------------------------------------
# Skills loader (ADR-007)
# ---------------------------------------------------------------------------

def load_skills(global_skills_dir: str, group_skills_dir: str) -> list[dict]:
    """Load SKILL.md files from global and group skills directories.

    Returns list of {"name": str, "description": str, "content": str}.
    Group skills override global skills with the same directory name.
    """
    skills: dict[str, dict] = {}

    for skills_dir in (global_skills_dir, group_skills_dir):
        if not skills_dir:
            continue
        base = Path(skills_dir)
        if not base.is_dir():
            continue
        for skill_dir in sorted(base.iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                continue
            raw = skill_file.read_text(encoding="utf-8").strip()
            name = skill_dir.name
            description = ""
            content = raw

            # Parse optional YAML frontmatter
            if raw.startswith("---"):
                parts = raw.split("---", 2)
                if len(parts) >= 3:
                    for line in parts[1].strip().splitlines():
                        if line.startswith("name:"):
                            name = line.split(":", 1)[1].strip()
                        elif line.startswith("description:"):
                            description = line.split(":", 1)[1].strip()
                    content = parts[2].strip()

            skills[skill_dir.name] = {
                "name": name,
                "description": description,
                "content": content,
            }

    return list(skills.values())


def _inject_skills(prompt: str, group: str) -> str:
    """Load skills and prepend them to the prompt if any are found."""
    global_skills_dir = "/workspace/global/skills"
    group_skills_dir = f"/workspace/group/skills"
    skills = load_skills(global_skills_dir, group_skills_dir)
    if not skills:
        return prompt

    skills_section = "\n# Active Skills\n"
    for s in skills:
        skills_section += f"\n## {s['name']}\n{s['content']}\n"
    log.info("skills.loaded", count=len(skills), names=[s["name"] for s in skills])
    return skills_section + "\n---\n\n" + prompt


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

    # --- Start local proxy if upstream doesn't support /v1/models/{id} ---
    # Claude Code CLI validates the model via GET /v1/models/{id}?beta=true before
    # sending any prompt. Third-party providers (e.g. Kimi) return 404 for this
    # endpoint, causing the CLI to abort. The proxy intercepts that call and returns
    # a fake 200, then forwards all other requests to the real upstream.
    upstream_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    if upstream_url and "anthropic.com" not in upstream_url and "127.0.0.1" not in upstream_url:
        from api_proxy import start_proxy
        proxy_port = 9099
        start_proxy(upstream=upstream_url, port=proxy_port)
        os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{proxy_port}"
        log.info("api_proxy.started", upstream=upstream_url, port=proxy_port)

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

    # --- Load Skills (ADR-007) ---
    prompt = _inject_skills(prompt, group)

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

            response, new_session_id, input_tokens, output_tokens = await _run_with_resume_fallback(
                prompt, session_id, hooks, api_key, on_chunk=on_chunk
            )
            log.info("agent done (streaming)", chars=len(response))
            # If the API returned an empty response (no chunks emitted), write a
            # fallback send_message so the host IPC watcher always receives at
            # least one outbox file and doesn't wait forever.
            if not response:
                send_message(
                    group=group,
                    chat_id=chat_id,
                    text="[Agent returned empty response]",
                    ipc_base=ipc_base,
                )
                log.warning("agent empty response, wrote fallback IPC")
        else:
            # Non-streaming path: collect full response, then send_message
            response, new_session_id, input_tokens, output_tokens = await _run_with_resume_fallback(
                prompt, session_id, hooks, api_key
            )
            send_message(group=group, chat_id=chat_id, text=response, ipc_base=ipc_base)
            log.info("agent done", chars=len(response))

        # Write token usage so the host can record it to the DB
        token_usage_file = Path(ipc_base) / group / "token_usage.json"
        token_usage_file.parent.mkdir(parents=True, exist_ok=True)
        token_usage_file.write_text(
            json.dumps({"input_tokens": input_tokens, "output_tokens": output_tokens}),
            encoding="utf-8",
        )
        log.info("token_usage written", input=input_tokens, output=output_tokens)

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


async def _write_heartbeat(heartbeat_path: Path) -> None:
    """Write a heartbeat.json file with the current timestamp."""
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"timestamp": time.time()}, ensure_ascii=False)
    tmp = heartbeat_path.with_suffix(".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.rename(heartbeat_path)


async def persistent_loop(group: str, ipc_base: str) -> None:
    """Run a persistent message loop: poll inbox, process prompts, heartbeat.

    Reads prompt files from {ipc_base}/{group}/inbox/, processes each with
    run_agent(), writes responses to outbox via ipc_bridge, and writes a
    heartbeat.json every 30 seconds so the host can detect liveness.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.error("missing ANTHROPIC_API_KEY")
        sys.exit(1)

    inbox_dir = Path(ipc_base) / group / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    heartbeat_path = Path(ipc_base) / group / "heartbeat.json"
    audit_dir = Path(ipc_base) / group / "audit"

    runner_dir = Path(__file__).parent
    sys.path.insert(0, str(runner_dir))
    from ipc_bridge import send_message, stream_chunk  # type: ignore

    from claude_agent_sdk import HookMatcher  # type: ignore

    session_id: str = os.environ.get("LYNXCLAW_SESSION_ID", "")
    streaming_enabled = os.environ.get("LYNXCLAW_STREAMING", "1") == "1"

    last_heartbeat = 0.0

    log.info("persistent_loop.started", group=group, ipc_base=ipc_base)

    while True:
        # Write heartbeat every 30 seconds
        now = time.time()
        if now - last_heartbeat >= 30:
            await _write_heartbeat(heartbeat_path)
            last_heartbeat = time.time()
            log.debug("persistent_loop.heartbeat", group=group)

        # Scan inbox for prompt files
        prompt_files = sorted(inbox_dir.glob("*.json"))
        if not prompt_files:
            await asyncio.sleep(0.5)
            continue

        for prompt_file in prompt_files:
            try:
                raw = prompt_file.read_text(encoding="utf-8")
                payload = json.loads(raw)
            except Exception as exc:
                log.warning("persistent_loop.bad_inbox_file", file=str(prompt_file), error=str(exc))
                try:
                    prompt_file.unlink(missing_ok=True)
                except Exception:
                    pass
                continue

            # Consume the file immediately to avoid double-processing
            try:
                prompt_file.unlink(missing_ok=True)
            except Exception:
                pass

            params = payload.get("params", {})
            prompt = params.get("prompt", "")
            chat_id = params.get("chat_id", "")

            if not prompt:
                log.warning("persistent_loop.empty_prompt", file=str(prompt_file))
                continue

            log.info("persistent_loop.processing", group=group, chat_id=chat_id)

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

            async def post_tool_use(input: dict, tool_use_id: str, context: Any) -> dict:
                tool_name = input.get("tool_name", "")
                tool_input = input.get("tool_input", {})
                write_audit(audit_dir, tool_name, tool_input, tool_use_id, False, group, session_id)
                return {}

            hooks = {
                "PreToolUse": [HookMatcher(matcher="Bash", hooks=[pre_tool_use])],
                "PostToolUse": [HookMatcher(matcher=".*", hooks=[post_tool_use])],
            }

            try:
                if streaming_enabled:
                    async def on_chunk(text: str, is_final: bool) -> None:
                        stream_chunk(
                            group=group,
                            chat_id=chat_id,
                            text=text,
                            is_final=is_final,
                            ipc_base=ipc_base,
                        )
                    response, new_session_id, in_tok, out_tok = await _run_with_resume_fallback(
                        prompt, session_id, hooks, api_key, on_chunk=on_chunk
                    )
                    # Fallback for empty streaming response (same as ephemeral path)
                    if not response:
                        send_message(
                            group=group,
                            chat_id=chat_id,
                            text="[Agent returned empty response]",
                            ipc_base=ipc_base,
                        )
                        log.warning("agent empty response, wrote fallback IPC")
                else:
                    response, new_session_id, in_tok, out_tok = await _run_with_resume_fallback(
                        prompt, session_id, hooks, api_key
                    )
                    send_message(group=group, chat_id=chat_id, text=response, ipc_base=ipc_base)

                if new_session_id:
                    session_id = new_session_id

                # Write token usage for host to record
                token_usage_file = Path(ipc_base) / group / "token_usage.json"
                token_usage_file.parent.mkdir(parents=True, exist_ok=True)
                token_usage_file.write_text(
                    json.dumps({"input_tokens": in_tok, "output_tokens": out_tok}),
                    encoding="utf-8",
                )
                log.info("persistent_loop.done", group=group, in_tok=in_tok, out_tok=out_tok)

            except Exception as exc:
                log.error("persistent_loop.agent_error", group=group, error=str(exc))
                try:
                    send_message(
                        group=group,
                        chat_id=chat_id,
                        text=f"[Agent error: {exc}]",
                        ipc_base=ipc_base,
                    )
                except Exception:
                    pass


if __name__ == "__main__":
    mode = os.environ.get("LYNXCLAW_MODE", "")
    if mode == "persistent":
        _group = os.environ.get("LYNXCLAW_GROUP", "main")
        _ipc_base = os.environ.get("IPC_BASE_DIR", "/workspace/ipc")
        asyncio.run(persistent_loop(_group, _ipc_base))
    else:
        asyncio.run(main())
