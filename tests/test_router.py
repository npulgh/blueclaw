"""Tests for src/router.py — MessageRouter (T1.6)."""

from __future__ import annotations

import time

import pytest

from src.config import Config, GroupConfig, RouterConfig
from src.db import Database
from src.router import MessageRouter, RouteResult
from src.types import IncomingMessage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def db(tmp_path):
    """In-memory-backed Database (temp file so backup works)."""
    database = Database()
    await database.init(str(tmp_path / "test.db"))
    yield database
    await database.close()


def _make_config(
    *,
    groups: list[GroupConfig] | None = None,
    queue_max: int = 10,
) -> Config:
    """Build a minimal Config for tests."""
    cfg = Config()
    cfg.anthropic_api_key = "sk-test"
    cfg.router = RouterConfig(group_queue_max=queue_max)
    cfg.groups = groups or [
        GroupConfig(
            name="test-group",
            channel="telegram",
            chat_id="-1001111111111",
            is_main=False,
            trigger="@bot",
        )
    ]
    return cfg


@pytest.fixture
async def router(db):
    """Router initialised with a single test group (group chat)."""
    cfg = _make_config()
    r = MessageRouter()
    await r.init(db, cfg)
    return r


def _msg(
    *,
    message_id: str = "msg1",
    chat_id: str = "-1001111111111",
    channel: str = "telegram",
    text: str = "@bot hello",
    sender_id: str = "user1",
) -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        chat_id=chat_id,
        sender_id=sender_id,
        sender_name="Test User",
        text=text,
        channel=channel,
        timestamp=int(time.time()),
    )


# ---------------------------------------------------------------------------
# Core routing tests
# ---------------------------------------------------------------------------

async def test_normal_routing_returns_queued(router: MessageRouter):
    """A valid message with trigger prefix is accepted and queued."""
    result = await router.route(_msg())
    assert result == RouteResult.QUEUED


async def test_duplicate_message_returns_duplicate(router: MessageRouter):
    """Same (channel, chat_id, message_id) twice → second call is DUPLICATE."""
    msg = _msg(message_id="dup1")
    first = await router.route(msg)
    second = await router.route(msg)

    assert first == RouteResult.QUEUED
    assert second == RouteResult.DUPLICATE


async def test_queue_full_returns_backpressure(db: Database):
    """When group queue is full (maxsize=2), next message → BACKPRESSURE."""
    cfg = _make_config(queue_max=2)
    r = MessageRouter()
    await r.init(db, cfg)

    r1 = await r.route(_msg(message_id="m1"))
    r2 = await r.route(_msg(message_id="m2"))
    r3 = await r.route(_msg(message_id="m3"))

    assert r1 == RouteResult.QUEUED
    assert r2 == RouteResult.QUEUED
    assert r3 == RouteResult.BACKPRESSURE


async def test_no_group_returns_no_group(router: MessageRouter):
    """Message for an unregistered chat_id → NO_GROUP."""
    msg = _msg(chat_id="-9999999999")
    result = await router.route(msg)
    assert result == RouteResult.NO_GROUP


# ---------------------------------------------------------------------------
# Trigger matching tests
# ---------------------------------------------------------------------------

async def test_group_chat_with_trigger_prefix_queued(router: MessageRouter):
    """Group chat message starting with @bot is accepted."""
    result = await router.route(_msg(text="@bot what time is it?"))
    assert result == RouteResult.QUEUED


async def test_group_chat_without_trigger_returns_no_trigger(router: MessageRouter):
    """Group chat message without trigger prefix → NO_TRIGGER."""
    result = await router.route(_msg(text="hello everyone"))
    assert result == RouteResult.NO_TRIGGER


async def test_dm_always_triggers(db: Database):
    """DM (chat_id without '-' prefix) always routes regardless of trigger."""
    # Register a DM-style group (positive chat_id)
    cfg = _make_config(
        groups=[
            GroupConfig(
                name="dm-group",
                channel="telegram",
                chat_id="123456789",     # no leading "-" → DM
                trigger="@bot",
            )
        ]
    )
    r = MessageRouter()
    await r.init(db, cfg)

    # Text does NOT start with @bot — should still be QUEUED for DM
    msg = _msg(chat_id="123456789", text="plain message without trigger")
    result = await r.route(msg)
    assert result == RouteResult.QUEUED


async def test_dm_with_trigger_prefix_also_queued(db: Database):
    """DM with explicit trigger prefix is also accepted."""
    cfg = _make_config(
        groups=[
            GroupConfig(
                name="dm-group",
                channel="telegram",
                chat_id="123456789",
                trigger="@bot",
            )
        ]
    )
    r = MessageRouter()
    await r.init(db, cfg)

    msg = _msg(chat_id="123456789", text="@bot what's up?")
    result = await r.route(msg)
    assert result == RouteResult.QUEUED


# ---------------------------------------------------------------------------
# Queue consumption tests
# ---------------------------------------------------------------------------

async def test_get_next_returns_queued_message(router: MessageRouter):
    """get_next() yields the message placed by route()."""
    msg = _msg(message_id="gn1", text="@bot fetch")
    await router.route(msg)

    received = await router.get_next("test-group")
    assert received.message_id == "gn1"


async def test_queue_size_reflects_queued_messages(router: MessageRouter):
    """queue_size() tracks the current depth."""
    assert router.queue_size("test-group") == 0

    await router.route(_msg(message_id="q1"))
    await router.route(_msg(message_id="q2"))

    assert router.queue_size("test-group") == 2


# ---------------------------------------------------------------------------
# Config groups seeding tests
# ---------------------------------------------------------------------------

async def test_init_writes_groups_to_db(db: Database):
    """Groups from config are persisted to the DB on init."""
    cfg = _make_config(
        groups=[
            GroupConfig(name="grp1", channel="telegram", chat_id="-1001"),
            GroupConfig(name="grp2", channel="feishu", chat_id="feishu_chat_1"),
        ]
    )
    r = MessageRouter()
    await r.init(db, cfg)

    row1 = await db.get_group_by_chat(channel="telegram", chat_id="-1001")
    row2 = await db.get_group_by_chat(channel="feishu", chat_id="feishu_chat_1")

    assert row1 is not None and row1["name"] == "grp1"
    assert row2 is not None and row2["name"] == "grp2"


async def test_reinit_does_not_lose_groups(db: Database):
    """Calling init() twice (e.g. restart) is idempotent — groups survive."""
    cfg = _make_config()
    r = MessageRouter()
    await r.init(db, cfg)
    # second init — simulates restart
    await r.init(db, cfg)

    row = await db.get_group_by_chat(channel="telegram", chat_id="-1001111111111")
    assert row is not None
    assert row["name"] == "test-group"


# ---------------------------------------------------------------------------
# recover_pending tests (T2.6)
# ---------------------------------------------------------------------------

async def test_recover_pending_requeues_processing_messages(db: Database):
    """Messages with status='processing' are re-enqueued on recovery."""
    cfg = _make_config()
    r = MessageRouter()
    await r.init(db, cfg)

    # Simulate a message that was stuck in 'processing' at crash time
    await db.insert_message(
        channel="telegram",
        chat_id="-1001111111111",
        message_id="crash-msg-1",
        sender_id="user1",
        sender_name="Test User",
        content="@bot hello",
        direction="inbound",
        status="processing",
    )

    count = await r.recover_pending()

    assert count == 1
    assert r.queue_size("test-group") == 1

    recovered_msg = await r.get_next("test-group")
    assert recovered_msg.message_id == "crash-msg-1"
    assert recovered_msg.text == "@bot hello"


async def test_recover_pending_returns_zero_when_nothing_stuck(db: Database):
    """recover_pending returns 0 when there are no 'processing' messages."""
    cfg = _make_config()
    r = MessageRouter()
    await r.init(db, cfg)

    count = await r.recover_pending()

    assert count == 0
    assert r.queue_size("test-group") == 0


async def test_recover_pending_ignores_completed_messages(db: Database):
    """Messages with status='completed' are not re-enqueued."""
    cfg = _make_config()
    r = MessageRouter()
    await r.init(db, cfg)

    await db.insert_message(
        channel="telegram",
        chat_id="-1001111111111",
        message_id="done-msg-1",
        sender_id="user1",
        content="@bot done",
        direction="inbound",
        status="completed",
    )

    count = await r.recover_pending()

    assert count == 0
    assert r.queue_size("test-group") == 0


async def test_recover_pending_multiple_messages(db: Database):
    """Multiple stuck messages are all re-enqueued."""
    cfg = _make_config(queue_max=20)
    r = MessageRouter()
    await r.init(db, cfg)

    for i in range(3):
        await db.insert_message(
            channel="telegram",
            chat_id="-1001111111111",
            message_id=f"crash-{i}",
            sender_id="user1",
            content=f"@bot msg {i}",
            direction="inbound",
            status="processing",
        )

    count = await r.recover_pending()

    assert count == 3
    assert r.queue_size("test-group") == 3


async def test_recover_pending_marks_failed_when_no_group(db: Database):
    """A stuck message for an unknown chat_id is marked 'failed' (not re-enqueued)."""
    cfg = _make_config()
    r = MessageRouter()
    await r.init(db, cfg)

    await db.insert_message(
        channel="telegram",
        chat_id="-9999999999",   # no group registered for this chat
        message_id="orphan-msg",
        sender_id="user1",
        content="hello",
        direction="inbound",
        status="processing",
    )

    count = await r.recover_pending()

    assert count == 0
    # Message should now be 'failed'
    row = await db.get_message(
        channel="telegram", chat_id="-9999999999", message_id="orphan-msg"
    )
    assert row is not None
    assert row["status"] == "failed"
