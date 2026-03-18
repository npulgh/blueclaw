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
    assert row[0] == 1


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
