# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ChannelAdapter ABC and ChannelRegistry.

Uses a MockAdapter to verify register/lookup and start/stop lifecycle.
"""

from __future__ import annotations

import pytest

from src.channels.registry import ChannelAdapter, ChannelRegistry
from src.types import ChannelCapabilities, IncomingMessage, OutgoingMessage


# ---------------------------------------------------------------------------
# MockAdapter — minimal concrete implementation of ChannelAdapter
# ---------------------------------------------------------------------------

class MockAdapter(ChannelAdapter):
    """Concrete adapter for testing; records calls in order lists."""

    def __init__(self, name: str = "mock") -> None:
        self.name = name
        self.calls: list[str] = []
        self._handler = None

    async def init(self, config) -> None:
        self.calls.append("init")

    async def start(self) -> None:
        self.calls.append("start")

    async def stop(self) -> None:
        self.calls.append("stop")

    def on_message(self, handler) -> None:
        self._handler = handler
        self.calls.append("on_message")

    async def send_message(self, chat_id: str, content: OutgoingMessage) -> str:
        self.calls.append("send_message")
        return "mock-msg-id"

    async def edit_message(self, chat_id: str, msg_id: str, content: OutgoingMessage) -> None:
        self.calls.append("edit_message")

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            supports_edit=True,
            supports_rich_text=False,
            supports_attachments=False,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_register_and_lookup():
    registry = ChannelRegistry()
    adapter = MockAdapter("tg")
    registry.register("telegram", adapter)
    assert registry.get("telegram") is adapter


def test_lookup_missing_raises():
    registry = ChannelRegistry()
    with pytest.raises(KeyError, match="feishu"):
        registry.get("feishu")


def test_duplicate_register_raises():
    registry = ChannelRegistry()
    registry.register("telegram", MockAdapter())
    with pytest.raises(ValueError, match="already registered"):
        registry.register("telegram", MockAdapter())


def test_names_returns_insertion_order():
    registry = ChannelRegistry()
    registry.register("telegram", MockAdapter())
    registry.register("feishu", MockAdapter())
    assert registry.names == ["telegram", "feishu"]


@pytest.mark.asyncio
async def test_start_all_calls_start_on_each():
    registry = ChannelRegistry()
    a = MockAdapter("a")
    b = MockAdapter("b")
    registry.register("a", a)
    registry.register("b", b)

    await registry.start_all()

    assert "start" in a.calls
    assert "start" in b.calls


@pytest.mark.asyncio
async def test_stop_all_calls_stop_on_each():
    registry = ChannelRegistry()
    a = MockAdapter("a")
    b = MockAdapter("b")
    registry.register("a", a)
    registry.register("b", b)

    await registry.stop_all()

    assert "stop" in a.calls
    assert "stop" in b.calls


@pytest.mark.asyncio
async def test_stop_all_reverse_order():
    """stop_all must stop adapters in reverse registration order."""
    registry = ChannelRegistry()
    stop_order: list[str] = []

    class TrackingAdapter(MockAdapter):
        async def stop(self) -> None:
            stop_order.append(self.name)
            await super().stop()

    a = TrackingAdapter("a")
    b = TrackingAdapter("b")
    c = TrackingAdapter("c")
    registry.register("a", a)
    registry.register("b", b)
    registry.register("c", c)

    await registry.stop_all()

    assert stop_order == ["c", "b", "a"]


@pytest.mark.asyncio
async def test_full_lifecycle():
    """register → start_all → stop_all in sequence."""
    registry = ChannelRegistry()
    adapter = MockAdapter()
    registry.register("mock", adapter)

    await registry.start_all()
    await registry.stop_all()

    assert adapter.calls == ["start", "stop"]


@pytest.mark.asyncio
async def test_send_message_returns_id():
    adapter = MockAdapter()
    msg_id = await adapter.send_message("chat-1", OutgoingMessage(text="hello"))
    assert msg_id == "mock-msg-id"


def test_capabilities():
    adapter = MockAdapter()
    caps = adapter.capabilities()
    assert caps.supports_edit is True
    assert caps.supports_rich_text is False
    assert caps.supports_attachments is False
