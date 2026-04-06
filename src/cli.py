# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lynxclaw Management CLI.

Entry point: python -m src.cli <command> [options]

Commands:
  status                        Health / DB summary
  groups list                   List all groups
  groups show <name>            Show one group + session info
  tasks list [--group <name>]   List tasks
  tasks create --group --schedule --prompt
  tasks cancel <task-id>
  usage [--group <name>]        Token usage summary
  audit [--group <name>] [--limit N]
  messages [--group <name>] [--status <s>] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _ts(unix: Optional[int]) -> str:
    """Format a Unix timestamp as UTC string, or '-'."""
    if unix is None:
        return "-"
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _col(value: Any, width: int) -> str:
    s = str(value) if value is not None else "-"
    if len(s) > width:
        s = s[: width - 1] + "…"
    return s.ljust(width)


def _header(*cols: tuple[str, int]) -> str:
    line = "  ".join(_col(name, w) for name, w in cols)
    sep = "  ".join("-" * w for _, w in cols)
    return f"{line}\n{sep}"


def _row(*vals_widths: tuple[Any, int]) -> str:
    return "  ".join(_col(v, w) for v, w in vals_widths)


# ---------------------------------------------------------------------------
# Config loading (best-effort; falls back to default db_path)
# ---------------------------------------------------------------------------

def _load_db_path(config_path: Optional[str]) -> str:
    try:
        from src.config import load_config
        cfg = load_config(config_path or "lynxclaw.config.yaml")
        return cfg.db_path
    except Exception:
        return "data/store/messages.db"


# ---------------------------------------------------------------------------
# Async subcommand handlers
# ---------------------------------------------------------------------------

async def cmd_status(args, db) -> int:
    groups = await db.get_all_groups()
    sessions = await db.get_all_sessions()
    tasks = await db.get_all_tasks()
    active_tasks = [t for t in tasks if t["status"] == "active"]
    print("Lynxclaw — DB Status")
    print(f"  DB path   : {db._db_path}")
    print(f"  Groups    : {len(groups)}")
    print(f"  Sessions  : {len(sessions)}")
    print(f"  Tasks     : {len(tasks)} total, {len(active_tasks)} active")
    return 0


async def cmd_groups_list(args, db) -> int:
    groups = await db.get_all_groups()
    if not groups:
        print("No groups found.")
        return 0
    print(_header(("NAME", 20), ("CHANNEL", 10), ("CHAT_ID", 20), ("MAIN", 5), ("TRIGGER", 10)))
    for g in groups:
        print(_row(
            (g["name"], 20), (g["channel"], 10), (g["chat_id"], 20),
            ("yes" if g["is_main"] else "no", 5), (g["trigger"], 10),
        ))
    return 0


async def cmd_groups_show(args, db) -> int:
    group = await db.get_group(args.name)
    if group is None:
        print(f"Group '{args.name}' not found.", file=sys.stderr)
        return 1
    session_id = await db.get_session(group_name=args.name)
    usage = await db.get_token_usage(group_name=args.name)
    print(f"Group: {group['name']}")
    for k, v in group.items():
        if k != "name":
            print(f"  {k:<14}: {v}")
    print(f"  {'session_id':<14}: {session_id or '-'}")
    print(f"  {'input_tokens':<14}: {usage['input_tokens']}")
    print(f"  {'output_tokens':<14}: {usage['output_tokens']}")
    return 0


async def cmd_tasks_list(args, db) -> int:
    tasks = await db.get_all_tasks(group_name=getattr(args, "group", None))
    if not tasks:
        print("No tasks found.")
        return 0
    print(_header(
        ("ID", 12), ("GROUP", 16), ("STATUS", 10),
        ("SCHEDULE", 16), ("NEXT_RUN", 20), ("PROMPT", 30),
    ))
    for t in tasks:
        print(_row(
            (t["id"][:12], 12), (t["group_name"], 16), (t["status"], 10),
            (t["schedule"], 16), (_ts(t.get("next_run")), 20),
            (t["prompt"][:30], 30),
        ))
    return 0


async def cmd_tasks_create(args, db) -> int:
    task_id = str(uuid.uuid4())[:8]
    await db.create_task(
        id=task_id,
        group_name=args.group,
        type="scheduled",
        schedule=args.schedule,
        prompt=args.prompt,
    )
    print(f"Task created: {task_id}")
    return 0


async def cmd_tasks_cancel(args, db) -> int:
    tasks = await db.get_all_tasks()
    ids = [t["id"] for t in tasks]
    if args.task_id not in ids:
        print(f"Task '{args.task_id}' not found.", file=sys.stderr)
        return 1
    await db.cancel_task(task_id=args.task_id)
    print(f"Task '{args.task_id}' cancelled.")
    return 0


async def cmd_usage(args, db) -> int:
    group_name = getattr(args, "group", None)
    if group_name:
        usage = await db.get_token_usage(group_name=group_name)
        total = usage["input_tokens"] + usage["output_tokens"]
        print(_header(("GROUP", 20), ("INPUT", 10), ("OUTPUT", 10), ("TOTAL", 10)))
        print(_row((group_name, 20), (usage["input_tokens"], 10),
                   (usage["output_tokens"], 10), (total, 10)))
    else:
        rows = await db.get_token_usage_all_groups()
        if not rows:
            print("No token usage recorded.")
            return 0
        print(_header(("GROUP", 20), ("INPUT", 10), ("OUTPUT", 10), ("TOTAL", 10)))
        for r in rows:
            total = r["input_tokens"] + r["output_tokens"]
            print(_row((r["group_name"], 20), (r["input_tokens"], 10),
                       (r["output_tokens"], 10), (total, 10)))
    return 0


async def cmd_audit(args, db) -> int:
    limit = getattr(args, "limit", 50)
    group_name = getattr(args, "group", None)
    entries = await db.get_audit_log(group_name=group_name, limit=limit)
    if not entries:
        print("No audit log entries.")
        return 0
    print(_header(
        ("ID", 6), ("GROUP", 16), ("TOOL", 20),
        ("BLOCKED", 7), ("CREATED", 20),
    ))
    for e in entries:
        print(_row(
            (e["id"], 6), (e["group_name"], 16), (e["tool_name"], 20),
            ("yes" if e["blocked"] else "no", 7), (_ts(e["created_at"]), 20),
        ))
    return 0


async def cmd_messages(args, db) -> int:
    limit = getattr(args, "limit", 20)
    group_name = getattr(args, "group", None)
    status = getattr(args, "status", None)
    msgs = await db.get_messages(group_name=group_name, status=status, limit=limit)
    if not msgs:
        print("No messages found.")
        return 0
    print(_header(
        ("ID", 6), ("GROUP", 16), ("CHANNEL", 10),
        ("DIR", 8), ("STATUS", 10), ("CREATED", 20), ("CONTENT", 30),
    ))
    for m in msgs:
        print(_row(
            (m["id"], 6), (m.get("group_name") or "-", 16), (m["channel"], 10),
            (m["direction"], 8), (m["status"], 10),
            (_ts(m["created_at"]), 20), (m["content"], 30),
        ))
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.cli",
        description="Lynxclaw management CLI",
    )
    parser.add_argument(
        "--config", metavar="PATH",
        help="Path to lynxclaw.config.yaml (default: lynxclaw.config.yaml)",
    )
    parser.add_argument(
        "--db", metavar="PATH",
        help="Override db_path (skips config loading)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # status
    sub.add_parser("status", help="Show DB health summary")

    # groups
    grp = sub.add_parser("groups", help="Group management")
    grp_sub = grp.add_subparsers(dest="groups_cmd", required=True)
    grp_sub.add_parser("list", help="List all groups")
    show_p = grp_sub.add_parser("show", help="Show group details")
    show_p.add_argument("name", help="Group name")

    # tasks
    tsk = sub.add_parser("tasks", help="Task management")
    tsk_sub = tsk.add_subparsers(dest="tasks_cmd", required=True)
    tsk_list = tsk_sub.add_parser("list", help="List tasks")
    tsk_list.add_argument("--group", metavar="NAME", help="Filter by group")
    tsk_create = tsk_sub.add_parser("create", help="Create a scheduled task")
    tsk_create.add_argument("--group", required=True, metavar="NAME")
    tsk_create.add_argument("--schedule", required=True, metavar="CRON",
                            help='Cron expression e.g. "*/5 * * * *"')
    tsk_create.add_argument("--prompt", required=True, metavar="TEXT")
    tsk_cancel = tsk_sub.add_parser("cancel", help="Cancel a task")
    tsk_cancel.add_argument("task_id", metavar="TASK_ID")

    # usage
    usage_p = sub.add_parser("usage", help="Token usage summary")
    usage_p.add_argument("--group", metavar="NAME", help="Filter by group")

    # audit
    audit_p = sub.add_parser("audit", help="View audit log")
    audit_p.add_argument("--group", metavar="NAME", help="Filter by group")
    audit_p.add_argument("--limit", type=int, default=50, metavar="N")

    # messages
    msg_p = sub.add_parser("messages", help="View message history")
    msg_p.add_argument("--group", metavar="NAME", help="Filter by group")
    msg_p.add_argument("--status", metavar="STATUS", help="Filter by status")
    msg_p.add_argument("--limit", type=int, default=20, metavar="N")

    return parser


# ---------------------------------------------------------------------------
# Async main (called by sync entry point)
# ---------------------------------------------------------------------------

async def _async_main(argv: Optional[list[str]] = None) -> int:
    from src.db import Database

    parser = build_parser()
    args = parser.parse_args(argv)

    # Resolve db_path
    db_path = getattr(args, "db", None) or _load_db_path(getattr(args, "config", None))

    # Open DB
    db = Database()
    try:
        await db.init(db_path)
    except Exception as exc:
        print(f"Failed to open database at '{db_path}': {exc}", file=sys.stderr)
        return 1

    try:
        cmd = args.command
        if cmd == "status":
            return await cmd_status(args, db)
        elif cmd == "groups":
            if args.groups_cmd == "list":
                return await cmd_groups_list(args, db)
            elif args.groups_cmd == "show":
                return await cmd_groups_show(args, db)
        elif cmd == "tasks":
            if args.tasks_cmd == "list":
                return await cmd_tasks_list(args, db)
            elif args.tasks_cmd == "create":
                return await cmd_tasks_create(args, db)
            elif args.tasks_cmd == "cancel":
                return await cmd_tasks_cancel(args, db)
        elif cmd == "usage":
            return await cmd_usage(args, db)
        elif cmd == "audit":
            return await cmd_audit(args, db)
        elif cmd == "messages":
            return await cmd_messages(args, db)
    finally:
        await db.close()

    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """Sync entry point — wraps _async_main in asyncio.run()."""
    return asyncio.run(_async_main(argv))


if __name__ == "__main__":
    sys.exit(main())
