"""Lynxclaw Container Manager — Ephemeral lifecycle.

Launches hardened Docker (or Podman) containers, enforces concurrency limits,
validates mounts against a security blacklist, and cleans up on timeout.

Usage::

    manager = ContainerManager()
    manager.init(config.container, config.security)
    result = await manager.spawn(
        group_name="my-group",
        prompt="Hello",
        env_vars={"ANTHROPIC_API_KEY": "sk-..."},
        mounts={
            "group_dir": "/host/groups/my-group",
            "global_dir": "/host/groups",
            "ipc_dir": "/host/data/ipc/my-group",
        },
    )
"""

from __future__ import annotations

import asyncio
import fnmatch
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import structlog

from src.config import ContainerConfig, SecurityConfig

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ContainerResult:
    """Outcome of a single container run."""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False
    container_id: str = ""


# ---------------------------------------------------------------------------
# ContainerManager
# ---------------------------------------------------------------------------

class MountValidationError(ValueError):
    """Raised when a requested mount path matches a blocked pattern."""


class ContainerManager:
    """Manages ephemeral hardened container launches.

    Thread-safety: all public methods are async and safe to call from any
    asyncio task.  The semaphore serialises concurrent launches.
    """

    def __init__(self) -> None:
        self._config: Optional[ContainerConfig] = None
        self._security: Optional[SecurityConfig] = None
        self._semaphore: Optional[asyncio.Semaphore] = None

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def init(self, config: ContainerConfig, security: SecurityConfig) -> None:
        """Configure the manager.  Must be called before :meth:`spawn`.

        Args:
            config: Container configuration (image, memory, cpus, timeout, etc.)
            security: Security configuration (blocked_patterns, etc.)
        """
        self._config = config
        self._security = security
        self._semaphore = asyncio.Semaphore(config.max_concurrent)
        log.info(
            "container_manager.init",
            runtime=config.runtime,
            image=config.image,
            max_concurrent=config.max_concurrent,
            timeout=config.timeout,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def spawn(
        self,
        group_name: str,
        prompt: str,
        env_vars: dict[str, str],
        mounts: dict[str, str],
        *,
        session_id: Optional[str] = None,
        extra_cmd: Optional[list[str]] = None,
    ) -> ContainerResult:
        """Spawn an ephemeral hardened container.

        Args:
            group_name: Logical group name (used in env vars and log context).
            prompt: The prompt / initial message for the agent (currently passed
                as env var ``LYNXCLAW_PROMPT``; the agent runner reads it).
            env_vars: Additional environment variables to inject.
            mounts: Host paths for named mount points.  Recognised keys:
                ``group_dir``, ``global_dir``, ``project_dir``, ``ipc_dir``.
            session_id: Optional session ID for resumable mode.
            extra_cmd: Optional command override appended after the image name
                (useful in tests, e.g. ``["echo", "hello"]``).

        Returns:
            :class:`ContainerResult` with stdout, stderr, exit_code, timed_out.

        Raises:
            MountValidationError: If any mount path matches a blocked pattern.
            RuntimeError: If :meth:`init` has not been called.
        """
        if self._config is None or self._semaphore is None or self._security is None:
            raise RuntimeError("ContainerManager.init() must be called before spawn()")

        # Validate all mount paths
        self._validate_mounts(mounts)

        sid = session_id or str(uuid.uuid4())
        cmd = self._build_command(
            group_name=group_name,
            env_vars={**env_vars, "LYNXCLAW_PROMPT": prompt},
            mounts=mounts,
            session_id=sid,
            extra_cmd=extra_cmd,
        )

        log.info(
            "container.spawn.waiting",
            group=group_name,
            session_id=sid,
        )

        async with self._semaphore:
            log.info(
                "container.spawn.start",
                group=group_name,
                session_id=sid,
                image=self._config.image,
            )
            return await self._run(cmd, group_name=group_name, session_id=sid)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_command(
        self,
        *,
        group_name: str,
        env_vars: dict[str, str],
        mounts: dict[str, str],
        session_id: str,
        extra_cmd: Optional[list[str]] = None,
    ) -> list[str]:
        """Build the full ``docker run`` command as a list of strings.

        Uses ``config.runtime`` instead of a hardcoded "docker" so that Podman
        (or any OCI-compatible runtime) can be substituted via config.

        Args:
            group_name: Logical group name.
            env_vars: Environment variables to pass with ``-e KEY=VALUE``.
            mounts: Named mount paths (see :meth:`spawn` for keys).
            session_id: Session identifier string.
            extra_cmd: Optional list of arguments appended after the image name.

        Returns:
            A list suitable for ``asyncio.create_subprocess_exec(*cmd)``.
        """
        cfg = self._config
        assert cfg is not None  # satisfied after init()

        cmd: list[str] = [
            cfg.runtime, "run", "--rm",
            # Capabilities
            "--cap-drop", "ALL",
            # Privilege escalation prevention
            "--security-opt", "no-new-privileges:true",
            # Read-only root filesystem
            "--read-only",
            # Writable /tmp via tmpfs (noexec to prevent code injection)
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
            # Process count limit
            "--pids-limit", "256",
            # Non-root user
            "--user", "1000:1000",
            # Network isolation (overridable via config)
            "--network", cfg.network,
            # Resource limits
            "--memory", cfg.memory,
            "--cpus", str(cfg.cpus),
        ]

        # Volume mounts — only attach mounts whose host paths are provided
        _mount_map = {
            "group_dir":   "/workspace/group:rw",
            "global_dir":  "/workspace/global:ro",
            "project_dir": "/workspace/project:ro",
            "ipc_dir":     "/workspace/ipc:rw",
        }
        for key, container_path in _mount_map.items():
            host_path = mounts.get(key)
            if host_path:
                cmd += ["-v", f"{host_path}:{container_path}"]

        # Environment variables
        # Lynxclaw-specific vars added first (can be overridden by caller)
        builtin_env = {
            "LYNXCLAW_GROUP": group_name,
            "LYNXCLAW_SESSION_ID": session_id,
        }
        for k, v in {**builtin_env, **env_vars}.items():
            cmd += ["-e", f"{k}={v}"]

        # Image
        cmd.append(cfg.image)

        # Optional command override (useful for tests)
        if extra_cmd:
            cmd.extend(extra_cmd)

        return cmd

    def _validate_mounts(self, mounts: dict[str, str]) -> None:
        """Raise :class:`MountValidationError` if any path matches a blocked pattern.

        Checks each path component against the security blocked_patterns list,
        using :func:`fnmatch.fnmatch` for glob-style patterns (e.g. ``*.pem``).

        Args:
            mounts: Dict of mount name → host path.

        Raises:
            MountValidationError: On the first blocked pattern match found.
        """
        assert self._security is not None  # satisfied after init()
        blocked = self._security.blocked_patterns

        for mount_key, host_path in mounts.items():
            if not host_path:
                continue
            path = Path(host_path)
            # Check every component of the path against blocked patterns
            for part in path.parts:
                for pattern in blocked:
                    if fnmatch.fnmatch(part, pattern):
                        raise MountValidationError(
                            f"Mount '{mount_key}' path '{host_path}' contains "
                            f"blocked pattern '{pattern}' (matched part: '{part}')"
                        )

    async def _run(
        self,
        cmd: list[str],
        *,
        group_name: str,
        session_id: str,
    ) -> ContainerResult:
        """Execute the container command, enforcing the configured timeout.

        Uses ``asyncio.create_subprocess_exec`` for non-blocking I/O.
        On timeout, sends ``docker kill`` (using ``config.runtime kill``) before
        raising, ensuring no orphaned containers remain.

        Args:
            cmd: Full command list, as produced by :meth:`_build_command`.
            group_name: Used for logging context.
            session_id: Used for logging context.

        Returns:
            :class:`ContainerResult`.
        """
        cfg = self._config
        assert cfg is not None

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        timed_out = False
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=cfg.timeout,
            )
        except asyncio.TimeoutError:
            timed_out = True
            log.warning(
                "container.timeout",
                group=group_name,
                session_id=session_id,
                timeout=cfg.timeout,
            )
            # Attempt graceful kill — fire and forget (ignore errors)
            try:
                kill_proc = await asyncio.create_subprocess_exec(
                    cfg.runtime, "kill", str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await kill_proc.wait()
            except Exception as kill_exc:  # noqa: BLE001
                log.debug("container.kill.failed", error=str(kill_exc))
            # Drain the process so its resources are released
            try:
                stdout_bytes, stderr_bytes = await proc.communicate()
            except Exception:  # noqa: BLE001
                stdout_bytes = b""
                stderr_bytes = b""

        exit_code = proc.returncode if proc.returncode is not None else -1

        result = ContainerResult(
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            exit_code=exit_code,
            timed_out=timed_out,
        )

        log.info(
            "container.done",
            group=group_name,
            session_id=session_id,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            stdout_len=len(result.stdout),
            stderr_len=len(result.stderr),
        )
        return result
