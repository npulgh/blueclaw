"""Lynxclaw Persistent Container — long-running agent container per group.

Instead of spawning a new container per message (ephemeral mode), a persistent
container runs continuously and receives prompts via the IPC inbox directory.

Flow:
  Host writes prompt → data/ipc/{group}/inbox/{uuid}.json
  Container reads inbox → processes with run_agent() → writes outbox (existing IPC)
  Container writes heartbeat → data/ipc/{group}/heartbeat.json every 30s
  Host checks heartbeat staleness; restarts container if > 90s stale

Usage::

    pc = PersistentContainer()
    await pc.start("my-group", config, security)
    await pc.send_prompt("Hello!", chat_id="-100123")
    healthy = await pc.health_check()
    await pc.stop()
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import structlog

from src.config import Config, ContainerConfig, SecurityConfig

log = structlog.get_logger(__name__)

# Heartbeat constants
_HEARTBEAT_INTERVAL = 30   # seconds between container heartbeat writes
_HEARTBEAT_STALE_THRESHOLD = 90  # seconds before host considers container dead
_HEALTH_POLL_INTERVAL = 15  # seconds between host health checks

# Proxy volume name must match src/proxy.py
_PROXY_VOLUME_NAME = "lynxclaw_proxy_vol"


# ---------------------------------------------------------------------------
# PersistentContainer
# ---------------------------------------------------------------------------

class PersistentContainer:
    """Manages a long-running agent container for a single group.

    The container runs a persistent message loop (LYNXCLAW_MODE=persistent)
    that polls the inbox directory for prompt files, processes each one, and
    writes responses to the outbox (same IPC mechanism as ephemeral mode).

    A background health-monitor task checks the heartbeat file every
    _HEALTH_POLL_INTERVAL seconds and restarts the container if stale.
    """

    def __init__(self) -> None:
        self._group_name: str = ""
        self._config: Optional[Config] = None
        self._container_cfg: Optional[ContainerConfig] = None
        self._security: Optional[SecurityConfig] = None
        self._ipc_base: str = ""
        self._container_id: Optional[str] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._running: bool = False
        self._is_main: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """True if the container process is currently active."""
        return self._running and self._proc is not None and self._proc.returncode is None

    async def start(
        self,
        group_name: str,
        config: Config,
        security: SecurityConfig,
        *,
        is_main: bool = False,
        ipc_base: Optional[str] = None,
    ) -> None:
        """Start the persistent container for *group_name*.

        Args:
            group_name: Logical group name.
            config: Full application config.
            security: Security config (for mount validation).
            is_main: Whether this is the main group.
            ipc_base: Override for IPC base directory (default: data/ipc).
        """
        self._group_name = group_name
        self._config = config
        self._container_cfg = config.container
        self._security = security
        self._is_main = is_main
        self._ipc_base = ipc_base or os.path.join(os.getcwd(), "data", "ipc")

        # Ensure inbox directory exists
        inbox_dir = Path(self._ipc_base) / group_name / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)

        await self._launch()

        # Start background health monitor
        self._monitor_task = asyncio.create_task(
            self._health_monitor(), name=f"persistent-monitor-{group_name}"
        )
        log.info("persistent_container.started", group=group_name)

    async def stop(self) -> None:
        """Stop the persistent container and cancel the health monitor."""
        self._running = False

        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        await self._kill_container()
        log.info("persistent_container.stopped", group=self._group_name)

    async def send_prompt(self, prompt: str, chat_id: str) -> None:
        """Write a prompt file to the group inbox for the container to pick up.

        Args:
            prompt: The user prompt text.
            chat_id: Platform chat identifier (forwarded to the agent).
        """
        rpc_id = str(uuid.uuid4())
        payload = {
            "jsonrpc": "2.0",
            "method": "run_prompt",
            "params": {
                "group": self._group_name,
                "chat_id": chat_id,
                "prompt": prompt,
            },
            "id": rpc_id,
        }
        inbox_dir = Path(self._ipc_base) / self._group_name / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)

        # Atomic write: tmp → rename (same pattern as ipc_bridge outbox)
        tmp_path = inbox_dir / f"{rpc_id}.tmp"
        final_path = inbox_dir / f"{rpc_id}.json"
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp_path.rename(final_path)

        log.info(
            "persistent_container.prompt_written",
            group=self._group_name,
            rpc_id=rpc_id,
            chat_id=chat_id,
        )

    async def health_check(self) -> bool:
        """Return True if the container heartbeat is fresh (< 90s old).

        Also returns False if the container process has exited.
        """
        if not self.is_running:
            return False

        heartbeat_path = Path(self._ipc_base) / self._group_name / "heartbeat.json"
        try:
            raw = await asyncio.to_thread(heartbeat_path.read_text, encoding="utf-8")
            data = json.loads(raw)
            ts = float(data.get("timestamp", 0))
            age = time.time() - ts
            healthy = age < _HEARTBEAT_STALE_THRESHOLD
            if not healthy:
                log.warning(
                    "persistent_container.heartbeat_stale",
                    group=self._group_name,
                    age_seconds=round(age, 1),
                )
            return healthy
        except FileNotFoundError:
            # Container hasn't written its first heartbeat yet — give it grace
            # if it just started (process still running)
            return self.is_running
        except Exception as exc:
            log.warning(
                "persistent_container.heartbeat_read_error",
                group=self._group_name,
                error=str(exc),
            )
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _launch(self) -> None:
        """Build and launch the docker run command in detached mode."""
        cfg = self._container_cfg
        assert cfg is not None

        cmd = self._build_command()
        log.info(
            "persistent_container.launching",
            group=self._group_name,
            image=cfg.image,
        )

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._running = True
        self._container_id = str(self._proc.pid)
        log.info(
            "persistent_container.launched",
            group=self._group_name,
            pid=self._container_id,
        )

    async def _kill_container(self) -> None:
        """Terminate the container process if still running."""
        if self._proc is None:
            return
        if self._proc.returncode is None:
            try:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
            except Exception as exc:
                log.warning(
                    "persistent_container.kill_error",
                    group=self._group_name,
                    error=str(exc),
                )
        self._proc = None

    async def _health_monitor(self) -> None:
        """Background task: poll heartbeat and restart container if stale."""
        while self._running:
            await asyncio.sleep(_HEALTH_POLL_INTERVAL)
            if not self._running:
                break

            healthy = await self.health_check()
            if not healthy:
                log.warning(
                    "persistent_container.restarting",
                    group=self._group_name,
                    reason="heartbeat_stale_or_dead",
                )
                await self._kill_container()
                try:
                    await self._launch()
                except Exception as exc:
                    log.error(
                        "persistent_container.restart_failed",
                        group=self._group_name,
                        error=str(exc),
                    )

    def _build_command(self) -> list[str]:
        """Build the docker run command for persistent mode."""
        cfg = self._container_cfg
        assert cfg is not None
        assert self._config is not None

        cwd = os.getcwd()
        group = self._group_name

        _use_proxy_network = cfg.network == "proxy"
        _docker_network = "none" if _use_proxy_network else cfg.network

        cmd: list[str] = [
            cfg.runtime, "run", "--rm",
            "--label", "lynxclaw",
            "--label", f"lynxclaw-persistent={group}",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
            "--pids-limit", "256",
            "--user", "1000:1000",
            "--network", _docker_network,
            "--memory", cfg.memory,
            "--cpus", str(cfg.cpus),
        ]

        # Volume mounts
        _group_dir_mode = "rw" if self._is_main else "ro"
        group_dir = os.path.join(cwd, "groups", group)
        global_dir = os.path.join(cwd, "groups")
        ipc_dir = os.path.join(self._ipc_base, group)

        cmd += ["-v", f"{group_dir}:/workspace/group:{_group_dir_mode}"]
        cmd += ["-v", f"{global_dir}:/workspace/global:ro"]
        cmd += ["-v", f"{ipc_dir}:/workspace/ipc:rw"]

        if self._is_main:
            cmd += ["-v", f"{cwd}:/workspace/project:ro"]

        if _use_proxy_network:
            cmd += ["-v", f"{_PROXY_VOLUME_NAME}:/proxy:rw"]

        # Environment variables
        env_vars: dict[str, str] = {
            "LYNXCLAW_GROUP": group,
            "LYNXCLAW_MODE": "persistent",
            "ANTHROPIC_API_KEY": self._config.anthropic_api_key,
            "IPC_BASE_DIR": "/workspace/ipc",
        }
        if _use_proxy_network:
            env_vars["HTTP_PROXY"] = "http+unix:///proxy/proxy.sock"
            env_vars["HTTPS_PROXY"] = "http+unix:///proxy/proxy.sock"

        for k, v in env_vars.items():
            cmd += ["-e", f"{k}={v}"]

        cmd.append(cfg.image)
        return cmd
