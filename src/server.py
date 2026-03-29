# LynxClaw - AI Coding Agent Framework
# Copyright (C) 2026
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Lynxclaw Webhook HTTP Server (optional mode).

Provides FastAPI endpoints for Telegram and Feishu webhook callbacks.
Enabled when config mode is 'webhook' instead of the default long-connection modes.

Endpoints:
  POST /webhook/telegram  — Telegram update callbacks
  POST /webhook/feishu    — Feishu event callbacks (with challenge verification)
  GET  /health            — Health check
  GET  /metrics           — Prometheus metrics redirect
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import structlog
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

if TYPE_CHECKING:
    from src.channels.feishu import FeishuAdapter
    from src.channels.telegram import TelegramAdapter

log = structlog.get_logger(__name__)


class WebhookServer:
    """FastAPI-based webhook server for Telegram and Feishu callbacks.

    Usage::

        server = WebhookServer()
        server.setup_telegram(bot, dp)
        server.setup_feishu(feishu_adapter)
        await server.start(host="0.0.0.0", port=8080)
        ...
        await server.stop()
    """

    def __init__(self) -> None:
        self.app = FastAPI(title="Lynxclaw Webhook", docs_url=None, redoc_url=None)
        self._server: Optional[uvicorn.Server] = None
        self._serve_task: Optional[asyncio.Task] = None
        self._telegram_adapter: Optional["TelegramAdapter"] = None
        self._feishu_adapter: Optional["FeishuAdapter"] = None
        self._db: Any = None
        self._config: Any = None

        # Register built-in routes
        self.app.add_api_route("/health", self._health, methods=["GET"])
        self.app.add_api_route("/metrics", self._metrics_redirect, methods=["GET"])

        # Dashboard API router — registered here so it precedes StaticFiles mount.
        # IMPORTANT: StaticFiles at "/" is a catch-all; it must be mounted last
        # (in start()), after all webhook routes are registered via setup_*().
        from src.dashboard.api import router as _dash_router
        self.app.include_router(_dash_router)

        # Startup event: inject shared state into app.state
        @self.app.on_event("startup")
        async def _startup() -> None:
            self.app.state.db = self._db
            self.app.state.config = self._config

    def set_db(self, db: Any) -> None:
        """Inject the shared Database instance before start()."""
        self._db = db

    def set_config(self, config: Any) -> None:
        """Inject the loaded Config before start()."""
        self._config = config

    def setup_telegram(self, telegram_adapter: "TelegramAdapter") -> None:
        """Register Telegram webhook endpoint at /webhook/telegram."""
        self._telegram_adapter = telegram_adapter
        self.app.add_api_route(
            "/webhook/telegram",
            self._handle_telegram,
            methods=["POST"],
        )
        log.info("webhook.telegram_route_registered")

    def setup_feishu(self, feishu_adapter: "FeishuAdapter") -> None:
        """Register Feishu webhook endpoint at /webhook/feishu."""
        self._feishu_adapter = feishu_adapter
        self.app.add_api_route(
            "/webhook/feishu",
            self._handle_feishu,
            methods=["POST"],
        )
        log.info("webhook.feishu_route_registered")

    async def start(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        """Start uvicorn server in a background asyncio task."""
        # Mount StaticFiles last — it's a "/" catch-all and must come after
        # all webhook routes registered via setup_telegram/setup_feishu().
        _static_dir = Path(__file__).parent / "dashboard" / "static"
        if _static_dir.exists():
            self.app.mount(
                "/", StaticFiles(directory=str(_static_dir), html=True), name="static"
            )
        config = uvicorn.Config(
            app=self.app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._serve_task = asyncio.create_task(
            self._server.serve(),
            name="webhook-server",
        )
        log.info("webhook.server_started", host=host, port=port)

    async def stop(self) -> None:
        """Stop the uvicorn server gracefully."""
        if self._server is not None:
            self._server.should_exit = True

        if self._serve_task is not None:
            try:
                await asyncio.wait_for(self._serve_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._serve_task.cancel()
            self._serve_task = None

        log.info("webhook.server_stopped")

    # ------------------------------------------------------------------
    # Route handlers
    # ------------------------------------------------------------------

    async def _health(self) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def _metrics_redirect(self) -> Response:
        """Redirect to Prometheus metrics endpoint (default port 9090)."""
        return Response(
            status_code=307,
            headers={"Location": "http://localhost:9090/metrics"},
        )

    async def _handle_telegram(self, request: Request) -> JSONResponse:
        """Feed raw Telegram update JSON to the aiogram dispatcher."""
        if self._telegram_adapter is None:
            return JSONResponse({"error": "telegram not configured"}, status_code=503)

        try:
            body = await request.json()
            await self._telegram_adapter.feed_webhook_update(body)
            return JSONResponse({"ok": True})
        except Exception as exc:
            log.exception("webhook.telegram_error")
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def _handle_feishu(self, request: Request) -> JSONResponse:
        """Handle Feishu event callback, including URL verification challenge."""
        if self._feishu_adapter is None:
            return JSONResponse({"error": "feishu not configured"}, status_code=503)

        try:
            body: dict[str, Any] = await request.json()

            # Feishu URL verification: respond with challenge immediately
            if body.get("type") == "url_verification":
                challenge = body.get("challenge", "")
                log.info("webhook.feishu_challenge_response")
                return JSONResponse({"challenge": challenge})

            # Regular event callback
            await self._feishu_adapter.feed_webhook_event(body)
            return JSONResponse({"ok": True})
        except Exception as exc:
            log.exception("webhook.feishu_error")
            return JSONResponse({"error": str(exc)}, status_code=500)
