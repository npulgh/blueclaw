"""Lynxclaw observability — structured logging + Prometheus metrics.

Provides:
- structlog JSON output with per-task correlation IDs (via contextvars)
- 7 core Prometheus metrics + event_loop_lag gauge
- Lightweight HTTP server exposing /metrics (prometheus_client built-in)
- Async event loop lag monitor task
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import sys
import time
import uuid
from typing import Any, Optional

import structlog
from prometheus_client import Counter, Gauge, Histogram, start_http_server

# ---------------------------------------------------------------------------
# Correlation ID — one per asyncio Task, propagated automatically via copy_context
# ---------------------------------------------------------------------------

_correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)


def get_correlation_id() -> str:
    """Return current correlation ID, generating one if none is set."""
    cid = _correlation_id_var.get()
    if not cid:
        cid = uuid.uuid4().hex
        _correlation_id_var.set(cid)
    return cid


def set_correlation_id(cid: str) -> None:
    """Explicitly set correlation ID for the current context."""
    _correlation_id_var.set(cid)


def new_correlation_id() -> str:
    """Generate and set a fresh correlation ID, return it."""
    cid = uuid.uuid4().hex
    _correlation_id_var.set(cid)
    return cid


# ---------------------------------------------------------------------------
# structlog processor — injects correlation_id into every log record
# ---------------------------------------------------------------------------

def _add_correlation_id(
    logger: Any, method_name: str, event_dict: dict
) -> dict:
    if "correlation_id" not in event_dict:
        event_dict["correlation_id"] = get_correlation_id()
    return event_dict


# ---------------------------------------------------------------------------
# Prometheus metric definitions (module-level singletons)
# ---------------------------------------------------------------------------

messages_total = Counter(
    "lynxclaw_messages_total",
    "Total messages processed",
    ["channel", "direction"],
)

container_duration_seconds = Histogram(
    "lynxclaw_container_duration_seconds",
    "Container run duration in seconds",
    ["group", "status"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600),
)

tokens_total = Counter(
    "lynxclaw_tokens_total",
    "Token usage",
    ["group", "type"],
)

active_containers = Gauge(
    "lynxclaw_active_containers",
    "Number of currently running agent containers",
)

ipc_latency_seconds = Histogram(
    "lynxclaw_ipc_latency_seconds",
    "IPC file processing latency in seconds",
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
)

tool_calls_total = Counter(
    "lynxclaw_tool_calls_total",
    "Number of tool calls made by agents",
    ["tool_name", "blocked"],
)

event_loop_lag_seconds = Gauge(
    "lynxclaw_event_loop_lag_seconds",
    "Current asyncio event loop lag in seconds",
)


# ---------------------------------------------------------------------------
# Observability class
# ---------------------------------------------------------------------------

class Observability:
    """Metrics + structured logging with correlation IDs.

    Usage::

        obs = Observability()
        Observability.setup_logging(level="info")
        obs.init(port=9090)
        # inside async context:
        asyncio.create_task(obs.start_event_loop_monitor())
        ...
        obs.stop()
    """

    # Expose module-level metric objects as instance attributes so callers can
    # access them via obs.messages_total etc. without an extra import.
    messages_total = messages_total
    container_duration_seconds = container_duration_seconds
    tokens_total = tokens_total
    active_containers = active_containers
    ipc_latency_seconds = ipc_latency_seconds
    tool_calls_total = tool_calls_total
    event_loop_lag_seconds = event_loop_lag_seconds

    def __init__(self) -> None:
        self._server: Optional[Any] = None  # returned by start_http_server
        self._monitor_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    @staticmethod
    def setup_logging(level: str = "info") -> None:
        """Configure structlog with JSON output and correlation_id injection."""
        structlog.configure(
            processors=[
                structlog.stdlib.add_log_level,
                structlog.stdlib.PositionalArgumentsFormatter(),
                structlog.processors.TimeStamper(fmt="iso"),
                _add_correlation_id,
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.stdlib.BoundLogger,
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
        )
        numeric = getattr(logging, level.upper(), logging.INFO)
        logging.basicConfig(stream=sys.stdout, level=numeric, force=True)

    # ------------------------------------------------------------------
    # Prometheus HTTP server
    # ------------------------------------------------------------------

    def init(self, port: int = 9090) -> None:
        """Start the Prometheus metrics HTTP server on the given port."""
        self._server = start_http_server(port)

    def stop(self) -> None:
        """Stop the metrics HTTP server and the event loop monitor task."""
        self.stop_event_loop_monitor()
        # prometheus_client's start_http_server returns an HTTPServer instance;
        # shut it down if available.
        if self._server is not None:
            try:
                httpd, thread = self._server
                httpd.shutdown()
            except (TypeError, ValueError):
                # Older versions return just the HTTPServer
                try:
                    self._server.shutdown()
                except Exception:
                    pass
            self._server = None

    # ------------------------------------------------------------------
    # Event loop lag monitor
    # ------------------------------------------------------------------

    async def start_event_loop_monitor(self) -> None:
        """Spawn a background task that continuously measures event loop lag.

        This method creates an asyncio Task and returns immediately.
        Use stop_event_loop_monitor() to cancel it.
        """
        if self._monitor_task is not None and not self._monitor_task.done():
            return  # already running
        self._monitor_task = asyncio.create_task(
            self._measure_loop_lag(), name="event-loop-monitor"
        )

    def stop_event_loop_monitor(self) -> None:
        """Cancel the event loop lag monitor task if running."""
        if self._monitor_task is not None and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._monitor_task = None

    async def _measure_loop_lag(self) -> None:
        """Measure event loop lag every second and update the gauge."""
        while True:
            t0 = time.monotonic()
            await asyncio.sleep(1.0)
            lag = time.monotonic() - t0 - 1.0
            event_loop_lag_seconds.set(max(0.0, lag))
