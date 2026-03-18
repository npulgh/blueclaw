"""Lynxclaw host process — asyncio entry point.

Wires together: Config → DB → Registry → Router → ContainerManager → IPCWatcher.
Message flow: IM → Router → Container → IPC → IM reply.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from typing import Optional

import structlog

from src.channels.registry import ChannelRegistry
from src.channels.telegram import TelegramAdapter
from src.config import Config, load_config
from src.container_manager import ContainerManager
from src.db import Database
from src.ipc import IPCWatcher
from src.router import MessageRouter, RouteResult
from src.types import IncomingMessage, OutgoingMessage

log = structlog.get_logger(__name__)


def _setup_logging(level: str = "info") -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(stream=sys.stdout, level=numeric, force=True)


# ---------------------------------------------------------------------------
# Streaming state tracker
# ---------------------------------------------------------------------------

class _StreamState:
    """Tracks per-group streaming state for IPC → IM delivery."""

    def __init__(self) -> None:
        # group_name → chat_id of the message currently being processed
        self.active_chat: dict[str, str] = {}
        # group_name → channel name
        self.active_channel: dict[str, str] = {}
        # group_name → msg_id of the placeholder/streaming message
        self.stream_msg_id: dict[str, str] = {}
        # group_name → accumulated text for streaming edits
        self.stream_buffer: dict[str, str] = {}

    def begin(self, group: str, channel: str, chat_id: str) -> None:
        self.active_chat[group] = chat_id
        self.active_channel[group] = channel
        self.stream_msg_id.pop(group, None)
        self.stream_buffer.pop(group, None)

    def clear(self, group: str) -> None:
        self.active_chat.pop(group, None)
        self.active_channel.pop(group, None)
        self.stream_msg_id.pop(group, None)
        self.stream_buffer.pop(group, None)


# ---------------------------------------------------------------------------
# IPC dispatch handler
# ---------------------------------------------------------------------------

async def _ipc_dispatch(
    method: str,
    params: dict,
    rpc_id: str,
    *,
    registry: ChannelRegistry,
    stream: _StreamState,
) -> None:
    """Handle IPC messages from containers and deliver to IM."""
    group = params.get("group", "")
    channel_name = stream.active_channel.get(group)
    chat_id = stream.active_chat.get(group)

    if not channel_name or not chat_id:
        log.warning("ipc.no_active_session", method=method, group=group)
        return

    try:
        adapter = registry.get(channel_name)
    except KeyError:
        log.error("ipc.adapter_not_found", channel=channel_name)
        return

    if method == "send_message":
        text = params.get("text", "")
        msg_id = await adapter.send_message(chat_id, OutgoingMessage(text=text))
        log.info("ipc.sent", group=group, msg_id=msg_id)

    elif method == "stream_chunk":
        chunk = params.get("text", "")
        is_final = params.get("is_final", False)

        buf = stream.stream_buffer.get(group, "")
        buf += chunk
        stream.stream_buffer[group] = buf

        existing_id = stream.stream_msg_id.get(group)
        if existing_id:
            await adapter.edit_message(
                chat_id, existing_id, OutgoingMessage(text=buf)
            )
        else:
            msg_id = await adapter.send_message(
                chat_id, OutgoingMessage(text=buf)
            )
            stream.stream_msg_id[group] = msg_id

        if is_final:
            stream.stream_msg_id.pop(group, None)
            stream.stream_buffer.pop(group, None)


# ---------------------------------------------------------------------------
# Error messages sent to IM users (never silently fail)
# ---------------------------------------------------------------------------

_ERR_TIMEOUT = "⏱ Sorry, the agent timed out. Please try again."
_ERR_CONTAINER = "⚠ Sorry, something went wrong while processing your message."
_THINKING = "💭 Thinking..."


# ---------------------------------------------------------------------------
# Per-group consumer task
# ---------------------------------------------------------------------------

async def _group_consumer(
    group_name: str,
    *,
    config: Config,
    router: MessageRouter,
    container_mgr: ContainerManager,
    registry: ChannelRegistry,
    stream: _StreamState,
    is_main: bool,
) -> None:
    """Consume messages from a group queue, spawn containers, handle errors."""
    log.info("consumer.started", group=group_name)

    while True:
        msg: IncomingMessage = await router.get_next(group_name)
        log.info(
            "consumer.processing",
            group=group_name,
            message_id=msg.message_id,
            sender=msg.sender_name,
        )

        # Track which chat this group is currently serving
        stream.begin(group_name, msg.channel, msg.chat_id)

        # Send "thinking..." placeholder to ease cold-start wait
        try:
            adapter = registry.get(msg.channel)
            placeholder_id = await adapter.send_message(
                msg.chat_id, OutgoingMessage(text=_THINKING)
            )
            # Store as the stream message so first chunk edits it
            stream.stream_msg_id[group_name] = placeholder_id
        except Exception:
            log.exception("consumer.thinking_failed", group=group_name)

        # Build mounts and env
        cwd = os.getcwd()
        mounts = {
            "group_dir": os.path.join(cwd, "groups", group_name),
            "global_dir": os.path.join(cwd, "groups"),
            "ipc_dir": os.path.join(cwd, "data", "ipc", group_name),
        }
        if is_main:
            mounts["project_dir"] = cwd

        env_vars = {
            "ANTHROPIC_API_KEY": config.anthropic_api_key,
            "LYNXCLAW_CHAT_ID": msg.chat_id,
        }

        # Spawn container
        try:
            result = await container_mgr.spawn(
                group_name=group_name,
                prompt=msg.text,
                env_vars=env_vars,
                mounts=mounts,
            )
        except Exception as exc:
            log.exception("consumer.spawn_error", group=group_name)
            await _send_error(registry, msg.channel, msg.chat_id, _ERR_CONTAINER)
            stream.clear(group_name)
            continue

        # Handle container failure / timeout
        if result.timed_out:
            log.warning("consumer.timeout", group=group_name)
            await _send_error(registry, msg.channel, msg.chat_id, _ERR_TIMEOUT)
        elif result.exit_code != 0:
            log.error(
                "consumer.nonzero_exit",
                group=group_name,
                exit_code=result.exit_code,
                stderr=result.stderr[:500],
            )
            await _send_error(registry, msg.channel, msg.chat_id, _ERR_CONTAINER)

        stream.clear(group_name)


async def _send_error(
    registry: ChannelRegistry, channel: str, chat_id: str, text: str
) -> None:
    """Best-effort error message to the IM user."""
    try:
        adapter = registry.get(channel)
        await adapter.send_message(chat_id, OutgoingMessage(text=text))
    except Exception:
        log.exception("send_error.failed", channel=channel, chat_id=chat_id)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def main(config_path: str = "lynxclaw.config.yaml") -> None:
    """Start Lynxclaw: load config, init components, run until shutdown."""
    config = load_config(config_path)
    _setup_logging(config.host.log_level)

    log.info("lynxclaw.starting")

    # --- Init components ---
    db = Database()
    await db.init(config.db_path)

    registry = ChannelRegistry()
    if config.telegram.enabled:
        tg = TelegramAdapter()
        await tg.init(config.telegram)
        registry.register("telegram", tg)

    router = MessageRouter()
    await router.init(db, config)

    container_mgr = ContainerManager()
    container_mgr.init(config.container, config.security)

    stream = _StreamState()

    async def dispatch_cb(method: str, params: dict, rpc_id: str) -> None:
        await _ipc_dispatch(
            method, params, rpc_id, registry=registry, stream=stream
        )

    ipc = IPCWatcher()
    await ipc.init(base_dir="data/ipc", dispatch_callback=dispatch_cb)

    # Wire message handler: adapter.on_message → router.route
    async def on_incoming(msg: IncomingMessage) -> None:
        result = await router.route(msg)
        log.info("message.routed", result=result.name, channel=msg.channel,
                 message_id=msg.message_id)

    for name in registry.names:
        registry.get(name).on_message(on_incoming)

    # --- Build group config lookup ---
    group_main_map = {g.name: g.is_main for g in config.groups}
    group_names = [g.name for g in config.groups]

    # --- Start all components ---
    await registry.start_all()
    await ipc.start(groups=group_names)

    # --- Launch per-group consumer tasks ---
    consumer_tasks: list[asyncio.Task] = []
    for g in config.groups:
        task = asyncio.create_task(
            _group_consumer(
                g.name,
                config=config,
                router=router,
                container_mgr=container_mgr,
                registry=registry,
                stream=stream,
                is_main=g.is_main,
            ),
            name=f"consumer-{g.name}",
        )
        consumer_tasks.append(task)

    # --- Wait for shutdown signal ---
    stop_event = asyncio.Event()

    def _handle_signal(sig: signal.Signals) -> None:
        log.info("shutdown_signal", signal=sig.name)
        stop_event.set()

    loop = asyncio.get_running_loop()

    def _make_handler(sig: signal.Signals):
        def _handler(signum, frame):
            loop.call_soon_threadsafe(_handle_signal, sig)
        return _handler

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _make_handler(sig))

    log.info("lynxclaw.ready", groups=group_names)
    await stop_event.wait()

    # --- Graceful shutdown ---
    log.info("lynxclaw.shutting_down")

    for task in consumer_tasks:
        task.cancel()
    await asyncio.gather(*consumer_tasks, return_exceptions=True)

    await registry.stop_all()
    await ipc.stop()
    await db.backup()
    await db.close()

    log.info("lynxclaw.stopped")


if __name__ == "__main__":
    asyncio.run(main())
