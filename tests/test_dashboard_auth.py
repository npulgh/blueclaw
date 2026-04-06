# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for Bearer Token authentication in the dashboard."""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.dashboard.auth import verify_token


def test_missing_token_returns_503(monkeypatch):
    """No LYNXCLAW_DASHBOARD_TOKEN env var → 503."""
    import src.dashboard.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_DASHBOARD_TOKEN", None)

    app = FastAPI()

    @app.get("/t")
    def t(v=Depends(verify_token)):
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/t", headers={"Authorization": "Bearer sometoken"})
    assert resp.status_code == 503


def test_wrong_token_returns_401(monkeypatch):
    """Wrong token → 401."""
    import src.dashboard.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_DASHBOARD_TOKEN", "correct-token")

    app = FastAPI()

    @app.get("/t")
    def t(v=Depends(verify_token)):
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/t", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401


def test_correct_token_returns_200(monkeypatch):
    """Correct token → 200."""
    import src.dashboard.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_DASHBOARD_TOKEN", "my-secret")

    app = FastAPI()

    @app.get("/t")
    def t(v=Depends(verify_token)):
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/t", headers={"Authorization": "Bearer my-secret"})
    assert resp.status_code == 200


def test_no_auth_header_returns_401(monkeypatch):
    """Missing Authorization header → 401."""
    import src.dashboard.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_DASHBOARD_TOKEN", "my-secret")

    app = FastAPI()

    @app.get("/t")
    def t(v=Depends(verify_token)):
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/t")
    assert resp.status_code == 401
