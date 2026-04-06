# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later

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

# Proxy volume name must match the one declared in src/proxy.py
_PROXY_VOLUME_NAME = "lynxclaw_proxy_vol"

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
        is_main: bool = False,
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
                ``group_dir``, ``global_dir``, ``project_dir``, ``ipc_dir``,
                ``global_memory`` (main groups only — path to global CLAUDE.md,
                mounted :rw so the main agent can update shared memory).
            is_main: Whether this group is the main group.  Non-main groups have
                ``group_dir`` forced to read-only in the generated command.
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
            is_main=is_main,
            # Pass the original session_id (None = new session) so _build_command
            # only sets LYNXCLAW_SESSION_ID when there is a real Claude Code session
            # to resume.  sid is only used for logging/tracking, not for --resume.
            session_id=session_id or "",
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

    async def cleanup_orphans(self) -> int:
        """Kill any containers left running from a previous crash.

        Finds containers with the ``lynxclaw`` label via
        ``docker ps --filter label=lynxclaw -q`` and kills each one.
        This is best-effort: errors are logged but never propagate, so a
        failed cleanup never prevents startup.

        Returns:
            Number of containers killed (0 if none found or on error).
        """
        if self._config is None:
            return 0

        runtime = self._config.runtime
        killed = 0

        try:
            proc = await asyncio.create_subprocess_exec(
                runtime, "ps", "--filter", "label=lynxclaw", "-q",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout_bytes, _ = await proc.communicate()
            container_ids = stdout_bytes.decode("utf-8", errors="replace").split()
            container_ids = [c.strip() for c in container_ids if c.strip()]
        except Exception as exc:
            log.warning("container.cleanup_orphans.list_failed", error=str(exc))
            return 0

        for cid in container_ids:
            try:
                kill_proc = await asyncio.create_subprocess_exec(
                    runtime, "kill", cid,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await kill_proc.wait()
                killed += 1
                log.info("container.cleanup_orphans.killed", container_id=cid)
            except Exception as exc:
                log.warning(
                    "container.cleanup_orphans.kill_failed",
                    container_id=cid,
                    error=str(exc),
                )

        if killed:
            log.info("container.cleanup_orphans.done", killed=killed)

        return killed

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
        is_main: bool = False,
        extra_cmd: Optional[list[str]] = None,
    ) -> list[str]:
        """Build the full ``docker run`` command as a list of strings.

        Uses ``config.runtime`` instead of a hardcoded "docker" so that Podman
        (or any OCI-compatible runtime) can be substituted via config.

        Args:
            group_name: Logical group name.
            env_vars: Environment variables to pass with ``-e KEY=VALUE``.
            mounts: Named mount paths (see :meth:`spawn` for keys).
                Main groups may include ``global_memory`` for rw access to
                the global CLAUDE.md file.
            session_id: Session identifier string.
            is_main: Whether this is the main group.  When ``False``, the
                ``group_dir`` mount is forced to ``:ro`` (read-only) so that
                non-main agents cannot write outside their IPC directory.
            extra_cmd: Optional list of arguments appended after the image name.

        Returns:
            A list suitable for ``asyncio.create_subprocess_exec(*cmd)``.
        """
        cfg = self._config
        assert cfg is not None  # satisfied after init()

        # When network mode is "proxy", the agent container still uses --network
        # none (fully isolated), but we mount the proxy volume so it can reach
        # the sidecar via HTTP_PROXY pointing at the Unix Socket.
        _use_proxy_network = cfg.network == "proxy"
        # When Credential Proxy (ADR-006) is active, the container needs bridge
        # network to reach host.docker.internal:<port>. Otherwise, --network none.
        _has_cred_proxy = bool(env_vars.get("ANTHROPIC_BASE_URL", "").startswith("http://host.docker.internal"))
        if _has_cred_proxy:
            _docker_network = "bridge"
        elif _use_proxy_network:
            _docker_network = "none"
        else:
            _docker_network = cfg.network

        cmd: list[str] = [
            cfg.runtime, "run", "--rm",
            # Label for orphan detection on restart
            "--label", "lynxclaw",
            # Capabilities
            "--cap-drop", "ALL",
            # Privilege escalation prevention
            "--security-opt", "no-new-privileges:true",
            # Read-only root filesystem
            "--read-only",
            # Writable /tmp via tmpfs (nosuid to prevent privilege escalation;
            # noexec omitted — Node.js JIT requires executable mappings in /tmp)
            "--tmpfs", "/tmp:rw,nosuid,size=256m",
            # Writable home dir for Claude Code CLI — it writes ~/.claude.json and
            # ~/.claude/backups/ on startup; without this the CLI silently exits
            # under --read-only before sending any output.
            # uid=1000 matches the 'agent' user inside the container.
            "--tmpfs", "/home/agent:rw,nosuid,size=64m,uid=1000,gid=1000",
            # Process count limit
            "--pids-limit", "256",
            # Non-root user
            "--user", "1000:1000",
            # Network isolation (agent is always --network none; proxy handles egress)
            "--network", _docker_network,
            # Resource limits
            "--memory", cfg.memory,
            "--cpus", str(cfg.cpus),
        ]

        # Volume mounts — only attach mounts whose host paths are provided.
        # For non-main groups, group_dir is forced to :ro to prevent filesystem
        # writes outside the IPC directory.  ipc_dir always stays :rw so the
        # agent can write outbox / inbox files.
        #
        # global_dir is always :ro so non-main agents cannot modify global memory.
        # Main groups get an additional global_memory mount pointing to the global
        # CLAUDE.md file with :rw so they can update shared knowledge.
        _group_dir_mode = "rw" if is_main else "ro"
        _mount_map = {
            "group_dir":      f"/workspace/group:{_group_dir_mode}",
            "global_dir":     "/workspace/global:ro",
            "project_dir":    "/workspace/project:ro",
            "ipc_dir":        "/workspace/ipc:rw",
            "global_memory":  "/workspace/global_memory/CLAUDE.md:rw",
        }
        for key, container_path in _mount_map.items():
            # global_memory is only added for main groups
            if key == "global_memory" and not is_main:
                continue
            host_path = mounts.get(key)
            if host_path:
                cmd += ["-v", f"{host_path}:{container_path}"]

        # Proxy network: attach the shared proxy volume so the agent can reach
        # the proxy sidecar via the Unix Socket.
        if _use_proxy_network:
            cmd += ["-v", f"{_PROXY_VOLUME_NAME}:/proxy:rw"]

        # Environment variables
        # Lynxclaw-specific vars added first (can be overridden by caller)
        builtin_env = {
            "LYNXCLAW_GROUP": group_name,
            "LYNXCLAW_SESSION_ID": session_id,
        }
        # When proxy mode is active, tell the agent to route all HTTP/HTTPS
        # traffic through the Unix Socket proxy sidecar.
        if _use_proxy_network:
            builtin_env["HTTP_PROXY"] = "http+unix:///proxy/proxy.sock"
            builtin_env["HTTPS_PROXY"] = "http+unix:///proxy/proxy.sock"

        # Environment variable prefix whitelist — only vars matching these
        # prefixes are forwarded to containers. Prevents accidental credential
        # leakage (ADR-006).
        _ALLOWED_ENV_PREFIXES = (
            "ANTHROPIC_",
            "LYNXCLAW_",
            "CLAUDE_CODE_DISABLE_",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "IPC_BASE_DIR",
        )

        for k, v in {**builtin_env, **env_vars}.items():
            if not any(k.startswith(p) for p in _ALLOWED_ENV_PREFIXES):
                log.warning("container.env_blocked", key=k, group=group_name)
                continue
            cmd += ["-e", f"{k}={v}"]

        # Image
        cmd.append(cfg.image)

        # Optional command override (useful for tests)
        if extra_cmd:
            cmd.extend(extra_cmd)

        return cmd

    def _validate_mounts(self, mounts: dict[str, str]) -> None:
        """Raise :class:`MountValidationError` if any path is unsafe or blocked.

        Performs three layers of validation for each mount path:

        1. **Path traversal**: Rejects any path containing ``..`` components,
           which could escape the intended directory tree.
        2. **Blocked patterns**: Checks each path component against the
           security ``blocked_patterns`` list using :func:`fnmatch.fnmatch`
           for glob-style patterns (e.g. ``*.pem``).
        3. **Symlink resolution**: Resolves symlinks via :meth:`pathlib.Path.resolve`
           then re-runs blocked-pattern validation on the resolved path's
           components, preventing symlink-based bypasses.

        Args:
            mounts: Dict of mount name → host path.

        Raises:
            MountValidationError: On the first unsafe or blocked path found.
        """
        assert self._security is not None  # satisfied after init()
        blocked = self._security.blocked_patterns

        def _check_components(mount_key: str, host_path: str, path: Path) -> None:
            """Check each component of *path* against blocked patterns."""
            for part in path.parts:
                for pattern in blocked:
                    if fnmatch.fnmatch(part, pattern):
                        raise MountValidationError(
                            f"Mount '{mount_key}' path '{host_path}' contains "
                            f"blocked pattern '{pattern}' (matched part: '{part}')"
                        )

        for mount_key, host_path in mounts.items():
            if not host_path:
                continue

            path = Path(host_path)

            # --- 1. Path traversal detection ---
            # Reject any path that contains ".." regardless of position.
            # This catches both "../../etc/passwd" and "/data/../../../etc/passwd".
            for part in path.parts:
                if part == "..":
                    raise MountValidationError(
                        f"Mount '{mount_key}' path '{host_path}' contains a "
                        "path traversal component ('..') which is not allowed"
                    )

            # --- 2. Blocked-pattern check on the raw (unresolved) path ---
            _check_components(mount_key, host_path, path)

            # --- 3. Symlink resolution + re-validation ---
            # Only resolve if the path actually exists on disk; non-existent
            # paths are skipped (they will fail at container launch instead).
            if path.exists():
                resolved = path.resolve()
                # Re-run blocked pattern check on the resolved path to catch
                # symlinks that point into blocked directories (e.g. a symlink
                # named "workspace" that points to /home/user/.ssh).
                _check_components(mount_key, host_path, resolved)

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
