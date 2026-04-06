# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Lynxclaw IPC Bridge — container-side file writer.

Provides simple utilities for the Agent Runner to communicate with the host
via filesystem JSON-RPC 2.0 (ADR-001).

Each tool call writes a single .json file to the outbox directory using
atomic write (write-tmp + rename) to prevent host from reading partial files.

The atomic write pattern triggers watchdog's on_moved (not on_created) on the
host side — this is why the host MUST implement both handlers.

This module is intentionally minimal: just file writing utilities.
MCP server integration is handled in T1.9.

Usage::

    from ipc_bridge import send_message, stream_chunk

    send_message(group="main", chat_id="-1001234567890", text="Hello!")
    stream_chunk(group="main", chat_id="-1001234567890", text="Partial...", is_final=False)
    stream_chunk(group="main", chat_id="-1001234567890", text=" done.", is_final=True)
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Optional


# Default IPC base directory (matches host-side config)
# Can be overridden via IPC_BASE_DIR environment variable.
_DEFAULT_IPC_BASE = "/workspace/ipc"


def _get_outbox(group: str, ipc_base: Optional[str] = None) -> Path:
    """Return the outbox Path for the given group, creating it if needed."""
    base = Path(ipc_base or os.environ.get("IPC_BASE_DIR", _DEFAULT_IPC_BASE))
    outbox = base / group / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    return outbox


def _write_atomic(outbox: Path, payload: dict) -> Path:
    """Write payload as JSON to outbox using atomic write (tmp + rename).

    Returns:
        Path of the final file.
    """
    file_id = str(uuid.uuid4())
    final_path = outbox / f"{file_id}.json"
    tmp_path = outbox / f"{file_id}.tmp"

    data = json.dumps(payload, ensure_ascii=False)
    tmp_path.write_text(data, encoding="utf-8")
    # Atomic rename — triggers on_moved on host watchdog
    tmp_path.rename(final_path)
    return final_path


def send_message(
    group: str,
    chat_id: str,
    text: str,
    ipc_base: Optional[str] = None,
) -> str:
    """Send a complete message to the host via IPC.

    Args:
        group: Group name (must match a registered group on the host).
        chat_id: Platform-specific chat/conversation identifier.
        text: Message text to send.
        ipc_base: Override for the IPC base directory (default: /workspace/ipc).

    Returns:
        UUID string identifying this IPC request.
    """
    rpc_id = str(uuid.uuid4())
    payload = {
        "jsonrpc": "2.0",
        "method": "send_message",
        "params": {
            "group": group,
            "chat_id": chat_id,
            "text": text,
        },
        "id": rpc_id,
    }
    outbox = _get_outbox(group, ipc_base)
    _write_atomic(outbox, payload)
    return rpc_id


def stream_chunk(
    group: str,
    chat_id: str,
    text: str,
    is_final: bool = False,
    ipc_base: Optional[str] = None,
) -> str:
    """Send a streaming text chunk to the host via IPC.

    Args:
        group: Group name.
        chat_id: Platform-specific chat/conversation identifier.
        text: Partial text chunk from the streaming response.
        is_final: True if this is the last chunk of the stream.
        ipc_base: Override for the IPC base directory.

    Returns:
        UUID string identifying this IPC request.
    """
    rpc_id = str(uuid.uuid4())
    payload = {
        "jsonrpc": "2.0",
        "method": "stream_chunk",
        "params": {
            "group": group,
            "chat_id": chat_id,
            "text": text,
            "is_final": is_final,
        },
        "id": rpc_id,
    }
    outbox = _get_outbox(group, ipc_base)
    _write_atomic(outbox, payload)
    return rpc_id


def schedule_task(
    group: str,
    task_id: str,
    schedule: str,
    prompt: str,
    task_type: str = "cron",
    ipc_base: Optional[str] = None,
) -> str:
    """Request the host to create a new scheduled task.

    Args:
        group: Group name owning the task.
        task_id: Unique identifier for the task (e.g. UUID).
        schedule: Cron expression (e.g. '*/5 * * * *').
        prompt: Prompt text to execute when the task fires.
        task_type: Task type string (default: 'cron').
        ipc_base: Override for the IPC base directory.

    Returns:
        UUID string identifying this IPC request.
    """
    rpc_id = str(uuid.uuid4())
    payload = {
        "jsonrpc": "2.0",
        "method": "schedule_task",
        "params": {
            "group": group,
            "task_id": task_id,
            "schedule": schedule,
            "prompt": prompt,
            "type": task_type,
        },
        "id": rpc_id,
    }
    outbox = _get_outbox(group, ipc_base)
    _write_atomic(outbox, payload)
    return rpc_id


def list_tasks(
    group: str,
    ipc_base: Optional[str] = None,
) -> str:
    """Request the host to list all tasks for a group.

    The host writes the result as a JSON-RPC response to the group inbox.

    Args:
        group: Group name.
        ipc_base: Override for the IPC base directory.

    Returns:
        UUID RPC ID that will be used as the inbox result filename.
    """
    rpc_id = str(uuid.uuid4())
    payload = {
        "jsonrpc": "2.0",
        "method": "list_tasks",
        "params": {
            "group": group,
        },
        "id": rpc_id,
    }
    outbox = _get_outbox(group, ipc_base)
    _write_atomic(outbox, payload)
    return rpc_id


def cancel_task(
    group: str,
    task_id: str,
    ipc_base: Optional[str] = None,
) -> str:
    """Request the host to cancel (deactivate) a scheduled task.

    Args:
        group: Group name owning the task.
        task_id: ID of the task to cancel.
        ipc_base: Override for the IPC base directory.

    Returns:
        UUID string identifying this IPC request.
    """
    rpc_id = str(uuid.uuid4())
    payload = {
        "jsonrpc": "2.0",
        "method": "cancel_task",
        "params": {
            "group": group,
            "task_id": task_id,
        },
        "id": rpc_id,
    }
    outbox = _get_outbox(group, ipc_base)
    _write_atomic(outbox, payload)
    return rpc_id


def delegate_task(
    group: str,
    from_group: str,
    to_group: str,
    prompt: str,
    context: str = "",
    ipc_base: Optional[str] = None,
) -> str:
    """Request the host to delegate a task to another group's agent.

    The host SwarmCoordinator will spawn a container in *to_group* with the
    given prompt.  Results are delivered via that group's normal IPC outbox.

    Args:
        group: The requesting group name (must match the container's own group).
        from_group: Logical source group (usually same as *group*).
        to_group: Target group to delegate to.
        prompt: Prompt to pass to the delegated agent.
        context: Optional extra context prepended to the prompt.
        ipc_base: Override for the IPC base directory.

    Returns:
        UUID string identifying this IPC request.
    """
    rpc_id = str(uuid.uuid4())
    payload = {
        "jsonrpc": "2.0",
        "method": "delegate_task",
        "params": {
            "group": group,
            "from_group": from_group,
            "to_group": to_group,
            "prompt": prompt,
            "context": context,
        },
        "id": rpc_id,
    }
    outbox = _get_outbox(group, ipc_base)
    _write_atomic(outbox, payload)
    return rpc_id


def read_context(
    group: str,
    target_group: str,
    ipc_base: Optional[str] = None,
) -> str:
    """Request the host to read another group's CLAUDE.md context.

    The host writes the result as a JSON-RPC response to the group inbox.

    Args:
        group: The requesting group name.
        target_group: The group whose CLAUDE.md to read.
        ipc_base: Override for the IPC base directory.

    Returns:
        UUID RPC ID that will be used as the inbox result filename.
    """
    rpc_id = str(uuid.uuid4())
    payload = {
        "jsonrpc": "2.0",
        "method": "read_context",
        "params": {
            "group": group,
            "target_group": target_group,
        },
        "id": rpc_id,
    }
    outbox = _get_outbox(group, ipc_base)
    _write_atomic(outbox, payload)
    return rpc_id
