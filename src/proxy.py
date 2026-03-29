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

"""Lynxclaw Proxy Sidecar — host-side lifecycle manager.

Manages the lifecycle of the proxy sidecar container that provides filtered
network access for agent containers.  The actual filtering proxy logic runs
inside the ``lynxclaw-proxy:latest`` container; this module only handles
start/stop of that container and provides the volume name for sharing the
Unix Socket with agent containers.

Architecture::

    agent container (--network none)
        └─ HTTP_PROXY=http+unix:///proxy/proxy.sock
                 ↑ Docker Volume shared (lynxclaw_proxy_vol:/proxy)
    Proxy Sidecar container (lynxclaw-proxy, --network bridge)
        └─ listens /proxy/proxy.sock → domain whitelist + request log + rate limit
        └─ outbound requests → Internet (whitelist only)

Usage::

    sidecar = ProxySidecar()
    sidecar.init(config.proxy)
    await sidecar.start()
    # ... run agents ...
    await sidecar.stop()
"""

from __future__ import annotations

import asyncio
from typing import Optional

import structlog

from src.config import ProxyConfig

log = structlog.get_logger(__name__)

# Docker volume name used to share the Unix Socket between the proxy sidecar
# and all agent containers.
_PROXY_VOLUME_NAME = "lynxclaw_proxy_vol"
# Container name (for idempotent stop/rm).
_PROXY_CONTAINER_NAME = "lynxclaw-proxy"
# Image that contains the proxy_server.py logic.
_PROXY_IMAGE = "lynxclaw-proxy:latest"


class ProxySidecar:
    """Manages the proxy sidecar container lifecycle.

    The actual domain-filtering proxy runs inside the container; this class
    is only responsible for starting and stopping it on the host.
    """

    def __init__(self) -> None:
        self._config: Optional[ProxyConfig] = None
        self._runtime: str = "docker"
        self._running: bool = False

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def init(self, config: ProxyConfig, runtime: str = "docker") -> None:
        """Configure the sidecar.

        Args:
            config: Proxy configuration (enabled flag, allowed_domains list).
            runtime: OCI runtime to use (``docker`` or ``podman``).
        """
        self._config = config
        self._runtime = runtime
        log.info(
            "proxy_sidecar.init",
            enabled=config.enabled,
            allowed_domains=config.allowed_domains,
            runtime=runtime,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the proxy sidecar container.

        Creates (or re-uses) the shared Docker volume, then starts the
        ``lynxclaw-proxy`` container in detached mode.  The container is
        removed on stop.

        The ``ALLOWED_DOMAINS`` env var is passed as a comma-separated string
        so the proxy_server.py inside the container can read the whitelist.

        Raises:
            RuntimeError: If :meth:`init` has not been called.
            RuntimeError: If the container fails to start.
        """
        if self._config is None:
            raise RuntimeError("ProxySidecar.init() must be called before start()")
        if not self._config.enabled:
            log.debug("proxy_sidecar.disabled — skipping start")
            return
        if self._running:
            log.warning("proxy_sidecar.already_running")
            return

        # 1. Ensure the shared volume exists (idempotent — ``docker volume create``
        #    is a no-op if the volume already exists).
        await self._ensure_volume()

        # 2. Remove any stale container with the same name to ensure a clean start.
        await self._remove_stale()

        # 3. Start the proxy container.
        allowed = ",".join(self._config.allowed_domains)
        cmd: list[str] = [
            self._runtime, "run",
            "--detach",
            "--rm",
            "--name", _PROXY_CONTAINER_NAME,
            # Needs bridge networking for outbound internet access
            "--network", "bridge",
            # Non-root user
            "--user", "1000:1000",
            # Shared volume — /proxy/ holds proxy.sock
            "-v", f"{_PROXY_VOLUME_NAME}:/proxy:rw",
            # Pass domain whitelist to the proxy process
            "-e", f"ALLOWED_DOMAINS={allowed}",
            _PROXY_IMAGE,
        ]

        log.info(
            "proxy_sidecar.starting",
            image=_PROXY_IMAGE,
            allowed_domains=self._config.allowed_domains,
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()

        if proc.returncode != 0:
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Failed to start proxy sidecar (exit {proc.returncode}): {stderr_text}"
            )

        container_id = stdout_bytes.decode("utf-8", errors="replace").strip()
        self._running = True
        log.info(
            "proxy_sidecar.started",
            container_id=container_id[:12],
            volume=_PROXY_VOLUME_NAME,
        )

    async def stop(self) -> None:
        """Stop and remove the proxy sidecar container.

        Sends ``docker stop`` followed by ``docker rm -f`` (the latter is a
        safety net in case ``--rm`` did not clean up).  Errors are logged but
        never re-raised so that shutdown always completes cleanly.
        """
        if not self._running:
            return

        log.info("proxy_sidecar.stopping")

        # Stop (graceful SIGTERM with 10-second timeout)
        for subcmd in (
            [self._runtime, "stop", _PROXY_CONTAINER_NAME],
            [self._runtime, "rm", "-f", _PROXY_CONTAINER_NAME],
        ):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *subcmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.communicate()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "proxy_sidecar.stop_error",
                    cmd=subcmd[1],
                    error=str(exc),
                )

        self._running = False
        log.info("proxy_sidecar.stopped")

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def get_volume_name(self) -> str:
        """Return the Docker volume name for proxy socket sharing.

        Agent containers should be launched with::

            -v <volume_name>:/proxy:rw

        Returns:
            The volume name string (``lynxclaw_proxy_vol``).
        """
        return _PROXY_VOLUME_NAME

    @property
    def is_running(self) -> bool:
        """True if the sidecar was successfully started and not yet stopped."""
        return self._running

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_volume(self) -> None:
        """Create the shared proxy volume if it does not already exist."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self._runtime, "volume", "create", _PROXY_VOLUME_NAME,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
            log.debug("proxy_sidecar.volume_ensured", volume=_PROXY_VOLUME_NAME)
        except Exception as exc:  # noqa: BLE001
            log.warning("proxy_sidecar.volume_create_error", error=str(exc))

    async def _remove_stale(self) -> None:
        """Remove any existing container named ``lynxclaw-proxy`` (best-effort)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self._runtime, "rm", "-f", _PROXY_CONTAINER_NAME,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
            log.debug("proxy_sidecar.stale_removed")
        except Exception as exc:  # noqa: BLE001
            log.debug("proxy_sidecar.remove_stale_error", error=str(exc))
