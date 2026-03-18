"""Lynxclaw host process — asyncio entry point.

Wires together: Config → DB → Registry → Router → ContainerManager → IPCWatcher.
Message flow: IM → Router → Container → IPC → IM reply.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Callable, Coroutine, Optional

import structlog

from src.channels.feishu import FeishuAdapter
from src.channels.registry import ChannelRegistry
from src.channels.telegram import TelegramAdapter
from src.config import Config, load_config
from src.container_manager import ContainerManager
from src.db import Database
from src.ipc import IPCWatcher
from src.memory import ensure_group_dirs, get_global_memory_path
from src.router import MessageRouter, RouteResult
from src.stream_debouncer import StreamDebouncer
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
    debouncer: StreamDebouncer,
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
        # Delegate to debouncer; the flush callback handles send/edit
        await debouncer.add_chunk(group, chunk, is_final)


def _make_debounce_flush(
    registry: ChannelRegistry,
    stream: _StreamState,
) -> "Callable[[str, str, bool], Coroutine]":
    """Build the flush callback wired to the registry and stream state."""

    async def _on_flush(group: str, text: str, is_final: bool) -> None:
        """Called by the debouncer when it's time to update the IM message."""
        channel_name = stream.active_channel.get(group)
        chat_id = stream.active_chat.get(group)

        if not channel_name or not chat_id:
            log.warning("debounce.flush_no_session", group=group)
            return

        try:
            adapter = registry.get(channel_name)
        except KeyError:
            log.error("debounce.flush_adapter_not_found", channel=channel_name)
            return

        existing_id = stream.stream_msg_id.get(group)
        if existing_id:
            # Append incoming text to the buffer to build the full accumulated text
            buf = stream.stream_buffer.get(group, "")
            buf += text
            stream.stream_buffer[group] = buf
            await adapter.edit_message(chat_id, existing_id, OutgoingMessage(text=buf))
            log.debug("debounce.edit", group=group, chars=len(buf), is_final=is_final)
        else:
            # First flush: send a new message (or edit the thinking placeholder)
            # The "thinking..." placeholder_id is pre-seeded in stream.stream_msg_id
            # by _group_consumer — but if it isn't present we send fresh.
            msg_id = await adapter.send_message(chat_id, OutgoingMessage(text=text))
            stream.stream_msg_id[group] = msg_id
            stream.stream_buffer[group] = text
            log.debug("debounce.send", group=group, chars=len(text))

        if is_final:
            stream.stream_msg_id.pop(group, None)
            stream.stream_buffer.pop(group, None)

    return _on_flush


# ---------------------------------------------------------------------------
# Error messages sent to IM users (never silently fail)
# ---------------------------------------------------------------------------

_ERR_TIMEOUT = "⏱ Sorry, the agent timed out. Please try again."
_ERR_CONTAINER = "⚠ Sorry, something went wrong while processing your message."
_THINKING = "💭 Thinking..."
_ERR_BUDGET = (
    "🚫 This group has reached its monthly token budget. "
    "No new requests will be processed until the budget resets or is increased. "
    "Contact your administrator to raise the token_budget in the config."
)


def _month_start_ts() -> int:
    """Return the Unix timestamp of the start of the current calendar month (UTC)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    month_start = datetime.datetime(now.year, now.month, 1, tzinfo=datetime.timezone.utc)
    return int(month_start.timestamp())


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
    db: Database,
    token_budget: int = 0,
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

        # --- Budget enforcement (monthly) ---
        if token_budget > 0:
            month_start = _month_start_ts()
            usage = await db.get_token_usage(group_name=group_name, since=month_start)
            total_used = usage["input_tokens"] + usage["output_tokens"]
            if total_used >= token_budget:
                log.warning(
                    "consumer.budget_exceeded",
                    group=group_name,
                    used=total_used,
                    budget=token_budget,
                )
                await _send_error(registry, msg.channel, msg.chat_id, _ERR_BUDGET)
                continue

        # Track which chat this group is currently serving
        stream.begin(group_name, msg.channel, msg.chat_id)

        # Send "thinking..." placeholder to ease cold-start wait
        try:
            adapter = registry.get(msg.channel)
            placeholder_id = await adapter.send_message(
                msg.chat_id, OutgoingMessage(text=_THINKING)
            )
            # Store as the stream message so first debounce flush edits it
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
            # Main group gets rw access to global CLAUDE.md via a dedicated mount
            mounts["global_memory"] = get_global_memory_path(os.path.join(cwd, "groups"))

        env_vars = {
            "ANTHROPIC_API_KEY": config.anthropic_api_key,
            "LYNXCLAW_CHAT_ID": msg.chat_id,
        }

        # Mark message as 'processing' before spawning container
        await db.update_message_status(
            channel=msg.channel,
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            status="processing",
        )

        # Spawn container
        try:
            session_id = await db.get_session(group_name=group_name)
            result = await container_mgr.spawn(
                group_name=group_name,
                prompt=msg.text,
                env_vars=env_vars,
                mounts=mounts,
                is_main=is_main,
                session_id=session_id,
            )
        except Exception:
            log.exception("consumer.spawn_error", group=group_name)
            await db.update_message_status(
                channel=msg.channel,
                chat_id=msg.chat_id,
                message_id=msg.message_id,
                status="failed",
            )
            await _send_error(registry, msg.channel, msg.chat_id, _ERR_CONTAINER)
            stream.clear(group_name)
            continue

        # Handle container failure / timeout
        if result.timed_out:
            log.warning("consumer.timeout", group=group_name)
            await db.update_message_status(
                channel=msg.channel,
                chat_id=msg.chat_id,
                message_id=msg.message_id,
                status="failed",
            )
            await _send_error(registry, msg.channel, msg.chat_id, _ERR_TIMEOUT)
        elif result.exit_code != 0:
            log.error(
                "consumer.nonzero_exit",
                group=group_name,
                exit_code=result.exit_code,
                stderr=result.stderr[:500],
            )
            await db.update_message_status(
                channel=msg.channel,
                chat_id=msg.chat_id,
                message_id=msg.message_id,
                status="failed",
            )
            await _send_error(registry, msg.channel, msg.chat_id, _ERR_CONTAINER)
        else:
            # Successful run — record token usage then persist session_id
            cwd = os.getcwd()
            ipc_group_dir = os.path.join(cwd, "data", "ipc", group_name)

            # Read and record token usage written by the agent runner
            token_usage_file = os.path.join(ipc_group_dir, "token_usage.json")
            try:
                raw = await asyncio.to_thread(
                    Path(token_usage_file).read_text, encoding="utf-8"
                )
                token_data = json.loads(raw)
                await asyncio.to_thread(os.unlink, token_usage_file)
                await db.record_token_usage(
                    group_name=group_name,
                    input_tokens=int(token_data.get("input_tokens", 0)),
                    output_tokens=int(token_data.get("output_tokens", 0)),
                )
                log.info(
                    "tokens.recorded",
                    group=group_name,
                    input=token_data.get("input_tokens", 0),
                    output=token_data.get("output_tokens", 0),
                )
            except FileNotFoundError:
                pass  # agent didn't write token_usage.json (e.g. mock/test path)
            except Exception:
                log.exception("tokens.read_error", group=group_name)

            # Persist session_id for next turn
            session_file = os.path.join(ipc_group_dir, "session_id.txt")
            try:
                new_session_id = await asyncio.to_thread(
                    Path(session_file).read_text, encoding="utf-8"
                )
                new_session_id = new_session_id.strip()
                await asyncio.to_thread(os.unlink, session_file)
                if new_session_id:
                    await db.save_session(group_name=group_name, session_id=new_session_id)
                    log.info("session.saved", group=group_name, session_id=new_session_id)
            except FileNotFoundError:
                pass  # agent didn't write a session_id (e.g. error path)
            except Exception:
                log.exception("session.read_error", group=group_name)

            await db.update_message_status(
                channel=msg.channel,
                chat_id=msg.chat_id,
                message_id=msg.message_id,
                status="completed",
            )

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

    # --- Seed group directories and CLAUDE.md templates ---
    cwd = os.getcwd()
    groups_dir = os.path.join(cwd, "groups")
    group_names_from_config = [g.name for g in config.groups]
    await asyncio.to_thread(ensure_group_dirs, groups_dir, group_names_from_config)
    log.info("memory.dirs_ensured", groups=group_names_from_config)

    # --- Init components ---
    db = Database()
    await db.init(config.db_path)

    registry = ChannelRegistry()
    if config.telegram.enabled:
        tg = TelegramAdapter()
        await tg.init(config.telegram)
        registry.register("telegram", tg)

    if config.feishu.enabled:
        feishu = FeishuAdapter()
        await feishu.init(config.feishu)
        registry.register("feishu", feishu)

    router = MessageRouter()
    await router.init(db, config)

    container_mgr = ContainerManager()
    container_mgr.init(config.container, config.security)

    stream = _StreamState()

    # Streaming debouncer — flush every 500 ms or 200 chars, whichever first
    debouncer = StreamDebouncer(
        debounce_ms=config.streaming.debounce_ms,
        debounce_chars=config.streaming.debounce_chars,
    )
    flush_cb = _make_debounce_flush(registry, stream)
    debouncer.set_flush_callback(flush_cb)
    await debouncer.start()

    async def dispatch_cb(method: str, params: dict, rpc_id: str) -> None:
        await _ipc_dispatch(
            method, params, rpc_id,
            registry=registry,
            stream=stream,
            debouncer=debouncer,
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

    # --- Crash recovery: clean up orphan containers, re-enqueue stuck messages ---
    await container_mgr.cleanup_orphans()
    recovered = await router.recover_pending()
    if recovered:
        log.info("startup.recovery", recovered=recovered)

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
                db=db,
                token_budget=g.token_budget,
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

    await debouncer.stop()
    await registry.stop_all()
    await ipc.stop()
    await db.backup()
    await db.close()

    log.info("lynxclaw.stopped")


if __name__ == "__main__":
    asyncio.run(main())
