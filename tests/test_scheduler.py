# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for src/scheduler.py — TaskScheduler.

All tests mock ContainerManager and Database — no real Docker required.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.scheduler import TaskScheduler, _POLL_INTERVAL


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_stream():
    """Return a mock _StreamState with begin() and clear() methods."""
    stream = MagicMock()
    stream.begin = MagicMock()
    stream.clear = MagicMock()
    return stream


def _make_task(
    *,
    task_id: Optional[str] = None,
    group_name: str = "main",
    schedule: str = "*/1 * * * *",
    prompt: str = "Hello",
    next_run: Optional[int] = None,
    last_run: int = 0,
    status: str = "active",
) -> dict:
    """Build a task dict as returned by db.get_active_tasks()."""
    return {
        "id": task_id or str(uuid.uuid4()),
        "group_name": group_name,
        "type": "cron",
        "schedule": schedule,
        "prompt": prompt,
        "status": status,
        "last_run": last_run,
        "next_run": next_run,
        "created_at": int(time.time()),
    }


def _make_group_config(
    name: str = "main",
    channel: str = "telegram",
    chat_id: str = "chat-123",
    is_main: bool = True,
):
    g = MagicMock()
    g.name = name
    g.channel = channel
    g.chat_id = chat_id
    g.is_main = is_main
    return g


def _make_config(groups=None):
    cfg = MagicMock()
    cfg.groups = groups or [_make_group_config()]
    cfg.anthropic_api_key = "test-key"
    return cfg


def _make_container_result(exit_code: int = 0, timed_out: bool = False):
    from src.container_manager import ContainerResult
    return ContainerResult(
        stdout="", stderr="", exit_code=exit_code, timed_out=timed_out
    )


# ---------------------------------------------------------------------------
# _compute_next_run
# ---------------------------------------------------------------------------

def test_compute_next_run_every_minute():
    """Every-minute cron schedule advances by ~60 seconds."""
    sched = TaskScheduler()
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    next_run = sched._compute_next_run("* * * * *", base)
    # Should be 2026-01-01 12:01:00 UTC = base + 60s
    assert next_run == int(datetime(2026, 1, 1, 12, 1, 0, tzinfo=timezone.utc).timestamp())


def test_compute_next_run_every_5_minutes():
    """*/5 cron schedule advances to next 5-minute mark."""
    sched = TaskScheduler()
    base = datetime(2026, 1, 1, 12, 3, 0, tzinfo=timezone.utc)  # 12:03
    next_run = sched._compute_next_run("*/5 * * * *", base)
    # Next occurrence after 12:03 is 12:05
    expected = int(datetime(2026, 1, 1, 12, 5, 0, tzinfo=timezone.utc).timestamp())
    assert next_run == expected


def test_compute_next_run_returns_int():
    """_compute_next_run always returns an integer timestamp."""
    sched = TaskScheduler()
    base = datetime.now(timezone.utc)
    result = sched._compute_next_run("0 * * * *", base)
    assert isinstance(result, int)
    assert result > int(base.timestamp())


def test_compute_next_run_daily():
    """Daily cron (midnight) advances by ~24 hours."""
    sched = TaskScheduler()
    base = datetime(2026, 3, 15, 8, 0, 0, tzinfo=timezone.utc)
    next_run = sched._compute_next_run("0 0 * * *", base)
    expected = int(datetime(2026, 3, 16, 0, 0, 0, tzinfo=timezone.utc).timestamp())
    assert next_run == expected


# ---------------------------------------------------------------------------
# init() — seeds next_run for tasks missing it
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_init_seeds_missing_next_run():
    """init() calls update_task_run for tasks with no next_run."""
    task = _make_task(next_run=None)

    db = MagicMock()
    db.get_active_tasks = AsyncMock(return_value=[task])
    db.update_task_run = AsyncMock()

    sched = TaskScheduler()
    await sched.init(
        db=db,
        container_mgr=MagicMock(),
        config=_make_config(),
        registry=MagicMock(),
        stream=_make_stream(),
    )

    db.update_task_run.assert_awaited_once()
    call_kwargs = db.update_task_run.call_args.kwargs
    assert call_kwargs["task_id"] == task["id"]
    assert call_kwargs["next_run"] > int(time.time()) - 5  # computed recently


@pytest.mark.asyncio
async def test_init_skips_tasks_with_next_run():
    """init() does not call update_task_run for tasks that already have next_run."""
    task = _make_task(next_run=int(time.time()) + 3600)

    db = MagicMock()
    db.get_active_tasks = AsyncMock(return_value=[task])
    db.update_task_run = AsyncMock()

    sched = TaskScheduler()
    await sched.init(
        db=db,
        container_mgr=MagicMock(),
        config=_make_config(),
        registry=MagicMock(),
        stream=_make_stream(),
    )

    db.update_task_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_init_handles_bad_cron_gracefully():
    """init() logs a warning but does not raise for an invalid cron expression."""
    task = _make_task(schedule="not-a-cron", next_run=None)

    db = MagicMock()
    db.get_active_tasks = AsyncMock(return_value=[task])
    db.update_task_run = AsyncMock()

    sched = TaskScheduler()
    # Should not raise
    await sched.init(
        db=db,
        container_mgr=MagicMock(),
        config=_make_config(),
        registry=MagicMock(),
        stream=_make_stream(),
    )
    # update_task_run should NOT be called for the bad cron
    db.update_task_run.assert_not_awaited()


# ---------------------------------------------------------------------------
# _check_and_run_due_tasks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_due_tasks_trigger_container_spawn():
    """Tasks with next_run <= now are executed via container spawn."""
    now_ts = int(time.time())
    task = _make_task(next_run=now_ts - 60)  # overdue by 60s

    db = MagicMock()
    db.get_active_tasks = AsyncMock(return_value=[task])
    db.update_task_run = AsyncMock()
    db.get_session = AsyncMock(return_value=None)

    container_mgr = MagicMock()
    container_mgr.spawn = AsyncMock(return_value=_make_container_result())

    stream = _make_stream()

    sched = TaskScheduler()
    sched._db = db
    sched._container_mgr = container_mgr
    sched._config = _make_config()
    sched._registry = MagicMock()
    sched._stream = stream

    await sched._check_and_run_due_tasks()

    # Give the background task a chance to run
    await asyncio.sleep(0.1)

    container_mgr.spawn.assert_awaited_once()
    call_kwargs = container_mgr.spawn.call_args.kwargs
    assert call_kwargs["group_name"] == task["group_name"]
    assert call_kwargs["prompt"] == task["prompt"]


@pytest.mark.asyncio
async def test_future_tasks_not_triggered():
    """Tasks with next_run in the future are NOT executed."""
    task = _make_task(next_run=int(time.time()) + 3600)  # 1 hour from now

    db = MagicMock()
    db.get_active_tasks = AsyncMock(return_value=[task])
    db.update_task_run = AsyncMock()

    container_mgr = MagicMock()
    container_mgr.spawn = AsyncMock(return_value=_make_container_result())

    sched = TaskScheduler()
    sched._db = db
    sched._container_mgr = container_mgr
    sched._config = _make_config()
    sched._registry = MagicMock()
    sched._stream = _make_stream()

    await sched._check_and_run_due_tasks()
    await asyncio.sleep(0.05)

    container_mgr.spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_without_next_run_not_triggered():
    """Tasks with next_run=None are skipped (not yet seeded)."""
    task = _make_task(next_run=None)

    db = MagicMock()
    db.get_active_tasks = AsyncMock(return_value=[task])
    db.update_task_run = AsyncMock()

    container_mgr = MagicMock()
    container_mgr.spawn = AsyncMock(return_value=_make_container_result())

    sched = TaskScheduler()
    sched._db = db
    sched._container_mgr = container_mgr
    sched._config = _make_config()
    sched._registry = MagicMock()
    sched._stream = _make_stream()

    await sched._check_and_run_due_tasks()
    await asyncio.sleep(0.05)

    container_mgr.spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_task_run_called_before_spawn():
    """update_task_run is called before container spawn to prevent double-firing on crash."""
    now_ts = int(time.time())
    task = _make_task(next_run=now_ts - 10)

    call_order = []

    db = MagicMock()
    db.get_active_tasks = AsyncMock(return_value=[task])

    async def _update_task_run(*, task_id, last_run, next_run):
        call_order.append("update")
    db.update_task_run = AsyncMock(side_effect=_update_task_run)
    db.get_session = AsyncMock(return_value=None)

    async def _spawn(**kwargs):
        call_order.append("spawn")
        return _make_container_result()
    container_mgr = MagicMock()
    container_mgr.spawn = AsyncMock(side_effect=_spawn)

    sched = TaskScheduler()
    sched._db = db
    sched._container_mgr = container_mgr
    sched._config = _make_config()
    sched._registry = MagicMock()
    sched._stream = _make_stream()

    await sched._check_and_run_due_tasks()
    await asyncio.sleep(0.1)

    assert call_order == ["update", "spawn"]


# ---------------------------------------------------------------------------
# Cancelled tasks skipped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancelled_tasks_not_returned_by_get_active():
    """get_active_tasks only returns status='active'; cancelled tasks are not present."""
    # This test verifies the DB layer contract used by the scheduler.
    # We simply confirm that if get_active_tasks returns an empty list, no spawn happens.
    db = MagicMock()
    db.get_active_tasks = AsyncMock(return_value=[])  # no active tasks

    container_mgr = MagicMock()
    container_mgr.spawn = AsyncMock(return_value=_make_container_result())

    sched = TaskScheduler()
    sched._db = db
    sched._container_mgr = container_mgr
    sched._config = _make_config()
    sched._registry = MagicMock()
    sched._stream = _make_stream()

    await sched._check_and_run_due_tasks()
    await asyncio.sleep(0.05)

    container_mgr.spawn.assert_not_awaited()


# ---------------------------------------------------------------------------
# _run_task — error handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_task_clears_stream_on_spawn_error():
    """stream.clear() is called even when container spawn raises an exception."""
    task = _make_task()
    stream = _make_stream()

    db = MagicMock()
    db.get_session = AsyncMock(return_value=None)

    container_mgr = MagicMock()
    container_mgr.spawn = AsyncMock(side_effect=RuntimeError("docker not found"))

    sched = TaskScheduler()
    sched._db = db
    sched._container_mgr = container_mgr
    sched._config = _make_config()
    sched._registry = MagicMock()
    sched._stream = stream

    await sched._run_task(task)

    stream.clear.assert_called_once_with(task["group_name"])


@pytest.mark.asyncio
async def test_run_task_skips_unknown_group():
    """_run_task logs an error and returns early if group is not in config."""
    task = _make_task(group_name="unknown-group")

    container_mgr = MagicMock()
    container_mgr.spawn = AsyncMock(return_value=_make_container_result())

    sched = TaskScheduler()
    sched._db = MagicMock()
    sched._container_mgr = container_mgr
    sched._config = _make_config(groups=[_make_group_config(name="main")])
    sched._registry = MagicMock()
    sched._stream = _make_stream()

    await sched._run_task(task)

    container_mgr.spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_task_clears_stream_on_success():
    """stream.clear() is called after successful container execution."""
    task = _make_task()
    stream = _make_stream()

    db = MagicMock()
    db.get_session = AsyncMock(return_value=None)

    container_mgr = MagicMock()
    container_mgr.spawn = AsyncMock(return_value=_make_container_result(exit_code=0))

    sched = TaskScheduler()
    sched._db = db
    sched._container_mgr = container_mgr
    sched._config = _make_config()
    sched._registry = MagicMock()
    sched._stream = stream

    await sched._run_task(task)

    stream.clear.assert_called_once_with(task["group_name"])
    stream.begin.assert_called_once()


# ---------------------------------------------------------------------------
# start / stop lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_creates_background_task():
    """start() creates an asyncio task that can be cancelled via stop()."""
    sched = TaskScheduler()
    sched._db = MagicMock()
    sched._db.get_active_tasks = AsyncMock(return_value=[])
    sched._container_mgr = MagicMock()
    sched._config = _make_config()
    sched._registry = MagicMock()
    sched._stream = _make_stream()

    await sched.start()
    assert sched._task is not None
    assert not sched._task.done()

    await sched.stop()
    assert sched._task is None


@pytest.mark.asyncio
async def test_stop_is_idempotent():
    """Calling stop() when no task is running does not raise."""
    sched = TaskScheduler()
    # stop() before start() must not raise
    await sched.stop()
    await sched.stop()


# ---------------------------------------------------------------------------
# DB helpers — integration with Database class
# ---------------------------------------------------------------------------

@pytest.fixture
async def db(tmp_path):
    """Provide an initialised Database backed by a temp file."""
    from src.db import Database

    database = Database()
    await database.init(str(tmp_path / "test.db"))
    # Register a group for FK compliance
    await database.upsert_group(name="main", channel="telegram", chat_id="c1")
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_create_and_get_task(db):
    """create_task inserts a row, get_active_tasks returns it."""
    task_id = str(uuid.uuid4())
    await db.create_task(
        id=task_id,
        group_name="main",
        type="cron",
        schedule="*/5 * * * *",
        prompt="Do something",
    )
    tasks = await db.get_active_tasks()
    assert len(tasks) == 1
    assert tasks[0]["id"] == task_id
    assert tasks[0]["schedule"] == "*/5 * * * *"
    assert tasks[0]["status"] == "active"


@pytest.mark.asyncio
async def test_update_task_run(db):
    """update_task_run updates last_run and next_run."""
    task_id = str(uuid.uuid4())
    await db.create_task(
        id=task_id,
        group_name="main",
        type="cron",
        schedule="*/5 * * * *",
        prompt="Do something",
    )
    now_ts = int(time.time())
    await db.update_task_run(
        task_id=task_id, last_run=now_ts, next_run=now_ts + 300
    )
    tasks = await db.get_active_tasks()
    assert tasks[0]["last_run"] == now_ts
    assert tasks[0]["next_run"] == now_ts + 300


@pytest.mark.asyncio
async def test_cancel_task_hides_from_active(db):
    """cancel_task sets status='cancelled' so get_active_tasks excludes it."""
    task_id = str(uuid.uuid4())
    await db.create_task(
        id=task_id,
        group_name="main",
        type="cron",
        schedule="*/5 * * * *",
        prompt="Do something",
    )
    await db.cancel_task(task_id=task_id)

    tasks = await db.get_active_tasks()
    assert tasks == []


@pytest.mark.asyncio
async def test_list_tasks_returns_all_statuses(db):
    """list_tasks returns both active and cancelled tasks for a group."""
    id1 = str(uuid.uuid4())
    id2 = str(uuid.uuid4())
    await db.create_task(
        id=id1, group_name="main", type="cron", schedule="* * * * *", prompt="A"
    )
    await db.create_task(
        id=id2, group_name="main", type="cron", schedule="0 * * * *", prompt="B"
    )
    await db.cancel_task(task_id=id2)

    tasks = await db.list_tasks(group_name="main")
    ids = {t["id"] for t in tasks}
    assert id1 in ids
    assert id2 in ids


@pytest.mark.asyncio
async def test_list_tasks_empty_for_other_group(db):
    """list_tasks returns no tasks for a group that has none."""
    await db.upsert_group(name="other", channel="telegram", chat_id="c2")
    tasks = await db.list_tasks(group_name="other")
    assert tasks == []
