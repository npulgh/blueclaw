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

"""Lynxclaw Swarm Coordinator — cross-group agent delegation.

Enables agents in different groups to collaborate:
- delegate(): spawn a container in another group with a delegated prompt
- read_context(): read another group's CLAUDE.md (with permission check)
- broadcast(): delegate to multiple groups in parallel

Permission model:
- main group can delegate to any group
- non-main groups can only delegate to main group
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

import structlog

from src.config import Config
from src.container_manager import ContainerManager
from src.db import Database

log = structlog.get_logger(__name__)


class PermissionError(Exception):
    """Raised when a group attempts a delegation it is not permitted to make."""


class SwarmCoordinator:
    """Manages cross-group agent communication.

    Agents can:
    - Delegate tasks to other groups' agents
    - Read context from other groups (with permission)
    - Broadcast messages to multiple groups
    """

    def __init__(self) -> None:
        self._db: Optional[Database] = None
        self._container_mgr: Optional[ContainerManager] = None
        self._config: Optional[Config] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init(
        self,
        db: Database,
        container_mgr: ContainerManager,
        config: Config,
    ) -> None:
        """Wire up dependencies.

        Args:
            db: Initialised Database instance.
            container_mgr: Initialised ContainerManager instance.
            config: Loaded Config.
        """
        self._db = db
        self._container_mgr = container_mgr
        self._config = config
        log.info(
            "swarm.init",
            groups=[g.name for g in config.groups],
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def delegate(
        self,
        from_group: str,
        to_group: str,
        prompt: str,
        context: str = "",
    ) -> str:
        """Delegate a task to another group's agent.

        Spawns an ephemeral container in *to_group* with the given prompt.
        The delegated container writes its response via IPC as normal.

        Args:
            from_group: The requesting group name.
            to_group: The target group name.
            prompt: Prompt to pass to the delegated agent.
            context: Optional extra context prepended to the prompt.

        Returns:
            Container stdout (may be empty if agent uses IPC for output).

        Raises:
            PermissionError: If from_group is not allowed to delegate to to_group.
            ValueError: If to_group is not a configured group.
        """
        self._check_init()
        self._check_permission(from_group, to_group)

        target_cfg = self._get_group_config(to_group)
        if target_cfg is None:
            raise ValueError(f"Target group '{to_group}' is not configured")

        full_prompt = f"{context}\n\n{prompt}".strip() if context else prompt

        cwd = os.getcwd()
        mounts = {
            "group_dir": os.path.join(cwd, "groups", to_group),
            "global_dir": os.path.join(cwd, "groups"),
            "ipc_dir": os.path.join(cwd, "data", "ipc", to_group),
        }
        if target_cfg.is_main:
            mounts["project_dir"] = cwd

        env_vars = {
            "ANTHROPIC_API_KEY": self._config.anthropic_api_key,  # type: ignore[union-attr]
            "LYNXCLAW_SWARM_FROM": from_group,
            "LYNXCLAW_SWARM_DELEGATED": "1",
        }

        log.info(
            "swarm.delegate",
            from_group=from_group,
            to_group=to_group,
            prompt_len=len(full_prompt),
        )

        result = await self._container_mgr.spawn(  # type: ignore[union-attr]
            group_name=to_group,
            prompt=full_prompt,
            env_vars=env_vars,
            mounts=mounts,
            is_main=target_cfg.is_main,
        )

        log.info(
            "swarm.delegate.done",
            from_group=from_group,
            to_group=to_group,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
        )
        return result.stdout

    async def read_context(
        self,
        requesting_group: str,
        target_group: str,
    ) -> Optional[str]:
        """Read the CLAUDE.md context of another group.

        Args:
            requesting_group: The group making the request.
            target_group: The group whose context is being read.

        Returns:
            Contents of the target group's CLAUDE.md, or None if not found.

        Raises:
            PermissionError: If requesting_group is not allowed to read target_group.
        """
        self._check_init()
        self._check_permission(requesting_group, target_group)

        cwd = os.getcwd()
        claude_md = Path(cwd) / "groups" / target_group / "CLAUDE.md"

        if not claude_md.exists():
            log.debug(
                "swarm.read_context.not_found",
                requesting=requesting_group,
                target=target_group,
                path=str(claude_md),
            )
            return None

        content = await asyncio.to_thread(claude_md.read_text, encoding="utf-8")
        log.info(
            "swarm.read_context",
            requesting=requesting_group,
            target=target_group,
            chars=len(content),
        )
        return content

    async def broadcast(
        self,
        from_group: str,
        prompt: str,
        target_groups: Optional[list[str]] = None,
    ) -> dict[str, str]:
        """Delegate a prompt to multiple groups in parallel.

        Args:
            from_group: The requesting group name.
            prompt: Prompt to broadcast.
            target_groups: Groups to target. If None, targets all groups except
                           from_group (main group only — non-main groups cannot
                           broadcast to arbitrary groups).

        Returns:
            Dict mapping group_name → container stdout for each target.

        Raises:
            PermissionError: If from_group is not the main group and target_groups
                             contains groups other than main.
        """
        self._check_init()

        cfg = self._config
        assert cfg is not None

        if target_groups is None:
            # Default: main group broadcasts to all other groups
            from_is_main = self._is_main(from_group)
            if not from_is_main:
                raise PermissionError(
                    f"Non-main group '{from_group}' cannot broadcast to all groups. "
                    "Specify target_groups explicitly (only 'main' is permitted)."
                )
            target_groups = [g.name for g in cfg.groups if g.name != from_group]

        # Validate each target permission
        for tg in target_groups:
            self._check_permission(from_group, tg)

        log.info(
            "swarm.broadcast",
            from_group=from_group,
            targets=target_groups,
        )

        tasks = {
            tg: asyncio.create_task(self.delegate(from_group, tg, prompt))
            for tg in target_groups
        }

        results: dict[str, str] = {}
        for tg, task in tasks.items():
            try:
                results[tg] = await task
            except Exception as exc:
                log.error(
                    "swarm.broadcast.target_failed",
                    from_group=from_group,
                    target=tg,
                    error=str(exc),
                )
                results[tg] = ""

        log.info(
            "swarm.broadcast.done",
            from_group=from_group,
            targets=list(results.keys()),
        )
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_init(self) -> None:
        if self._db is None or self._container_mgr is None or self._config is None:
            raise RuntimeError("SwarmCoordinator.init() must be called before use")

    def _is_main(self, group_name: str) -> bool:
        """Return True if group_name is the main group."""
        cfg = self._config
        assert cfg is not None
        for g in cfg.groups:
            if g.name == group_name:
                return g.is_main
        return False

    def _check_permission(self, from_group: str, to_group: str) -> None:
        """Enforce delegation permission model.

        - main group → any group: allowed
        - non-main group → main group: allowed
        - non-main group → non-main group: denied
        """
        if self._is_main(from_group):
            return  # main can delegate anywhere

        to_is_main = self._is_main(to_group)
        if not to_is_main:
            raise PermissionError(
                f"Non-main group '{from_group}' can only delegate to the main group, "
                f"not to '{to_group}'."
            )

    def _get_group_config(self, group_name: str):
        """Return GroupConfig for group_name, or None."""
        cfg = self._config
        assert cfg is not None
        for g in cfg.groups:
            if g.name == group_name:
                return g
        return None
