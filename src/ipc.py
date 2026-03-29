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

"""Lynxclaw IPC Watcher — host-side file-based JSON-RPC consumer.

Architecture (ADR-001):
- Container writes JSON-RPC 2.0 files to data/ipc/{group}/outbox/
- Host watchdog monitors outbox dirs for new files
- MUST implement BOTH on_created AND on_moved:
    - on_created: triggered by direct file writes
    - on_moved: triggered by atomic write (write-tmp + rename), uses event.dest_path
- Files are parsed, validated, dispatched to callback, then deleted
- Optional 5-second periodic scan as defensive fallback alongside event-driven
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Callable, Coroutine, Optional

import structlog
from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers import Observer

log = structlog.get_logger(__name__)

# Type alias for the async dispatch callback
DispatchCallback = Callable[[str, dict, str], Coroutine]

# Supported IPC methods (container → host)
VALID_METHODS = frozenset({
    "send_message",
    "stream_chunk",
    "schedule_task",
    "list_tasks",
    "cancel_task",
    "delegate_task",
    "read_context",
})


class _OutboxEventHandler(FileSystemEventHandler):
    """Watchdog event handler for a single group's outbox directory."""

    def __init__(
        self,
        group: str,
        loop: asyncio.AbstractEventLoop,
        dispatch_cb: DispatchCallback,
    ) -> None:
        super().__init__()
        self._group = group
        self._loop = loop
        self._dispatch_cb = dispatch_cb

    # ------------------------------------------------------------------
    # Watchdog callbacks (called from watchdog's background thread)
    # ------------------------------------------------------------------

    def on_created(self, event: FileCreatedEvent) -> None:  # type: ignore[override]
        """Handle direct file creation events."""
        if event.is_directory:
            return
        path = str(event.src_path)
        if path.endswith(".json"):
            log.debug("ipc.file_created", group=self._group, path=path)
            self._schedule(path)

    def on_moved(self, event: FileMovedEvent) -> None:  # type: ignore[override]
        """Handle atomic write events (write-tmp + rename).

        Container uses atomic writes, which trigger on_moved in watchdog.
        CRITICAL: use event.dest_path, NOT event.src_path.
        """
        if event.is_directory:
            return
        dest = str(event.dest_path)
        if dest.endswith(".json"):
            log.debug("ipc.file_moved", group=self._group, dest=dest)
            self._schedule(dest)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _schedule(self, path: str) -> None:
        """Bridge from watchdog thread to asyncio event loop."""
        self._loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(
                self._process_file(path), loop=self._loop
            )
        )

    async def _process_file(self, path: str) -> None:
        """Parse, validate, dispatch, and delete a single IPC file."""
        file_path = Path(path)

        # File may have already been processed (race between on_created/on_moved)
        if not file_path.exists():
            return

        try:
            text = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("ipc.read_error", path=path, error=str(exc))
            return

        # Parse JSON-RPC 2.0
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            log.error("ipc.invalid_json", group=self._group, path=path, error=str(exc))
            _safe_delete(file_path)
            return

        # Validate JSON-RPC structure
        method = payload.get("method")
        params = payload.get("params")
        rpc_id = payload.get("id", "")

        if not isinstance(method, str) or not isinstance(params, dict):
            log.error(
                "ipc.invalid_rpc",
                group=self._group,
                path=path,
                method=method,
                params_type=type(params).__name__,
            )
            _safe_delete(file_path)
            return

        if method not in VALID_METHODS:
            log.warning(
                "ipc.unknown_method",
                group=self._group,
                method=method,
                path=path,
            )
            _safe_delete(file_path)
            return

        # Delete before dispatch so that even if dispatch raises, we don't reprocess
        _safe_delete(file_path)

        log.info("ipc.dispatch", group=self._group, method=method, id=rpc_id)
        try:
            await self._dispatch_cb(method, params, str(rpc_id))
        except Exception as exc:  # noqa: BLE001
            log.error(
                "ipc.dispatch_error",
                group=self._group,
                method=method,
                error=str(exc),
            )


class IPCWatcher:
    """Host-side IPC Watcher: monitors group outbox directories via watchdog.

    Usage::

        async def my_dispatch(method: str, params: dict, id: str) -> None:
            print(method, params)

        watcher = IPCWatcher()
        await watcher.init(base_dir="data/ipc", dispatch_callback=my_dispatch)
        await watcher.start()
        # ... run ...
        await watcher.stop()
    """

    def __init__(self) -> None:
        self._base_dir: Optional[Path] = None
        self._dispatch_cb: Optional[DispatchCallback] = None
        self._observer: Optional[Observer] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._handlers: dict[str, _OutboxEventHandler] = {}
        self._scan_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init(
        self,
        base_dir: str,
        dispatch_callback: DispatchCallback,
    ) -> None:
        """Configure the watcher.

        Args:
            base_dir: Root IPC directory (e.g. "data/ipc"). Group subdirs are
                      created automatically: {base_dir}/{group}/outbox|inbox|audit
            dispatch_callback: Async callable invoked for each valid IPC message.
                               Signature: async def cb(method, params, id) -> None
        """
        self._base_dir = Path(base_dir)
        self._dispatch_cb = dispatch_callback
        self._loop = asyncio.get_event_loop()
        log.info("ipc.init", base_dir=str(self._base_dir))

    async def start(self, groups: Optional[list[str]] = None) -> None:
        """Start the watchdog observer and optional periodic scan.

        Args:
            groups: List of group names to watch. If None, the watcher will
                    discover groups from existing subdirectories under base_dir.
                    Directories are auto-created for listed groups.
        """
        assert self._base_dir is not None, "IPCWatcher.init() must be called first"
        assert self._loop is not None

        self._observer = Observer()

        # Determine groups to watch
        watch_groups = list(groups) if groups else []
        if not watch_groups and self._base_dir.exists():
            # Discover existing group dirs
            watch_groups = [
                d.name
                for d in self._base_dir.iterdir()
                if d.is_dir()
            ]

        for group in watch_groups:
            self._ensure_ipc_dirs(group)
            outbox = self._base_dir / group / "outbox"
            handler = _OutboxEventHandler(
                group=group,
                loop=self._loop,
                dispatch_cb=self._dispatch_cb,  # type: ignore[arg-type]
            )
            self._handlers[group] = handler
            self._observer.schedule(handler, str(outbox), recursive=False)
            log.info("ipc.watching", group=group, outbox=str(outbox))

        self._observer.start()
        log.info("ipc.started", groups=list(self._handlers.keys()))

        # Optional 5-second periodic scan as defensive fallback
        self._scan_task = asyncio.ensure_future(self._periodic_scan())

    async def stop(self) -> None:
        """Stop the watchdog observer and periodic scan."""
        if self._scan_task:
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
            self._scan_task = None

        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None

        log.info("ipc.stopped")

    def add_group(self, group: str) -> None:
        """Dynamically add a new group to watch (after start() is called)."""
        assert self._observer is not None, "IPCWatcher.start() must be called first"
        assert self._loop is not None

        if group in self._handlers:
            return  # already watching

        self._ensure_ipc_dirs(group)
        outbox = self._base_dir / group / "outbox"  # type: ignore[operator]
        handler = _OutboxEventHandler(
            group=group,
            loop=self._loop,
            dispatch_cb=self._dispatch_cb,  # type: ignore[arg-type]
        )
        self._handlers[group] = handler
        self._observer.schedule(handler, str(outbox), recursive=False)
        log.info("ipc.group_added", group=group, outbox=str(outbox))

    # ------------------------------------------------------------------
    # Periodic scan (defensive fallback)
    # ------------------------------------------------------------------

    async def _periodic_scan(self) -> None:
        """Scan all outbox dirs every 5 seconds as a defensive fallback.

        This catches any files that watchdog events may have missed.
        The scan is a non-essential complement to event-driven dispatch.
        """
        while True:
            await asyncio.sleep(5)
            for group, handler in list(self._handlers.items()):
                outbox = self._base_dir / group / "outbox"  # type: ignore[operator]
                if not outbox.exists():
                    continue
                for f in outbox.glob("*.json"):
                    log.debug("ipc.periodic_scan_found", group=group, file=str(f))
                    await handler._process_file(str(f))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_ipc_dirs(self, group: str) -> None:
        """Create outbox, inbox, and audit directories for a group."""
        assert self._base_dir is not None
        for subdir in ("outbox", "inbox", "audit"):
            (self._base_dir / group / subdir).mkdir(parents=True, exist_ok=True)


def _safe_delete(path: Path) -> None:
    """Delete a file, ignoring errors if it's already gone."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("ipc.delete_error", path=str(path), error=str(exc))
