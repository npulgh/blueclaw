# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Lynxclaw Task Scheduler — cron-based agent task runner.

Reads active tasks from the tasks table, parses cron expressions with
croniter, and triggers due tasks via ContainerManager spawn.

Design:
- Polls every 30 seconds for simplicity (no distributed locking needed)
- Reuses the same container spawn logic as regular message processing
- Task responses go to the group's IM channel via IPC stream state
- Uses structlog for all logging
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Optional

import structlog
from croniter import croniter

from src.channels.registry import ChannelRegistry
from src.config import Config
from src.container_manager import ContainerManager
from src.db import Database
from src.memory import get_global_memory_path

log = structlog.get_logger(__name__)

# Scheduler check interval in seconds
_POLL_INTERVAL = 30


class TaskScheduler:
    """Cron-based task scheduler.

    Checks every ``_POLL_INTERVAL`` seconds for due tasks and spawns
    agent containers to execute them.

    Usage::

        scheduler = TaskScheduler()
        await scheduler.init(db, container_mgr, config, registry, stream)
        await scheduler.start()
        # ... run ...
        await scheduler.stop()
    """

    def __init__(self) -> None:
        self._db: Optional[Database] = None
        self._container_mgr: Optional[ContainerManager] = None
        self._config: Optional[Config] = None
        self._registry: Optional[ChannelRegistry] = None
        self._stream: Optional[object] = None  # _StreamState from main.py
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init(
        self,
        db: Database,
        container_mgr: ContainerManager,
        config: Config,
        registry: ChannelRegistry,
        stream: object,
    ) -> None:
        """Load active tasks, compute next_run times for any tasks missing it.

        Args:
            db: Initialised Database instance.
            container_mgr: Initialised ContainerManager.
            config: Loaded application config.
            registry: Channel adapter registry.
            stream: _StreamState object from main.py (tracks per-group IPC state).
        """
        self._db = db
        self._container_mgr = container_mgr
        self._config = config
        self._registry = registry
        self._stream = stream

        # Seed next_run for tasks that don't have it yet
        now = datetime.now(timezone.utc)
        tasks = await db.get_active_tasks()
        for task in tasks:
            if not task.get("next_run"):
                try:
                    next_run = self._compute_next_run(task["schedule"], now)
                    await db.update_task_run(
                        task_id=task["id"],
                        last_run=task.get("last_run") or 0,
                        next_run=next_run,
                    )
                    log.info(
                        "scheduler.task_seeded",
                        task_id=task["id"],
                        group=task["group_name"],
                        next_run=next_run,
                    )
                except Exception as exc:
                    log.warning(
                        "scheduler.seed_failed",
                        task_id=task["id"],
                        schedule=task["schedule"],
                        error=str(exc),
                    )

        log.info("scheduler.init", task_count=len(tasks))

    async def start(self) -> None:
        """Start the scheduler loop as an asyncio task."""
        self._task = asyncio.create_task(self._scheduler_loop(), name="scheduler")
        log.info("scheduler.started")

    async def stop(self) -> None:
        """Stop the scheduler."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("scheduler.stopped")

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _scheduler_loop(self) -> None:
        """Main loop: check every 30 seconds for due tasks."""
        while True:
            await asyncio.sleep(_POLL_INTERVAL)
            try:
                await self._check_and_run_due_tasks()
            except Exception as exc:  # noqa: BLE001
                log.error("scheduler.loop_error", error=str(exc))

    async def _check_and_run_due_tasks(self) -> None:
        """Find tasks where next_run <= now and execute them."""
        assert self._db is not None
        now_ts = int(time.time())
        tasks = await self._db.get_active_tasks()

        for task in tasks:
            next_run = task.get("next_run")
            if next_run is None or next_run > now_ts:
                continue

            task_id = task["id"]
            group_name = task["group_name"]
            schedule = task["schedule"]
            prompt = task["prompt"]

            log.info(
                "scheduler.task_due",
                task_id=task_id,
                group=group_name,
                schedule=schedule,
            )

            # Compute and persist next_run before running, so a crash during
            # execution doesn't cause the task to re-fire on recovery
            now_dt = datetime.now(timezone.utc)
            try:
                next_run_new = self._compute_next_run(schedule, now_dt)
            except Exception as exc:
                log.error(
                    "scheduler.cron_parse_error",
                    task_id=task_id,
                    schedule=schedule,
                    error=str(exc),
                )
                continue

            await self._db.update_task_run(
                task_id=task_id,
                last_run=now_ts,
                next_run=next_run_new,
            )

            # Run in a background task so due tasks don't block each other
            asyncio.create_task(
                self._run_task(task),
                name=f"sched-{task_id}",
            )

    async def _run_task(self, task: dict) -> None:
        """Spawn a container for a single scheduled task.

        Looks up the group's channel and chat_id from config, sets up
        stream state, spawns the container, then clears stream state.
        """
        assert self._db is not None
        assert self._container_mgr is not None
        assert self._config is not None
        assert self._registry is not None
        assert self._stream is not None

        task_id = task["id"]
        group_name = task["group_name"]
        prompt = task["prompt"]

        # Look up channel + chat_id for this group from config
        group_cfg = next(
            (g for g in self._config.groups if g.name == group_name), None
        )
        if group_cfg is None:
            log.error(
                "scheduler.group_not_found",
                task_id=task_id,
                group=group_name,
            )
            return

        channel_name = group_cfg.channel
        chat_id = group_cfg.chat_id
        is_main = group_cfg.is_main

        # Signal IPC stream state so that IPC replies land in the right chat
        self._stream.begin(group_name, channel_name, chat_id)  # type: ignore[attr-defined]

        # Build mounts
        cwd = os.getcwd()
        mounts = {
            "group_dir": os.path.join(cwd, "groups", group_name),
            "global_dir": os.path.join(cwd, "groups"),
            "ipc_dir": os.path.join(cwd, "data", "ipc", group_name),
        }
        if is_main:
            mounts["project_dir"] = cwd
            mounts["global_memory"] = get_global_memory_path(
                os.path.join(cwd, "groups")
            )

        env_vars = {
            "ANTHROPIC_API_KEY": self._config.anthropic_api_key,
            "LYNXCLAW_CHAT_ID": chat_id,
        }

        try:
            session_id = await self._db.get_session(group_name=group_name)
            result = await self._container_mgr.spawn(
                group_name=group_name,
                prompt=prompt,
                env_vars=env_vars,
                mounts=mounts,
                is_main=is_main,
                session_id=session_id,
            )
        except Exception as exc:
            log.error(
                "scheduler.spawn_error",
                task_id=task_id,
                group=group_name,
                error=str(exc),
            )
            self._stream.clear(group_name)  # type: ignore[attr-defined]
            return

        if result.timed_out:
            log.warning("scheduler.task_timed_out", task_id=task_id, group=group_name)
        elif result.exit_code != 0:
            log.error(
                "scheduler.task_nonzero_exit",
                task_id=task_id,
                group=group_name,
                exit_code=result.exit_code,
                stderr=result.stderr[:500],
            )
        else:
            log.info(
                "scheduler.task_completed",
                task_id=task_id,
                group=group_name,
            )

        self._stream.clear(group_name)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _compute_next_run(self, schedule: str, after: datetime) -> int:
        """Use croniter to compute the next run Unix timestamp.

        Args:
            schedule: Cron expression string (e.g. '*/5 * * * *').
            after: Datetime to compute the next occurrence after.

        Returns:
            Unix timestamp (int) of the next scheduled run.
        """
        cron = croniter(schedule, after)
        next_dt: datetime = cron.get_next(datetime)
        return int(next_dt.timestamp())
