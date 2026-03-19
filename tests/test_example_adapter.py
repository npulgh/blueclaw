"""Tests for ExampleAdapter.

No external dependencies — the echo adapter is fully self-contained.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.channels.example_adapter import ExampleAdapter, ExampleConfig
from src.channels.registry import ChannelAdapter
from src.types import ChannelCapabilities, IncomingMessage, OutgoingMessage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def adapter():
    """Initialised and started ExampleAdapter, stopped after the test."""
    a = ExampleAdapter()
    await a.init(ExampleConfig(enabled=True, channel_name="example"))
    await a.start()
    yield a
    await a.stop()


# ---------------------------------------------------------------------------
# ABC compliance
# ---------------------------------------------------------------------------

def test_is_channel_adapter():
    assert issubclass(ExampleAdapter, ChannelAdapter)


def test_all_abstract_methods_implemented():
    """Instantiating ExampleAdapter must not raise TypeError."""
    a = ExampleAdapter()
    assert a is not None


# ---------------------------------------------------------------------------
# init()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_init_sets_config():
    a = ExampleAdapter()
    cfg = ExampleConfig(enabled=True, channel_name="test-chan")
    await a.init(cfg)
    assert a._config is cfg


@pytest.mark.asyncio
async def test_init_raises_on_empty_channel_name():
    a = ExampleAdapter()
    with pytest.raises(ValueError, match="channel_name"):
        await a.init(ExampleConfig(channel_name=""))


# ---------------------------------------------------------------------------
# start() / stop()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_before_init_raises():
    a = ExampleAdapter()
    with pytest.raises(RuntimeError, match="init"):
        await a.start()


@pytest.mark.asyncio
async def test_stop_is_idempotent(adapter):
    await adapter.stop()   # second stop — must not raise
    await adapter.stop()


# ---------------------------------------------------------------------------
# send_message()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_message_returns_nonempty_id(adapter):
    msg_id = await adapter.send_message("chat-1", OutgoingMessage(text="hello"))
    assert msg_id
    assert isinstance(msg_id, str)


@pytest.mark.asyncio
async def test_send_message_ids_are_unique(adapter):
    id1 = await adapter.send_message("chat-1", OutgoingMessage(text="a"))
    id2 = await adapter.send_message("chat-1", OutgoingMessage(text="b"))
    assert id1 != id2


@pytest.mark.asyncio
async def test_send_message_before_start_raises():
    a = ExampleAdapter()
    await a.init(ExampleConfig(enabled=True))
    with pytest.raises(RuntimeError):
        await a.send_message("chat-1", OutgoingMessage(text="hi"))


# ---------------------------------------------------------------------------
# edit_message()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_edit_message_does_not_raise(adapter):
    msg_id = await adapter.send_message("chat-1", OutgoingMessage(text="original"))
    await adapter.edit_message("chat-1", msg_id, OutgoingMessage(text="updated"))


# ---------------------------------------------------------------------------
# capabilities()
# ---------------------------------------------------------------------------

def test_capabilities_returns_correct_type():
    a = ExampleAdapter()
    caps = a.capabilities()
    assert isinstance(caps, ChannelCapabilities)


def test_capabilities_supports_edit():
    caps = ExampleAdapter().capabilities()
    assert caps.supports_edit is True


def test_capabilities_no_attachments():
    caps = ExampleAdapter().capabilities()
    assert caps.supports_attachments is False


# ---------------------------------------------------------------------------
# on_message() + simulate_incoming()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_simulate_incoming_dispatches_to_handler(adapter):
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        received.append(msg)

    adapter.on_message(handler)
    await adapter.simulate_incoming("hello world", chat_id="c1", sender_id="u1")

    assert len(received) == 1
    assert received[0].text == "hello world"
    assert received[0].chat_id == "c1"
    assert received[0].channel == "example"


@pytest.mark.asyncio
async def test_simulate_incoming_without_handler_raises(adapter):
    with pytest.raises(RuntimeError, match="handler"):
        await adapter.simulate_incoming("oops")


@pytest.mark.asyncio
async def test_simulate_incoming_message_ids_are_unique(adapter):
    ids: list[str] = []

    async def handler(msg: IncomingMessage) -> None:
        ids.append(msg.message_id)

    adapter.on_message(handler)
    await adapter.simulate_incoming("first")
    await adapter.simulate_incoming("second")

    assert len(ids) == 2
    assert ids[0] != ids[1]


@pytest.mark.asyncio
async def test_handler_exception_does_not_crash_adapter(adapter):
    """A handler that raises must not propagate out of simulate_incoming."""

    async def bad_handler(msg: IncomingMessage) -> None:
        raise ValueError("intentional error")

    adapter.on_message(bad_handler)
    # Should not raise
    await adapter.simulate_incoming("trigger error")


# ---------------------------------------------------------------------------
# discover_adapters() integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discover_adapters_returns_telegram_when_enabled():
    from src.channels.registry import discover_adapters
    from src.config import Config, TelegramConfig

    cfg = Config()
    cfg.telegram = TelegramConfig(enabled=True, bot_token="fake-token")
    cfg.feishu.enabled = False

    adapters = discover_adapters(cfg)
    assert "telegram" in adapters


@pytest.mark.asyncio
async def test_discover_adapters_empty_when_nothing_enabled():
    from src.channels.registry import discover_adapters
    from src.config import Config

    cfg = Config()
    cfg.telegram.enabled = False
    cfg.feishu.enabled = False

    adapters = discover_adapters(cfg)
    assert adapters == {}
