# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for src/cli.py — Management CLI."""

from __future__ import annotations

import asyncio
import sys
import time
from unittest.mock import MagicMock

import pytest

from src.cli import (
    build_parser,
    cmd_audit,
    cmd_groups_list,
    cmd_groups_show,
    cmd_messages,
    cmd_status,
    cmd_tasks_cancel,
    cmd_tasks_create,
    cmd_tasks_list,
    cmd_usage,
    _async_main,
)
from src.db import Database


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def db(tmp_path):
    database = Database()
    await database.init(str(tmp_path / "cli_test.db"))
    yield database
    await database.close()


@pytest.fixture
async def populated_db(db):
    """DB with one group, session, task, token usage, message, and audit entry."""
    await db.upsert_group(
        name="alpha", channel="telegram", chat_id="100", is_main=True, trigger="@bot"
    )
    await db.save_session(group_name="alpha", session_id="sess-abc")
    await db.create_task(
        id="task-001",
        group_name="alpha",
        type="scheduled",
        schedule="*/5 * * * *",
        prompt="Say hello",
    )
    await db.record_token_usage(group_name="alpha", input_tokens=500, output_tokens=200)
    await db.insert_message(
        channel="telegram",
        chat_id="100",
        message_id="m1",
        sender_id="u1",
        content="Hello world",
        direction="inbound",
        group_name="alpha",
        status="pending",
    )
    await db._conn.execute(
        "INSERT INTO tool_audit_log (group_name, tool_name, blocked, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("alpha", "bash", 0, int(time.time())),
    )
    await db._conn.commit()
    return db


# ---------------------------------------------------------------------------
# Parser tests (sync — no DB needed)
# ---------------------------------------------------------------------------

def test_parser_status():
    args = build_parser().parse_args(["status"])
    assert args.command == "status"


def test_parser_groups_list():
    args = build_parser().parse_args(["groups", "list"])
    assert args.command == "groups"
    assert args.groups_cmd == "list"


def test_parser_groups_show():
    args = build_parser().parse_args(["groups", "show", "mygroup"])
    assert args.groups_cmd == "show"
    assert args.name == "mygroup"


def test_parser_tasks_list_with_group():
    args = build_parser().parse_args(["tasks", "list", "--group", "alpha"])
    assert args.tasks_cmd == "list"
    assert args.group == "alpha"


def test_parser_tasks_create():
    args = build_parser().parse_args([
        "tasks", "create",
        "--group", "alpha",
        "--schedule", "*/5 * * * *",
        "--prompt", "Do something",
    ])
    assert args.tasks_cmd == "create"
    assert args.group == "alpha"
    assert args.schedule == "*/5 * * * *"
    assert args.prompt == "Do something"


def test_parser_tasks_cancel():
    args = build_parser().parse_args(["tasks", "cancel", "task-001"])
    assert args.tasks_cmd == "cancel"
    assert args.task_id == "task-001"


def test_parser_usage_no_group():
    args = build_parser().parse_args(["usage"])
    assert args.command == "usage"
    assert args.group is None


def test_parser_audit_with_limit():
    args = build_parser().parse_args(["audit", "--limit", "10"])
    assert args.limit == 10


def test_parser_messages_all_flags():
    args = build_parser().parse_args([
        "messages", "--group", "alpha", "--status", "pending", "--limit", "5"
    ])
    assert args.group == "alpha"
    assert args.status == "pending"
    assert args.limit == 5


def test_parser_missing_required_subcommand():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["tasks"])


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------

async def test_cmd_status_empty_db(db, capsys):
    rc = await cmd_status(MagicMock(), db)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Groups" in out
    assert "Tasks" in out


async def test_cmd_status_populated(populated_db, capsys):
    rc = await cmd_status(MagicMock(), populated_db)
    assert rc == 0
    assert "1" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_groups_list
# ---------------------------------------------------------------------------

async def test_cmd_groups_list_empty(db, capsys):
    rc = await cmd_groups_list(MagicMock(), db)
    assert rc == 0
    assert "No groups" in capsys.readouterr().out


async def test_cmd_groups_list_shows_groups(populated_db, capsys):
    rc = await cmd_groups_list(MagicMock(), populated_db)
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out
    assert "telegram" in out


# ---------------------------------------------------------------------------
# cmd_groups_show
# ---------------------------------------------------------------------------

async def test_cmd_groups_show_found(populated_db, capsys):
    args = MagicMock()
    args.name = "alpha"
    rc = await cmd_groups_show(args, populated_db)
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out
    assert "sess-abc" in out
    assert "500" in out


async def test_cmd_groups_show_not_found(db, capsys):
    args = MagicMock()
    args.name = "nonexistent"
    rc = await cmd_groups_show(args, db)
    assert rc == 1
    assert "not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_tasks_list
# ---------------------------------------------------------------------------

async def test_cmd_tasks_list_empty(db, capsys):
    args = MagicMock()
    args.group = None
    rc = await cmd_tasks_list(args, db)
    assert rc == 0
    assert "No tasks" in capsys.readouterr().out


async def test_cmd_tasks_list_with_data(populated_db, capsys):
    args = MagicMock()
    args.group = None
    rc = await cmd_tasks_list(args, populated_db)
    assert rc == 0
    out = capsys.readouterr().out
    assert "task-001" in out
    assert "alpha" in out


async def test_cmd_tasks_list_filter_by_group(populated_db, capsys):
    args = MagicMock()
    args.group = "alpha"
    rc = await cmd_tasks_list(args, populated_db)
    assert rc == 0
    assert "task-001" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_tasks_create
# ---------------------------------------------------------------------------

async def test_cmd_tasks_create(db, capsys):
    await db.upsert_group(name="beta", channel="feishu", chat_id="200")
    args = MagicMock()
    args.group = "beta"
    args.schedule = "0 * * * *"
    args.prompt = "Hourly check"
    rc = await cmd_tasks_create(args, db)
    assert rc == 0
    assert "Task created" in capsys.readouterr().out
    tasks = await db.get_all_tasks(group_name="beta")
    assert len(tasks) == 1
    assert tasks[0]["prompt"] == "Hourly check"


# ---------------------------------------------------------------------------
# cmd_tasks_cancel
# ---------------------------------------------------------------------------

async def test_cmd_tasks_cancel_success(populated_db, capsys):
    args = MagicMock()
    args.task_id = "task-001"
    rc = await cmd_tasks_cancel(args, populated_db)
    assert rc == 0
    assert "cancelled" in capsys.readouterr().out
    tasks = await populated_db.get_all_tasks()
    assert next(t for t in tasks if t["id"] == "task-001")["status"] == "cancelled"


async def test_cmd_tasks_cancel_not_found(db, capsys):
    args = MagicMock()
    args.task_id = "no-such-task"
    rc = await cmd_tasks_cancel(args, db)
    assert rc == 1
    assert "not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_usage
# ---------------------------------------------------------------------------

async def test_cmd_usage_all_groups(populated_db, capsys):
    args = MagicMock()
    args.group = None
    rc = await cmd_usage(args, populated_db)
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out
    assert "500" in out


async def test_cmd_usage_specific_group(populated_db, capsys):
    args = MagicMock()
    args.group = "alpha"
    rc = await cmd_usage(args, populated_db)
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out
    assert "200" in out


async def test_cmd_usage_empty(db, capsys):
    args = MagicMock()
    args.group = None
    rc = await cmd_usage(args, db)
    assert rc == 0
    assert "No token usage" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_audit
# ---------------------------------------------------------------------------

async def test_cmd_audit_empty(db, capsys):
    args = MagicMock()
    args.group = None
    args.limit = 50
    rc = await cmd_audit(args, db)
    assert rc == 0
    assert "No audit" in capsys.readouterr().out


async def test_cmd_audit_with_data(populated_db, capsys):
    args = MagicMock()
    args.group = None
    args.limit = 50
    rc = await cmd_audit(args, populated_db)
    assert rc == 0
    out = capsys.readouterr().out
    assert "bash" in out
    assert "alpha" in out


async def test_cmd_audit_filter_group(populated_db, capsys):
    args = MagicMock()
    args.group = "alpha"
    args.limit = 10
    rc = await cmd_audit(args, populated_db)
    assert rc == 0
    assert "bash" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_messages
# ---------------------------------------------------------------------------

async def test_cmd_messages_empty(db, capsys):
    args = MagicMock()
    args.group = None
    args.status = None
    args.limit = 20
    rc = await cmd_messages(args, db)
    assert rc == 0
    assert "No messages" in capsys.readouterr().out


async def test_cmd_messages_with_data(populated_db, capsys):
    args = MagicMock()
    args.group = None
    args.status = None
    args.limit = 20
    rc = await cmd_messages(args, populated_db)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Hello world" in out
    assert "alpha" in out


async def test_cmd_messages_filter_status(populated_db, capsys):
    args = MagicMock()
    args.group = None
    args.status = "pending"
    args.limit = 20
    rc = await cmd_messages(args, populated_db)
    assert rc == 0
    assert "Hello world" in capsys.readouterr().out


async def test_cmd_messages_filter_status_no_match(populated_db, capsys):
    args = MagicMock()
    args.group = None
    args.status = "delivered"
    args.limit = 20
    rc = await cmd_messages(args, populated_db)
    assert rc == 0
    assert "No messages" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _async_main integration — uses --db to bypass config loading
# ---------------------------------------------------------------------------

async def test_async_main_status(tmp_path):
    db_file = str(tmp_path / "main_test.db")
    rc = await _async_main(["--db", db_file, "status"])
    assert rc == 0


async def test_async_main_groups_list_empty(tmp_path, capsys):
    db_file = str(tmp_path / "main_test.db")
    rc = await _async_main(["--db", db_file, "groups", "list"])
    assert rc == 0
    assert "No groups" in capsys.readouterr().out


async def test_async_main_groups_show_missing(tmp_path, capsys):
    db_file = str(tmp_path / "main_test.db")
    rc = await _async_main(["--db", db_file, "groups", "show", "ghost"])
    assert rc == 1


async def test_async_main_tasks_list_empty(tmp_path, capsys):
    db_file = str(tmp_path / "main_test.db")
    rc = await _async_main(["--db", db_file, "tasks", "list"])
    assert rc == 0
    assert "No tasks" in capsys.readouterr().out


def test_parser_invalid_subcommand():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["groups"])
