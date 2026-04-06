# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Lynxclaw Proxy Server — domain-filtering HTTP CONNECT proxy.

Runs inside the ``lynxclaw-proxy`` container.  Listens on a Unix Socket at
``/proxy/proxy.sock`` and acts as an HTTP CONNECT proxy for HTTPS tunnelling.
Plain HTTP CONNECT tunnels are also handled for HTTP-over-proxy.

Features:
- Listens on Unix Socket (shared with agent containers via Docker volume)
- Domain whitelist (``ALLOWED_DOMAINS`` env var, comma-separated)
- Wildcard domain support (``*.google.com`` matches ``www.google.com``)
- Rejects non-whitelisted domains with HTTP 403
- Logs each request: domain, response size, duration
- Rate limiting: max 60 requests per minute per client (by peer identity)

Usage (inside container)::

    ALLOWED_DOMAINS=api.anthropic.com,*.google.com python proxy_server.py

The Unix Socket path defaults to ``/proxy/proxy.sock`` and can be overridden
with the ``PROXY_SOCK`` environment variable (useful in tests).
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import time
from collections import defaultdict

import structlog

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
)
logging.basicConfig(level=logging.INFO)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration (from environment)
# ---------------------------------------------------------------------------

SOCK_PATH: str = os.environ.get("PROXY_SOCK", "/proxy/proxy.sock")
_raw_domains: str = os.environ.get("ALLOWED_DOMAINS", "api.anthropic.com")
ALLOWED_DOMAINS: list[str] = [d.strip() for d in _raw_domains.split(",") if d.strip()]

# Rate limit: max requests per window per client
RATE_LIMIT_MAX: int = int(os.environ.get("RATE_LIMIT_MAX", "60"))
RATE_LIMIT_WINDOW: int = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))  # seconds

# ---------------------------------------------------------------------------
# Domain matching
# ---------------------------------------------------------------------------


def is_domain_allowed(domain: str, allowed: list[str]) -> bool:
    """Return True if *domain* matches any entry in *allowed*.

    Matching rules:
    - Exact match: ``api.anthropic.com`` matches ``api.anthropic.com``
    - Wildcard: ``*.google.com`` matches ``www.google.com`` and
      ``mail.google.com`` but NOT ``google.com`` itself.

    Args:
        domain: The target domain (hostname only, no port).
        allowed: List of allowed domain patterns.

    Returns:
        True if the domain is permitted.
    """
    domain_lower = domain.lower()
    for pattern in allowed:
        pattern_lower = pattern.lower()
        if fnmatch.fnmatch(domain_lower, pattern_lower):
            return True
    return False


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """Simple per-client sliding-window rate limiter.

    Tracks request timestamps per client key.  A client is identified by a
    string key (e.g. ``"unix"`` since all agents share the same socket path).
    """

    def __init__(self, max_requests: int = RATE_LIMIT_MAX, window: int = RATE_LIMIT_WINDOW) -> None:
        self._max = max_requests
        self._window = window
        # client_key → list of request timestamps (floats)
        self._buckets: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, client_key: str) -> bool:
        """Return True if the client has not exceeded the rate limit.

        Prunes timestamps older than the window before checking.

        Args:
            client_key: Opaque string identifying the client.

        Returns:
            True if the request is within the rate limit.
        """
        now = time.monotonic()
        bucket = self._buckets[client_key]
        # Prune expired entries
        cutoff = now - self._window
        self._buckets[client_key] = [t for t in bucket if t > cutoff]
        if len(self._buckets[client_key]) >= self._max:
            return False
        self._buckets[client_key].append(now)
        return True


# ---------------------------------------------------------------------------
# HTTP CONNECT proxy handler
# ---------------------------------------------------------------------------


async def _send_response(writer: asyncio.StreamWriter, status: int, reason: str) -> None:
    """Write a minimal HTTP/1.1 response to *writer*."""
    writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\n\r\n".encode())
    await writer.drain()


async def _tunnel(
    reader_a: asyncio.StreamReader,
    writer_b: asyncio.StreamWriter,
) -> int:
    """Forward bytes from *reader_a* to *writer_b* until EOF.

    Returns:
        Total bytes forwarded.
    """
    total = 0
    try:
        while True:
            chunk = await reader_a.read(65536)
            if not chunk:
                break
            writer_b.write(chunk)
            await writer_b.drain()
            total += len(chunk)
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    return total


async def _handle_connect(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    host: str,
    port: int,
    rate_limiter: RateLimiter,
    client_key: str,
) -> None:
    """Handle an HTTP CONNECT tunnel request.

    Opens a TCP connection to ``host:port``, sends 200 Connection established,
    then bidirectionally forwards bytes between the client and the upstream.
    """
    t0 = time.monotonic()
    total_bytes = 0

    try:
        up_reader, up_writer = await asyncio.open_connection(host, port)
    except (OSError, ConnectionRefusedError) as exc:
        log.warning("proxy.connect_failed", host=host, port=port, error=str(exc))
        await _send_response(writer, 502, "Bad Gateway")
        return

    await _send_response(writer, 200, "Connection established")

    # Bidirectional tunnel
    fwd, rev = await asyncio.gather(
        _tunnel(reader, up_writer),
        _tunnel(up_reader, writer),
        return_exceptions=True,
    )

    # Account for bytes in both directions
    if isinstance(fwd, int):
        total_bytes += fwd
    if isinstance(rev, int):
        total_bytes += rev

    try:
        up_writer.close()
        await up_writer.wait_closed()
    except Exception:  # noqa: BLE001
        pass

    elapsed = time.monotonic() - t0
    log.info(
        "proxy.request",
        host=host,
        port=port,
        bytes=total_bytes,
        duration_ms=round(elapsed * 1000, 2),
    )


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    allowed_domains: list[str],
    rate_limiter: RateLimiter,
) -> None:
    """Handle a single proxy client connection.

    Reads the first line of the HTTP request to determine the CONNECT target,
    validates the domain against the whitelist, applies rate limiting, then
    either tunnels the connection or rejects it.

    Args:
        reader: Asyncio stream reader for the client socket.
        writer: Asyncio stream writer for the client socket.
        allowed_domains: Whitelist of domain patterns.
        rate_limiter: Shared rate limiter instance.
    """
    client_key = "unix"  # All agents share the same Unix Socket path

    try:
        # Read request line (e.g. "CONNECT api.anthropic.com:443 HTTP/1.1\r\n")
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=30.0)
        except asyncio.TimeoutError:
            await _send_response(writer, 408, "Request Timeout")
            return

        request_line = request_line.decode("utf-8", errors="replace").strip()
        if not request_line:
            return

        parts = request_line.split()
        if len(parts) < 2:
            await _send_response(writer, 400, "Bad Request")
            return

        method = parts[0].upper()
        if method != "CONNECT":
            # Only CONNECT is supported for tunnelling
            await _send_response(writer, 405, "Method Not Allowed")
            return

        # Parse host:port
        target = parts[1]
        if ":" in target:
            host, port_str = target.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                await _send_response(writer, 400, "Bad Request")
                return
        else:
            host = target
            port = 443  # default HTTPS port

        # Consume remaining request headers until blank line
        try:
            while True:
                header_line = await asyncio.wait_for(reader.readline(), timeout=10.0)
                if header_line in (b"\r\n", b"\n", b""):
                    break
        except asyncio.TimeoutError:
            pass

        # Domain whitelist check
        if not is_domain_allowed(host, allowed_domains):
            log.warning(
                "proxy.rejected",
                host=host,
                port=port,
                reason="domain_not_allowed",
            )
            await _send_response(writer, 403, "Forbidden")
            return

        # Rate limiting check
        if not rate_limiter.is_allowed(client_key):
            log.warning(
                "proxy.rate_limited",
                host=host,
                port=port,
                client=client_key,
            )
            await _send_response(writer, 429, "Too Many Requests")
            return

        # All checks passed — establish tunnel
        await _handle_connect(reader, writer, host, port, rate_limiter, client_key)

    except Exception as exc:  # noqa: BLE001
        log.error("proxy.handler_error", error=str(exc))
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Server entrypoint
# ---------------------------------------------------------------------------


async def run_server(
    sock_path: str = SOCK_PATH,
    allowed_domains: list[str] | None = None,
    rate_limiter: RateLimiter | None = None,
) -> None:
    """Start the Unix Socket proxy server.

    Removes any stale socket file, starts the server, then waits for
    ``KeyboardInterrupt`` or ``asyncio.CancelledError``.

    Args:
        sock_path: Path to the Unix Socket file (created on startup).
        allowed_domains: Domain whitelist.  Defaults to :data:`ALLOWED_DOMAINS`.
        rate_limiter: Rate limiter instance.  A new one is created if not provided.
    """
    if allowed_domains is None:
        allowed_domains = ALLOWED_DOMAINS
    if rate_limiter is None:
        rate_limiter = RateLimiter()

    # Ensure the /proxy directory exists
    os.makedirs(os.path.dirname(sock_path), exist_ok=True)

    # Remove stale socket file (from a previous run)
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_client(
            reader,
            writer,
            allowed_domains=allowed_domains,
            rate_limiter=rate_limiter,
        )

    server = await asyncio.start_unix_server(_handler, path=sock_path)

    # Make socket world-writable so the non-root agent user can connect
    try:
        os.chmod(sock_path, 0o777)
    except OSError as exc:
        log.warning("proxy.chmod_failed", error=str(exc))

    log.info(
        "proxy.started",
        sock_path=sock_path,
        allowed_domains=allowed_domains,
        rate_limit_max=RATE_LIMIT_MAX,
        rate_limit_window=RATE_LIMIT_WINDOW,
    )

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        log.info("proxy.shutdown")
