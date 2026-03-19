"""Layer-1 End-to-End tests: ExampleAdapter + real Docker + real IPC filesystem.

No IM credentials required.  Requires:
  - Docker daemon running
  - lynxclaw-agent:latest image built:
      docker build -t lynxclaw-agent:latest container/agent-runner/
  - ANTHROPIC_API_KEY set in environment (or .env)

Run:
    pytest tests/test_e2e_local.py -v --timeout=120
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Skip entire module when Docker is unavailable
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not shutil.which("docker"),
    reason="Docker not available — skipping Layer-1 E2E tests",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_config(tmp_path: Path, api_key: str) -> "Config":
    """Build a minimal Config that points IPC/DB at tmp_path."""
    from src.config import (
        Config, ContainerConfig, GroupConfig, HostConfig,
        ProxyConfig, RouterConfig, SecurityConfig, StreamingConfig,
    )

    return Config(
        anthropic_api_key=api_key,
        db_path=str(tmp_path / "messages.db"),
        host=HostConfig(log_level="warning"),
        container=ContainerConfig(
            image="lynxclaw-agent:latest",
            timeout=90,
            max_concurrent=2,
            network="bridge",   # allow container to reach Anthropic API
        ),
        router=RouterConfig(group_queue_max=5),
        security=SecurityConfig(),
        streaming=StreamingConfig(debounce_ms=200, debounce_chars=50),
        proxy=ProxyConfig(enabled=False),
        groups=[
            GroupConfig(
                name="e2e-group",
                channel="example",
                chat_id="e2e-chat",
                is_main=False,
                trigger="",          # empty trigger = accept all messages
                token_budget=0,
                container_mode="ephemeral",
            )
        ],
    )


async def _wait_for_condition(
    cond_fn,
    *,
    timeout: float = 60.0,
    poll_interval: float = 0.5,
    description: str = "condition",
) -> None:
    """Poll cond_fn() until it returns True or timeout expires.

    cond_fn may be a plain callable or an async coroutine function.
    """
    import inspect
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = cond_fn()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        await asyncio.sleep(poll_interval)
    raise TimeoutError(f"Timed out waiting for: {description}")


# ---------------------------------------------------------------------------
# Instrumented ExampleAdapter — records all sent/edited messages
# ---------------------------------------------------------------------------

class _RecordingAdapter:
    """Wraps ExampleAdapter and records every send/edit call."""

    def __init__(self) -> None:
        from src.channels.example_adapter import ExampleAdapter, ExampleConfig
        self._inner = ExampleAdapter()
        self._cfg = ExampleConfig(enabled=True, channel_name="example")
        self.sent: list[dict] = []    # {"chat_id", "text", "msg_id"}
        self.edits: list[dict] = []   # {"chat_id", "msg_id", "text"}
        self._msg_counter = 0

    async def init(self, _config=None) -> None:
        await self._inner.init(self._cfg)

    async def start(self) -> None:
        await self._inner.start()

    async def stop(self) -> None:
        await self._inner.stop()

    def on_message(self, handler) -> None:
        self._inner.on_message(handler)

    async def send_message(self, chat_id: str, content) -> str:
        self._msg_counter += 1
        msg_id = f"rec-{self._msg_counter}"
        self.sent.append({"chat_id": chat_id, "text": content.text or "", "msg_id": msg_id})
        return msg_id

    async def edit_message(self, chat_id: str, msg_id: str, content) -> None:
        self.edits.append({"chat_id": chat_id, "msg_id": msg_id, "text": content.text or ""})

    def capabilities(self):
        from src.types import ChannelCapabilities
        return ChannelCapabilities(supports_edit=True, supports_rich_text=False, supports_attachments=False)

    async def simulate_incoming(self, text: str, chat_id: str = "e2e-chat") -> None:
        await self._inner.simulate_incoming(text=text, chat_id=chat_id)


# ---------------------------------------------------------------------------
# E2E harness — assembles all real components
# ---------------------------------------------------------------------------

class _E2EHarness:
    """Assembles the full Lynxclaw stack with a recording adapter instead of IM."""

    def __init__(self, tmp_path: Path, api_key: str) -> None:
        self.tmp_path = tmp_path
        self.api_key = api_key
        self.adapter: Optional[_RecordingAdapter] = None
        self._consumer_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._orig_cwd: Optional[str] = None

        # Components set during setup()
        self.db = None
        self.registry = None
        self.router = None
        self.container_mgr = None
        self.ipc = None
        self.debouncer = None
        self.stream = None
        self.swarm = None
        self.obs = None
        self.config = None

    async def setup(self) -> None:
        from src.channels.registry import ChannelRegistry
        from src.config import ContainerConfig, SecurityConfig
        from src.container_manager import ContainerManager
        from src.db import Database
        from src.ipc import IPCWatcher
        from src.main import _StreamState, _ipc_dispatch, _make_debounce_flush
        from src.observability import Observability
        from src.router import MessageRouter
        from src.stream_debouncer import StreamDebouncer
        from src.swarm import SwarmCoordinator

        # Change cwd to tmp_path so _group_consumer's os.getcwd()-based mount
        # paths (groups/e2e-group, data/ipc/e2e-group) resolve here.
        # This also makes the container IPC dir match what IPCWatcher monitors.
        self._orig_cwd = os.getcwd()
        os.chdir(str(self.tmp_path))

        # Create directory structure expected by _group_consumer
        ipc_base = self.tmp_path / "data" / "ipc"
        (ipc_base / "e2e-group" / "outbox").mkdir(parents=True, exist_ok=True)
        (ipc_base / "e2e-group" / "inbox").mkdir(parents=True, exist_ok=True)
        (ipc_base / "e2e-group" / "audit").mkdir(parents=True, exist_ok=True)
        (self.tmp_path / "groups" / "e2e-group").mkdir(parents=True, exist_ok=True)
        (self.tmp_path / "groups").mkdir(parents=True, exist_ok=True)

        self.config = _build_config(self.tmp_path, self.api_key)

        # DB
        self.db = Database()
        await self.db.init(self.config.db_path)

        # Recording adapter
        self.adapter = _RecordingAdapter()
        await self.adapter.init()

        # Registry — register our recording adapter as "example"
        self.registry = ChannelRegistry()
        self.registry.register("example", self.adapter)

        # Router
        self.router = MessageRouter()
        await self.router.init(self.db, self.config)

        # Container manager — uses real Docker
        self.container_mgr = ContainerManager()
        self.container_mgr.init(self.config.container, self.config.security)

        # Swarm
        self.swarm = SwarmCoordinator()
        await self.swarm.init(self.db, self.container_mgr, self.config)

        # Stream state + debouncer
        self.stream = _StreamState()
        self.debouncer = StreamDebouncer(
            debounce_ms=self.config.streaming.debounce_ms,
            debounce_chars=self.config.streaming.debounce_chars,
        )
        flush_cb = _make_debounce_flush(self.registry, self.stream)
        self.debouncer.set_flush_callback(flush_cb)
        await self.debouncer.start()

        # Observability (no-op metrics for tests)
        self.obs = _MockObs()

        # IPC watcher — points at tmp_path/ipc
        async def dispatch_cb(method: str, params: dict, rpc_id: str) -> None:
            await _ipc_dispatch(
                method, params, rpc_id,
                registry=self.registry,
                stream=self.stream,
                debouncer=self.debouncer,
                db=self.db,
                swarm=self.swarm,
            )

        self.ipc = IPCWatcher()
        await self.ipc.init(
            base_dir=str(ipc_base),   # tmp_path/data/ipc — matches _group_consumer mounts
            dispatch_callback=dispatch_cb,
        )
        await self.ipc.start(groups=["e2e-group"])

        # Wire incoming messages: adapter → router
        from src.observability import new_correlation_id

        async def on_incoming(msg):
            new_correlation_id()
            await self.router.route(msg)

        self.adapter.on_message(on_incoming)
        await self.adapter.start()

        # Start per-group consumer
        from src.main import _group_consumer
        self._consumer_task = asyncio.create_task(
            _group_consumer(
                "e2e-group",
                config=self.config,
                router=self.router,
                container_mgr=self.container_mgr,
                registry=self.registry,
                stream=self.stream,
                is_main=False,
                db=self.db,
                obs=self.obs,
                token_budget=0,
                container_mode="ephemeral",
                persistent_container=None,
            ),
            name="consumer-e2e-group",
        )

    async def teardown(self) -> None:
        if self._consumer_task:
            self._consumer_task.cancel()
            await asyncio.gather(self._consumer_task, return_exceptions=True)
        if self.debouncer:
            await self.debouncer.stop()
        if self.adapter:
            await self.adapter.stop()
        if self.ipc:
            await self.ipc.stop()
        if self.db:
            await self.db.close()
        # Restore original working directory
        if self._orig_cwd:
            os.chdir(self._orig_cwd)

    def ipc_outbox(self) -> Path:
        return self.tmp_path / "data" / "ipc" / "e2e-group" / "outbox"


class _MockObs:
    """Minimal Observability stub for E2E tests."""

    class _Counter:
        def inc(self): pass
        def dec(self): pass
        def labels(self, **_): return self

    class _Histogram:
        def observe(self, _): pass
        def labels(self, **_): return self

    def __init__(self):
        self.messages_total = self._Counter()
        self.active_containers = self._Counter()
        self.container_duration_seconds = self._Histogram()
        self.tokens_total = self._Counter()


# ---------------------------------------------------------------------------
# Pytest fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        pytest.skip("ANTHROPIC_API_KEY not set — skipping E2E tests that need real SDK")
    return key


@pytest.fixture
async def harness(tmp_path: Path, api_key: str):
    h = _E2EHarness(tmp_path, api_key)
    await h.setup()
    yield h
    await h.teardown()


# ---------------------------------------------------------------------------
# Test: IPC roundtrip without Docker (fastest, no API key needed)
# ---------------------------------------------------------------------------

class TestIPCRoundtrip:
    """Verify IPC watcher picks up files and dispatches to the adapter.

    These tests write JSON-RPC files directly into the outbox — no container
    or API key required.  They validate the IPC → debouncer → adapter path.
    """

    @pytest.mark.asyncio
    async def test_send_message_via_ipc(self, tmp_path: Path) -> None:
        """Writing a send_message IPC file triggers adapter.send_message()."""
        from src.channels.registry import ChannelRegistry
        from src.config import StreamingConfig
        from src.ipc import IPCWatcher
        from src.main import _StreamState, _ipc_dispatch, _make_debounce_flush
        from src.stream_debouncer import StreamDebouncer

        ipc_base = tmp_path / "ipc"
        outbox = ipc_base / "test-group" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)

        adapter = _RecordingAdapter()
        await adapter.init()
        await adapter.start()

        registry = ChannelRegistry()
        registry.register("example", adapter)

        stream = _StreamState()
        # Pre-seed active session so IPC dispatch can find the adapter
        stream.begin("test-group", "example", "chat-1")

        debouncer = StreamDebouncer(debounce_ms=50, debounce_chars=999)
        debouncer.set_flush_callback(_make_debounce_flush(registry, stream))
        await debouncer.start()

        from unittest.mock import AsyncMock, MagicMock
        mock_db = MagicMock()
        mock_db.create_task = AsyncMock()
        mock_db.list_tasks = AsyncMock(return_value=[])
        mock_db.cancel_task = AsyncMock()
        mock_swarm = MagicMock()
        mock_swarm.delegate = AsyncMock()
        mock_swarm.read_context = AsyncMock(return_value="")

        async def dispatch_cb(method, params, rpc_id):
            await _ipc_dispatch(
                method, params, rpc_id,
                registry=registry,
                stream=stream,
                debouncer=debouncer,
                db=mock_db,
                swarm=mock_swarm,
            )

        ipc = IPCWatcher()
        await ipc.init(base_dir=str(ipc_base), dispatch_callback=dispatch_cb)
        await ipc.start(groups=["test-group"])

        try:
            # Write IPC file atomically (rename triggers on_moved)
            payload = json.dumps({
                "jsonrpc": "2.0",
                "method": "send_message",
                "params": {"group": "test-group", "text": "hello from IPC"},
                "id": "rpc-1",
            })
            tmp_file = outbox / "msg_001.json.tmp"
            tmp_file.write_text(payload, encoding="utf-8")
            tmp_file.rename(outbox / "msg_001.json")

            # Wait for adapter to receive the message (no consumer here, so just 1)
            await _wait_for_condition(
                lambda: len(adapter.sent) >= 1,
                timeout=10.0,
                description="adapter.sent has ≥1 message",
            )

            texts = [m["text"] for m in adapter.sent]
            assert any("hello from IPC" in t for t in texts), f"Expected IPC text in {texts}"

        finally:
            await debouncer.stop()
            await ipc.stop()
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_stream_chunk_via_ipc(self, tmp_path: Path) -> None:
        """stream_chunk IPC files accumulate and flush to edit_message."""
        from src.channels.registry import ChannelRegistry
        from src.ipc import IPCWatcher
        from src.main import _StreamState, _ipc_dispatch, _make_debounce_flush
        from src.stream_debouncer import StreamDebouncer

        ipc_base = tmp_path / "ipc"
        outbox = ipc_base / "stream-group" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)

        adapter = _RecordingAdapter()
        await adapter.init()
        await adapter.start()

        registry = ChannelRegistry()
        registry.register("example", adapter)

        stream = _StreamState()
        stream.begin("stream-group", "example", "chat-2")
        # Pre-seed a placeholder message ID so chunks trigger edit_message
        stream.stream_msg_id["stream-group"] = "placeholder-1"
        stream.stream_buffer["stream-group"] = ""

        debouncer = StreamDebouncer(debounce_ms=50, debounce_chars=999)
        debouncer.set_flush_callback(_make_debounce_flush(registry, stream))
        await debouncer.start()

        from unittest.mock import AsyncMock, MagicMock
        mock_db = MagicMock()
        mock_swarm = MagicMock()
        mock_swarm.delegate = AsyncMock()
        mock_swarm.read_context = AsyncMock(return_value="")

        async def dispatch_cb(method, params, rpc_id):
            await _ipc_dispatch(
                method, params, rpc_id,
                registry=registry,
                stream=stream,
                debouncer=debouncer,
                db=mock_db,
                swarm=mock_swarm,
            )

        ipc = IPCWatcher()
        await ipc.init(base_dir=str(ipc_base), dispatch_callback=dispatch_cb)
        await ipc.start(groups=["stream-group"])

        try:
            # Write two stream_chunk files
            for i, (chunk, is_final) in enumerate([("Hello ", False), ("world!", True)]):
                payload = json.dumps({
                    "jsonrpc": "2.0",
                    "method": "stream_chunk",
                    "params": {
                        "group": "stream-group",
                        "text": chunk,
                        "is_final": is_final,
                    },
                    "id": f"rpc-{i}",
                })
                tmp_file = outbox / f"chunk_{i:03d}.json.tmp"
                tmp_file.write_text(payload, encoding="utf-8")
                tmp_file.rename(outbox / f"chunk_{i:03d}.json")
                await asyncio.sleep(0.05)

            # Wait for at least one edit
            await _wait_for_condition(
                lambda: len(adapter.edits) >= 1,
                timeout=10.0,
                description="at least one edit_message call",
            )

            # Final accumulated text should contain both chunks
            final_texts = [e["text"] for e in adapter.edits]
            assert any("world!" in t for t in final_texts), f"Expected 'world!' in edits: {final_texts}"

        finally:
            await debouncer.stop()
            await ipc.stop()
            await adapter.stop()


# ---------------------------------------------------------------------------
# Test: Full E2E with real Docker container
# ---------------------------------------------------------------------------

class TestFullE2E:
    """Full message flow: ExampleAdapter → Router → real Docker → IPC → adapter.

    These tests spawn a real lynxclaw-agent container and require:
      - Docker daemon running
      - lynxclaw-agent:latest image built
      - ANTHROPIC_API_KEY set
    """

    @pytest.mark.asyncio
    async def test_message_recorded_in_db(self, harness: _E2EHarness) -> None:
        """Injected message is stored in the DB with status 'completed'."""
        await harness.adapter.simulate_incoming("@bot ping")

        # Wait for DB status to reach a terminal state ('completed' or 'failed')
        async def _check():
            rows = await harness.db._conn.execute_fetchall(
                "SELECT status FROM messages WHERE message_id LIKE 'sim-%' LIMIT 1"
            )
            return bool(rows and rows[0][0] in ("completed", "failed"))

        await _wait_for_condition(
            _check,
            timeout=90.0,
            description="message status = completed/failed",
        )

        rows = await harness.db._conn.execute_fetchall(
            "SELECT status FROM messages WHERE message_id LIKE 'sim-%' LIMIT 1"
        )
        assert rows, "No message record found in DB"
        status = rows[0][0]
        assert status == "completed", (
            f"Message status is '{status}' — if 'failed', ensure the agent image is built: "
            "docker build -t lynxclaw-agent:latest container/agent-runner/"
        )

    @pytest.mark.asyncio
    async def test_adapter_receives_reply(self, harness: _E2EHarness) -> None:
        """After injecting a message, the adapter receives a real agent reply (not an error)."""
        from src.main import _ERR_CONTAINER, _ERR_TIMEOUT

        initial_sent = len(harness.adapter.sent)

        await harness.adapter.simulate_incoming("Say exactly: PONG")

        # Wait for at least one new message beyond the "thinking..." placeholder
        await _wait_for_condition(
            lambda: len(harness.adapter.sent) > initial_sent + 1,
            timeout=90.0,
            description="adapter received reply from agent",
        )

        last = harness.adapter.sent[-1]
        assert last["text"], "Reply text should not be empty"
        assert last["text"] not in (_ERR_CONTAINER, _ERR_TIMEOUT), (
            f"Got error message instead of agent reply: {last['text']!r}"
        )

    @pytest.mark.asyncio
    async def test_idempotency(self, harness: _E2EHarness) -> None:
        """Sending the same message_id twice results in only one DB record."""
        from src.types import IncomingMessage

        msg = IncomingMessage(
            channel="example",
            message_id="dedup-test-001",
            chat_id="e2e-chat",
            sender_id="user-1",
            sender_name="Test User",
            text="@bot hello",
            attachments=[],
            timestamp=int(time.time()),
            raw=None,
        )

        # Route the same message twice
        from src.router import RouteResult
        r1 = await harness.router.route(msg)
        r2 = await harness.router.route(msg)

        assert r1 == RouteResult.QUEUED
        assert r2 == RouteResult.DUPLICATE

        # DB should have exactly one record for this message_id
        rows = await harness.db._conn.execute_fetchall(
            "SELECT COUNT(*) FROM messages WHERE message_id = 'dedup-test-001'"
        )
        assert rows[0][0] == 1, f"Expected 1 DB record, got {rows[0][0]}"

    @pytest.mark.asyncio
    async def test_thinking_placeholder_sent(self, harness: _E2EHarness) -> None:
        """The '💭 Thinking...' placeholder is sent before the container starts."""
        from src.main import _THINKING

        initial_sent = len(harness.adapter.sent)
        await harness.adapter.simulate_incoming("@bot test placeholder")

        # The very first new message should be the thinking placeholder
        await _wait_for_condition(
            lambda: len(harness.adapter.sent) > initial_sent,
            timeout=10.0,
            description="thinking placeholder sent",
        )

        first_new = harness.adapter.sent[initial_sent]
        assert first_new["text"] == _THINKING, (
            f"Expected thinking placeholder, got: {first_new['text']!r}"
        )


# ---------------------------------------------------------------------------
# Test: IPC file written directly (no container, no API key)
# ---------------------------------------------------------------------------

class TestIPCDirectWrite:
    """Write IPC files manually to verify the watcher + dispatch pipeline.

    No Docker or API key needed — simulates what a container would write.
    """

    @pytest.mark.asyncio
    async def test_on_created_event(self, tmp_path: Path) -> None:
        """Files created directly (not via rename) are also picked up."""
        from src.channels.registry import ChannelRegistry
        from src.ipc import IPCWatcher
        from src.main import _StreamState, _ipc_dispatch, _make_debounce_flush
        from src.stream_debouncer import StreamDebouncer
        from unittest.mock import AsyncMock, MagicMock

        ipc_base = tmp_path / "ipc"
        outbox = ipc_base / "direct-group" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)

        adapter = _RecordingAdapter()
        await adapter.init()
        await adapter.start()

        registry = ChannelRegistry()
        registry.register("example", adapter)

        stream = _StreamState()
        stream.begin("direct-group", "example", "chat-direct")

        debouncer = StreamDebouncer(debounce_ms=50, debounce_chars=999)
        debouncer.set_flush_callback(_make_debounce_flush(registry, stream))
        await debouncer.start()

        mock_db = MagicMock()
        mock_db.create_task = AsyncMock()
        mock_swarm = MagicMock()
        mock_swarm.delegate = AsyncMock()
        mock_swarm.read_context = AsyncMock(return_value="")

        dispatched: list[str] = []

        async def dispatch_cb(method, params, rpc_id):
            dispatched.append(method)
            await _ipc_dispatch(
                method, params, rpc_id,
                registry=registry,
                stream=stream,
                debouncer=debouncer,
                db=mock_db,
                swarm=mock_swarm,
            )

        ipc = IPCWatcher()
        await ipc.init(base_dir=str(ipc_base), dispatch_callback=dispatch_cb)
        await ipc.start(groups=["direct-group"])

        try:
            # Write directly (triggers on_created, not on_moved)
            payload = json.dumps({
                "jsonrpc": "2.0",
                "method": "send_message",
                "params": {"group": "direct-group", "text": "direct write test"},
                "id": "rpc-direct",
            })
            (outbox / "direct_001.json").write_text(payload, encoding="utf-8")

            await _wait_for_condition(
                lambda: "send_message" in dispatched,
                timeout=15.0,
                description="send_message dispatched via on_created",
            )

        finally:
            await debouncer.stop()
            await ipc.stop()
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_invalid_json_is_ignored(self, tmp_path: Path) -> None:
        """Malformed JSON files are silently ignored without crashing the watcher."""
        from src.ipc import IPCWatcher

        ipc_base = tmp_path / "ipc"
        outbox = ipc_base / "bad-group" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)

        dispatched: list[str] = []

        async def dispatch_cb(method, params, rpc_id):
            dispatched.append(method)

        ipc = IPCWatcher()
        await ipc.init(base_dir=str(ipc_base), dispatch_callback=dispatch_cb)
        await ipc.start(groups=["bad-group"])

        try:
            (outbox / "bad.json").write_text("not valid json {{{{", encoding="utf-8")
            await asyncio.sleep(1.0)  # give watcher time to process
            assert dispatched == [], f"Expected no dispatch for bad JSON, got: {dispatched}"
        finally:
            await ipc.stop()
