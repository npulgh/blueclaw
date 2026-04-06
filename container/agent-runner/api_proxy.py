# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later
# LynxClaw - AI Coding Agent Framework
# Copyright (C) 2026
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Lightweight proxy that intercepts Claude Code CLI model validation requests.

The Claude Code CLI calls GET /v1/models/{model_id}?beta=true before sending
any prompt. Some third-party providers don't implement this endpoint,
causing the CLI to abort with "model not found" before making any real API call.

This proxy:
- Intercepts GET /v1/models/* → returns a fake 200 response
- Passes all other requests (POST /v1/messages, etc.) to the real upstream

Usage (set as ANTHROPIC_BASE_URL):
    python api_proxy.py --upstream https://api.example.com/v1 --port 9099
"""

from __future__ import annotations

import argparse
import json
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer


def make_handler(upstream: str) -> type:
    class ProxyHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # silence access logs

        def do_GET(self):
            # Intercept model validation: GET /v1/models/{id}?beta=true
            if "/v1/models/" in self.path:
                model_id = self.path.split("/v1/models/")[1].split("?")[0]
                fake = {
                    "id": model_id,
                    "type": "model",
                    "display_name": model_id,
                    "created_at": "2025-01-01T00:00:00Z",
                }
                body = json.dumps(fake).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._proxy("GET")

        def do_POST(self):
            self._proxy("POST")

        def _proxy(self, method: str):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else None

            # The CLI sends absolute paths like /v1/messages, /v1/models/...
            # Smart stripping: only remove the /v1 prefix from the request path
            # when the upstream URL already contains a version prefix (e.g. /v1, /v4).
            # This prevents double-versioning when upstream already has a version suffix:
            #   upstream=https://api.example.com/coding/v1  + /v1/messages → strip → /messages ✓
            # While preserving /v1 when upstream has no version suffix:
            #   upstream=https://api.example.com/api/proxy + /v1/messages → keep → /v1/messages ✓
            import re, sys
            has_version_suffix = bool(re.search(r'/v\d+/?$', upstream.rstrip('/')))
            path = self.path
            if has_version_suffix and path.startswith("/v1"):
                path = path[3:]  # strip /v1 prefix, keep /messages etc.

            url = upstream.rstrip("/") + path
            headers = {
                k: v for k, v in self.headers.items()
                if k.lower() not in ("host", "content-length", "transfer-encoding")
            }

            print(f"[PROXY] {method} {url}", file=sys.stderr)

            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req) as resp:
                    resp_body = resp.read()
                    print(f"[PROXY] Response: {resp.status}", file=sys.stderr)
                    self.send_response(resp.status)
                    for k, v in resp.headers.items():
                        if k.lower() not in ("transfer-encoding", "connection"):
                            self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(resp_body)
            except urllib.error.HTTPError as e:
                resp_body = e.read()
                print(f"[PROXY] HTTP Error {e.code}: {resp_body[:500]}", file=sys.stderr)
                self.send_response(e.code)
                for k, v in e.headers.items():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp_body)

    return ProxyHandler


def start_proxy(upstream: str, port: int) -> None:
    handler = make_handler(upstream)
    server = HTTPServer(("127.0.0.1", port), handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    # Wait until the proxy is actually accepting connections
    import socket, time
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--port", type=int, default=9099)
    args = parser.parse_args()
    print(f"Proxy listening on http://127.0.0.1:{args.port} → {args.upstream}")
    handler = make_handler(args.upstream)
    HTTPServer(("127.0.0.1", args.port), handler).serve_forever()
