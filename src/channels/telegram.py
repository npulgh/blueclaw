"""Telegram channel adapter for Lynxclaw.

Uses aiogram v3 with Long Polling mode. Includes a token-bucket rate limiter
for edit_message to stay within Telegram's 30 edits/sec global limit.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message

from src.channels.registry import ChannelAdapter, MessageHandler
from src.config import TelegramConfig
from src.types import ChannelCapabilities, IncomingMessage, OutgoingMessage

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Token bucket rate limiter
# ---------------------------------------------------------------------------

class TokenBucket:
    """Simple token bucket for rate limiting.

    Args:
        rate: Tokens refilled per second.
        capacity: Maximum token capacity (burst size).
    """

    def __init__(self, rate: float, capacity: int) -> None:
        self._rate = rate
        self._capacity = capacity
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()

    async def acquire(self) -> None:
        """Wait until a token is available, then consume one."""
        while True:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(
                self._capacity,
                self._tokens + elapsed * self._rate,
            )
            self._last_refill = now

            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return

            # Sleep until the next token is available
            wait = (1.0 - self._tokens) / self._rate
            await asyncio.sleep(wait)


# ---------------------------------------------------------------------------
# Telegram adapter
# ---------------------------------------------------------------------------

class TelegramAdapter(ChannelAdapter):
    """Telegram channel adapter using aiogram v3 Long Polling.

    Lifecycle::

        adapter = TelegramAdapter()
        await adapter.init(config)
        adapter.on_message(handler)
        await adapter.start()   # begins polling in background
        ...
        await adapter.stop()    # graceful shutdown
    """

    def __init__(self) -> None:
        self._bot: Optional[Bot] = None
        self._dp: Optional[Dispatcher] = None
        self._handler: Optional[MessageHandler] = None
        self._polling_task: Optional[asyncio.Task] = None
        # 30 edits/sec global Telegram limit; capacity=30 allows short bursts
        self._edit_limiter = TokenBucket(rate=30.0, capacity=30)

    async def init(self, config: TelegramConfig) -> None:
        """Create Bot and Dispatcher from config."""
        if not config.bot_token:
            raise ValueError("TelegramConfig.bot_token is required")

        self._bot = Bot(
            token=config.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self._dp = Dispatcher()

        # Register the message handler on the dispatcher
        self._dp.message.register(self._on_aiogram_message)
        logger.info("telegram_adapter_initialized")

    async def start(self) -> None:
        """Start Long Polling in a background asyncio task."""
        if self._bot is None or self._dp is None:
            raise RuntimeError("Call init() before start()")

        self._polling_task = asyncio.create_task(
            self._dp.start_polling(self._bot, handle_signals=False),
            name="telegram-polling",
        )
        logger.info("telegram_polling_started")

    async def stop(self) -> None:
        """Stop polling and close the bot session."""
        if self._dp is not None:
            await self._dp.stop_polling()

        if self._polling_task is not None:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except (asyncio.CancelledError, Exception):
                pass
            self._polling_task = None

        if self._bot is not None:
            await self._bot.session.close()

        logger.info("telegram_polling_stopped")

    def on_message(self, handler: MessageHandler) -> None:
        """Register the callback for incoming messages."""
        self._handler = handler

    async def send_message(self, chat_id: str, content: OutgoingMessage) -> str:
        """Send a text message; returns the Telegram message_id as a string."""
        if self._bot is None:
            raise RuntimeError("Adapter not initialised")

        text = content.text or ""
        sent = await self._bot.send_message(chat_id=int(chat_id), text=text)
        return str(sent.message_id)

    async def edit_message(
        self, chat_id: str, msg_id: str, content: OutgoingMessage
    ) -> None:
        """Edit an existing message (rate-limited to 30 edits/sec)."""
        if self._bot is None:
            raise RuntimeError("Adapter not initialised")

        await self._edit_limiter.acquire()
        text = content.text or ""
        await self._bot.edit_message_text(
            chat_id=int(chat_id),
            message_id=int(msg_id),
            text=text,
        )

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            supports_edit=True,
            supports_rich_text=True,   # HTML parse mode enabled
            supports_attachments=False,
        )

    # ------------------------------------------------------------------
    # Internal aiogram handler
    # ------------------------------------------------------------------

    async def _on_aiogram_message(self, message: Message) -> None:
        """Convert aiogram Message → IncomingMessage and dispatch to handler."""
        if self._handler is None:
            return

        sender = message.from_user
        incoming = IncomingMessage(
            channel="telegram",
            message_id=str(message.message_id),
            chat_id=str(message.chat.id),
            sender_id=str(sender.id) if sender else "unknown",
            sender_name=(
                sender.full_name if sender else "unknown"
            ),
            text=message.text or message.caption or "",
            attachments=[],
            timestamp=int(message.date.timestamp()) if message.date else 0,
            raw=message,
        )

        try:
            await self._handler(incoming)
        except Exception:
            logger.exception("telegram_handler_error", message_id=incoming.message_id)
