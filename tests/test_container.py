"""Tests for src/container_manager.py — T1.8 Container Manager (Ephemeral).

All tests mock subprocess calls — no real Docker required.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import ContainerConfig, SecurityConfig
from src.container_manager import ContainerManager, ContainerResult, MountValidationError


# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------

def _can_create_symlinks() -> bool:
    """Return True if the current process can create symlinks.

    On Windows, symlink creation requires either Developer Mode or the
    SeCreateSymbolicLinkPrivilege.  This probe creates and immediately
    removes a test symlink to detect the capability at runtime.
    """
    if os.name != "nt":
        return True
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "target"
            target.mkdir()
            link = Path(tmpdir) / "probe_link"
            link.symlink_to(target)
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_config(**kwargs) -> ContainerConfig:
    defaults = dict(
        image="lynxclaw-agent:latest",
        memory="512m",
        cpus=1.0,
        network="none",
        timeout=30,
        max_concurrent=3,
        lifecycle="ephemeral",
        runtime="docker",
    )
    defaults.update(kwargs)
    return ContainerConfig(**defaults)


def _make_security(**kwargs) -> SecurityConfig:
    defaults = dict(
        blocked_commands=["rm -rf /", "sudo"],
        blocked_patterns=[".ssh", ".aws", ".gnupg", ".env", "*.pem", "*.key", "credentials*"],
    )
    defaults.update(kwargs)
    return SecurityConfig(**defaults)


def _make_manager(**cfg_kwargs) -> ContainerManager:
    m = ContainerManager()
    m.init(_make_config(**cfg_kwargs), _make_security())
    return m


def _make_proc(stdout: bytes = b"hello\n", stderr: bytes = b"", returncode: int = 0):
    """Return a mock asyncio.Process that resolves communicate() immediately."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.pid = 12345
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ---------------------------------------------------------------------------
# _build_command — hardening flags
# ---------------------------------------------------------------------------

class TestBuildCommand:
    def test_starts_with_runtime(self):
        m = _make_manager(runtime="docker")
        cmd = m._build_command(
            group_name="grp",
            env_vars={},
            mounts={},
            session_id="sid-1",
        )
        assert cmd[0] == "docker"
        assert cmd[1] == "run"
        assert cmd[2] == "--rm"

    def test_podman_runtime(self):
        m = _make_manager(runtime="podman")
        cmd = m._build_command(
            group_name="grp",
            env_vars={},
            mounts={},
            session_id="sid-1",
        )
        assert cmd[0] == "podman"

    def test_cap_drop_all(self):
        m = _make_manager()
        cmd = m._build_command(group_name="g", env_vars={}, mounts={}, session_id="s")
        assert "--cap-drop" in cmd
        idx = cmd.index("--cap-drop")
        assert cmd[idx + 1] == "ALL"

    def test_no_new_privileges(self):
        m = _make_manager()
        cmd = m._build_command(group_name="g", env_vars={}, mounts={}, session_id="s")
        assert "--security-opt" in cmd
        idx = cmd.index("--security-opt")
        assert cmd[idx + 1] == "no-new-privileges:true"

    def test_read_only(self):
        m = _make_manager()
        cmd = m._build_command(group_name="g", env_vars={}, mounts={}, session_id="s")
        assert "--read-only" in cmd

    def test_tmpfs(self):
        m = _make_manager()
        cmd = m._build_command(group_name="g", env_vars={}, mounts={}, session_id="s")
        assert "--tmpfs" in cmd
        idx = cmd.index("--tmpfs")
        assert "noexec" in cmd[idx + 1]
        assert "nosuid" in cmd[idx + 1]

    def test_pids_limit(self):
        m = _make_manager()
        cmd = m._build_command(group_name="g", env_vars={}, mounts={}, session_id="s")
        assert "--pids-limit" in cmd
        idx = cmd.index("--pids-limit")
        assert cmd[idx + 1] == "256"

    def test_user_1000(self):
        m = _make_manager()
        cmd = m._build_command(group_name="g", env_vars={}, mounts={}, session_id="s")
        assert "--user" in cmd
        idx = cmd.index("--user")
        assert cmd[idx + 1] == "1000:1000"

    def test_network_none(self):
        m = _make_manager(network="none")
        cmd = m._build_command(group_name="g", env_vars={}, mounts={}, session_id="s")
        assert "--network" in cmd
        idx = cmd.index("--network")
        assert cmd[idx + 1] == "none"

    def test_memory_and_cpus(self):
        m = _make_manager(memory="512m", cpus=1.0)
        cmd = m._build_command(group_name="g", env_vars={}, mounts={}, session_id="s")
        assert "--memory" in cmd
        assert cmd[cmd.index("--memory") + 1] == "512m"
        assert "--cpus" in cmd
        assert cmd[cmd.index("--cpus") + 1] == "1.0"

    def test_image_at_end_before_extra_cmd(self):
        m = _make_manager(image="my-image:v2")
        cmd = m._build_command(
            group_name="g",
            env_vars={},
            mounts={},
            session_id="s",
            extra_cmd=["echo", "hello"],
        )
        img_idx = cmd.index("my-image:v2")
        assert cmd[img_idx + 1] == "echo"
        assert cmd[img_idx + 2] == "hello"

    def test_volume_mounts_included(self):
        m = _make_manager()
        cmd = m._build_command(
            group_name="g",
            env_vars={},
            mounts={
                "group_dir": "/host/groups/g",
                "global_dir": "/host/groups",
                "ipc_dir": "/host/data/ipc/g",
            },
            session_id="s",
            is_main=True,
        )
        # All three -v flags should appear
        v_indices = [i for i, x in enumerate(cmd) if x == "-v"]
        assert len(v_indices) == 3
        mount_strs = [cmd[i + 1] for i in v_indices]
        assert any("/host/groups/g:/workspace/group:rw" in s for s in mount_strs)
        assert any("/host/groups:/workspace/global:ro" in s for s in mount_strs)
        assert any("/host/data/ipc/g:/workspace/ipc:rw" in s for s in mount_strs)

    def test_env_vars_injected(self):
        m = _make_manager()
        cmd = m._build_command(
            group_name="mygroup",
            env_vars={"ANTHROPIC_API_KEY": "sk-test"},
            mounts={},
            session_id="sess-42",
        )
        e_indices = [i for i, x in enumerate(cmd) if x == "-e"]
        env_pairs = [cmd[i + 1] for i in e_indices]
        assert any("LYNXCLAW_GROUP=mygroup" in p for p in env_pairs)
        assert any("LYNXCLAW_SESSION_ID=sess-42" in p for p in env_pairs)
        assert any("ANTHROPIC_API_KEY=sk-test" in p for p in env_pairs)

    def test_project_dir_omitted_when_not_provided(self):
        m = _make_manager()
        cmd = m._build_command(group_name="g", env_vars={}, mounts={}, session_id="s")
        # No -v flags when no mounts provided
        assert "-v" not in cmd


# ---------------------------------------------------------------------------
# _validate_mounts — blocked patterns
# ---------------------------------------------------------------------------

class TestValidateMounts:
    def test_clean_paths_pass(self):
        m = _make_manager()
        # Should not raise
        m._validate_mounts({
            "group_dir": "/data/groups/mygroup",
            "ipc_dir": "/data/ipc/mygroup",
        })

    def test_ssh_dir_blocked(self):
        m = _make_manager()
        with pytest.raises(MountValidationError, match=".ssh"):
            m._validate_mounts({"group_dir": "/home/user/.ssh"})

    def test_aws_dir_blocked(self):
        m = _make_manager()
        with pytest.raises(MountValidationError, match=".aws"):
            m._validate_mounts({"group_dir": "/home/user/.aws"})

    def test_pem_file_blocked(self):
        m = _make_manager()
        with pytest.raises(MountValidationError, match=r"\*.pem"):
            m._validate_mounts({"group_dir": "/certs/server.pem"})

    def test_key_file_blocked(self):
        m = _make_manager()
        with pytest.raises(MountValidationError, match=r"\*.key"):
            m._validate_mounts({"group_dir": "/certs/private.key"})

    def test_credentials_file_blocked(self):
        m = _make_manager()
        with pytest.raises(MountValidationError, match="credentials"):
            m._validate_mounts({"group_dir": "/home/user/credentials_aws"})

    def test_env_file_blocked(self):
        m = _make_manager()
        with pytest.raises(MountValidationError, match=".env"):
            m._validate_mounts({"group_dir": "/app/.env"})

    def test_empty_path_skipped(self):
        m = _make_manager()
        # Empty string should not raise
        m._validate_mounts({"group_dir": ""})


# ---------------------------------------------------------------------------
# T2.4 — Path traversal, symlink resolution, read-only enforcement
# ---------------------------------------------------------------------------

class TestMountSecurityValidation:
    """T2.4 acceptance tests: path traversal, symlinks, and read-only mounts."""

    # --- Path traversal ---

    def test_path_traversal_relative_rejected(self):
        """'../../../etc/passwd' must be rejected."""
        m = _make_manager()
        with pytest.raises(MountValidationError, match="path traversal"):
            m._validate_mounts({"group_dir": "../../../etc/passwd"})

    def test_path_traversal_absolute_rejected(self):
        """/data/../../etc/passwd must be rejected."""
        m = _make_manager()
        with pytest.raises(MountValidationError, match="path traversal"):
            m._validate_mounts({"group_dir": "/data/../../etc/passwd"})

    def test_path_traversal_double_dot_in_middle(self):
        """/data/groups/../../../etc must be rejected."""
        m = _make_manager()
        with pytest.raises(MountValidationError, match="path traversal"):
            m._validate_mounts({"ipc_dir": "/data/groups/../../../etc"})

    def test_clean_absolute_path_passes(self):
        """A normal absolute path with no '..' must pass validation."""
        m = _make_manager()
        # Should not raise
        m._validate_mounts({
            "group_dir": "/data/groups/mygroup",
            "ipc_dir": "/data/ipc/mygroup",
        })

    # --- Blocked pattern detection via raw path ---

    def test_ssh_directory_rejected(self):
        """/home/user/.ssh must be rejected (blocked pattern '.ssh')."""
        m = _make_manager()
        with pytest.raises(MountValidationError, match=".ssh"):
            m._validate_mounts({"group_dir": "/home/user/.ssh"})

    # --- Symlink resolution ---

    @pytest.mark.skipif(
        os.name == "nt" and not _can_create_symlinks(),
        reason="Creating symlinks requires elevated privileges on Windows",
    )
    def test_symlink_to_blocked_dir_rejected(self):
        """A symlink whose resolved target contains a blocked component is rejected."""
        m = _make_manager()
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a real .ssh directory inside tmpdir
            ssh_dir = Path(tmpdir) / ".ssh"
            ssh_dir.mkdir()
            # Create a symlink named "safe_name" that points to .ssh
            link = Path(tmpdir) / "safe_name"
            link.symlink_to(ssh_dir)

            # "safe_name" looks innocent but resolves to .ssh → must be rejected
            with pytest.raises(MountValidationError, match=".ssh"):
                m._validate_mounts({"group_dir": str(link)})

    @pytest.mark.skipif(
        os.name == "nt" and not _can_create_symlinks(),
        reason="Creating symlinks requires elevated privileges on Windows",
    )
    def test_symlink_to_safe_dir_passes(self):
        """A symlink to a normal (non-blocked) directory must pass."""
        m = _make_manager()
        with tempfile.TemporaryDirectory() as tmpdir:
            safe_target = Path(tmpdir) / "workspace"
            safe_target.mkdir()
            link = Path(tmpdir) / "group_link"
            link.symlink_to(safe_target)

            # Should not raise
            m._validate_mounts({"group_dir": str(link)})

    def test_nonexistent_path_skips_symlink_check(self):
        """Non-existent paths skip symlink resolution (no OSError raised)."""
        m = _make_manager()
        # A non-existent path with no blocked components should pass raw check
        m._validate_mounts({"group_dir": "/nonexistent/path/workspace"})

    # --- Read-only enforcement for non-main groups ---

    def test_non_main_group_dir_is_readonly(self):
        """Non-main group: group_dir mount must use :ro."""
        m = _make_manager()
        cmd = m._build_command(
            group_name="secondary",
            env_vars={},
            mounts={"group_dir": "/host/groups/secondary", "ipc_dir": "/host/ipc/secondary"},
            session_id="s",
            is_main=False,
        )
        mount_strs = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-v"]
        assert any("/host/groups/secondary:/workspace/group:ro" in s for s in mount_strs), (
            f"Expected group_dir to be :ro for non-main group; got: {mount_strs}"
        )

    def test_non_main_ipc_dir_stays_readwrite(self):
        """Non-main group: ipc_dir must remain :rw so IPC still works."""
        m = _make_manager()
        cmd = m._build_command(
            group_name="secondary",
            env_vars={},
            mounts={"group_dir": "/host/groups/secondary", "ipc_dir": "/host/ipc/secondary"},
            session_id="s",
            is_main=False,
        )
        mount_strs = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-v"]
        assert any("/host/ipc/secondary:/workspace/ipc:rw" in s for s in mount_strs), (
            f"Expected ipc_dir to be :rw; got: {mount_strs}"
        )

    def test_main_group_dir_is_readwrite(self):
        """Main group: group_dir mount must use :rw."""
        m = _make_manager()
        cmd = m._build_command(
            group_name="main",
            env_vars={},
            mounts={"group_dir": "/host/groups/main"},
            session_id="s",
            is_main=True,
        )
        mount_strs = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-v"]
        assert any("/host/groups/main:/workspace/group:rw" in s for s in mount_strs), (
            f"Expected group_dir to be :rw for main group; got: {mount_strs}"
        )

    def test_default_is_main_false(self):
        """Omitting is_main from _build_command defaults to non-main (:ro)."""
        m = _make_manager()
        cmd = m._build_command(
            group_name="g",
            env_vars={},
            mounts={"group_dir": "/host/groups/g"},
            session_id="s",
            # is_main not passed — should default to False
        )
        mount_strs = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-v"]
        assert any(":/workspace/group:ro" in s for s in mount_strs), (
            f"Expected default is_main=False to produce :ro; got: {mount_strs}"
        )


# ---------------------------------------------------------------------------
# spawn — subprocess integration (mocked)
# ---------------------------------------------------------------------------

class TestSpawn:
    @pytest.mark.asyncio
    async def test_spawn_returns_stdout(self):
        m = _make_manager()
        proc = _make_proc(stdout=b"hello\n", returncode=0)

        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            result = await m.spawn(
                group_name="grp",
                prompt="say hello",
                env_vars={"ANTHROPIC_API_KEY": "sk-test"},
                mounts={},
                extra_cmd=["echo", "hello"],
            )

        assert result.stdout == "hello\n"
        assert result.exit_code == 0
        assert result.timed_out is False
        mock_exec.assert_called_once()

    @pytest.mark.asyncio
    async def test_spawn_passes_hardened_flags(self):
        m = _make_manager()
        proc = _make_proc()
        captured_cmd = []

        async def fake_exec(*args, **kwargs):
            captured_cmd.extend(args)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await m.spawn(
                group_name="grp",
                prompt="test",
                env_vars={},
                mounts={},
            )

        assert "--cap-drop" in captured_cmd
        assert "--read-only" in captured_cmd
        assert "--security-opt" in captured_cmd

    @pytest.mark.asyncio
    async def test_spawn_raises_on_blocked_mount(self):
        m = _make_manager()
        with pytest.raises(MountValidationError):
            await m.spawn(
                group_name="grp",
                prompt="test",
                env_vars={},
                mounts={"group_dir": "/home/user/.ssh"},
            )

    @pytest.mark.asyncio
    async def test_spawn_raises_if_not_initialised(self):
        m = ContainerManager()  # no init()
        with pytest.raises(RuntimeError, match="init\\(\\)"):
            await m.spawn(group_name="g", prompt="p", env_vars={}, mounts={})

    @pytest.mark.asyncio
    async def test_spawn_nonzero_exit_code(self):
        m = _make_manager()
        proc = _make_proc(stdout=b"", stderr=b"error\n", returncode=1)

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await m.spawn(
                group_name="grp",
                prompt="fail",
                env_vars={},
                mounts={},
            )

        assert result.exit_code == 1
        assert result.stderr == "error\n"


# ---------------------------------------------------------------------------
# Timeout handling
# ---------------------------------------------------------------------------

class TestTimeout:
    @pytest.mark.asyncio
    async def test_timeout_sets_timed_out_flag(self):
        m = _make_manager(timeout=1)

        proc = MagicMock()
        proc.returncode = None
        proc.pid = 99999
        # communicate() hangs until cancelled
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)

        kill_proc = MagicMock()
        kill_proc.wait = AsyncMock(return_value=None)

        async def fake_exec(*args, **kwargs):
            # First call: the container; second call: docker kill
            if args[1] == "kill":
                return kill_proc
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                result = await m.spawn(
                    group_name="grp",
                    prompt="slow",
                    env_vars={},
                    mounts={},
                )

        assert result.timed_out is True

    @pytest.mark.asyncio
    async def test_timeout_issues_kill_command(self):
        m = _make_manager(timeout=1)

        proc = MagicMock()
        proc.returncode = -1
        proc.pid = 99999
        proc.communicate = AsyncMock(return_value=(b"", b""))

        kill_proc = MagicMock()
        kill_proc.wait = AsyncMock(return_value=None)

        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                return proc
            return kill_proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                await m.spawn(
                    group_name="grp",
                    prompt="slow",
                    env_vars={},
                    mounts={},
                )

        # Second call should be the kill command
        assert len(calls) >= 2
        kill_args = calls[1]
        assert kill_args[1] == "kill"


# ---------------------------------------------------------------------------
# Semaphore / concurrency control
# ---------------------------------------------------------------------------

class TestConcurrency:
    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrent_containers(self):
        """Spawn max_concurrent+1 tasks; the extra one must wait."""
        max_c = 2
        m = _make_manager(max_concurrent=max_c)

        # Barrier to hold containers "running"
        barrier = asyncio.Event()
        started = []

        async def slow_communicate():
            started.append(1)
            await barrier.wait()
            return b"", b""

        proc = MagicMock()
        proc.returncode = 0
        proc.pid = 1
        proc.communicate = slow_communicate

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            tasks = [
                asyncio.create_task(
                    m.spawn(group_name="g", prompt="p", env_vars={}, mounts={})
                )
                for _ in range(max_c + 1)
            ]

            # Give tasks a moment to start
            await asyncio.sleep(0.05)

            # Only max_c containers should have started (semaphore blocks the rest)
            assert len(started) == max_c

            # Release the barrier — all containers finish
            barrier.set()
            await asyncio.gather(*tasks)

        # After all done, all max_c+1 should have run
        assert len(started) == max_c + 1

    @pytest.mark.asyncio
    async def test_semaphore_released_on_exception(self):
        """Semaphore must be released even when subprocess raises."""
        m = _make_manager(max_concurrent=1)

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=OSError("docker not found"),
        ):
            with pytest.raises(OSError):
                await m.spawn(group_name="g", prompt="p", env_vars={}, mounts={})

        # Semaphore should be back to 1 (not stuck at 0)
        assert m._semaphore is not None
        assert m._semaphore._value == 1


# ---------------------------------------------------------------------------
# cleanup_orphans — T2.6 crash recovery
# ---------------------------------------------------------------------------

class TestCleanupOrphans:
    @pytest.mark.asyncio
    async def test_cleanup_kills_found_containers(self):
        """When docker ps returns container IDs, each is killed."""
        m = _make_manager()

        ps_proc = MagicMock()
        ps_proc.communicate = AsyncMock(return_value=(b"abc123\ndef456\n", b""))

        kill_proc = MagicMock()
        kill_proc.wait = AsyncMock(return_value=None)

        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            if args[1] == "ps":
                return ps_proc
            return kill_proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            killed = await m.cleanup_orphans()

        assert killed == 2
        # First call: docker ps --filter label=lynxclaw -q
        assert calls[0][1] == "ps"
        assert "--filter" in calls[0]
        assert "label=lynxclaw" in calls[0]
        # Remaining calls: docker kill <id>
        kill_ids = [c[2] for c in calls[1:]]
        assert "abc123" in kill_ids
        assert "def456" in kill_ids

    @pytest.mark.asyncio
    async def test_cleanup_returns_zero_when_no_containers(self):
        """When docker ps returns nothing, cleanup_orphans returns 0."""
        m = _make_manager()

        ps_proc = MagicMock()
        ps_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=ps_proc):
            killed = await m.cleanup_orphans()

        assert killed == 0

    @pytest.mark.asyncio
    async def test_cleanup_is_best_effort_on_list_failure(self):
        """If docker ps fails, cleanup_orphans returns 0 without raising."""
        m = _make_manager()

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=OSError("docker not found"),
        ):
            killed = await m.cleanup_orphans()

        assert killed == 0

    @pytest.mark.asyncio
    async def test_cleanup_continues_after_individual_kill_failure(self):
        """If killing one container fails, the rest are still attempted."""
        m = _make_manager()

        ps_proc = MagicMock()
        ps_proc.communicate = AsyncMock(return_value=(b"aaa\nbbb\n", b""))

        good_kill = MagicMock()
        good_kill.wait = AsyncMock(return_value=None)

        call_count = 0

        async def fake_exec(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if args[1] == "ps":
                return ps_proc
            if call_count == 2:
                raise OSError("kill failed")
            return good_kill

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            killed = await m.cleanup_orphans()

        # One kill succeeded, one failed — should still report 1 killed
        assert killed == 1

    @pytest.mark.asyncio
    async def test_cleanup_before_init_returns_zero(self):
        """cleanup_orphans is a no-op (returns 0) when init() has not been called."""
        m = ContainerManager()  # no init()
        killed = await m.cleanup_orphans()
        assert killed == 0

    def test_build_command_includes_lynxclaw_label(self):
        """_build_command must include --label lynxclaw for orphan detection."""
        m = _make_manager()
        cmd = m._build_command(group_name="g", env_vars={}, mounts={}, session_id="s")
        assert "--label" in cmd
        idx = cmd.index("--label")
        assert cmd[idx + 1] == "lynxclaw"
