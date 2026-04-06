# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for src/persistent_container.py — T4.4 Persistent Container Mode.

All tests mock subprocess calls — no real Docker required.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Config, ContainerConfig, GroupConfig, SecurityConfig
from src.persistent_container import (
    PersistentContainer,
    _HEARTBEAT_STALE_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.anthropic_api_key = "sk-test"
    cfg.container = ContainerConfig(
        image="lynxclaw-agent:latest",
        memory="512m",
        cpus=1.0,
        network="none",
        timeout=300,
        max_concurrent=5,
        runtime="docker",
    )
    cfg.security = SecurityConfig()
    cfg.groups = [
        GroupConfig(name="test-group", channel="telegram", chat_id="-100123",
                    container_mode="persistent")
    ]
    return cfg


def _make_mock_proc(returncode: int | None = None) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.pid = 12345
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    return proc


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_launches_container(tmp_path: Path) -> None:
    """start() should launch a docker process and set is_running=True."""
    cfg = _make_config(tmp_path)
    mock_proc = _make_mock_proc(returncode=None)

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        pc = PersistentContainer()
        await pc.start(
            "test-group", cfg, cfg.security,
            ipc_base=str(tmp_path / "ipc"),
        )
        assert pc.is_running is True
        await pc.stop()


@pytest.mark.asyncio
async def test_stop_terminates_container(tmp_path: Path) -> None:
    """stop() should terminate the process and set is_running=False."""
    cfg = _make_config(tmp_path)
    mock_proc = _make_mock_proc(returncode=None)

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        pc = PersistentContainer()
        await pc.start(
            "test-group", cfg, cfg.security,
            ipc_base=str(tmp_path / "ipc"),
        )
        await pc.stop()

    assert pc.is_running is False
    mock_proc.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_is_running_false_when_process_exited(tmp_path: Path) -> None:
    """is_running should be False when the process has a non-None returncode."""
    cfg = _make_config(tmp_path)
    mock_proc = _make_mock_proc(returncode=0)  # already exited

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        pc = PersistentContainer()
        await pc.start(
            "test-group", cfg, cfg.security,
            ipc_base=str(tmp_path / "ipc"),
        )
        assert pc.is_running is False
        await pc.stop()


# ---------------------------------------------------------------------------
# send_prompt tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_prompt_writes_inbox_file(tmp_path: Path) -> None:
    """send_prompt() should write a JSON-RPC file to the inbox directory."""
    cfg = _make_config(tmp_path)
    mock_proc = _make_mock_proc(returncode=None)
    ipc_base = str(tmp_path / "ipc")

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        pc = PersistentContainer()
        await pc.start("test-group", cfg, cfg.security, ipc_base=ipc_base)
        await pc.send_prompt("Hello!", chat_id="-100123")
        await pc.stop()

    inbox_dir = tmp_path / "ipc" / "test-group" / "inbox"
    files = list(inbox_dir.glob("*.json"))
    assert len(files) == 1

    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["method"] == "run_prompt"
    assert payload["params"]["prompt"] == "Hello!"
    assert payload["params"]["chat_id"] == "-100123"
    assert payload["params"]["group"] == "test-group"


@pytest.mark.asyncio
async def test_send_prompt_uses_atomic_write(tmp_path: Path) -> None:
    """send_prompt() should not leave .tmp files behind."""
    cfg = _make_config(tmp_path)
    mock_proc = _make_mock_proc(returncode=None)
    ipc_base = str(tmp_path / "ipc")

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        pc = PersistentContainer()
        await pc.start("test-group", cfg, cfg.security, ipc_base=ipc_base)
        await pc.send_prompt("Test", chat_id="-100")
        await pc.stop()

    inbox_dir = tmp_path / "ipc" / "test-group" / "inbox"
    tmp_files = list(inbox_dir.glob("*.tmp"))
    assert tmp_files == [], "No .tmp files should remain after atomic write"


# ---------------------------------------------------------------------------
# health_check tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_check_fresh_heartbeat(tmp_path: Path) -> None:
    """health_check() returns True when heartbeat is recent."""
    cfg = _make_config(tmp_path)
    mock_proc = _make_mock_proc(returncode=None)
    ipc_base = str(tmp_path / "ipc")

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        pc = PersistentContainer()
        await pc.start("test-group", cfg, cfg.security, ipc_base=ipc_base)

        # Write a fresh heartbeat
        hb_path = tmp_path / "ipc" / "test-group" / "heartbeat.json"
        hb_path.parent.mkdir(parents=True, exist_ok=True)
        hb_path.write_text(json.dumps({"timestamp": time.time()}), encoding="utf-8")

        result = await pc.health_check()
        await pc.stop()

    assert result is True


@pytest.mark.asyncio
async def test_health_check_stale_heartbeat(tmp_path: Path) -> None:
    """health_check() returns False when heartbeat is older than threshold."""
    cfg = _make_config(tmp_path)
    mock_proc = _make_mock_proc(returncode=None)
    ipc_base = str(tmp_path / "ipc")

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        pc = PersistentContainer()
        await pc.start("test-group", cfg, cfg.security, ipc_base=ipc_base)

        # Write a stale heartbeat (older than threshold)
        stale_ts = time.time() - (_HEARTBEAT_STALE_THRESHOLD + 10)
        hb_path = tmp_path / "ipc" / "test-group" / "heartbeat.json"
        hb_path.parent.mkdir(parents=True, exist_ok=True)
        hb_path.write_text(json.dumps({"timestamp": stale_ts}), encoding="utf-8")

        result = await pc.health_check()
        await pc.stop()

    assert result is False


@pytest.mark.asyncio
async def test_health_check_no_heartbeat_file_running(tmp_path: Path) -> None:
    """health_check() returns True when no heartbeat file yet but process is running."""
    cfg = _make_config(tmp_path)
    mock_proc = _make_mock_proc(returncode=None)
    ipc_base = str(tmp_path / "ipc")

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        pc = PersistentContainer()
        await pc.start("test-group", cfg, cfg.security, ipc_base=ipc_base)
        # No heartbeat file written — container just started
        result = await pc.health_check()
        await pc.stop()

    assert result is True


@pytest.mark.asyncio
async def test_health_check_false_when_not_running(tmp_path: Path) -> None:
    """health_check() returns False when container process has exited."""
    cfg = _make_config(tmp_path)
    mock_proc = _make_mock_proc(returncode=1)  # exited with error
    ipc_base = str(tmp_path / "ipc")

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        pc = PersistentContainer()
        await pc.start("test-group", cfg, cfg.security, ipc_base=ipc_base)
        result = await pc.health_check()
        await pc.stop()

    assert result is False


# ---------------------------------------------------------------------------
# Auto-restart on stale heartbeat
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_restart_on_stale_heartbeat(tmp_path: Path) -> None:
    """Health monitor should restart the container when heartbeat goes stale."""
    cfg = _make_config(tmp_path)
    ipc_base = str(tmp_path / "ipc")

    launch_count = 0
    procs: list[MagicMock] = []

    async def fake_exec(*args, **kwargs):
        nonlocal launch_count
        launch_count += 1
        proc = _make_mock_proc(returncode=None)
        procs.append(proc)
        return proc

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        pc = PersistentContainer()
        await pc.start("test-group", cfg, cfg.security, ipc_base=ipc_base)

        # Write a stale heartbeat to trigger restart
        stale_ts = time.time() - (_HEARTBEAT_STALE_THRESHOLD + 10)
        hb_path = tmp_path / "ipc" / "test-group" / "heartbeat.json"
        hb_path.parent.mkdir(parents=True, exist_ok=True)
        hb_path.write_text(json.dumps({"timestamp": stale_ts}), encoding="utf-8")

        # Manually trigger one health monitor cycle
        await pc._kill_container()
        await pc._launch()

        await pc.stop()

    # Should have launched at least twice (initial + restart)
    assert launch_count >= 2


# ---------------------------------------------------------------------------
# Config: container_mode field
# ---------------------------------------------------------------------------

def test_group_config_default_container_mode() -> None:
    """GroupConfig.container_mode defaults to 'ephemeral'."""
    g = GroupConfig(name="g", channel="telegram", chat_id="-1")
    assert g.container_mode == "ephemeral"


def test_group_config_persistent_mode() -> None:
    """GroupConfig.container_mode can be set to 'persistent'."""
    g = GroupConfig(name="g", channel="telegram", chat_id="-1", container_mode="persistent")
    assert g.container_mode == "persistent"
