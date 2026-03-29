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

"""Lynxclaw message router.

Pipeline:
  Idempotent check → Group lookup → Trigger matching → Backpressure check →
  Group Queue → return QUEUED

One asyncio.Queue per group (maxsize = config.router.group_queue_max).
Messages within the same group are processed serially by the consumer.
"""

from __future__ import annotations

import asyncio
from enum import Enum, auto
from typing import Optional

import structlog

from src.config import Config
from src.db import Database
from src.types import IncomingMessage

log = structlog.get_logger(__name__)


class RouteResult(Enum):
    QUEUED = auto()       # Message accepted and placed on the group queue
    DUPLICATE = auto()    # Already seen — idempotency guard triggered
    NO_GROUP = auto()     # No group registered for this channel+chat_id
    NO_TRIGGER = auto()   # Group chat message didn't start with trigger prefix
    DENIED = auto()       # Sender not in allowed_senders list
    BACKPRESSURE = auto() # Group queue is full — caller should retry later


class MessageRouter:
    """Routes incoming messages to per-group queues."""

    def __init__(self) -> None:
        self._db: Optional[Database] = None
        self._config: Optional[Config] = None
        # group_name → asyncio.Queue[IncomingMessage]
        self._queues: dict[str, asyncio.Queue[IncomingMessage]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init(self, db: Database, config: Config) -> None:
        """Seed groups from config into DB and create per-group queues."""
        self._db = db
        self._config = config

        queue_max = config.router.group_queue_max

        for group_cfg in config.groups:
            # Write group to DB (upsert — idempotent on restart)
            await db.upsert_group(
                name=group_cfg.name,
                channel=group_cfg.channel,
                chat_id=group_cfg.chat_id,
                is_main=group_cfg.is_main,
                trigger=group_cfg.trigger,
            )
            # Create queue if it doesn't already exist
            if group_cfg.name not in self._queues:
                self._queues[group_cfg.name] = asyncio.Queue(maxsize=queue_max)

            log.debug(
                "router.group_registered",
                name=group_cfg.name,
                channel=group_cfg.channel,
                chat_id=group_cfg.chat_id,
            )

        log.info("router.init_done", groups=len(config.groups))

    # ------------------------------------------------------------------
    # Routing pipeline
    # ------------------------------------------------------------------

    async def route(self, msg: IncomingMessage) -> RouteResult:
        """Route a single incoming message through the full pipeline.

        Returns:
            RouteResult indicating outcome.
        """
        assert self._db is not None, "MessageRouter.init() must be called first"
        assert self._config is not None, "MessageRouter.init() must be called first"

        # 1. Idempotent check — INSERT OR IGNORE; None rowid → duplicate
        rowid = await self._db.insert_message(
            channel=msg.channel,
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            sender_id=msg.sender_id,
            sender_name=msg.sender_name,
            content=msg.text,
            direction="inbound",
            status="pending",
            created_at=msg.timestamp if msg.timestamp else None,
        )
        if rowid is None:
            log.debug(
                "router.duplicate",
                channel=msg.channel,
                message_id=msg.message_id,
            )
            return RouteResult.DUPLICATE

        # 2. Group lookup — find registered group for this channel+chat_id
        group_row = await self._db.get_group_by_chat(
            channel=msg.channel, chat_id=msg.chat_id
        )
        if group_row is None:
            log.debug(
                "router.no_group",
                channel=msg.channel,
                chat_id=msg.chat_id,
            )
            return RouteResult.NO_GROUP

        group_name: str = group_row["name"]
        trigger: str = group_row["trigger"]

        # 3. Trigger matching
        #    Telegram DM convention: chat_id that does NOT start with "-" is a DM.
        #    DMs always trigger; group chats require the trigger prefix.
        is_dm = not msg.chat_id.startswith("-")
        if not is_dm:
            if not msg.text.startswith(trigger):
                log.debug(
                    "router.no_trigger",
                    group=group_name,
                    trigger=trigger,
                    text_preview=msg.text[:40],
                )
                return RouteResult.NO_TRIGGER

        # 4. Sender allowlist — empty list means no restriction
        group_cfg = next(
            (g for g in self._config.groups if g.name == group_name), None
        )
        if group_cfg and group_cfg.allowed_senders:
            if msg.sender_id not in group_cfg.allowed_senders:
                log.warning(
                    "router.denied",
                    group=group_name,
                    sender_id=msg.sender_id,
                )
                return RouteResult.DENIED

        # 5. Backpressure — try to enqueue without blocking
        queue = self._get_or_create_queue(group_name)
        try:
            queue.put_nowait(msg)
        except asyncio.QueueFull:
            log.warning("router.backpressure", group=group_name, qsize=queue.qsize())
            return RouteResult.BACKPRESSURE

        log.info("router.queued",
            group=group_name,
            channel=msg.channel,
            message_id=msg.message_id,
            qsize=queue.qsize(),
        )
        return RouteResult.QUEUED

    # ------------------------------------------------------------------
    # Crash recovery
    # ------------------------------------------------------------------

    async def recover_pending(self) -> int:
        """Re-enqueue messages stuck in 'processing' status from a prior crash.

        On a clean shutdown messages should be 'completed' or 'failed'.
        Any message still in 'processing' was being handled when the process
        was killed — we re-enqueue it so the consumer can retry it.

        Returns:
            Number of messages re-enqueued.
        """
        assert self._db is not None, "MessageRouter.init() must be called first"

        stuck = await self._db.get_messages_by_status(status="processing")
        recovered = 0

        for row in stuck:
            group_name: str = row.get("group_name") or ""
            if not group_name or group_name not in self._queues:
                # Try to resolve group via channel+chat_id lookup
                group_row = await self._db.get_group_by_chat(
                    channel=row["channel"], chat_id=row["chat_id"]
                )
                if group_row is None:
                    log.warning(
                        "router.recover.no_group",
                        channel=row["channel"],
                        chat_id=row["chat_id"],
                        message_id=row["message_id"],
                    )
                    # Mark as failed — no group to route to
                    await self._db.update_message_status(
                        channel=row["channel"],
                        chat_id=row["chat_id"],
                        message_id=row["message_id"],
                        status="failed",
                    )
                    continue
                group_name = group_row["name"]

            msg = IncomingMessage(
                message_id=row["message_id"],
                chat_id=row["chat_id"],
                sender_id=row["sender_id"],
                sender_name=row.get("sender_name") or "",
                text=row["content"],
                channel=row["channel"],
                timestamp=row.get("created_at"),
            )

            queue = self._get_or_create_queue(group_name)
            try:
                queue.put_nowait(msg)
                recovered += 1
                log.info(
                    "router.recover.requeued",
                    group=group_name,
                    message_id=row["message_id"],
                )
            except asyncio.QueueFull:
                log.warning(
                    "router.recover.queue_full",
                    group=group_name,
                    message_id=row["message_id"],
                )

        if recovered:
            log.info("router.recover.done", recovered=recovered)

        return recovered

    # ------------------------------------------------------------------
    # Queue access
    # ------------------------------------------------------------------

    async def get_next(self, group_name: str) -> IncomingMessage:
        """Block until the next message is available for *group_name*."""
        queue = self._get_or_create_queue(group_name)
        return await queue.get()

    def queue_size(self, group_name: str) -> int:
        """Return current queue depth for *group_name* (0 if unknown)."""
        queue = self._queues.get(group_name)
        return queue.qsize() if queue else 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create_queue(self, group_name: str) -> asyncio.Queue[IncomingMessage]:
        """Return existing queue or create a new one at default max size."""
        if group_name not in self._queues:
            max_size = (
                self._config.router.group_queue_max
                if self._config is not None
                else 10
            )
            self._queues[group_name] = asyncio.Queue(maxsize=max_size)
        return self._queues[group_name]
