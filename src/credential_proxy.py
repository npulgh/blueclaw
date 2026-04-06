# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Lynxclaw Credential Proxy — API key never enters containers (ADR-006).

Runs on the host as a daemon thread. Intercepts agent HTTP requests,
injects the real API key, and forwards to the upstream endpoint.

Architecture::

    Container (no API key)
        -> ANTHROPIC_BASE_URL=http://host.docker.internal:<port>
            -> CredentialProxy (host, this module)
                -> injects x-api-key header
                -> forwards to real upstream (api.anthropic.com or mirror)
"""

from __future__ import annotations

import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

DEFAULT_PORT = 3001

# Headers that should be stripped from incoming requests before forwarding.
# The proxy injects the real credentials — any container-supplied auth is removed.
_STRIP_HEADERS = frozenset({"x-api-key", "authorization"})


class CredentialProxy:
    """HTTP proxy that injects API credentials into forwarded requests."""

    def __init__(
        self,
        upstream: str,
        api_key: str,
        port: int = DEFAULT_PORT,
        auth_token: str = "",
    ) -> None:
        self._upstream = upstream.rstrip("/")
        self._api_key = api_key
        self._port = port
        self._auth_token = auth_token
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self._port

    @property
    def base_url_for_container(self) -> str:
        """URL that containers should use as ANTHROPIC_BASE_URL."""
        return f"http://host.docker.internal:{self._port}"

    def start(self) -> None:
        """Start the proxy in a daemon thread. Blocks until port is ready."""
        upstream = self._upstream
        api_key = self._api_key
        auth_token = self._auth_token

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass  # silence default access logs

            def do_GET(self):
                self._forward("GET")

            def do_POST(self):
                self._forward("POST")

            def do_PUT(self):
                self._forward("PUT")

            def do_DELETE(self):
                self._forward("DELETE")

            def _forward(self, method: str):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else None

                url = upstream + self.path
                headers = {
                    k: v for k, v in self.headers.items()
                    if k.lower() not in _STRIP_HEADERS
                    and k.lower() not in ("host", "content-length", "transfer-encoding")
                }
                # Inject real credentials
                headers["x-api-key"] = api_key
                if auth_token:
                    headers["Authorization"] = f"Bearer {auth_token}"

                req = urllib.request.Request(
                    url, data=body, headers=headers, method=method,
                )
                try:
                    with urllib.request.urlopen(req) as resp:
                        resp_body = resp.read()
                        self.send_response(resp.status)
                        for k, v in resp.headers.items():
                            if k.lower() not in ("transfer-encoding", "connection"):
                                self.send_header(k, v)
                        self.end_headers()
                        self.wfile.write(resp_body)
                except urllib.error.HTTPError as e:
                    resp_body = e.read()
                    self.send_response(e.code)
                    for k, v in e.headers.items():
                        if k.lower() not in ("transfer-encoding", "connection"):
                            self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(resp_body)
                except Exception as exc:
                    log.warning("credential_proxy.forward_error", error=str(exc))
                    self.send_error(502, f"Proxy error: {exc}")

        self._server = HTTPServer(("0.0.0.0", self._port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
        )
        self._thread.start()

        # Wait until port is accepting connections
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self._port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)

        log.info(
            "credential_proxy.started",
            port=self._port,
            upstream=self._upstream,
        )

    def stop(self) -> None:
        """Shutdown the proxy server."""
        if self._server:
            self._server.shutdown()
            self._server = None
            self._thread = None
            log.info("credential_proxy.stopped")
