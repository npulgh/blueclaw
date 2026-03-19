"""Feishu (Lark) channel adapter for Lynxclaw.

Uses lark-oapi WebSocket long-connection mode (ADR-003).
The ws.Client.start() is blocking, so it runs in a dedicated thread
bridged back to the asyncio event loop via run_coroutine_threadsafe.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Optional

import lark_oapi as lark
import structlog
from lark_oapi.api.im.v1.model import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

from src.channels.registry import ChannelAdapter, MessageHandler
from src.config import FeishuConfig
from src.types import ChannelCapabilities, IncomingMessage, OutgoingMessage

logger = structlog.get_logger(__name__)


class FeishuAdapter(ChannelAdapter):
    """Feishu channel adapter using lark-oapi WebSocket long-connection.

    Lifecycle::

        adapter = FeishuAdapter()
        await adapter.init(config)
        adapter.on_message(handler)
        await adapter.start()   # begins WS connection in background thread
        ...
        await adapter.stop()    # graceful shutdown
    """

    def __init__(self) -> None:
        self._client: Optional[lark.Client] = None
        self._ws_client: Optional[lark.ws.Client] = None
        self._handler: Optional[MessageHandler] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._mode: str = "websocket"
        self._event_handler: Optional[lark.EventDispatcherHandler] = None

    async def init(self, config: FeishuConfig) -> None:
        """Create lark REST client and configure WebSocket client or webhook handler."""
        if not config.app_id:
            raise ValueError("FeishuConfig.app_id is required")
        if not config.app_secret:
            raise ValueError("FeishuConfig.app_secret is required")

        self._mode = config.mode

        self._client = (
            lark.Client.builder()
            .app_id(config.app_id)
            .app_secret(config.app_secret)
            .build()
        )

        self._event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_feishu_message)
            .build()
        )

        if self._mode == "websocket":
            self._ws_client = lark.ws.Client(
                app_id=config.app_id,
                app_secret=config.app_secret,
                event_handler=self._event_handler,
                log_level=lark.LogLevel.WARNING,
            )

        logger.info("feishu_adapter_initialized", mode=self._mode)

    async def start(self) -> None:
        """Start WebSocket connection (websocket mode) or no-op (webhook mode)."""
        if self._mode == "webhook":
            # Webhook mode: events arrive via feed_webhook_event()
            self._loop = asyncio.get_running_loop()
            logger.info("feishu_webhook_mode_ready")
            return

        if self._ws_client is None:
            raise RuntimeError("Call init() before start()")

        self._loop = asyncio.get_running_loop()

        self._ws_thread = threading.Thread(
            target=self._ws_client.start,
            name="feishu-ws",
            daemon=True,
        )
        self._ws_thread.start()
        logger.info("feishu_ws_started")

    async def stop(self) -> None:
        """Stop the WebSocket thread (daemon thread exits with process)."""
        self._ws_thread = None
        self._ws_client = None
        logger.info("feishu_adapter_stopped")

    async def feed_webhook_event(self, event_data: dict) -> None:
        """Process a raw Feishu event dict received via webhook.

        Called by WebhookServer for each incoming event callback.
        """
        if self._event_handler is None:
            raise RuntimeError("Adapter not initialised")

        # Feishu webhook events have a 'header' + 'event' structure.
        # We extract the message event and dispatch it directly.
        header = event_data.get("header", {})
        event_type = header.get("event_type", "")

        if event_type == "im.message.receive_v1":
            event_body = event_data.get("event", {})
            await self._dispatch_raw_event(event_body)
        else:
            logger.debug("feishu_webhook_unhandled_event", event_type=event_type)

    def on_message(self, handler: MessageHandler) -> None:
        """Register the callback for incoming messages."""
        self._handler = handler

    async def send_message(self, chat_id: str, content: OutgoingMessage) -> str:
        """Send a text or card message; returns the Feishu message_id."""
        if self._client is None:
            raise RuntimeError("Adapter not initialised")

        msg_type, body_content = _build_content(content)

        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(msg_type)
                .content(body_content)
                .build()
            )
            .build()
        )

        response = await self._client.im.v1.message.acreate(request)
        if not response.success():
            raise RuntimeError(
                f"Feishu send_message failed: code={response.code} msg={response.msg}"
            )

        msg_id: str = response.data.message_id
        return msg_id

    async def edit_message(
        self, chat_id: str, msg_id: str, content: OutgoingMessage
    ) -> None:
        """Update an existing message via Interactive Card patch."""
        if self._client is None:
            raise RuntimeError("Adapter not initialised")

        text = content.text or ""
        card_content = json.dumps({
            "config": {"wide_screen_mode": True},
            "elements": [{"tag": "markdown", "content": text}],
        })

        request = (
            PatchMessageRequest.builder()
            .message_id(msg_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(card_content)
                .build()
            )
            .build()
        )

        response = await self._client.im.v1.message.apatch(request)
        if not response.success():
            raise RuntimeError(
                f"Feishu edit_message failed: code={response.code} msg={response.msg}"
            )

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            supports_edit=True,
            supports_rich_text=True,
            supports_attachments=False,
        )

    # ------------------------------------------------------------------
    # Internal lark event handler (called from ws thread)
    # ------------------------------------------------------------------

    def _on_feishu_message(self, event: P2ImMessageReceiveV1) -> None:
        """Convert Feishu event → IncomingMessage and dispatch to handler.

        This runs in the ws thread, so we bridge to asyncio via
        run_coroutine_threadsafe.
        """
        if self._handler is None or self._loop is None:
            return

        try:
            msg_data = event.event.message
            sender = event.event.sender

            raw_content = msg_data.content or "{}"
            try:
                parsed = json.loads(raw_content)
                text = parsed.get("text", "")
            except (json.JSONDecodeError, AttributeError):
                text = raw_content

            sender_id = "unknown"
            sender_name = "unknown"
            if sender and sender.sender_id:
                sender_id = sender.sender_id.open_id or "unknown"

            create_time = getattr(msg_data, "create_time", None)
            timestamp = int(create_time) // 1000 if create_time else 0

            incoming = IncomingMessage(
                channel="feishu",
                message_id=msg_data.message_id or "",
                chat_id=msg_data.chat_id or "",
                sender_id=sender_id,
                sender_name=sender_name,
                text=text,
                attachments=[],
                timestamp=timestamp,
                raw=event,
            )

            asyncio.run_coroutine_threadsafe(
                self._dispatch(incoming), self._loop
            )
        except Exception:
            logger.exception("feishu_handler_error")

    async def _dispatch(self, incoming: IncomingMessage) -> None:
        """Invoke the registered handler; log exceptions without crashing."""
        try:
            await self._handler(incoming)
        except Exception:
            logger.exception(
                "feishu_dispatch_error", message_id=incoming.message_id
            )

    async def _dispatch_raw_event(self, event_body: dict) -> None:
        """Parse a raw Feishu event body dict and dispatch as IncomingMessage."""
        if self._handler is None:
            return

        try:
            message_data = event_body.get("message", {})
            sender_data = event_body.get("sender", {})

            raw_content = message_data.get("content", "{}")
            try:
                parsed = json.loads(raw_content)
                text = parsed.get("text", "")
            except (json.JSONDecodeError, AttributeError):
                text = raw_content

            sender_id_data = sender_data.get("sender_id", {})
            sender_id = sender_id_data.get("open_id", "unknown")

            create_time = message_data.get("create_time")
            timestamp = int(create_time) // 1000 if create_time else 0

            incoming = IncomingMessage(
                channel="feishu",
                message_id=message_data.get("message_id", ""),
                chat_id=message_data.get("chat_id", ""),
                sender_id=sender_id,
                sender_name="unknown",
                text=text,
                attachments=[],
                timestamp=timestamp,
                raw=event_body,
            )

            await self._dispatch(incoming)
        except Exception:
            logger.exception("feishu_webhook_dispatch_error")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_content(content: OutgoingMessage) -> tuple[str, str]:
    """Return (msg_type, content_json) for a Feishu send_message call."""
    if content.rich_text:
        return "interactive", json.dumps(content.rich_text)
    text = content.text or ""
    return "text", json.dumps({"text": text})
