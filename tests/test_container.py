"""Tests for src/container_manager.py — T1.8 Container Manager (Ephemeral).

All tests mock subprocess calls — no real Docker required.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import ContainerConfig, SecurityConfig
from src.container_manager import ContainerManager, ContainerResult, MountValidationError


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
