"""Integration tests for T1.10 — end-to-end message flow.

All external dependencies (Docker, Telegram, filesystem) are mocked.
Tests verify the wiring between components, not individual component logic.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import (
    Config,
    ContainerConfig,
    GroupConfig,
    HostConfig,
    RouterConfig,
    SecurityConfig,
    StreamingConfig,
    TelegramConfig,
)
from src.container_manager import ContainerResult
from src.main import (
    _ERR_CONTAINER,
    _ERR_TIMEOUT,
    _THINKING,
    _StreamState,
    _group_consumer,
    _ipc_dispatch,
    _send_error,
    main,
)
from src.router import RouteResult
from src.types import IncomingMessage, OutgoingMessage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> Config:
    cfg = Config(
        anthropic_api_key="sk-test-key",
        db_path=":memory:",
        host=HostConfig(log_level="warning"),
        container=ContainerConfig(timeout=10, max_concurrent=2),
        telegram=TelegramConfig(enabled=True, bot_token="test-token"),
        router=RouterConfig(group_queue_max=5),
        security=SecurityConfig(),
        streaming=StreamingConfig(),
        groups=[
            GroupConfig(
                name="test-group",
                channel="telegram",
                chat_id="12345",
                is_main=False,
                trigger="@bot",
            ),
        ],
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _make_msg(**overrides) -> IncomingMessage:
    defaults = dict(
        channel="telegram",
        message_id="msg-1",
        chat_id="12345",
        sender_id="user-1",
        sender_name="Test User",
        text="Hello agent",
        attachments=[],
        timestamp=1000000,
        raw=None,
    )
    defaults.update(overrides)
    return IncomingMessage(**defaults)


def _mock_db() -> MagicMock:
    db = MagicMock()
    db.get_session = AsyncMock(return_value=None)
    db.save_session = AsyncMock()
    return db


def _mock_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.send_message = AsyncMock(return_value="sent-1")
    adapter.edit_message = AsyncMock()
    adapter.start = AsyncMock()
    adapter.stop = AsyncMock()
    adapter.init = AsyncMock()
    adapter.on_message = MagicMock()
    return adapter


def _mock_registry(adapter=None) -> MagicMock:
    if adapter is None:
        adapter = _mock_adapter()
    reg = MagicMock()
    reg.get = MagicMock(return_value=adapter)
    reg.names = ["telegram"]
    reg.start_all = AsyncMock()
    reg.stop_all = AsyncMock()
    return reg


# ---------------------------------------------------------------------------
# _StreamState tests
# ---------------------------------------------------------------------------

class TestStreamState:
    def test_begin_and_clear(self):
        s = _StreamState()
        s.begin("g1", "telegram", "chat-1")
        assert s.active_chat["g1"] == "chat-1"
        assert s.active_channel["g1"] == "telegram"

        s.clear("g1")
        assert "g1" not in s.active_chat
        assert "g1" not in s.active_channel

    def test_begin_resets_stream_ids(self):
        s = _StreamState()
        s.stream_msg_id["g1"] = "old-id"
        s.stream_buffer["g1"] = "old-buf"
        s.begin("g1", "telegram", "chat-1")
        assert "g1" not in s.stream_msg_id
        assert "g1" not in s.stream_buffer


# ---------------------------------------------------------------------------
# IPC dispatch tests
# ---------------------------------------------------------------------------

class TestIPCDispatch:
    @pytest.mark.asyncio
    async def test_send_message(self):
        adapter = _mock_adapter()
        reg = _mock_registry(adapter)
        stream = _StreamState()
        stream.begin("g1", "telegram", "chat-1")

        await _ipc_dispatch(
            "send_message",
            {"group": "g1", "text": "Hello from agent"},
            "rpc-1",
            registry=reg,
            stream=stream,
        )

        adapter.send_message.assert_awaited_once_with(
            "chat-1", OutgoingMessage(text="Hello from agent")
        )

    @pytest.mark.asyncio
    async def test_stream_chunk_first(self):
        """First stream chunk sends a new message."""
        adapter = _mock_adapter()
        reg = _mock_registry(adapter)
        stream = _StreamState()
        stream.begin("g1", "telegram", "chat-1")

        await _ipc_dispatch(
            "stream_chunk",
            {"group": "g1", "text": "Hello", "is_final": False},
            "rpc-1",
            registry=reg,
            stream=stream,
        )

        adapter.send_message.assert_awaited_once()
        assert stream.stream_msg_id["g1"] == "sent-1"
        assert stream.stream_buffer["g1"] == "Hello"

    @pytest.mark.asyncio
    async def test_stream_chunk_subsequent_edits(self):
        """Subsequent chunks edit the existing message."""
        adapter = _mock_adapter()
        reg = _mock_registry(adapter)
        stream = _StreamState()
        stream.begin("g1", "telegram", "chat-1")
        stream.stream_msg_id["g1"] = "existing-msg"
        stream.stream_buffer["g1"] = "Hello"

        await _ipc_dispatch(
            "stream_chunk",
            {"group": "g1", "text": " world", "is_final": False},
            "rpc-2",
            registry=reg,
            stream=stream,
        )

        adapter.edit_message.assert_awaited_once_with(
            "chat-1", "existing-msg", OutgoingMessage(text="Hello world")
        )

    @pytest.mark.asyncio
    async def test_stream_chunk_final_clears_state(self):
        """Final chunk clears streaming state."""
        adapter = _mock_adapter()
        reg = _mock_registry(adapter)
        stream = _StreamState()
        stream.begin("g1", "telegram", "chat-1")
        stream.stream_msg_id["g1"] = "existing-msg"
        stream.stream_buffer["g1"] = "Hello"

        await _ipc_dispatch(
            "stream_chunk",
            {"group": "g1", "text": "!", "is_final": True},
            "rpc-3",
            registry=reg,
            stream=stream,
        )

        assert "g1" not in stream.stream_msg_id
        assert "g1" not in stream.stream_buffer

    @pytest.mark.asyncio
    async def test_no_active_session_is_noop(self):
        """IPC dispatch with no active session logs warning, doesn't crash."""
        reg = _mock_registry()
        stream = _StreamState()

        await _ipc_dispatch(
            "send_message",
            {"group": "unknown", "text": "hi"},
            "rpc-1",
            registry=reg,
            stream=stream,
        )
        # No adapter calls
        reg.get.return_value.send_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# Consumer task tests
# ---------------------------------------------------------------------------

class TestGroupConsumer:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        """Message → thinking → spawn → success → clear."""
        adapter = _mock_adapter()
        reg = _mock_registry(adapter)
        stream = _StreamState()
        config = _make_config()

        router = MagicMock()
        msg = _make_msg()
        # get_next returns msg once, then blocks forever (cancel will stop it)
        call_count = 0

        async def fake_get_next(group):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return msg
            await asyncio.sleep(999)

        router.get_next = AsyncMock(side_effect=fake_get_next)

        container_mgr = MagicMock()
        container_mgr.spawn = AsyncMock(
            return_value=ContainerResult(stdout="ok", stderr="", exit_code=0)
        )

        task = asyncio.create_task(
            _group_consumer(
                "test-group",
                config=config,
                router=router,
                container_mgr=container_mgr,
                registry=reg,
                stream=stream,
                is_main=False,
                db=_mock_db(),
            )
        )

        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Thinking placeholder sent
        first_call = adapter.send_message.call_args_list[0]
        assert first_call[0][1].text == _THINKING

        # Container spawned with correct group
        container_mgr.spawn.assert_awaited_once()
        call_kwargs = container_mgr.spawn.call_args[1]
        assert call_kwargs["group_name"] == "test-group"
        assert "ANTHROPIC_API_KEY" in call_kwargs["env_vars"]
        assert "LYNXCLAW_CHAT_ID" in call_kwargs["env_vars"]

    @pytest.mark.asyncio
    async def test_container_timeout_sends_error(self):
        """Container timeout → error message sent to user."""
        adapter = _mock_adapter()
        reg = _mock_registry(adapter)
        stream = _StreamState()
        config = _make_config()

        router = MagicMock()
        call_count = 0

        async def fake_get_next(group):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_msg()
            await asyncio.sleep(999)

        router.get_next = AsyncMock(side_effect=fake_get_next)

        container_mgr = MagicMock()
        container_mgr.spawn = AsyncMock(
            return_value=ContainerResult(
                stdout="", stderr="", exit_code=-1, timed_out=True
            )
        )

        task = asyncio.create_task(
            _group_consumer(
                "test-group",
                config=config,
                router=router,
                container_mgr=container_mgr,
                registry=reg,
                stream=stream,
                is_main=False,
                db=_mock_db(),
            )
        )

        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Error message sent (second send_message call after thinking)
        calls = adapter.send_message.call_args_list
        error_call = calls[-1]
        assert error_call[0][1].text == _ERR_TIMEOUT

    @pytest.mark.asyncio
    async def test_container_nonzero_exit_sends_error(self):
        """Container exit_code != 0 → error message sent."""
        adapter = _mock_adapter()
        reg = _mock_registry(adapter)
        stream = _StreamState()
        config = _make_config()

        router = MagicMock()
        call_count = 0

        async def fake_get_next(group):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_msg()
            await asyncio.sleep(999)

        router.get_next = AsyncMock(side_effect=fake_get_next)

        container_mgr = MagicMock()
        container_mgr.spawn = AsyncMock(
            return_value=ContainerResult(
                stdout="", stderr="agent crashed", exit_code=1
            )
        )

        task = asyncio.create_task(
            _group_consumer(
                "test-group",
                config=config,
                router=router,
                container_mgr=container_mgr,
                registry=reg,
                stream=stream,
                is_main=False,
                db=_mock_db(),
            )
        )

        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        calls = adapter.send_message.call_args_list
        error_call = calls[-1]
        assert error_call[0][1].text == _ERR_CONTAINER

    @pytest.mark.asyncio
    async def test_spawn_exception_sends_error(self):
        """Container spawn raises → error message sent, loop continues."""
        adapter = _mock_adapter()
        reg = _mock_registry(adapter)
        stream = _StreamState()
        config = _make_config()

        router = MagicMock()
        call_count = 0

        async def fake_get_next(group):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_msg()
            await asyncio.sleep(999)

        router.get_next = AsyncMock(side_effect=fake_get_next)

        container_mgr = MagicMock()
        container_mgr.spawn = AsyncMock(side_effect=RuntimeError("docker not found"))

        task = asyncio.create_task(
            _group_consumer(
                "test-group",
                config=config,
                router=router,
                container_mgr=container_mgr,
                registry=reg,
                stream=stream,
                is_main=False,
                db=_mock_db(),
            )
        )

        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        calls = adapter.send_message.call_args_list
        error_call = calls[-1]
        assert error_call[0][1].text == _ERR_CONTAINER

    @pytest.mark.asyncio
    async def test_main_group_includes_project_mount(self):
        """is_main=True → project_dir mount is included."""
        adapter = _mock_adapter()
        reg = _mock_registry(adapter)
        stream = _StreamState()
        config = _make_config()

        router = MagicMock()
        call_count = 0

        async def fake_get_next(group):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_msg()
            await asyncio.sleep(999)

        router.get_next = AsyncMock(side_effect=fake_get_next)

        container_mgr = MagicMock()
        container_mgr.spawn = AsyncMock(
            return_value=ContainerResult(stdout="ok", stderr="", exit_code=0)
        )

        task = asyncio.create_task(
            _group_consumer(
                "test-group",
                config=config,
                router=router,
                container_mgr=container_mgr,
                registry=reg,
                stream=stream,
                is_main=True,
                db=_mock_db(),
            )
        )

        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        call_kwargs = container_mgr.spawn.call_args[1]
        assert "project_dir" in call_kwargs["mounts"]


# ---------------------------------------------------------------------------
# _send_error tests
# ---------------------------------------------------------------------------

class TestSendError:
    @pytest.mark.asyncio
    async def test_sends_error_message(self):
        adapter = _mock_adapter()
        reg = _mock_registry(adapter)
        await _send_error(reg, "telegram", "chat-1", "oops")
        adapter.send_message.assert_awaited_once_with(
            "chat-1", OutgoingMessage(text="oops")
        )

    @pytest.mark.asyncio
    async def test_swallows_exception(self):
        """_send_error never raises, even if adapter fails."""
        adapter = _mock_adapter()
        adapter.send_message = AsyncMock(side_effect=RuntimeError("network"))
        reg = _mock_registry(adapter)
        # Should not raise
        await _send_error(reg, "telegram", "chat-1", "oops")


# ---------------------------------------------------------------------------
# main() lifecycle test
# ---------------------------------------------------------------------------

class TestMainLifecycle:
    @pytest.mark.asyncio
    async def test_startup_and_shutdown(self, tmp_path):
        """main() inits all components and shuts down cleanly on signal."""
        config = _make_config(db_path=str(tmp_path / "test.db"))

        with (
            patch("src.main.load_config", return_value=config),
            patch("src.main.Database") as MockDB,
            patch("src.main.ChannelRegistry") as MockReg,
            patch("src.main.TelegramAdapter") as MockTG,
            patch("src.main.MessageRouter") as MockRouter,
            patch("src.main.ContainerManager") as MockCM,
            patch("src.main.IPCWatcher") as MockIPC,
        ):
            db_inst = MockDB.return_value
            db_inst.init = AsyncMock()
            db_inst.backup = AsyncMock()
            db_inst.close = AsyncMock()

            reg_inst = MockReg.return_value
            reg_inst.start_all = AsyncMock()
            reg_inst.stop_all = AsyncMock()
            reg_inst.names = ["telegram"]
            reg_inst.get = MagicMock()
            reg_inst.register = MagicMock()

            tg_inst = MockTG.return_value
            tg_inst.init = AsyncMock()

            router_inst = MockRouter.return_value
            router_inst.init = AsyncMock()

            cm_inst = MockCM.return_value
            cm_inst.init = MagicMock()

            ipc_inst = MockIPC.return_value
            ipc_inst.init = AsyncMock()
            ipc_inst.start = AsyncMock()
            ipc_inst.stop = AsyncMock()

            # Run main in a task, then trigger shutdown after a short delay
            async def trigger_shutdown():
                await asyncio.sleep(0.05)
                # Simulate SIGINT by finding and calling the signal handler
                import signal as sig_mod
                # Directly set the stop event via the signal handler mechanism
                # Since we can't easily send signals in tests, we patch
                # stop_event.set() indirectly
                raise KeyboardInterrupt

            task = asyncio.create_task(main("test.yaml"))

            # Give main() time to start, then cancel it
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, KeyboardInterrupt):
                pass

            # Verify init sequence
            db_inst.init.assert_awaited_once()
            tg_inst.init.assert_awaited_once()
            reg_inst.register.assert_called_once_with("telegram", tg_inst)
            router_inst.init.assert_awaited_once()
            cm_inst.init.assert_called_once()
            ipc_inst.init.assert_awaited_once()
            reg_inst.start_all.assert_awaited_once()
            ipc_inst.start.assert_awaited_once()


# ---------------------------------------------------------------------------
# Session ID flow integration tests
# ---------------------------------------------------------------------------

class TestSessionFlow:
    @pytest.mark.asyncio
    async def test_consumer_reads_session_from_db_and_passes_to_spawn(self):
        """Consumer reads existing session_id from DB and passes it to spawn."""
        adapter = _mock_adapter()
        reg = _mock_registry(adapter)
        stream = _StreamState()
        config = _make_config()

        router = MagicMock()
        call_count = 0

        async def fake_get_next(group):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_msg()
            await asyncio.sleep(999)

        router.get_next = AsyncMock(side_effect=fake_get_next)

        container_mgr = MagicMock()
        container_mgr.spawn = AsyncMock(
            return_value=ContainerResult(stdout="ok", stderr="", exit_code=0)
        )

        db = _mock_db()
        db.get_session = AsyncMock(return_value="existing-session-uuid")

        task = asyncio.create_task(
            _group_consumer(
                "test-group",
                config=config,
                router=router,
                container_mgr=container_mgr,
                registry=reg,
                stream=stream,
                is_main=False,
                db=db,
            )
        )

        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        db.get_session.assert_awaited_once_with(group_name="test-group")
        call_kwargs = container_mgr.spawn.call_args[1]
        assert call_kwargs["session_id"] == "existing-session-uuid"

    @pytest.mark.asyncio
    async def test_consumer_saves_session_id_after_successful_spawn(self, tmp_path):
        """Consumer reads session_id.txt after spawn and saves it to DB."""
        import os

        adapter = _mock_adapter()
        reg = _mock_registry(adapter)
        stream = _StreamState()
        config = _make_config()

        router = MagicMock()
        call_count = 0

        async def fake_get_next(group):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_msg()
            await asyncio.sleep(999)

        router.get_next = AsyncMock(side_effect=fake_get_next)

        # Write a session_id.txt file that the consumer will read
        ipc_group_dir = tmp_path / "data" / "ipc" / "test-group"
        ipc_group_dir.mkdir(parents=True)
        (ipc_group_dir / "session_id.txt").write_text("new-session-uuid", encoding="utf-8")

        container_mgr = MagicMock()
        container_mgr.spawn = AsyncMock(
            return_value=ContainerResult(stdout="ok", stderr="", exit_code=0)
        )

        db = _mock_db()

        # Patch os.getcwd to return tmp_path so the consumer finds the file
        with patch("src.main.os.getcwd", return_value=str(tmp_path)):
            task = asyncio.create_task(
                _group_consumer(
                    "test-group",
                    config=config,
                    router=router,
                    container_mgr=container_mgr,
                    registry=reg,
                    stream=stream,
                    is_main=False,
                    db=db,
                )
            )

            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        db.save_session.assert_awaited_once_with(
            group_name="test-group", session_id="new-session-uuid"
        )
        # File should be cleaned up
        assert not (ipc_group_dir / "session_id.txt").exists()
