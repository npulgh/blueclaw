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

"""Lynxclaw Memory Management — directory structure and CLAUDE.md templates.

Responsible for creating the ``groups/`` directory tree and seeding template
CLAUDE.md files on first startup.  Subsequent starts leave existing files
intact so agent-written memories persist across restarts.

Layout::

    groups/
      CLAUDE.md                  ← global memory (all groups: ro; main: rw via global_memory mount)
      {group-name}/
        CLAUDE.md                ← group-specific memory (agent rw)
        session/                 ← Agent SDK session data
        files/                   ← Agent output files
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_GLOBAL_TEMPLATE = """\
# Lynxclaw Global Memory

> This file is shared across all groups. Main Group agents can edit this file.
> Non-Main Group agents have read-only access.

## System Notes
<!-- Agent-maintained notes visible to all groups -->

## Shared Knowledge
<!-- Cross-group knowledge and patterns -->
"""

_GROUP_TEMPLATE = """\
# {group_name} Group Memory

> This file is specific to the '{group_name}' group. The agent can read and write here.

## Context
<!-- Group-specific context and instructions -->

## History
<!-- Notable interactions and decisions -->
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ensure_group_dirs(groups_dir: str, group_names: list[str]) -> None:
    """Create group directories and seed CLAUDE.md templates if missing.

    Idempotent: existing CLAUDE.md files are **never** overwritten so that
    agent-written memories survive restarts.

    Args:
        groups_dir: Absolute path to the ``groups/`` directory.
        group_names: List of group names (e.g. ``["main", "support"]``).
    """
    groups_path = Path(groups_dir)
    groups_path.mkdir(parents=True, exist_ok=True)

    # Seed global CLAUDE.md
    global_memory = groups_path / "CLAUDE.md"
    if not global_memory.exists():
        global_memory.write_text(_GLOBAL_TEMPLATE, encoding="utf-8")

    # Seed global skills directory (ADR-007)
    (groups_path / "skills").mkdir(exist_ok=True)

    # Seed per-group directories and CLAUDE.md files
    for name in group_names:
        group_path = groups_path / name
        group_path.mkdir(parents=True, exist_ok=True)

        # Optional subdirectories used by the Agent SDK / container
        (group_path / "session").mkdir(exist_ok=True)
        (group_path / "files").mkdir(exist_ok=True)
        (group_path / "skills").mkdir(exist_ok=True)  # ADR-007

        group_memory = group_path / "CLAUDE.md"
        if not group_memory.exists():
            group_memory.write_text(
                _GROUP_TEMPLATE.format(group_name=name),
                encoding="utf-8",
            )


def get_global_memory_path(groups_dir: str) -> str:
    """Return the absolute path to the global CLAUDE.md file.

    Args:
        groups_dir: Absolute path to the ``groups/`` directory.

    Returns:
        Absolute path string to ``groups/CLAUDE.md``.
    """
    return str(Path(groups_dir) / "CLAUDE.md")


def get_group_memory_path(groups_dir: str, group_name: str) -> str:
    """Return the absolute path to a group-specific CLAUDE.md file.

    Args:
        groups_dir: Absolute path to the ``groups/`` directory.
        group_name: Name of the group.

    Returns:
        Absolute path string to ``groups/{group_name}/CLAUDE.md``.
    """
    return str(Path(groups_dir) / group_name / "CLAUDE.md")
