"""Tests for src/db.py — Database class."""

from __future__ import annotations

import pytest

from src.db import Database


@pytest.fixture
async def db(tmp_path):
    """Provide an initialised Database backed by a temp file."""
    database = Database()
    await database.init(str(tmp_path / "test.db"))
    yield database
    await database.close()


# ---------------------------------------------------------------------------
# Schema / table creation
# ---------------------------------------------------------------------------

async def test_tables_created(db: Database):
    """All six tables must exist after init."""
    expected = {"messages", "groups", "sessions", "tasks", "cursors", "tool_audit_log"}
    async with db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ) as cur:
        rows = await cur.fetchall()
    names = {row[0] for row in rows}
    assert expected.issubset(names)


async def test_wal_mode(db: Database):
    async with db._conn.execute("PRAGMA journal_mode") as cur:
        row = await cur.fetchone()
    assert row[0] == "wal"


async def test_foreign_keys_on(db: Database):
    async with db._conn.execute("PRAGMA foreign_keys") as cur:
        row = await cur.fetchone()
    assert row[0] == 1


async def test_schema_version(db: Database):
    async with db._conn.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    assert row[0] == 2


# ---------------------------------------------------------------------------
# insert_message / get_message
# ---------------------------------------------------------------------------

async def test_insert_and_query(db: Database):
    rowid = await db.insert_message(
        channel="telegram",
        chat_id="chat1",
        message_id="msg1",
        sender_id="user1",
        content="hello",
        direction="inbound",
    )
    assert rowid is not None

    row = await db.get_message(channel="telegram", chat_id="chat1", message_id="msg1")
    assert row is not None
    assert row["content"] == "hello"
    assert row["direction"] == "inbound"
    assert row["status"] == "pending"


async def test_duplicate_insert_ignored(db: Database):
    """Inserting the same (channel, chat_id, message_id) twice must not raise."""
    kwargs = dict(
        channel="telegram",
        chat_id="chat1",
        message_id="msg1",
        sender_id="user1",
        content="hello",
        direction="inbound",
    )
    first = await db.insert_message(**kwargs)
    second = await db.insert_message(**kwargs)

    assert first is not None
    assert second is None  # INSERT OR IGNORE → rowcount 0 → None


async def test_get_message_not_found(db: Database):
    row = await db.get_message(channel="x", chat_id="y", message_id="z")
    assert row is None


async def test_update_message_status(db: Database):
    await db.insert_message(
        channel="feishu",
        chat_id="c2",
        message_id="m2",
        sender_id="u2",
        content="hi",
        direction="outbound",
    )
    await db.update_message_status(
        channel="feishu", chat_id="c2", message_id="m2", status="delivered"
    )
    row = await db.get_message(channel="feishu", chat_id="c2", message_id="m2")
    assert row["status"] == "delivered"
    assert row["processed_at"] is not None


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

async def test_backup_created(tmp_path):
    """Backup file must exist after init."""
    db_file = tmp_path / "messages.db"
    database = Database()
    await database.init(str(db_file))
    await database.close()

    bak = tmp_path / "messages.db.bak"
    assert bak.exists(), "Backup file was not created"
    assert bak.stat().st_size > 0


async def test_backup_idempotent(tmp_path):
    """Calling backup() twice must not raise (old .bak is replaced)."""
    db_file = tmp_path / "messages.db"
    database = Database()
    await database.init(str(db_file))
    await database.backup()  # second call
    await database.close()

    bak = tmp_path / "messages.db.bak"
    assert bak.exists()


# ---------------------------------------------------------------------------
# save_session / get_session
# ---------------------------------------------------------------------------

async def test_save_and_get_session(db: Database):
    """save_session stores a session_id retrievable by get_session."""
    # sessions table has FK to groups; insert a group first
    await db.upsert_group(
        name="grp1", channel="telegram", chat_id="c1", is_main=False
    )
    await db.save_session(group_name="grp1", session_id="uuid-abc-123")
    result = await db.get_session(group_name="grp1")
    assert result == "uuid-abc-123"


async def test_save_session_upsert(db: Database):
    """Calling save_session twice updates the session_id."""
    await db.upsert_group(
        name="grp2", channel="telegram", chat_id="c2", is_main=False
    )
    await db.save_session(group_name="grp2", session_id="first-id")
    await db.save_session(group_name="grp2", session_id="second-id")
    result = await db.get_session(group_name="grp2")
    assert result == "second-id"


async def test_get_session_unknown_group(db: Database):
    """get_session returns None for a group with no session."""
    result = await db.get_session(group_name="nonexistent")
    assert result is None


# ---------------------------------------------------------------------------
# update_cursor / get_cursor
# ---------------------------------------------------------------------------

async def test_update_and_get_cursor(db: Database):
    """update_cursor stores a value retrievable by get_cursor."""
    await db.update_cursor(channel="telegram", chat_id="chat1", last_msg_id="msg-42")
    result = await db.get_cursor(channel="telegram", chat_id="chat1")
    assert result == "msg-42"


async def test_get_cursor_not_found(db: Database):
    """get_cursor returns None when no cursor exists for channel+chat_id."""
    result = await db.get_cursor(channel="telegram", chat_id="unknown")
    assert result is None


async def test_update_cursor_upsert(db: Database):
    """Calling update_cursor twice advances the cursor to the latest value."""
    await db.update_cursor(channel="feishu", chat_id="c1", last_msg_id="msg-1")
    await db.update_cursor(channel="feishu", chat_id="c1", last_msg_id="msg-99")
    result = await db.get_cursor(channel="feishu", chat_id="c1")
    assert result == "msg-99"


async def test_cursors_are_independent_per_channel_and_chat(db: Database):
    """Different channel+chat_id pairs have independent cursors."""
    await db.update_cursor(channel="telegram", chat_id="c1", last_msg_id="tg-10")
    await db.update_cursor(channel="feishu", chat_id="c1", last_msg_id="fs-20")

    assert await db.get_cursor(channel="telegram", chat_id="c1") == "tg-10"
    assert await db.get_cursor(channel="feishu", chat_id="c1") == "fs-20"


# ---------------------------------------------------------------------------
# get_messages_by_status
# ---------------------------------------------------------------------------

async def test_get_messages_by_status_returns_matching(db: Database):
    """get_messages_by_status returns only rows with the requested status."""
    await db.insert_message(
        channel="telegram", chat_id="c1", message_id="m1",
        sender_id="u1", content="hello", direction="inbound", status="processing",
    )
    await db.insert_message(
        channel="telegram", chat_id="c1", message_id="m2",
        sender_id="u1", content="world", direction="inbound", status="completed",
    )
    rows = await db.get_messages_by_status(status="processing")
    assert len(rows) == 1
    assert rows[0]["message_id"] == "m1"


async def test_get_messages_by_status_empty(db: Database):
    """get_messages_by_status returns an empty list when no rows match."""
    rows = await db.get_messages_by_status(status="processing")
    assert rows == []


# ---------------------------------------------------------------------------
# token_usage helpers
# ---------------------------------------------------------------------------

async def test_token_usage_table_exists(db: Database):
    """token_usage table must exist after schema migration."""
    async with db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='token_usage'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None, "token_usage table not found"


async def test_schema_version_is_2(db: Database):
    """SCHEMA_VERSION must be 2 after the token_usage migration."""
    async with db._conn.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    assert row[0] == 2


async def test_record_and_get_token_usage(db: Database):
    """record_token_usage inserts a row retrievable via get_token_usage."""
    await db.upsert_group(name="tg1", channel="telegram", chat_id="tc1")
    await db.record_token_usage(group_name="tg1", input_tokens=100, output_tokens=50)
    result = await db.get_token_usage(group_name="tg1")
    assert result["input_tokens"] == 100
    assert result["output_tokens"] == 50


async def test_get_token_usage_sums_multiple_rows(db: Database):
    """get_token_usage sums all rows for the group."""
    await db.upsert_group(name="tg2", channel="telegram", chat_id="tc2")
    await db.record_token_usage(group_name="tg2", input_tokens=200, output_tokens=80)
    await db.record_token_usage(group_name="tg2", input_tokens=300, output_tokens=120)
    result = await db.get_token_usage(group_name="tg2")
    assert result["input_tokens"] == 500
    assert result["output_tokens"] == 200


async def test_get_token_usage_no_rows_returns_zero(db: Database):
    """get_token_usage returns zeros when no rows exist for the group."""
    await db.upsert_group(name="tg3", channel="telegram", chat_id="tc3")
    result = await db.get_token_usage(group_name="tg3")
    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 0


async def test_get_token_usage_since_filters_old_rows(db: Database):
    """get_token_usage(since=ts) excludes rows with created_at < ts."""
    import time

    await db.upsert_group(name="tg4", channel="telegram", chat_id="tc4")
    old_ts = int(time.time()) - 10000
    new_ts = int(time.time())

    # Old row — should be excluded by 'since' filter
    await db.record_token_usage(
        group_name="tg4", input_tokens=999, output_tokens=999, created_at=old_ts
    )
    # Recent row — should be included
    await db.record_token_usage(
        group_name="tg4", input_tokens=10, output_tokens=5, created_at=new_ts
    )

    result = await db.get_token_usage(group_name="tg4", since=new_ts)
    assert result["input_tokens"] == 10
    assert result["output_tokens"] == 5


async def test_get_token_usage_unknown_group_returns_zero(db: Database):
    """get_token_usage returns zeros for a group with no rows (no FK error)."""
    result = await db.get_token_usage(group_name="no-such-group")
    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 0


# ---------------------------------------------------------------------------
# Dashboard: offset param + get_messages_count_since
# ---------------------------------------------------------------------------

async def test_get_messages_offset(db: Database):
    """offset parameter skips rows correctly."""
    import time as _time
    for i in range(5):
        await db.insert_message(
            channel="telegram", chat_id="c1", message_id=f"offset_m{i}",
            sender_id="u1", content=f"msg{i}", direction="inbound",
        )
    all_msgs = await db.get_messages(limit=10, offset=0)
    offset_msgs = await db.get_messages(limit=10, offset=2)
    assert len(offset_msgs) == len(all_msgs) - 2


async def test_get_audit_log_offset(db: Database):
    """offset parameter skips audit rows correctly."""
    import time as _time
    for i in range(4):
        await db._conn.execute(
            "INSERT INTO tool_audit_log (group_name, tool_name, created_at) VALUES (?,?,?)",
            ("g1", f"tool{i}", int(_time.time()) + i),
        )
    await db._conn.commit()
    all_rows = await db.get_audit_log(limit=10, offset=0)
    offset_rows = await db.get_audit_log(limit=10, offset=2)
    assert len(offset_rows) == len(all_rows) - 2


async def test_get_messages_count_since(db: Database):
    """Count messages created at or after a timestamp."""
    import time as _time
    now = int(_time.time())
    await db.insert_message(
        channel="telegram", chat_id="c1", message_id="count_old",
        sender_id="u1", content="old", direction="inbound",
        created_at=now - 10000,
    )
    await db.insert_message(
        channel="telegram", chat_id="c1", message_id="count_new",
        sender_id="u1", content="new", direction="inbound",
        created_at=now,
    )
    count = await db.get_messages_count_since(now - 1)
    assert count == 1
