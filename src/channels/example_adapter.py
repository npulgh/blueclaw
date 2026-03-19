"""Example channel adapter for Lynxclaw — template for third-party developers.

This adapter implements a simple "echo" channel: every incoming message is
immediately echoed back to the same chat_id.  It has no external dependencies
and is designed to be read as a reference implementation.

Copy this file, rename the class, and replace the echo logic with your
platform's SDK calls.  See docs/channel-development.md for the full guide.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

import structlog

from src.channels.registry import ChannelAdapter, MessageHandler
from src.types import ChannelCapabilities, IncomingMessage, OutgoingMessage

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Config dataclass — one per adapter
# ---------------------------------------------------------------------------

@dataclass
class ExampleConfig:
    """Configuration for the example (echo) adapter.

    In a real adapter this would hold credentials, endpoint URLs, etc.
    Add it to src/config.py alongside TelegramConfig / FeishuConfig.
    """

    enabled: bool = False
    # A human-readable name shown in log lines.
    channel_name: str = "example"


# ---------------------------------------------------------------------------
# Adapter implementation
# ---------------------------------------------------------------------------

class ExampleAdapter(ChannelAdapter):
    """Echo adapter — mirrors every incoming message back to the sender.

    Lifecycle (same for every adapter)::

        adapter = ExampleAdapter()
        await adapter.init(config)      # validate config, create SDK clients
        adapter.on_message(handler)     # wire the host's message handler
        await adapter.start()           # begin receiving messages
        ...
        await adapter.stop()            # graceful shutdown

    The host (main.py) calls these methods in exactly this order.
    """

    def __init__(self) -> None:
        # _handler is set by on_message(); it is None until the host wires it.
        self._handler: Optional[MessageHandler] = None
        self._config: Optional[ExampleConfig] = None
        # _running gates the background polling loop.
        self._running: bool = False
        self._poll_task: Optional[asyncio.Task] = None
        # Internal message counter — used to generate unique message IDs.
        self._msg_counter: int = 0

    # ------------------------------------------------------------------
    # ChannelAdapter interface
    # ------------------------------------------------------------------

    async def init(self, config: ExampleConfig) -> None:
        """Validate config and initialise any SDK clients.

        Contract:
        - Called once before start().
        - Raise ValueError for missing required credentials.
        - Do NOT start background tasks here; that belongs in start().

        Args:
            config: Adapter-specific config dataclass.
        """
        # Validate required fields (add your credential checks here).
        if not config.channel_name:
            raise ValueError("ExampleConfig.channel_name must not be empty")

        self._config = config
        logger.info("example_adapter_initialized", channel=config.channel_name)

    async def start(self) -> None:
        """Begin receiving messages.

        Contract:
        - Called after init() and on_message().
        - Must not block; start background tasks / threads here.
        - Raise RuntimeError if init() was not called first.

        For a real adapter this is where you'd start a polling loop,
        open a WebSocket, or register a webhook endpoint.
        """
        if self._config is None:
            raise RuntimeError("Call init() before start()")

        self._running = True
        # The echo adapter has no real network connection, so we just log.
        # A real adapter would open a connection here.
        logger.info("example_adapter_started", channel=self._config.channel_name)

    async def stop(self) -> None:
        """Gracefully stop the adapter and release resources.

        Contract:
        - Must be idempotent (safe to call multiple times).
        - Cancel background tasks, close SDK clients, flush buffers.
        - Should not raise; log exceptions instead.
        """
        self._running = False

        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None

        logger.info("example_adapter_stopped")

    def on_message(self, handler: MessageHandler) -> None:
        """Register the callback the host uses to receive incoming messages.

        Contract:
        - Called once by the host after init(), before start().
        - Store the handler; invoke it for every incoming message.
        - The handler is an async coroutine: ``await handler(incoming)``.

        Args:
            handler: Async callable ``(IncomingMessage) -> None``.
        """
        self._handler = handler

    async def send_message(self, chat_id: str, content: OutgoingMessage) -> str:
        """Send a message to a chat and return the platform message ID.

        Contract:
        - Must return a non-empty string that uniquely identifies the sent
          message on the platform (used later by edit_message).
        - Raise RuntimeError if the adapter is not initialised.
        - Raise an appropriate exception on network / API errors.

        Args:
            chat_id: Platform-specific conversation identifier.
            content: Message payload (text and/or rich_text).

        Returns:
            Platform-assigned message ID as a string.
        """
        if not self._running:
            raise RuntimeError("Adapter not started")

        self._msg_counter += 1
        msg_id = f"example-{self._msg_counter}"
        text = content.text or ""

        # In a real adapter you'd call your platform SDK here, e.g.:
        #   response = await self._client.send(chat_id=chat_id, text=text)
        #   return str(response.message_id)
        logger.info(
            "example_adapter.send",
            chat_id=chat_id,
            msg_id=msg_id,
            text_preview=text[:80],
        )
        return msg_id

    async def edit_message(
        self, chat_id: str, msg_id: str, content: OutgoingMessage
    ) -> None:
        """Edit an already-sent message (used for streaming updates).

        Contract:
        - Called repeatedly during streaming to update the placeholder message.
        - If the platform does not support editing, raise NotImplementedError
          and set capabilities().supports_edit = False so the host falls back
          to sending a new message.
        - Raise RuntimeError if the adapter is not initialised.

        Args:
            chat_id: Platform-specific conversation identifier.
            msg_id:  The message ID returned by a previous send_message() call.
            content: New message payload.
        """
        if not self._running:
            raise RuntimeError("Adapter not started")

        text = content.text or ""
        logger.info(
            "example_adapter.edit",
            chat_id=chat_id,
            msg_id=msg_id,
            text_preview=text[:80],
        )
        # Real adapter: await self._client.edit(msg_id=msg_id, text=text)

    def capabilities(self) -> ChannelCapabilities:
        """Declare which optional features this adapter supports.

        The host uses this to decide whether to call edit_message() for
        streaming or fall back to sending a new message each time.

        Returns:
            ChannelCapabilities dataclass.
        """
        return ChannelCapabilities(
            supports_edit=True,
            supports_rich_text=False,   # echo adapter only handles plain text
            supports_attachments=False,
        )

    # ------------------------------------------------------------------
    # Public helper — simulate an incoming message (useful in tests)
    # ------------------------------------------------------------------

    async def simulate_incoming(
        self,
        text: str,
        chat_id: str = "test-chat",
        sender_id: str = "user-1",
        sender_name: str = "Test User",
    ) -> None:
        """Inject a synthetic IncomingMessage and dispatch it to the handler.

        This is NOT part of the ChannelAdapter ABC.  It exists so tests and
        integration scripts can drive the adapter without a real network
        connection.

        Args:
            text:        Message text.
            chat_id:     Conversation identifier (default: "test-chat").
            sender_id:   Sender identifier (default: "user-1").
            sender_name: Display name (default: "Test User").
        """
        if self._handler is None:
            raise RuntimeError("No handler registered — call on_message() first")

        self._msg_counter += 1
        incoming = IncomingMessage(
            channel="example",
            message_id=f"sim-{self._msg_counter}",
            chat_id=chat_id,
            sender_id=sender_id,
            sender_name=sender_name,
            text=text,
            attachments=[],
            timestamp=int(time.time()),
            raw=None,
        )

        try:
            await self._handler(incoming)
        except Exception:
            logger.exception(
                "example_adapter.dispatch_error",
                message_id=incoming.message_id,
            )
