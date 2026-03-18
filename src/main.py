"""Lynxclaw host process — asyncio entry point."""

import asyncio
import logging
import signal
import sys

import structlog

log = structlog.get_logger()


def _setup_logging() -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)


async def main() -> None:
    _setup_logging()

    stop_event = asyncio.Event()

    def _handle_signal(sig: signal.Signals) -> None:
        log.info("shutdown_signal_received", signal=sig.name)
        stop_event.set()

    # loop.add_signal_handler is Unix-only; use signal.signal() which works
    # cross-platform.  The lambda schedules the async event from the sync handler.
    loop = asyncio.get_running_loop()

    def _make_handler(sig: signal.Signals):
        def _handler(signum, frame):  # noqa: ARG001
            loop.call_soon_threadsafe(_handle_signal, sig)
        return _handler

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _make_handler(sig))

    print("Lynxclaw starting...")
    log.info("lynxclaw_started")

    await stop_event.wait()

    log.info("lynxclaw_stopped")


if __name__ == "__main__":
    asyncio.run(main())
