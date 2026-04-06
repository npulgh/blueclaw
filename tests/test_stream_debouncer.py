# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for src/stream_debouncer.py.

Covers:
- Immediate flush when chars >= debounce_chars threshold
- Time-based flush after debounce_ms
- is_final always flushes immediately
- Multiple groups don't interfere with each other
- No flush below both thresholds (chars and time)
- Flush callback is invoked with correct arguments
- stop() flushes pending buffers
"""

from __future__ import annotations

import asyncio
from typing import List, Tuple
from unittest.mock import AsyncMock

import pytest

from src.stream_debouncer import StreamDebouncer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_debouncer(ms: int = 500, chars: int = 200) -> StreamDebouncer:
    return StreamDebouncer(debounce_ms=ms, debounce_chars=chars)


def _capture_cb() -> Tuple[AsyncMock, List[Tuple[str, str, bool]]]:
    """Return (mock_cb, calls_list). calls_list is populated on each invocation."""
    calls: List[Tuple[str, str, bool]] = []

    async def cb(group: str, text: str, is_final: bool) -> None:
        calls.append((group, text, is_final))

    mock = AsyncMock(side_effect=cb)
    return mock, calls


# ---------------------------------------------------------------------------
# Char-threshold flush
# ---------------------------------------------------------------------------

class TestCharThresholdFlush:
    @pytest.mark.asyncio
    async def test_flush_when_chars_reach_threshold(self):
        """Accumulating >= debounce_chars triggers a flush."""
        d = _make_debouncer(ms=9999, chars=10)
        cb, calls = _capture_cb()
        d.set_flush_callback(cb)
        await d.start()

        # 9 chars — below threshold, no flush
        result = await d.add_chunk("g1", "A" * 9, is_final=False)
        assert result is None
        assert len(calls) == 0

        # 1 more char — hits threshold exactly, flush triggered
        result = await d.add_chunk("g1", "B", is_final=False)
        assert result == "A" * 9 + "B"
        assert len(calls) == 1
        group, text, is_final = calls[0]
        assert group == "g1"
        assert text == "A" * 9 + "B"
        assert is_final is False

        await d.stop()

    @pytest.mark.asyncio
    async def test_flush_when_chars_exceed_threshold(self):
        """A single chunk larger than the threshold flushes immediately."""
        d = _make_debouncer(ms=9999, chars=5)
        cb, calls = _capture_cb()
        d.set_flush_callback(cb)
        await d.start()

        result = await d.add_chunk("g1", "Hello World", is_final=False)
        assert result == "Hello World"
        assert len(calls) == 1

        await d.stop()

    @pytest.mark.asyncio
    async def test_buffer_resets_after_char_flush(self):
        """After a char-threshold flush, the buffer starts fresh."""
        d = _make_debouncer(ms=9999, chars=5)
        cb, calls = _capture_cb()
        d.set_flush_callback(cb)
        await d.start()

        await d.add_chunk("g1", "AAAAA", is_final=False)  # flush #1
        assert len(calls) == 1

        # New chunk — buffer is empty again
        result = await d.add_chunk("g1", "BB", is_final=False)
        assert result is None  # below threshold
        assert len(calls) == 1  # no new flush

        await d.stop()


# ---------------------------------------------------------------------------
# is_final flush
# ---------------------------------------------------------------------------

class TestIsFinalFlush:
    @pytest.mark.asyncio
    async def test_is_final_always_flushes(self):
        """is_final=True triggers flush regardless of char count."""
        d = _make_debouncer(ms=9999, chars=9999)
        cb, calls = _capture_cb()
        d.set_flush_callback(cb)
        await d.start()

        result = await d.add_chunk("g1", "tiny", is_final=True)
        assert result == "tiny"
        assert len(calls) == 1
        assert calls[0] == ("g1", "tiny", True)

        await d.stop()

    @pytest.mark.asyncio
    async def test_is_final_flushes_accumulated_text(self):
        """is_final flushes all accumulated text, not just the last chunk."""
        d = _make_debouncer(ms=9999, chars=9999)
        cb, calls = _capture_cb()
        d.set_flush_callback(cb)
        await d.start()

        await d.add_chunk("g1", "Hello ", is_final=False)
        await d.add_chunk("g1", "world", is_final=False)
        result = await d.add_chunk("g1", "!", is_final=True)

        assert result == "Hello world!"
        assert len(calls) == 1
        assert calls[0][1] == "Hello world!"
        assert calls[0][2] is True

        await d.stop()

    @pytest.mark.asyncio
    async def test_is_final_cleans_up_group_state(self):
        """After is_final, the group buffer is removed."""
        d = _make_debouncer(ms=9999, chars=9999)
        cb, calls = _capture_cb()
        d.set_flush_callback(cb)
        await d.start()

        await d.add_chunk("g1", "text", is_final=True)
        assert not d.has_pending("g1")

        await d.stop()

    @pytest.mark.asyncio
    async def test_is_final_empty_chunk_still_flushes(self):
        """is_final with empty text still invokes the callback."""
        d = _make_debouncer(ms=9999, chars=9999)
        cb, calls = _capture_cb()
        d.set_flush_callback(cb)
        await d.start()

        await d.add_chunk("g1", "partial", is_final=False)
        result = await d.add_chunk("g1", "", is_final=True)

        assert result == "partial"
        assert len(calls) == 1
        assert calls[0][2] is True

        await d.stop()


# ---------------------------------------------------------------------------
# Time-based flush
# ---------------------------------------------------------------------------

class TestTimeBasedFlush:
    @pytest.mark.asyncio
    async def test_timer_flush_after_debounce_ms(self):
        """Buffer flushes automatically after debounce_ms even without new chunks."""
        d = _make_debouncer(ms=50, chars=9999)  # 50 ms debounce
        cb, calls = _capture_cb()
        d.set_flush_callback(cb)
        await d.start()

        await d.add_chunk("g1", "hello", is_final=False)
        assert len(calls) == 0  # not flushed yet

        # Wait longer than debounce window
        await asyncio.sleep(0.15)
        assert len(calls) == 1
        assert calls[0][1] == "hello"
        assert calls[0][2] is False  # timer flush is not final

        await d.stop()

    @pytest.mark.asyncio
    async def test_no_flush_before_debounce_ms(self):
        """Buffer is NOT flushed before the debounce window expires."""
        d = _make_debouncer(ms=200, chars=9999)
        cb, calls = _capture_cb()
        d.set_flush_callback(cb)
        await d.start()

        await d.add_chunk("g1", "hello", is_final=False)
        await asyncio.sleep(0.05)  # well within 200 ms window
        assert len(calls) == 0

        await d.stop()


# ---------------------------------------------------------------------------
# Multiple groups isolation
# ---------------------------------------------------------------------------

class TestMultipleGroups:
    @pytest.mark.asyncio
    async def test_groups_are_independent(self):
        """Flushing one group does not affect another group's buffer."""
        d = _make_debouncer(ms=9999, chars=5)
        cb, calls = _capture_cb()
        d.set_flush_callback(cb)
        await d.start()

        # g1 accumulates 5 chars → flush
        await d.add_chunk("g1", "AAAAA", is_final=False)
        # g2 only has 2 chars → no flush
        await d.add_chunk("g2", "BB", is_final=False)

        assert len(calls) == 1
        assert calls[0][0] == "g1"
        assert d.has_pending("g2")

        await d.stop()

    @pytest.mark.asyncio
    async def test_final_on_one_group_does_not_affect_other(self):
        """is_final on g1 does not flush g2."""
        d = _make_debouncer(ms=9999, chars=9999)
        cb, calls = _capture_cb()
        d.set_flush_callback(cb)
        await d.start()

        await d.add_chunk("g1", "text1", is_final=False)
        await d.add_chunk("g2", "text2", is_final=False)
        await d.add_chunk("g1", "", is_final=True)

        # Only g1 flushed
        assert len(calls) == 1
        assert calls[0][0] == "g1"
        assert d.has_pending("g2")

        await d.stop()

    @pytest.mark.asyncio
    async def test_multiple_groups_flush_independently(self):
        """Both groups can flush independently."""
        d = _make_debouncer(ms=9999, chars=5)
        cb, calls = _capture_cb()
        d.set_flush_callback(cb)
        await d.start()

        await d.add_chunk("g1", "AAAAA", is_final=False)  # flush g1
        await d.add_chunk("g2", "BBBBB", is_final=False)  # flush g2

        assert len(calls) == 2
        groups_flushed = {c[0] for c in calls}
        assert groups_flushed == {"g1", "g2"}

        await d.stop()


# ---------------------------------------------------------------------------
# Below-threshold: no flush
# ---------------------------------------------------------------------------

class TestNoFlushBelowThresholds:
    @pytest.mark.asyncio
    async def test_small_chunk_no_flush(self):
        """A small chunk below both thresholds returns None."""
        d = _make_debouncer(ms=9999, chars=100)
        cb, calls = _capture_cb()
        d.set_flush_callback(cb)
        await d.start()

        result = await d.add_chunk("g1", "hi", is_final=False)
        assert result is None
        assert len(calls) == 0
        assert d.has_pending("g1")

        await d.stop()

    @pytest.mark.asyncio
    async def test_multiple_small_chunks_accumulate(self):
        """Multiple small chunks accumulate without flushing."""
        d = _make_debouncer(ms=9999, chars=100)
        cb, calls = _capture_cb()
        d.set_flush_callback(cb)
        await d.start()

        for _ in range(5):
            result = await d.add_chunk("g1", "x", is_final=False)
            assert result is None

        assert len(calls) == 0
        assert d.has_pending("g1")

        await d.stop()


# ---------------------------------------------------------------------------
# stop() flushes pending buffers
# ---------------------------------------------------------------------------

class TestStopFlushes:
    @pytest.mark.asyncio
    async def test_stop_flushes_pending(self):
        """stop() flushes any pending buffers before shutting down."""
        d = _make_debouncer(ms=9999, chars=9999)
        cb, calls = _capture_cb()
        d.set_flush_callback(cb)
        await d.start()

        await d.add_chunk("g1", "pending text", is_final=False)
        assert len(calls) == 0

        await d.stop()
        assert len(calls) == 1
        assert calls[0][1] == "pending text"
        assert calls[0][2] is True  # stop flushes as final

    @pytest.mark.asyncio
    async def test_stop_flushes_multiple_groups(self):
        """stop() flushes all pending groups."""
        d = _make_debouncer(ms=9999, chars=9999)
        cb, calls = _capture_cb()
        d.set_flush_callback(cb)
        await d.start()

        await d.add_chunk("g1", "text1", is_final=False)
        await d.add_chunk("g2", "text2", is_final=False)

        await d.stop()
        assert len(calls) == 2
        groups = {c[0] for c in calls}
        assert groups == {"g1", "g2"}


# ---------------------------------------------------------------------------
# flush_group manual flush
# ---------------------------------------------------------------------------

class TestManualFlush:
    @pytest.mark.asyncio
    async def test_flush_group_returns_text(self):
        d = _make_debouncer(ms=9999, chars=9999)
        cb, calls = _capture_cb()
        d.set_flush_callback(cb)
        await d.start()

        await d.add_chunk("g1", "hello", is_final=False)
        result = await d.flush_group("g1")
        assert result == "hello"
        assert len(calls) == 1

        await d.stop()

    @pytest.mark.asyncio
    async def test_flush_group_empty_returns_none(self):
        d = _make_debouncer(ms=9999, chars=9999)
        cb, calls = _capture_cb()
        d.set_flush_callback(cb)
        await d.start()

        result = await d.flush_group("nonexistent")
        assert result is None
        assert len(calls) == 0

        await d.stop()
