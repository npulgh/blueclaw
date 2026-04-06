# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for T3.1 — Network Proxy Sidecar.

Covers:
- Domain whitelist matching (exact, wildcard, rejection)
- RateLimiter sliding-window logic
- ProxySidecar start/stop (docker commands mocked)
- handle_client integration (whitelist + rate-limit paths)

No actual Docker or network access required.
"""

from __future__ import annotations

import asyncio
import sys
import time
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import helpers — proxy_server lives in container/proxy/, not on sys.path by
# default.  Add it so we can import without installing.
# ---------------------------------------------------------------------------

import importlib
import os

_PROXY_SERVER_DIR = os.path.join(
    os.path.dirname(__file__), "..", "container", "proxy"
)
if _PROXY_SERVER_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(_PROXY_SERVER_DIR))

# proxy_server imports structlog at module level; make sure it's available.
import proxy_server as ps  # noqa: E402  (after sys.path manipulation)

from src.config import ProxyConfig  # noqa: E402
from src.proxy import ProxySidecar  # noqa: E402


# ===========================================================================
# 1. Domain whitelist matching
# ===========================================================================


class TestIsDomainAllowed:
    """Unit tests for ps.is_domain_allowed()."""

    def test_exact_match(self):
        assert ps.is_domain_allowed("api.anthropic.com", ["api.anthropic.com"])

    def test_exact_match_case_insensitive(self):
        assert ps.is_domain_allowed("API.Anthropic.COM", ["api.anthropic.com"])

    def test_wildcard_subdomain(self):
        assert ps.is_domain_allowed("www.google.com", ["*.google.com"])

    def test_wildcard_deep_subdomain(self):
        assert ps.is_domain_allowed("mail.google.com", ["*.google.com"])

    def test_wildcard_does_not_match_apex(self):
        # *.google.com should NOT match google.com itself
        assert not ps.is_domain_allowed("google.com", ["*.google.com"])

    def test_rejection_not_in_list(self):
        assert not ps.is_domain_allowed("evil.com", ["api.anthropic.com", "*.google.com"])

    def test_empty_allowed_list(self):
        assert not ps.is_domain_allowed("api.anthropic.com", [])

    def test_multiple_patterns_first_matches(self):
        allowed = ["api.anthropic.com", "*.google.com"]
        assert ps.is_domain_allowed("api.anthropic.com", allowed)
        assert ps.is_domain_allowed("maps.google.com", allowed)

    def test_partial_domain_not_matched(self):
        # "anthropic.com" should not match "api.anthropic.com"
        assert not ps.is_domain_allowed("anthropic.com", ["api.anthropic.com"])


# ===========================================================================
# 2. RateLimiter
# ===========================================================================


class TestRateLimiter:
    """Unit tests for ps.RateLimiter."""

    def test_allows_within_limit(self):
        rl = ps.RateLimiter(max_requests=5, window=60)
        for _ in range(5):
            assert rl.is_allowed("client") is True

    def test_blocks_over_limit(self):
        rl = ps.RateLimiter(max_requests=3, window=60)
        for _ in range(3):
            rl.is_allowed("client")
        assert rl.is_allowed("client") is False

    def test_window_expiry(self):
        """Requests older than the window should not count."""
        rl = ps.RateLimiter(max_requests=2, window=1)
        rl.is_allowed("client")
        rl.is_allowed("client")
        # Both slots used — should be blocked
        assert rl.is_allowed("client") is False
        # Manually age the timestamps past the window
        rl._buckets["client"] = [t - 2 for t in rl._buckets["client"]]
        # Now the window has expired — should be allowed again
        assert rl.is_allowed("client") is True

    def test_independent_clients(self):
        rl = ps.RateLimiter(max_requests=1, window=60)
        assert rl.is_allowed("alice") is True
        assert rl.is_allowed("bob") is True
        # alice is now blocked, bob still has one slot
        assert rl.is_allowed("alice") is False


# ===========================================================================
# 3. handle_client — whitelist and rate-limit paths
# ===========================================================================


def _make_reader(data: bytes) -> asyncio.StreamReader:
    """Build a StreamReader pre-loaded with *data*."""
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def _make_writer() -> tuple[asyncio.StreamWriter, list[bytes]]:
    """Return a mock StreamWriter and a list that collects written bytes."""
    written: list[bytes] = []

    transport = MagicMock()
    transport.is_closing.return_value = False

    writer = MagicMock(spec=asyncio.StreamWriter)
    writer.write = lambda data: written.append(data)
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()

    return writer, written


@pytest.mark.asyncio
async def test_handle_client_rejected_domain():
    """Non-whitelisted domain → 403 Forbidden."""
    request = b"CONNECT evil.com:443 HTTP/1.1\r\nHost: evil.com\r\n\r\n"
    reader = _make_reader(request)
    writer, written = _make_writer()

    await ps.handle_client(
        reader,
        writer,
        allowed_domains=["api.anthropic.com"],
        rate_limiter=ps.RateLimiter(),
    )

    response = b"".join(written)
    assert b"403" in response


@pytest.mark.asyncio
async def test_handle_client_rate_limited():
    """Exhausted rate limit → 429 Too Many Requests."""
    # Use a limiter that's already at capacity
    rl = ps.RateLimiter(max_requests=0, window=60)

    request = b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n"
    reader = _make_reader(request)
    writer, written = _make_writer()

    await ps.handle_client(
        reader,
        writer,
        allowed_domains=["api.anthropic.com"],
        rate_limiter=rl,
    )

    response = b"".join(written)
    assert b"429" in response


@pytest.mark.asyncio
async def test_handle_client_bad_method():
    """Non-CONNECT method → 405 Method Not Allowed."""
    request = b"GET http://api.anthropic.com/ HTTP/1.1\r\n\r\n"
    reader = _make_reader(request)
    writer, written = _make_writer()

    await ps.handle_client(
        reader,
        writer,
        allowed_domains=["api.anthropic.com"],
        rate_limiter=ps.RateLimiter(),
    )

    response = b"".join(written)
    assert b"405" in response


@pytest.mark.asyncio
async def test_handle_client_allowed_domain_attempts_connect():
    """Allowed domain passes whitelist/rate-limit; upstream connect may fail (502)."""
    request = b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n"
    reader = _make_reader(request)
    writer, written = _make_writer()

    # Patch asyncio.open_connection to simulate upstream refusal
    with patch("asyncio.open_connection", side_effect=ConnectionRefusedError("refused")):
        await ps.handle_client(
            reader,
            writer,
            allowed_domains=["api.anthropic.com"],
            rate_limiter=ps.RateLimiter(),
        )

    response = b"".join(written)
    # Should get 502 Bad Gateway (upstream refused), NOT 403 or 429
    assert b"502" in response
    assert b"403" not in response


# ===========================================================================
# 4. ProxySidecar — start / stop (docker mocked)
# ===========================================================================


@pytest.fixture
def proxy_config_enabled():
    return ProxyConfig(enabled=True, allowed_domains=["api.anthropic.com", "*.google.com"])


@pytest.fixture
def proxy_config_disabled():
    return ProxyConfig(enabled=False)


class TestProxySidecarInit:
    def test_init_stores_config(self, proxy_config_enabled):
        sidecar = ProxySidecar()
        sidecar.init(proxy_config_enabled, runtime="docker")
        assert sidecar._config is proxy_config_enabled
        assert sidecar._runtime == "docker"

    def test_is_running_false_before_start(self, proxy_config_enabled):
        sidecar = ProxySidecar()
        sidecar.init(proxy_config_enabled)
        assert sidecar.is_running is False

    def test_get_volume_name(self, proxy_config_enabled):
        sidecar = ProxySidecar()
        sidecar.init(proxy_config_enabled)
        assert sidecar.get_volume_name() == "lynxclaw_proxy_vol"


class TestProxySidecarStart:
    @pytest.mark.asyncio
    async def test_start_disabled_is_noop(self, proxy_config_disabled):
        sidecar = ProxySidecar()
        sidecar.init(proxy_config_disabled)
        # Should not raise and should not set _running
        await sidecar.start()
        assert sidecar.is_running is False

    @pytest.mark.asyncio
    async def test_start_raises_without_init(self):
        sidecar = ProxySidecar()
        with pytest.raises(RuntimeError, match="init\\(\\)"):
            await sidecar.start()

    @pytest.mark.asyncio
    async def test_start_calls_docker_run(self, proxy_config_enabled):
        """start() should invoke docker volume create + docker rm -f + docker run -d."""
        sidecar = ProxySidecar()
        sidecar.init(proxy_config_enabled, runtime="docker")

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"abc123\n", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await sidecar.start()

        assert sidecar.is_running is True
        # Verify docker run was called at some point
        calls = [call.args for call in mock_exec.call_args_list]
        run_calls = [c for c in calls if len(c) >= 2 and c[1] == "run"]
        assert len(run_calls) == 1, f"Expected exactly one 'docker run' call, got: {run_calls}"

        run_args = run_calls[0]
        # Must include --detach
        assert "--detach" in run_args
        # Must include the proxy volume
        assert "-v" in run_args
        vol_idx = list(run_args).index("-v")
        assert "lynxclaw_proxy_vol:/proxy:rw" in run_args[vol_idx + 1]
        # Must pass ALLOWED_DOMAINS env var
        assert "-e" in run_args
        env_pairs = [
            run_args[i + 1]
            for i, a in enumerate(run_args)
            if a == "-e"
        ]
        assert any("ALLOWED_DOMAINS=" in ep for ep in env_pairs)

    @pytest.mark.asyncio
    async def test_start_raises_on_docker_failure(self, proxy_config_enabled):
        sidecar = ProxySidecar()
        sidecar.init(proxy_config_enabled)

        # volume create and rm -f succeed; docker run fails
        call_count = 0

        async def _fake_exec(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            proc = AsyncMock()
            if args[1] == "run":
                proc.returncode = 1
                proc.communicate = AsyncMock(return_value=(b"", b"image not found"))
            else:
                proc.returncode = 0
                proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
            with pytest.raises(RuntimeError, match="Failed to start proxy sidecar"):
                await sidecar.start()

        assert sidecar.is_running is False


class TestProxySidecarStop:
    @pytest.mark.asyncio
    async def test_stop_noop_when_not_running(self):
        sidecar = ProxySidecar()
        sidecar.init(ProxyConfig(enabled=True))
        # Should not raise even though never started
        await sidecar.stop()

    @pytest.mark.asyncio
    async def test_stop_calls_docker_stop_and_rm(self, proxy_config_enabled):
        sidecar = ProxySidecar()
        sidecar.init(proxy_config_enabled)
        sidecar._running = True  # simulate already started

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await sidecar.stop()

        assert sidecar.is_running is False
        calls = [call.args for call in mock_exec.call_args_list]
        subcmds = [c[1] for c in calls if len(c) >= 2]
        assert "stop" in subcmds
        assert "rm" in subcmds

    @pytest.mark.asyncio
    async def test_stop_tolerates_docker_errors(self, proxy_config_enabled):
        """stop() must not raise even if docker commands fail."""
        sidecar = ProxySidecar()
        sidecar.init(proxy_config_enabled)
        sidecar._running = True

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=OSError("docker not found"),
        ):
            await sidecar.stop()  # should not raise

        assert sidecar.is_running is False
