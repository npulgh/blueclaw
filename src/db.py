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

"""Lynxclaw database layer.

Wraps aiosqlite with schema migrations, WAL mode, and periodic VACUUM INTO backup.
Schema version is tracked via SQLite PRAGMA user_version.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite

# Bump this when the schema changes.
SCHEMA_VERSION = 2

_DDL = """
CREATE TABLE IF NOT EXISTS messages (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  channel       TEXT NOT NULL,
  chat_id       TEXT NOT NULL,
  message_id    TEXT NOT NULL,
  group_name    TEXT,
  sender_id     TEXT NOT NULL,
  sender_name   TEXT,
  content       TEXT NOT NULL,
  direction     TEXT NOT NULL,
  status        TEXT DEFAULT 'pending',
  created_at    INTEGER NOT NULL,
  processed_at  INTEGER,
  UNIQUE(channel, chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS groups (
  name          TEXT PRIMARY KEY,
  channel       TEXT NOT NULL,
  chat_id       TEXT NOT NULL,
  is_main       INTEGER DEFAULT 0,
  trigger       TEXT DEFAULT '@bot',
  created_at    INTEGER NOT NULL,
  UNIQUE(channel, chat_id)
);

CREATE TABLE IF NOT EXISTS sessions (
  group_name    TEXT PRIMARY KEY,
  session_id    TEXT,
  last_active   INTEGER,
  FOREIGN KEY (group_name) REFERENCES groups(name)
);

CREATE TABLE IF NOT EXISTS tasks (
  id            TEXT PRIMARY KEY,
  group_name    TEXT NOT NULL,
  type          TEXT NOT NULL,
  schedule      TEXT NOT NULL,
  prompt        TEXT NOT NULL,
  status        TEXT DEFAULT 'active',
  last_run      INTEGER,
  next_run      INTEGER,
  created_at    INTEGER NOT NULL,
  FOREIGN KEY (group_name) REFERENCES groups(name)
);

CREATE TABLE IF NOT EXISTS cursors (
  channel       TEXT NOT NULL,
  chat_id       TEXT NOT NULL,
  last_msg_id   TEXT NOT NULL,
  updated_at    INTEGER NOT NULL,
  PRIMARY KEY (channel, chat_id)
);

CREATE TABLE IF NOT EXISTS tool_audit_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  group_name    TEXT NOT NULL,
  session_id    TEXT,
  tool_name     TEXT NOT NULL,
  agent_id      TEXT,
  input_summary TEXT,
  blocked       INTEGER DEFAULT 0,
  created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS token_usage (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  group_name    TEXT NOT NULL,
  input_tokens  INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  created_at    INTEGER NOT NULL,
  FOREIGN KEY (group_name) REFERENCES groups(name)
);
"""


class Database:
    """Async SQLite database wrapper with migrations and backup support."""

    def __init__(self) -> None:
        self._conn: Optional[aiosqlite.Connection] = None
        self._db_path: Optional[Path] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init(self, db_path: str) -> None:
        """Open the database, run migrations, and create an initial backup."""
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row

        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._migrate()
        await self.backup()

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Migrations
    # ------------------------------------------------------------------

    async def _migrate(self) -> None:
        async with self._conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        version = row[0] if row else 0

        if version < SCHEMA_VERSION:
            await self._conn.executescript(_DDL)
            await self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await self._conn.commit()

    # ------------------------------------------------------------------
    # Backup
    # ------------------------------------------------------------------

    async def backup(self) -> None:
        """Write a backup using VACUUM INTO (SQLite 3.27+)."""
        if self._db_path is None or self._conn is None:
            return
        bak = str(self._db_path) + ".bak"
        # VACUUM INTO fails if the target already exists; remove it first.
        Path(bak).unlink(missing_ok=True)
        await self._conn.execute(f"VACUUM INTO '{bak}'")

    # ------------------------------------------------------------------
    # messages helpers
    # ------------------------------------------------------------------

    async def insert_message(
        self,
        *,
        channel: str,
        chat_id: str,
        message_id: str,
        sender_id: str,
        content: str,
        direction: str,
        group_name: Optional[str] = None,
        sender_name: Optional[str] = None,
        status: str = "pending",
        created_at: Optional[int] = None,
    ) -> Optional[int]:
        """Insert a message row; returns rowid or None if duplicate (IGNORE)."""
        ts = created_at if created_at is not None else int(time.time())
        async with self._conn.execute(
            """
            INSERT OR IGNORE INTO messages
              (channel, chat_id, message_id, group_name, sender_id, sender_name,
               content, direction, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (channel, chat_id, message_id, group_name, sender_id, sender_name,
             content, direction, status, ts),
        ) as cur:
            await self._conn.commit()
            return cur.lastrowid if cur.rowcount else None

    async def get_message(
        self, *, channel: str, chat_id: str, message_id: str
    ) -> Optional[dict[str, Any]]:
        """Fetch a single message by its unique key; returns dict or None."""
        async with self._conn.execute(
            "SELECT * FROM messages WHERE channel=? AND chat_id=? AND message_id=?",
            (channel, chat_id, message_id),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def update_message_status(
        self, *, channel: str, chat_id: str, message_id: str, status: str
    ) -> None:
        """Update the status of a message."""
        await self._conn.execute(
            """
            UPDATE messages SET status=?, processed_at=?
            WHERE channel=? AND chat_id=? AND message_id=?
            """,
            (status, int(time.time()), channel, chat_id, message_id),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # groups helpers
    # ------------------------------------------------------------------

    async def upsert_group(
        self,
        *,
        name: str,
        channel: str,
        chat_id: str,
        is_main: bool = False,
        trigger: str = "@bot",
    ) -> None:
        """Insert or replace a group row (upsert by primary key)."""
        ts = int(time.time())
        await self._conn.execute(
            """
            INSERT INTO groups (name, channel, chat_id, is_main, trigger, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
              channel=excluded.channel,
              chat_id=excluded.chat_id,
              is_main=excluded.is_main,
              trigger=excluded.trigger
            """,
            (name, channel, chat_id, int(is_main), trigger, ts),
        )
        await self._conn.commit()

    async def get_group_by_chat(
        self, *, channel: str, chat_id: str
    ) -> Optional[dict[str, Any]]:
        """Fetch a group by channel + chat_id; returns dict or None."""
        async with self._conn.execute(
            "SELECT * FROM groups WHERE channel=? AND chat_id=?",
            (channel, chat_id),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # sessions helpers
    # ------------------------------------------------------------------

    async def save_session(self, *, group_name: str, session_id: str) -> None:
        """Upsert session_id for a group."""
        await self._conn.execute(
            """INSERT INTO sessions (group_name, session_id, last_active)
               VALUES (?, ?, ?)
               ON CONFLICT(group_name) DO UPDATE SET
                 session_id=excluded.session_id,
                 last_active=excluded.last_active""",
            (group_name, session_id, int(time.time())),
        )
        await self._conn.commit()

    async def get_session(self, *, group_name: str) -> Optional[str]:
        """Get session_id for a group, or None if no session exists."""
        async with self._conn.execute(
            "SELECT session_id FROM sessions WHERE group_name=?",
            (group_name,),
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # cursors helpers
    # ------------------------------------------------------------------

    async def update_cursor(
        self, *, channel: str, chat_id: str, last_msg_id: str
    ) -> None:
        """Upsert the last processed message_id for a channel+chat_id pair."""
        ts = int(time.time())
        await self._conn.execute(
            """
            INSERT INTO cursors (channel, chat_id, last_msg_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(channel, chat_id) DO UPDATE SET
              last_msg_id=excluded.last_msg_id,
              updated_at=excluded.updated_at
            """,
            (channel, chat_id, last_msg_id, ts),
        )
        await self._conn.commit()

    async def get_cursor(self, *, channel: str, chat_id: str) -> Optional[str]:
        """Return the last processed message_id for a channel+chat_id, or None."""
        async with self._conn.execute(
            "SELECT last_msg_id FROM cursors WHERE channel=? AND chat_id=?",
            (channel, chat_id),
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # Recovery helpers
    # ------------------------------------------------------------------

    async def get_messages_by_status(
        self, *, status: str
    ) -> list[dict[str, Any]]:
        """Return all messages with the given status."""
        async with self._conn.execute(
            "SELECT * FROM messages WHERE status=?",
            (status,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # token_usage helpers
    # ------------------------------------------------------------------

    async def record_token_usage(
        self,
        *,
        group_name: str,
        input_tokens: int,
        output_tokens: int,
        created_at: Optional[int] = None,
    ) -> None:
        """Insert a token usage row for the given group."""
        ts = created_at if created_at is not None else int(time.time())
        await self._conn.execute(
            """
            INSERT INTO token_usage (group_name, input_tokens, output_tokens, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (group_name, input_tokens, output_tokens, ts),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # tasks helpers
    # ------------------------------------------------------------------

    async def create_task(
        self,
        *,
        id: str,
        group_name: str,
        type: str,
        schedule: str,
        prompt: str,
    ) -> None:
        """Insert a new scheduled task."""
        ts = int(time.time())
        await self._conn.execute(
            """
            INSERT INTO tasks (id, group_name, type, schedule, prompt, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'active', ?)
            """,
            (id, group_name, type, schedule, prompt, ts),
        )
        await self._conn.commit()

    async def get_active_tasks(self) -> list[dict[str, Any]]:
        """Return all tasks with status='active'."""
        async with self._conn.execute(
            "SELECT * FROM tasks WHERE status='active'"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def update_task_run(
        self, *, task_id: str, last_run: int, next_run: int
    ) -> None:
        """Update last_run and next_run timestamps after execution."""
        await self._conn.execute(
            "UPDATE tasks SET last_run=?, next_run=? WHERE id=?",
            (last_run, next_run, task_id),
        )
        await self._conn.commit()

    async def cancel_task(self, *, task_id: str) -> None:
        """Set task status to 'cancelled'."""
        await self._conn.execute(
            "UPDATE tasks SET status='cancelled' WHERE id=?",
            (task_id,),
        )
        await self._conn.commit()

    async def list_tasks(self, *, group_name: str) -> list[dict[str, Any]]:
        """List all tasks for a group."""
        async with self._conn.execute(
            "SELECT * FROM tasks WHERE group_name=?",
            (group_name,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # token_usage helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # CLI read-only query helpers
    # ------------------------------------------------------------------

    async def get_all_groups(self) -> list[dict[str, Any]]:
        """Return all groups ordered by name."""
        async with self._conn.execute(
            "SELECT * FROM groups ORDER BY name"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_group(self, name: str) -> Optional[dict[str, Any]]:
        """Return a single group by name, or None if not found."""
        async with self._conn.execute(
            "SELECT * FROM groups WHERE name=?", (name,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_messages(
        self,
        *,
        group_name: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return messages with optional group/status filters, newest first."""
        conditions: list[str] = []
        params: list[Any] = []
        if group_name is not None:
            conditions.append("group_name=?")
            params.append(group_name)
        if status is not None:
            conditions.append("status=?")
            params.append(status)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.extend([limit, offset])
        async with self._conn.execute(
            f"SELECT * FROM messages {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_audit_log(
        self,
        *,
        group_name: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return tool audit log entries, newest first."""
        if group_name is not None:
            sql = (
                "SELECT * FROM tool_audit_log WHERE group_name=? "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?"
            )
            params: tuple[Any, ...] = (group_name, limit, offset)
        else:
            sql = "SELECT * FROM tool_audit_log ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params = (limit, offset)
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_messages_count_since(self, ts: int) -> int:
        """Return the number of messages created at or after the given Unix timestamp."""
        async with self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE created_at >= ?",
            (ts,),
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def get_all_sessions(self) -> list[dict[str, Any]]:
        """Return all session rows ordered by last_active descending."""
        async with self._conn.execute(
            "SELECT * FROM sessions ORDER BY last_active DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_all_tasks(
        self, *, group_name: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Return all tasks (all statuses), optionally filtered by group."""
        if group_name is not None:
            sql = "SELECT * FROM tasks WHERE group_name=? ORDER BY created_at DESC"
            params_t: tuple[Any, ...] = (group_name,)
        else:
            sql = "SELECT * FROM tasks ORDER BY created_at DESC"
            params_t = ()
        async with self._conn.execute(sql, params_t) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_token_usage_all_groups(self) -> list[dict[str, Any]]:
        """Return summed token usage per group, ordered by total tokens desc."""
        async with self._conn.execute(
            """
            SELECT group_name,
                   COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens
            FROM token_usage
            GROUP BY group_name
            ORDER BY (input_tokens + output_tokens) DESC
            """
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_token_usage(
        self,
        *,
        group_name: str,
        since: Optional[int] = None,
    ) -> dict[str, int]:
        """Return summed token usage for a group.

        Args:
            group_name: Group to query.
            since: Optional Unix timestamp; only include rows with created_at >= since.

        Returns:
            Dict with keys ``input_tokens`` and ``output_tokens`` (both ints).
        """
        if since is not None:
            sql = """
                SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0)
                FROM token_usage
                WHERE group_name=? AND created_at >= ?
            """
            params = (group_name, since)
        else:
            sql = """
                SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0)
                FROM token_usage
                WHERE group_name=?
            """
            params = (group_name,)

        async with self._conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return {
            "input_tokens": row[0] if row else 0,
            "output_tokens": row[1] if row else 0,
        }
