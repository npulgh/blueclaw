"""Tests for src/swarm.py — T4.2 Agent Swarms.

All tests mock ContainerManager and DB — no real Docker or SQLite required.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Config, ContainerConfig, GroupConfig, SecurityConfig
from src.container_manager import ContainerResult
from src.swarm import PermissionError, SwarmCoordinator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_config(groups: list[GroupConfig]) -> Config:
    cfg = Config()
    cfg.anthropic_api_key = "sk-test"
    cfg.groups = groups
    return cfg


def _make_result(stdout: str = "", exit_code: int = 0) -> ContainerResult:
    return ContainerResult(stdout=stdout, stderr="", exit_code=exit_code)


@pytest.fixture
def two_group_config():
    return _make_config([
        GroupConfig(name="main", channel="telegram", chat_id="111", is_main=True),
        GroupConfig(name="worker", channel="telegram", chat_id="222", is_main=False),
    ])


@pytest.fixture
def three_group_config():
    return _make_config([
        GroupConfig(name="main", channel="telegram", chat_id="111", is_main=True),
        GroupConfig(name="alpha", channel="telegram", chat_id="222", is_main=False),
        GroupConfig(name="beta", channel="telegram", chat_id="333", is_main=False),
    ])


@pytest.fixture
def mock_container_mgr():
    mgr = MagicMock()
    mgr.spawn = AsyncMock(return_value=_make_result(stdout="delegated output"))
    return mgr


@pytest.fixture
def mock_db():
    return MagicMock()


async def _make_swarm(config, container_mgr, db) -> SwarmCoordinator:
    swarm = SwarmCoordinator()
    await swarm.init(db, container_mgr, config)
    return swarm


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_init_requires_init_before_use(two_group_config, mock_db):
    swarm = SwarmCoordinator()
    with pytest.raises(RuntimeError, match="init\\(\\)"):
        await swarm.delegate("main", "worker", "hello")


# ---------------------------------------------------------------------------
# delegate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delegate_spawns_container_in_target_group(
    two_group_config, mock_container_mgr, mock_db, tmp_path
):
    swarm = await _make_swarm(two_group_config, mock_container_mgr, mock_db)

    with patch("os.getcwd", return_value=str(tmp_path)):
        result = await swarm.delegate("main", "worker", "do something")

    assert result == "delegated output"
    mock_container_mgr.spawn.assert_awaited_once()
    call_kwargs = mock_container_mgr.spawn.call_args
    assert call_kwargs.kwargs["group_name"] == "worker"
    assert call_kwargs.kwargs["prompt"] == "do something"


@pytest.mark.asyncio
async def test_delegate_prepends_context_to_prompt(
    two_group_config, mock_container_mgr, mock_db, tmp_path
):
    swarm = await _make_swarm(two_group_config, mock_container_mgr, mock_db)

    with patch("os.getcwd", return_value=str(tmp_path)):
        await swarm.delegate("main", "worker", "do something", context="background info")

    call_kwargs = mock_container_mgr.spawn.call_args
    prompt_sent = call_kwargs.kwargs["prompt"]
    assert "background info" in prompt_sent
    assert "do something" in prompt_sent


@pytest.mark.asyncio
async def test_delegate_sets_swarm_env_vars(
    two_group_config, mock_container_mgr, mock_db, tmp_path
):
    swarm = await _make_swarm(two_group_config, mock_container_mgr, mock_db)

    with patch("os.getcwd", return_value=str(tmp_path)):
        await swarm.delegate("main", "worker", "task")

    env = mock_container_mgr.spawn.call_args.kwargs["env_vars"]
    assert env.get("LYNXCLAW_SWARM_FROM") == "main"
    assert env.get("LYNXCLAW_SWARM_DELEGATED") == "1"


@pytest.mark.asyncio
async def test_delegate_raises_for_unknown_target(
    two_group_config, mock_container_mgr, mock_db
):
    swarm = await _make_swarm(two_group_config, mock_container_mgr, mock_db)
    with pytest.raises(ValueError, match="not configured"):
        await swarm.delegate("main", "nonexistent", "task")


# ---------------------------------------------------------------------------
# Permission model
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_main_can_delegate_to_non_main(
    two_group_config, mock_container_mgr, mock_db, tmp_path
):
    swarm = await _make_swarm(two_group_config, mock_container_mgr, mock_db)
    with patch("os.getcwd", return_value=str(tmp_path)):
        # Should not raise
        await swarm.delegate("main", "worker", "task")
    mock_container_mgr.spawn.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_main_can_delegate_to_main(
    two_group_config, mock_container_mgr, mock_db, tmp_path
):
    swarm = await _make_swarm(two_group_config, mock_container_mgr, mock_db)
    with patch("os.getcwd", return_value=str(tmp_path)):
        await swarm.delegate("worker", "main", "help me")
    mock_container_mgr.spawn.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_main_cannot_delegate_to_non_main(
    three_group_config, mock_container_mgr, mock_db
):
    swarm = await _make_swarm(three_group_config, mock_container_mgr, mock_db)
    with pytest.raises(PermissionError, match="Non-main group"):
        await swarm.delegate("alpha", "beta", "task")
    mock_container_mgr.spawn.assert_not_awaited()


# ---------------------------------------------------------------------------
# read_context
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_context_returns_claude_md_content(
    two_group_config, mock_container_mgr, mock_db, tmp_path
):
    # Create a fake CLAUDE.md for the worker group
    worker_dir = tmp_path / "groups" / "worker"
    worker_dir.mkdir(parents=True)
    (worker_dir / "CLAUDE.md").write_text("# Worker context", encoding="utf-8")

    swarm = await _make_swarm(two_group_config, mock_container_mgr, mock_db)

    with patch("os.getcwd", return_value=str(tmp_path)):
        content = await swarm.read_context("main", "worker")

    assert content == "# Worker context"


@pytest.mark.asyncio
async def test_read_context_returns_none_when_missing(
    two_group_config, mock_container_mgr, mock_db, tmp_path
):
    swarm = await _make_swarm(two_group_config, mock_container_mgr, mock_db)

    with patch("os.getcwd", return_value=str(tmp_path)):
        content = await swarm.read_context("main", "worker")

    assert content is None


@pytest.mark.asyncio
async def test_read_context_permission_denied_for_non_main(
    three_group_config, mock_container_mgr, mock_db
):
    swarm = await _make_swarm(three_group_config, mock_container_mgr, mock_db)
    with pytest.raises(PermissionError):
        await swarm.read_context("alpha", "beta")


# ---------------------------------------------------------------------------
# broadcast
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_broadcast_delegates_to_all_other_groups(
    three_group_config, mock_container_mgr, mock_db, tmp_path
):
    swarm = await _make_swarm(three_group_config, mock_container_mgr, mock_db)

    with patch("os.getcwd", return_value=str(tmp_path)):
        results = await swarm.broadcast("main", "hello everyone")

    assert set(results.keys()) == {"alpha", "beta"}
    assert mock_container_mgr.spawn.await_count == 2


@pytest.mark.asyncio
async def test_broadcast_with_explicit_targets(
    three_group_config, mock_container_mgr, mock_db, tmp_path
):
    swarm = await _make_swarm(three_group_config, mock_container_mgr, mock_db)

    with patch("os.getcwd", return_value=str(tmp_path)):
        results = await swarm.broadcast("main", "ping", target_groups=["alpha"])

    assert list(results.keys()) == ["alpha"]
    assert mock_container_mgr.spawn.await_count == 1


@pytest.mark.asyncio
async def test_broadcast_non_main_without_targets_raises(
    three_group_config, mock_container_mgr, mock_db
):
    swarm = await _make_swarm(three_group_config, mock_container_mgr, mock_db)
    with pytest.raises(PermissionError, match="Non-main group"):
        await swarm.broadcast("alpha", "hello")


@pytest.mark.asyncio
async def test_broadcast_continues_on_individual_failure(
    three_group_config, mock_container_mgr, mock_db, tmp_path
):
    # alpha succeeds, beta raises
    async def _spawn_side_effect(**kwargs):
        if kwargs["group_name"] == "beta":
            raise RuntimeError("beta container failed")
        return _make_result(stdout="ok")

    mock_container_mgr.spawn = AsyncMock(side_effect=_spawn_side_effect)

    swarm = await _make_swarm(three_group_config, mock_container_mgr, mock_db)

    with patch("os.getcwd", return_value=str(tmp_path)):
        results = await swarm.broadcast("main", "task")

    assert results["alpha"] == "ok"
    assert results["beta"] == ""  # failure → empty string, not exception
