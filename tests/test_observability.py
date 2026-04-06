# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for src/observability.py."""

from __future__ import annotations

import asyncio
import socket
import urllib.request

import pytest

from src.observability import (
    Observability,
    active_containers,
    container_duration_seconds,
    event_loop_lag_seconds,
    get_correlation_id,
    ipc_latency_seconds,
    messages_total,
    new_correlation_id,
    set_correlation_id,
    tokens_total,
    tool_calls_total,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    """Return a free TCP port on localhost."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Metrics registration
# ---------------------------------------------------------------------------

def test_all_metrics_are_registered():
    """All 8 metric objects must be importable with correct names.

    Counters expose the base name in _name and the full name (with _total) in
    _original_name.  Gauges and Histograms only have _name.
    """
    # Counters: registered as <name>_total, stored in _original_name
    assert messages_total._original_name == "lynxclaw_messages_total"
    assert tokens_total._original_name == "lynxclaw_tokens_total"
    assert tool_calls_total._original_name == "lynxclaw_tool_calls_total"
    # Histogram / Gauge: registered name is in _name
    assert container_duration_seconds._name == "lynxclaw_container_duration_seconds"
    assert active_containers._name == "lynxclaw_active_containers"
    assert ipc_latency_seconds._name == "lynxclaw_ipc_latency_seconds"
    assert event_loop_lag_seconds._name == "lynxclaw_event_loop_lag_seconds"


def test_instance_attributes_match_module_singletons():
    """Observability instance attributes must be the same objects as module singletons."""
    obs = Observability()
    assert obs.messages_total is messages_total
    assert obs.container_duration_seconds is container_duration_seconds
    assert obs.tokens_total is tokens_total
    assert obs.active_containers is active_containers
    assert obs.ipc_latency_seconds is ipc_latency_seconds
    assert obs.tool_calls_total is tool_calls_total
    assert obs.event_loop_lag_seconds is event_loop_lag_seconds


# ---------------------------------------------------------------------------
# Metrics server
# ---------------------------------------------------------------------------

def test_metrics_server_responds():
    """start_http_server must expose Prometheus format including event_loop_lag."""
    port = _free_port()
    obs = Observability()
    obs.init(port=port)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=3) as r:
            body = r.read().decode()
        assert "lynxclaw_event_loop_lag_seconds" in body
        assert "lynxclaw_messages_total" in body
        assert "lynxclaw_active_containers" in body
    finally:
        obs.stop()


# ---------------------------------------------------------------------------
# Event loop lag monitor
# ---------------------------------------------------------------------------

async def test_event_loop_monitor_updates_gauge():
    """After running for >1 s the gauge must have been written (value >= 0)."""
    obs = Observability()
    # Reset gauge to a sentinel so we can detect it was set
    event_loop_lag_seconds.set(-1.0)

    await obs.start_event_loop_monitor()
    # Wait long enough for at least one measurement cycle (sleep(1.0) inside)
    await asyncio.sleep(1.2)
    obs.stop_event_loop_monitor()

    # The monitor sets max(0, lag), so value must be >= 0 (not the -1 sentinel)
    from prometheus_client import REGISTRY
    value = REGISTRY.get_sample_value("lynxclaw_event_loop_lag_seconds")
    assert value is not None
    assert value >= 0.0


async def test_event_loop_monitor_cancels_cleanly():
    """stop_event_loop_monitor must cancel without raising."""
    obs = Observability()
    await obs.start_event_loop_monitor()
    assert obs._monitor_task is not None
    obs.stop_event_loop_monitor()
    # Give event loop a tick to process the cancellation
    await asyncio.sleep(0)
    assert obs._monitor_task is None


async def test_event_loop_monitor_no_duplicate_task():
    """Calling start_event_loop_monitor twice must not spawn a second task."""
    obs = Observability()
    await obs.start_event_loop_monitor()
    first_task = obs._monitor_task
    await obs.start_event_loop_monitor()
    assert obs._monitor_task is first_task
    obs.stop_event_loop_monitor()


# ---------------------------------------------------------------------------
# Correlation ID
# ---------------------------------------------------------------------------

def test_correlation_id_generated_on_first_access():
    """get_correlation_id must return a non-empty hex string."""
    # Run in a fresh context to avoid pollution from previous tests
    import contextvars

    ctx = contextvars.copy_context()

    def _check():
        from src.observability import _correlation_id_var
        _correlation_id_var.set("")  # reset
        cid = get_correlation_id()
        assert isinstance(cid, str)
        assert len(cid) == 32  # uuid4().hex

    ctx.run(_check)


def test_set_correlation_id():
    """set_correlation_id must persist within the same context."""
    import contextvars

    ctx = contextvars.copy_context()

    def _check():
        set_correlation_id("abc123")
        assert get_correlation_id() == "abc123"

    ctx.run(_check)


def test_new_correlation_id_returns_fresh_value():
    """new_correlation_id must return a new ID different from any previous one."""
    import contextvars

    ctx = contextvars.copy_context()

    def _check():
        cid1 = new_correlation_id()
        cid2 = new_correlation_id()
        assert cid1 != cid2
        assert len(cid2) == 32

    ctx.run(_check)


def test_correlation_id_added_to_log_events():
    """_add_correlation_id processor must inject correlation_id into event_dict."""
    import contextvars

    from src.observability import _add_correlation_id, _correlation_id_var

    ctx = contextvars.copy_context()

    def _check():
        _correlation_id_var.set("test-cid-xyz")
        event_dict: dict = {"event": "something_happened"}
        result = _add_correlation_id(None, "info", event_dict)
        assert result["correlation_id"] == "test-cid-xyz"

    ctx.run(_check)


def test_correlation_id_not_overwritten_if_present():
    """Processor must not overwrite an existing correlation_id in event_dict."""
    from src.observability import _add_correlation_id

    event_dict = {"event": "x", "correlation_id": "already-set"}
    result = _add_correlation_id(None, "info", event_dict)
    assert result["correlation_id"] == "already-set"


# ---------------------------------------------------------------------------
# setup_logging smoke test
# ---------------------------------------------------------------------------

def test_setup_logging_does_not_raise():
    """setup_logging must configure structlog without raising."""
    Observability.setup_logging(level="debug")
    Observability.setup_logging(level="info")  # idempotent
