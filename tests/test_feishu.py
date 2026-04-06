# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for FeishuAdapter.

All tests use mocks — no real Feishu credentials required.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.channels.feishu import FeishuAdapter, _build_content
from src.config import FeishuConfig
from src.types import ChannelCapabilities, IncomingMessage, OutgoingMessage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_feishu_event(
    message_id: str = "om_abc123",
    chat_id: str = "oc_chat001",
    open_id: str = "ou_user001",
    text: str = "hello feishu",
    create_time: str = "1704067200000",
) -> MagicMock:
    """Build a minimal P2ImMessageReceiveV1 mock."""
    event = MagicMock()

    msg = MagicMock()
    msg.message_id = message_id
    msg.chat_id = chat_id
    msg.content = json.dumps({"text": text})
    msg.create_time = create_time

    sender_id = MagicMock()
    sender_id.open_id = open_id

    sender = MagicMock()
    sender.sender_id = sender_id

    event.event = MagicMock()
    event.event.message = msg
    event.event.sender = sender

    return event


async def _make_initialised_adapter(
    app_id: str = "cli_fake",
    app_secret: str = "secret_fake",
) -> FeishuAdapter:
    """Return a FeishuAdapter with lark SDK mocked out."""
    adapter = FeishuAdapter()
    config = FeishuConfig(enabled=True, app_id=app_id, app_secret=app_secret)

    with (
        patch("src.channels.feishu.lark.Client") as MockClient,
        patch("src.channels.feishu.lark.ws.Client"),
        patch("src.channels.feishu.lark.EventDispatcherHandler") as MockHandler,
    ):
        mock_builder = MagicMock()
        mock_builder.app_id.return_value = mock_builder
        mock_builder.app_secret.return_value = mock_builder
        mock_builder.build.return_value = MagicMock()
        MockClient.builder.return_value = mock_builder

        mock_handler_builder = MagicMock()
        mock_handler_builder.register_p2_im_message_receive_v1.return_value = mock_handler_builder
        mock_handler_builder.build.return_value = MagicMock()
        MockHandler.builder.return_value = mock_handler_builder

        await adapter.init(config)

    return adapter


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

def test_capabilities():
    adapter = FeishuAdapter()
    caps = adapter.capabilities()
    assert caps.supports_edit is True
    assert caps.supports_rich_text is True
    assert caps.supports_attachments is False


# ---------------------------------------------------------------------------
# init validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_init_raises_without_app_id():
    adapter = FeishuAdapter()
    with pytest.raises(ValueError, match="app_id"):
        await adapter.init(FeishuConfig(enabled=True, app_id=None, app_secret="s"))


@pytest.mark.asyncio
async def test_init_raises_without_app_secret():
    adapter = FeishuAdapter()
    with pytest.raises(ValueError, match="app_secret"):
        await adapter.init(FeishuConfig(enabled=True, app_id="id", app_secret=None))


# ---------------------------------------------------------------------------
# IncomingMessage parsing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_incoming_message_parsing():
    """_on_feishu_message must map Feishu event fields to IncomingMessage correctly."""
    adapter = await _make_initialised_adapter()
    adapter._loop = asyncio.get_running_loop()

    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        received.append(msg)

    adapter.on_message(handler)

    feishu_event = _make_feishu_event(
        message_id="om_abc123",
        chat_id="oc_chat001",
        open_id="ou_user001",
        text="hello feishu",
        create_time="1704067200000",
    )

    # Call the sync handler directly (simulates ws thread)
    adapter._on_feishu_message(feishu_event)

    # Give the coroutine a chance to run
    await asyncio.sleep(0.05)

    assert len(received) == 1
    m = received[0]
    assert m.channel == "feishu"
    assert m.message_id == "om_abc123"
    assert m.chat_id == "oc_chat001"
    assert m.sender_id == "ou_user001"
    assert m.text == "hello feishu"
    assert m.timestamp == 1704067200  # ms → seconds
    assert m.raw is feishu_event


@pytest.mark.asyncio
async def test_incoming_message_no_handler_is_noop():
    """No handler registered → _on_feishu_message must not raise."""
    adapter = await _make_initialised_adapter()
    adapter._loop = asyncio.get_running_loop()
    event = _make_feishu_event()
    adapter._on_feishu_message(event)  # should not raise


@pytest.mark.asyncio
async def test_incoming_message_malformed_content():
    """Non-JSON content falls back to raw string."""
    adapter = await _make_initialised_adapter()
    adapter._loop = asyncio.get_running_loop()

    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        received.append(msg)

    adapter.on_message(handler)

    event = _make_feishu_event(text="ignored")
    event.event.message.content = "not-json"

    adapter._on_feishu_message(event)
    await asyncio.sleep(0.05)

    assert received[0].text == "not-json"


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_message_returns_message_id():
    adapter = await _make_initialised_adapter()

    mock_response = MagicMock()
    mock_response.success.return_value = True
    mock_response.data = MagicMock()
    mock_response.data.message_id = "om_sent001"

    adapter._client = MagicMock()
    adapter._client.im.v1.message.acreate = AsyncMock(return_value=mock_response)

    msg_id = await adapter.send_message("oc_chat001", OutgoingMessage(text="hi"))
    assert msg_id == "om_sent001"
    adapter._client.im.v1.message.acreate.assert_called_once()


@pytest.mark.asyncio
async def test_send_message_raises_on_api_error():
    adapter = await _make_initialised_adapter()

    mock_response = MagicMock()
    mock_response.success.return_value = False
    mock_response.code = 99991663
    mock_response.msg = "app not installed"

    adapter._client = MagicMock()
    adapter._client.im.v1.message.acreate = AsyncMock(return_value=mock_response)

    with pytest.raises(RuntimeError, match="send_message failed"):
        await adapter.send_message("oc_chat001", OutgoingMessage(text="hi"))


@pytest.mark.asyncio
async def test_send_message_raises_when_not_initialised():
    adapter = FeishuAdapter()
    with pytest.raises(RuntimeError, match="not initialised"):
        await adapter.send_message("oc_chat001", OutgoingMessage(text="x"))


# ---------------------------------------------------------------------------
# edit_message
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_edit_message_calls_patch_api():
    adapter = await _make_initialised_adapter()

    mock_response = MagicMock()
    mock_response.success.return_value = True

    adapter._client = MagicMock()
    adapter._client.im.v1.message.apatch = AsyncMock(return_value=mock_response)

    await adapter.edit_message("oc_chat001", "om_msg001", OutgoingMessage(text="updated"))
    adapter._client.im.v1.message.apatch.assert_called_once()


@pytest.mark.asyncio
async def test_edit_message_raises_on_api_error():
    adapter = await _make_initialised_adapter()

    mock_response = MagicMock()
    mock_response.success.return_value = False
    mock_response.code = 230002
    mock_response.msg = "message not found"

    adapter._client = MagicMock()
    adapter._client.im.v1.message.apatch = AsyncMock(return_value=mock_response)

    with pytest.raises(RuntimeError, match="edit_message failed"):
        await adapter.edit_message("oc_chat001", "om_msg001", OutgoingMessage(text="x"))


@pytest.mark.asyncio
async def test_edit_message_raises_when_not_initialised():
    adapter = FeishuAdapter()
    with pytest.raises(RuntimeError, match="not initialised"):
        await adapter.edit_message("oc_chat001", "om_msg001", OutgoingMessage(text="x"))


# ---------------------------------------------------------------------------
# _build_content helper
# ---------------------------------------------------------------------------

def test_build_content_text():
    # Plain text is wrapped in an Interactive Card so that subsequent
    # edit_message() calls (which PATCH with card content) work correctly.
    # Feishu does not allow patching a plain-text message into a card.
    msg_type, content = _build_content(OutgoingMessage(text="hello"))
    assert msg_type == "interactive"
    card = json.loads(content)
    assert card["elements"][0]["content"] == "hello"


def test_build_content_rich_text():
    card = {"config": {}, "elements": []}
    msg_type, content = _build_content(OutgoingMessage(rich_text=card))
    assert msg_type == "interactive"
    assert json.loads(content) == card


def test_build_content_empty_text():
    msg_type, content = _build_content(OutgoingMessage())
    assert msg_type == "interactive"
    card = json.loads(content)
    assert card["elements"][0]["content"] == ""


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_launches_thread():
    adapter = await _make_initialised_adapter()

    with patch.object(adapter._ws_client, "start") as mock_start:
        await adapter.start()
        # Give thread a moment to start
        await asyncio.sleep(0.05)
        assert adapter._ws_thread is not None
        assert adapter._loop is not None


@pytest.mark.asyncio
async def test_stop_clears_references():
    adapter = await _make_initialised_adapter()
    await adapter.stop()
    assert adapter._ws_client is None
    assert adapter._ws_thread is None
