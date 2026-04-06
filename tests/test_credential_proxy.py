# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for src/credential_proxy.py — Credential Proxy (ADR-006)."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from src.credential_proxy import CredentialProxy


# ---------------------------------------------------------------------------
# Fake upstream server — echoes headers back as JSON
# ---------------------------------------------------------------------------

def _start_fake_upstream(port: int) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_POST(self):
            self._echo()

        def do_GET(self):
            self._echo()

        def _echo(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            resp = json.dumps({
                "path": self.path,
                "x_api_key": self.headers.get("x-api-key", ""),
                "authorization": self.headers.get("Authorization", ""),
                "body_len": length,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

    server = HTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_upstream():
    server = _start_fake_upstream(19876)
    yield "http://127.0.0.1:19876"
    server.shutdown()


@pytest.fixture
def proxy(fake_upstream):
    p = CredentialProxy(
        upstream=fake_upstream,
        api_key="sk-real-secret-key",
        port=19877,
    )
    p.start()
    yield p
    p.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCredentialProxy:
    def test_injects_api_key(self, proxy):
        """Proxy injects x-api-key header into forwarded requests."""
        url = f"http://127.0.0.1:{proxy.port}/v1/messages"
        req = urllib.request.Request(url, data=b'{"prompt":"hi"}', method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        assert data["x_api_key"] == "sk-real-secret-key"

    def test_strips_incoming_auth(self, proxy):
        """Proxy strips any auth header the container might send."""
        url = f"http://127.0.0.1:{proxy.port}/v1/messages"
        req = urllib.request.Request(url, data=b'{}', method="POST")
        req.add_header("x-api-key", "sk-placeholder-from-container")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        assert data["x_api_key"] == "sk-real-secret-key"

    def test_preserves_path(self, proxy):
        """Request path is forwarded to upstream unchanged."""
        url = f"http://127.0.0.1:{proxy.port}/v1/messages?beta=true"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        assert "/v1/messages" in data["path"]

    def test_port_property(self, proxy):
        assert proxy.port == 19877

    def test_base_url_for_container(self, proxy):
        """base_url_for_container returns URL with host.docker.internal."""
        url = proxy.base_url_for_container
        assert "host.docker.internal" in url
        assert str(proxy.port) in url
