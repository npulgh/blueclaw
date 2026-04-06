# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for Dashboard API endpoints."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.dashboard.api import router
from src.dashboard import auth as auth_mod


@pytest.fixture
async def db(tmp_path):
    from src.db import Database
    database = Database()
    await database.init(str(tmp_path / "test.db"))
    yield database
    await database.close()


@pytest.fixture
async def client(db, monkeypatch):
    """TestClient with token patched and db/config injected into app.state.

    NOTE: must be async because it depends on the async `db` fixture.
    """
    monkeypatch.setattr(auth_mod, "_DASHBOARD_TOKEN", "test-token")

    from src.config import Config
    app = FastAPI()
    app.include_router(router)
    app.state.db = db
    app.state.config = Config(anthropic_api_key="dummy")

    return TestClient(app, raise_server_exceptions=False)


AUTH = {"Authorization": "Bearer test-token"}


# --- /api/system/overview ---

async def test_overview_shape(client):
    resp = client.get("/api/system/overview", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert "groups_count" in data
    assert "messages_today" in data
    assert "total_input_tokens" in data
    assert "total_output_tokens" in data
    assert "active_containers" in data


# --- /api/groups ---

async def test_groups_shape(client):
    resp = client.get("/api/groups", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data


# --- /api/messages ---

async def test_messages_pagination(client, db):
    for i in range(5):
        await db.insert_message(
            channel="telegram", chat_id="c1", message_id=f"m{i}",
            sender_id="u1", content=f"msg{i}", direction="inbound",
        )
    resp = client.get("/api/messages?limit=3&offset=0", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 3
    assert data["total"] >= 5

    resp2 = client.get("/api/messages?limit=3&offset=3", headers=AUTH)
    assert resp2.status_code == 200
    assert len(resp2.json()["items"]) >= 2


# --- /api/audit ---

async def test_audit_shape(client):
    resp = client.get("/api/audit", headers=AUTH)
    assert resp.status_code == 200
    assert "items" in resp.json()
    assert "total" in resp.json()


# --- /api/tasks ---

async def test_tasks_shape(client):
    resp = client.get("/api/tasks", headers=AUTH)
    assert resp.status_code == 200
    assert "items" in resp.json()


# --- /api/usage ---

async def test_usage_shape(client):
    resp = client.get("/api/usage", headers=AUTH)
    assert resp.status_code == 200
    assert "items" in resp.json()


# --- /api/config ---

async def test_config_redacts_secrets(client):
    resp = client.get("/api/config", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert data["anthropic_api_key"] == "***"
    assert data["telegram"]["bot_token"] == "***"
    assert data["feishu"]["app_secret"] == "***"
    assert data["feishu"]["app_id"] == "***"


# --- auth rejection ---

async def test_missing_token_rejected(client):
    resp = client.get("/api/system/overview")
    assert resp.status_code == 401
