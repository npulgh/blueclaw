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

"""Lynxclaw StreamDebouncer — debounce streaming text chunks before IM delivery.

Architecture (ARCHITECTURE.md §3.7):
- First chunk: caller sends a new message and gets a msg_id
- Subsequent chunks: caller edits the existing message
- Debounce rule: flush when accumulated chars >= debounce_chars OR
  time since last flush >= debounce_ms, whichever comes first
- is_final=True always triggers an immediate flush

The debouncer is intentionally decoupled from channel adapters.
It only manages timing and text accumulation; the caller decides what
to do with the flushed text (send_message vs edit_message).

Usage::

    async def on_flush(group: str, text: str, is_final: bool) -> None:
        # send or edit the IM message
        ...

    debouncer = StreamDebouncer(debounce_ms=500, debounce_chars=200)
    debouncer.set_flush_callback(on_flush)
    await debouncer.start()

    # As chunks arrive from IPC:
    await debouncer.add_chunk("my-group", "Hello ", is_final=False)
    await debouncer.add_chunk("my-group", "world!", is_final=True)

    await debouncer.stop()
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Coroutine, Optional

import structlog

log = structlog.get_logger(__name__)

# Type alias for the async flush callback
FlushCallback = Callable[[str, str, bool], Coroutine]


class _GroupBuffer:
    """Accumulation buffer and timing metadata for a single group."""

    def __init__(self) -> None:
        self.text: str = ""
        self.last_flush_time: float = time.monotonic()
        # Timer handle for time-based auto-flush; set when a chunk arrives
        self.timer_handle: Optional[asyncio.TimerHandle] = None

    def append(self, chunk: str) -> None:
        self.text += chunk

    def reset(self, now: float) -> str:
        """Take the accumulated text, reset the buffer, update flush time."""
        text = self.text
        self.text = ""
        self.last_flush_time = now
        return text

    def cancel_timer(self) -> None:
        if self.timer_handle is not None:
            self.timer_handle.cancel()
            self.timer_handle = None


class StreamDebouncer:
    """Debounce streaming chunks: flush every N ms or M chars, whichever first.

    Args:
        debounce_ms:    Maximum time (milliseconds) between flushes.
        debounce_chars: Maximum accumulated characters before a forced flush.
    """

    def __init__(
        self,
        debounce_ms: int = 500,
        debounce_chars: int = 200,
    ) -> None:
        self._debounce_ms = debounce_ms
        self._debounce_chars = debounce_chars
        self._debounce_secs = debounce_ms / 1000.0

        self._buffers: dict[str, _GroupBuffer] = {}
        self._flush_cb: Optional[FlushCallback] = None
        self._lock = asyncio.Lock()
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def set_flush_callback(self, cb: FlushCallback) -> None:
        """Register the async callback invoked on each flush.

        Signature: async def cb(group: str, text: str, is_final: bool) -> None
        """
        self._flush_cb = cb

    async def start(self) -> None:
        """Start the debouncer (currently a no-op, reserved for future use)."""
        self._started = True
        log.info(
            "debouncer.started",
            debounce_ms=self._debounce_ms,
            debounce_chars=self._debounce_chars,
        )

    async def stop(self) -> None:
        """Flush all pending buffers and stop the debouncer."""
        async with self._lock:
            for group in list(self._buffers.keys()):
                buf = self._buffers[group]
                buf.cancel_timer()
                if buf.text:
                    log.info("debouncer.stop_flush", group=group, chars=len(buf.text))
                    await self._invoke_cb(group, buf.text, is_final=True)
                    buf.text = ""
            self._buffers.clear()
        self._started = False
        log.info("debouncer.stopped")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def add_chunk(
        self,
        group: str,
        text: str,
        is_final: bool,
    ) -> Optional[str]:
        """Add a streaming chunk for a group.

        Returns the accumulated text if a flush is triggered, else None.
        Always returns text when is_final=True (even if text is empty
        after the chunk, to signal stream completion).

        The flush callback (if set) is also invoked on every flush.
        """
        async with self._lock:
            buf = self._get_or_create(group)
            buf.append(text)

            should_flush = (
                is_final
                or len(buf.text) >= self._debounce_chars
                or (time.monotonic() - buf.last_flush_time) >= self._debounce_secs
            )

            if should_flush:
                buf.cancel_timer()
                now = time.monotonic()
                flushed = buf.reset(now)

                if is_final:
                    # Clean up group state entirely
                    del self._buffers[group]

                log.debug(
                    "debouncer.flush",
                    group=group,
                    chars=len(flushed),
                    is_final=is_final,
                )
                await self._invoke_cb(group, flushed, is_final)
                return flushed

            else:
                # Schedule time-based flush if not already scheduled
                if buf.timer_handle is None:
                    loop = asyncio.get_event_loop()
                    buf.timer_handle = loop.call_later(
                        self._debounce_secs,
                        lambda g=group: asyncio.ensure_future(
                            self._timer_flush(g)
                        ),
                    )
                return None

    async def flush_group(self, group: str, is_final: bool = False) -> Optional[str]:
        """Manually flush a group's buffer.

        Useful for forced flush on stream end without a final chunk arriving.
        Returns flushed text, or None if buffer was empty.
        """
        async with self._lock:
            buf = self._buffers.get(group)
            if buf is None or not buf.text:
                if group in self._buffers and is_final:
                    del self._buffers[group]
                return None

            buf.cancel_timer()
            now = time.monotonic()
            flushed = buf.reset(now)

            if is_final:
                del self._buffers[group]

            log.debug(
                "debouncer.manual_flush",
                group=group,
                chars=len(flushed),
                is_final=is_final,
            )
            await self._invoke_cb(group, flushed, is_final)
            return flushed

    def has_pending(self, group: str) -> bool:
        """Return True if the group has buffered text not yet flushed."""
        buf = self._buffers.get(group)
        return buf is not None and bool(buf.text)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_or_create(self, group: str) -> _GroupBuffer:
        if group not in self._buffers:
            self._buffers[group] = _GroupBuffer()
        return self._buffers[group]

    async def _timer_flush(self, group: str) -> None:
        """Timer callback: flush a group if it still has pending text."""
        async with self._lock:
            buf = self._buffers.get(group)
            if buf is None or not buf.text:
                return

            # Clear the timer handle (it already fired)
            buf.timer_handle = None

            now = time.monotonic()
            elapsed = now - buf.last_flush_time
            if elapsed < self._debounce_secs * 0.9:
                # Spurious wakeup before debounce window; re-arm
                remaining = self._debounce_secs - elapsed
                loop = asyncio.get_event_loop()
                buf.timer_handle = loop.call_later(
                    remaining,
                    lambda g=group: asyncio.ensure_future(
                        self._timer_flush(g)
                    ),
                )
                return

            flushed = buf.reset(now)
            log.debug(
                "debouncer.timer_flush",
                group=group,
                chars=len(flushed),
            )

        # Invoke callback outside the lock to avoid re-entrancy issues
        await self._invoke_cb(group, flushed, is_final=False)

    async def _invoke_cb(self, group: str, text: str, is_final: bool) -> None:
        """Call the flush callback, swallowing exceptions."""
        if self._flush_cb is None:
            return
        try:
            await self._flush_cb(group, text, is_final)
        except Exception as exc:
            log.error(
                "debouncer.callback_error",
                group=group,
                is_final=is_final,
                error=str(exc),
            )
