# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for WebhookServer (T4.5).

Uses FastAPI TestClient (httpx) — no real network required.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.server import WebhookServer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def server() -> WebhookServer:
    return WebhookServer()


@pytest.fixture
def client(server: WebhookServer) -> TestClient:
    return TestClient(server.app)


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

def test_health_returns_200(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Telegram webhook endpoint
# ---------------------------------------------------------------------------

def test_telegram_endpoint_not_registered_without_setup(client: TestClient) -> None:
    """Before setup_telegram(), the route doesn't exist."""
    resp = client.post("/webhook/telegram", json={})
    assert resp.status_code == 404


def test_telegram_endpoint_processes_update(server: WebhookServer) -> None:
    """setup_telegram() registers the route and feeds updates to the adapter."""
    mock_adapter = MagicMock()
    mock_adapter.feed_webhook_update = AsyncMock()
    server.setup_telegram(mock_adapter)

    update = {
        "update_id": 1,
        "message": {
            "message_id": 42,
            "chat": {"id": 100, "type": "private"},
            "from": {"id": 7, "is_bot": False, "first_name": "Alice"},
            "text": "hello",
            "date": 1700000000,
        },
    }

    with TestClient(server.app) as c:
        resp = c.post("/webhook/telegram", json=update)

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    mock_adapter.feed_webhook_update.assert_awaited_once_with(update)


def test_telegram_endpoint_returns_503_when_not_configured() -> None:
    """If setup_telegram() was never called, return 503."""
    server = WebhookServer()
    # Manually add the route without setting _telegram_adapter
    from fastapi import Request
    from fastapi.responses import JSONResponse

    # Patch the internal adapter to None explicitly (it already is, but be explicit)
    server._telegram_adapter = None
    # Register the route as if setup was called
    server.app.add_api_route("/webhook/telegram", server._handle_telegram, methods=["POST"])

    with TestClient(server.app) as c:
        resp = c.post("/webhook/telegram", json={})
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Feishu webhook endpoint
# ---------------------------------------------------------------------------

def test_feishu_endpoint_not_registered_without_setup(client: TestClient) -> None:
    resp = client.post("/webhook/feishu", json={})
    assert resp.status_code == 404


def test_feishu_challenge_verification(server: WebhookServer) -> None:
    """Feishu URL verification challenge must be echoed back."""
    mock_adapter = MagicMock()
    mock_adapter.feed_webhook_event = AsyncMock()
    server.setup_feishu(mock_adapter)

    challenge_payload = {
        "type": "url_verification",
        "challenge": "abc123xyz",
        "token": "some_token",
    }

    with TestClient(server.app) as c:
        resp = c.post("/webhook/feishu", json=challenge_payload)

    assert resp.status_code == 200
    assert resp.json() == {"challenge": "abc123xyz"}
    # feed_webhook_event should NOT be called for challenge requests
    mock_adapter.feed_webhook_event.assert_not_awaited()


def test_feishu_event_callback_processed(server: WebhookServer) -> None:
    """Regular Feishu event callbacks are forwarded to the adapter."""
    mock_adapter = MagicMock()
    mock_adapter.feed_webhook_event = AsyncMock()
    server.setup_feishu(mock_adapter)

    event_payload = {
        "schema": "2.0",
        "header": {
            "event_id": "evt_001",
            "event_type": "im.message.receive_v1",
            "app_id": "cli_test",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_abc"}},
            "message": {
                "message_id": "om_001",
                "chat_id": "oc_chat1",
                "content": json.dumps({"text": "hello feishu"}),
                "create_time": "1700000000000",
            },
        },
    }

    with TestClient(server.app) as c:
        resp = c.post("/webhook/feishu", json=event_payload)

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    mock_adapter.feed_webhook_event.assert_awaited_once_with(event_payload)
