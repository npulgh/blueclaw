"""Tests for TelegramAdapter.

All tests use mocks — no real bot token required.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.channels.telegram import TelegramAdapter, TokenBucket
from src.config import TelegramConfig
from src.types import IncomingMessage, OutgoingMessage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_aiogram_message(
    message_id: int = 42,
    chat_id: int = 100,
    sender_id: int = 7,
    sender_name: str = "Alice",
    text: str = "hello",
) -> MagicMock:
    """Build a minimal aiogram Message mock."""
    msg = MagicMock()
    msg.message_id = message_id
    msg.text = text
    msg.caption = None
    msg.date = datetime(2024, 1, 1, tzinfo=timezone.utc)

    msg.chat = MagicMock()
    msg.chat.id = chat_id

    msg.from_user = MagicMock()
    msg.from_user.id = sender_id
    msg.from_user.full_name = sender_name

    return msg


async def _make_initialised_adapter(token: str = "123:fake") -> TelegramAdapter:
    """Return a TelegramAdapter with Bot/Dispatcher mocked out."""
    adapter = TelegramAdapter()
    config = TelegramConfig(enabled=True, bot_token=token)

    with patch("src.channels.telegram.Bot"), patch("src.channels.telegram.Dispatcher") as MockDp:
        mock_dp_instance = MagicMock()
        mock_dp_instance.message = MagicMock()
        mock_dp_instance.message.register = MagicMock()
        MockDp.return_value = mock_dp_instance
        await adapter.init(config)

    return adapter


# ---------------------------------------------------------------------------
# TokenBucket tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_token_bucket_immediate_acquire():
    """Full bucket should grant a token immediately."""
    bucket = TokenBucket(rate=30.0, capacity=30)
    start = time.monotonic()
    await bucket.acquire()
    elapsed = time.monotonic() - start
    assert elapsed < 0.05  # should be near-instant


@pytest.mark.asyncio
async def test_token_bucket_depletes_and_waits():
    """After draining the bucket, next acquire should wait ~1/rate seconds."""
    bucket = TokenBucket(rate=10.0, capacity=1)
    await bucket.acquire()  # drain the single token

    start = time.monotonic()
    await bucket.acquire()  # must wait ~0.1 s for refill
    elapsed = time.monotonic() - start
    assert elapsed >= 0.08  # at least 80 ms (10 tokens/sec → 100 ms/token)


@pytest.mark.asyncio
async def test_token_bucket_capacity_cap():
    """Tokens must not exceed capacity even after a long idle period."""
    bucket = TokenBucket(rate=30.0, capacity=5)
    # Simulate a long idle by back-dating _last_refill
    bucket._last_refill -= 100.0  # 100 seconds of "idle"
    await bucket.acquire()
    # After one acquire, tokens should be capacity - 1 = 4, not 3000
    assert bucket._tokens <= 4.0


# ---------------------------------------------------------------------------
# IncomingMessage parsing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_incoming_message_parsing():
    """_on_aiogram_message must map aiogram fields to IncomingMessage correctly."""
    adapter = await _make_initialised_adapter()

    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        received.append(msg)

    adapter.on_message(handler)

    aiogram_msg = _make_aiogram_message(
        message_id=99,
        chat_id=555,
        sender_id=12,
        sender_name="Bob",
        text="test text",
    )
    await adapter._on_aiogram_message(aiogram_msg)

    assert len(received) == 1
    m = received[0]
    assert m.channel == "telegram"
    assert m.message_id == "99"
    assert m.chat_id == "555"
    assert m.sender_id == "12"
    assert m.sender_name == "Bob"
    assert m.text == "test text"
    assert m.timestamp == int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
    assert m.raw is aiogram_msg


@pytest.mark.asyncio
async def test_incoming_message_uses_caption_when_no_text():
    """Falls back to caption (photo/video messages) when text is None."""
    adapter = await _make_initialised_adapter()

    received: list[IncomingMessage] = []
    adapter.on_message(lambda m: received.append(m) or asyncio.sleep(0))

    msg = _make_aiogram_message(text=None)
    msg.text = None
    msg.caption = "photo caption"

    # Use a proper async handler
    async def handler(m: IncomingMessage) -> None:
        received.append(m)

    adapter.on_message(handler)
    await adapter._on_aiogram_message(msg)

    assert received[-1].text == "photo caption"


@pytest.mark.asyncio
async def test_incoming_message_no_handler_is_noop():
    """No handler registered → _on_aiogram_message must not raise."""
    adapter = await _make_initialised_adapter()
    msg = _make_aiogram_message()
    await adapter._on_aiogram_message(msg)  # should not raise


@pytest.mark.asyncio
async def test_incoming_message_no_sender():
    """Handles messages with no from_user (channel posts)."""
    adapter = await _make_initialised_adapter()

    received: list[IncomingMessage] = []

    async def handler(m: IncomingMessage) -> None:
        received.append(m)

    adapter.on_message(handler)

    msg = _make_aiogram_message()
    msg.from_user = None
    await adapter._on_aiogram_message(msg)

    assert received[0].sender_id == "unknown"
    assert received[0].sender_name == "unknown"


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

def test_capabilities():
    adapter = TelegramAdapter()
    caps = adapter.capabilities()
    assert caps.supports_edit is True
    assert caps.supports_rich_text is True
    assert caps.supports_attachments is False


# ---------------------------------------------------------------------------
# init validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_init_raises_without_token():
    adapter = TelegramAdapter()
    with pytest.raises(ValueError, match="bot_token"):
        await adapter.init(TelegramConfig(enabled=True, bot_token=None))


# ---------------------------------------------------------------------------
# send_message / edit_message (mocked bot)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_message_returns_message_id():
    adapter = await _make_initialised_adapter()

    mock_sent = MagicMock()
    mock_sent.message_id = 77
    adapter._bot = AsyncMock()
    adapter._bot.send_message = AsyncMock(return_value=mock_sent)

    msg_id = await adapter.send_message("100", OutgoingMessage(text="hi"))
    assert msg_id == "77"
    adapter._bot.send_message.assert_called_once_with(chat_id=100, text="hi")


@pytest.mark.asyncio
async def test_edit_message_calls_bot():
    adapter = await _make_initialised_adapter()
    adapter._bot = AsyncMock()
    adapter._bot.edit_message_text = AsyncMock()

    await adapter.edit_message("100", "77", OutgoingMessage(text="updated"))
    adapter._bot.edit_message_text.assert_called_once_with(
        chat_id=100, message_id=77, text="updated"
    )


@pytest.mark.asyncio
async def test_send_message_raises_when_not_initialised():
    adapter = TelegramAdapter()
    with pytest.raises(RuntimeError, match="not initialised"):
        await adapter.send_message("1", OutgoingMessage(text="x"))


@pytest.mark.asyncio
async def test_edit_message_raises_when_not_initialised():
    adapter = TelegramAdapter()
    with pytest.raises(RuntimeError, match="not initialised"):
        await adapter.edit_message("1", "2", OutgoingMessage(text="x"))


# ---------------------------------------------------------------------------
# stop() — graceful shutdown
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_cancels_polling_task():
    adapter = await _make_initialised_adapter()

    # Simulate a running polling task
    async def _forever():
        await asyncio.sleep(9999)

    adapter._polling_task = asyncio.create_task(_forever())
    adapter._dp = MagicMock()
    adapter._dp.stop_polling = AsyncMock()
    adapter._bot = MagicMock()
    adapter._bot.session = AsyncMock()
    adapter._bot.session.close = AsyncMock()

    await adapter.stop()

    assert adapter._polling_task is None
    adapter._dp.stop_polling.assert_called_once()
    adapter._bot.session.close.assert_called_once()
