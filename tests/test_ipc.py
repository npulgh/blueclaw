"""Tests for T1.7 IPC Communication Layer.

Covers:
- IPCWatcher (src/ipc.py): direct write → on_created, atomic write → on_moved
- Error handling: malformed JSON is logged and deleted without crashing
- ipc_bridge (container/agent-runner/ipc_bridge.py): generates valid JSON-RPC files
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.ipc import VALID_METHODS, IPCWatcher, _safe_delete


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_rpc(method: str = "send_message", **params) -> dict:
    """Build a minimal valid JSON-RPC 2.0 payload."""
    return {
        "jsonrpc": "2.0",
        "method": method,
        "params": {
            "group": "main",
            "chat_id": "-1001234567890",
            **params,
        },
        "id": str(uuid.uuid4()),
    }


async def _wait_for_dispatch(dispatched: list, count: int = 1, timeout: float = 3.0) -> None:
    """Poll until *count* items appear in *dispatched* or *timeout* expires."""
    deadline = asyncio.get_event_loop().time() + timeout
    while len(dispatched) < count:
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(
                f"Expected {count} dispatch(es), got {len(dispatched)} after {timeout}s"
            )
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def ipc_env(tmp_path):
    """Provide a running IPCWatcher backed by a temp directory."""
    dispatched: list[tuple[str, dict, str]] = []

    async def _dispatch(method: str, params: dict, rpc_id: str) -> None:
        dispatched.append((method, params, rpc_id))

    watcher = IPCWatcher()
    await watcher.init(base_dir=str(tmp_path / "ipc"), dispatch_callback=_dispatch)
    await watcher.start(groups=["main"])

    yield watcher, tmp_path / "ipc", dispatched

    await watcher.stop()


# ---------------------------------------------------------------------------
# IPCWatcher: direct write → on_created
# ---------------------------------------------------------------------------

async def test_direct_write_dispatched(ipc_env):
    """Writing a JSON file directly to outbox triggers on_created and dispatch."""
    watcher, base, dispatched = ipc_env
    outbox = base / "main" / "outbox"

    payload = _make_rpc("send_message", text="hello from direct write")
    file_path = outbox / f"{uuid.uuid4()}.json"
    file_path.write_text(json.dumps(payload), encoding="utf-8")

    await _wait_for_dispatch(dispatched, count=1)

    assert len(dispatched) == 1
    method, params, rpc_id = dispatched[0]
    assert method == "send_message"
    assert params["text"] == "hello from direct write"
    assert params["chat_id"] == "-1001234567890"


async def test_direct_write_file_deleted(ipc_env):
    """After dispatch the IPC file must be deleted from outbox."""
    watcher, base, dispatched = ipc_env
    outbox = base / "main" / "outbox"

    payload = _make_rpc("send_message", text="cleanup test")
    file_path = outbox / f"{uuid.uuid4()}.json"
    file_path.write_text(json.dumps(payload), encoding="utf-8")

    await _wait_for_dispatch(dispatched, count=1)

    assert not file_path.exists(), "IPC file should be deleted after processing"


# ---------------------------------------------------------------------------
# IPCWatcher: atomic write → on_moved
# ---------------------------------------------------------------------------

async def test_atomic_write_dispatched(ipc_env):
    """Atomic write (write-tmp + rename) triggers on_moved and dispatch."""
    watcher, base, dispatched = ipc_env
    outbox = base / "main" / "outbox"

    payload = _make_rpc("stream_chunk", text="streaming...", is_final=False)
    file_id = str(uuid.uuid4())
    tmp_path = outbox / f"{file_id}.tmp"
    final_path = outbox / f"{file_id}.json"

    # Simulate container atomic write
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    tmp_path.rename(final_path)  # triggers on_moved on host

    await _wait_for_dispatch(dispatched, count=1)

    assert len(dispatched) == 1
    method, params, rpc_id = dispatched[0]
    assert method == "stream_chunk"
    assert params["text"] == "streaming..."
    assert params["is_final"] is False


async def test_atomic_write_final_chunk(ipc_env):
    """Atomic write with is_final=True is dispatched correctly."""
    watcher, base, dispatched = ipc_env
    outbox = base / "main" / "outbox"

    payload = _make_rpc("stream_chunk", text=" done.", is_final=True)
    file_id = str(uuid.uuid4())
    tmp_path = outbox / f"{file_id}.tmp"
    final_path = outbox / f"{file_id}.json"

    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    tmp_path.rename(final_path)

    await _wait_for_dispatch(dispatched, count=1)

    _, params, _ = dispatched[0]
    assert params["is_final"] is True


# ---------------------------------------------------------------------------
# IPCWatcher: error handling
# ---------------------------------------------------------------------------

async def test_malformed_json_logged_and_deleted(ipc_env):
    """Malformed JSON file is deleted without crashing the watcher."""
    watcher, base, dispatched = ipc_env
    outbox = base / "main" / "outbox"

    bad_file = outbox / f"{uuid.uuid4()}.json"
    bad_file.write_text("{not valid json{{", encoding="utf-8")

    # Give watcher time to process
    await asyncio.sleep(0.5)

    # No dispatch should occur for malformed files
    assert len(dispatched) == 0
    # File should be deleted
    assert not bad_file.exists(), "Malformed JSON file should be deleted"


async def test_unknown_method_deleted_no_dispatch(ipc_env):
    """Files with unknown IPC methods are deleted and not dispatched."""
    watcher, base, dispatched = ipc_env
    outbox = base / "main" / "outbox"

    payload = _make_rpc("unknown_method", text="should be ignored")
    file_path = outbox / f"{uuid.uuid4()}.json"
    file_path.write_text(json.dumps(payload), encoding="utf-8")

    await asyncio.sleep(0.5)

    assert len(dispatched) == 0
    assert not file_path.exists()


async def test_missing_params_deleted_no_dispatch(ipc_env):
    """Files with non-dict params are deleted and not dispatched."""
    watcher, base, dispatched = ipc_env
    outbox = base / "main" / "outbox"

    payload = {"jsonrpc": "2.0", "method": "send_message", "params": "not a dict", "id": "x"}
    file_path = outbox / f"{uuid.uuid4()}.json"
    file_path.write_text(json.dumps(payload), encoding="utf-8")

    await asyncio.sleep(0.5)

    assert len(dispatched) == 0
    assert not file_path.exists()


# ---------------------------------------------------------------------------
# IPCWatcher: multiple messages
# ---------------------------------------------------------------------------

async def test_multiple_files_all_dispatched(ipc_env):
    """Multiple JSON files written to outbox are all dispatched."""
    watcher, base, dispatched = ipc_env
    outbox = base / "main" / "outbox"

    count = 5
    for i in range(count):
        payload = _make_rpc("send_message", text=f"message {i}")
        (outbox / f"{uuid.uuid4()}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    await _wait_for_dispatch(dispatched, count=count, timeout=5.0)

    assert len(dispatched) == count
    texts = {p["text"] for _, p, _ in dispatched}
    assert texts == {f"message {i}" for i in range(count)}


# ---------------------------------------------------------------------------
# IPCWatcher: IPC directory auto-creation
# ---------------------------------------------------------------------------

async def test_ipc_dirs_created_on_start(tmp_path):
    """IPCWatcher creates outbox/inbox/audit for each group on start."""
    async def _noop(method, params, rpc_id):
        pass

    watcher = IPCWatcher()
    await watcher.init(base_dir=str(tmp_path / "ipc"), dispatch_callback=_noop)
    await watcher.start(groups=["alpha", "beta"])

    try:
        for group in ("alpha", "beta"):
            for subdir in ("outbox", "inbox", "audit"):
                assert (tmp_path / "ipc" / group / subdir).is_dir(), \
                    f"Missing {group}/{subdir}"
    finally:
        await watcher.stop()


# ---------------------------------------------------------------------------
# ipc_bridge: valid JSON-RPC file generation
# ---------------------------------------------------------------------------

@pytest.fixture
def bridge(tmp_path):
    """Load ipc_bridge module with tmp outbox dir."""
    # Add container/agent-runner to sys.path so we can import ipc_bridge
    container_path = str(Path(__file__).parent.parent / "container" / "agent-runner")
    if container_path not in sys.path:
        sys.path.insert(0, container_path)

    import importlib
    import ipc_bridge as _bridge
    # Reload to ensure clean state
    importlib.reload(_bridge)

    yield _bridge, tmp_path

    # Cleanup sys.path
    if container_path in sys.path:
        sys.path.remove(container_path)


def test_bridge_send_message_creates_file(bridge):
    """send_message() creates a valid JSON-RPC file in the outbox."""
    mod, tmp_path = bridge
    ipc_base = str(tmp_path / "ipc")

    rpc_id = mod.send_message(
        group="main",
        chat_id="-1001234567890",
        text="test message",
        ipc_base=ipc_base,
    )

    outbox = tmp_path / "ipc" / "main" / "outbox"
    files = list(outbox.glob("*.json"))
    assert len(files) == 1, "Expected exactly one JSON file"

    payload = json.loads(files[0].read_text())
    assert payload["jsonrpc"] == "2.0"
    assert payload["method"] == "send_message"
    assert payload["id"] == rpc_id
    assert payload["params"]["group"] == "main"
    assert payload["params"]["chat_id"] == "-1001234567890"
    assert payload["params"]["text"] == "test message"


def test_bridge_stream_chunk_creates_file(bridge):
    """stream_chunk() creates a valid JSON-RPC file with is_final flag."""
    mod, tmp_path = bridge
    ipc_base = str(tmp_path / "ipc")

    rpc_id = mod.stream_chunk(
        group="main",
        chat_id="-1001234567890",
        text="partial text",
        is_final=False,
        ipc_base=ipc_base,
    )

    outbox = tmp_path / "ipc" / "main" / "outbox"
    files = list(outbox.glob("*.json"))
    assert len(files) == 1

    payload = json.loads(files[0].read_text())
    assert payload["method"] == "stream_chunk"
    assert payload["id"] == rpc_id
    assert payload["params"]["text"] == "partial text"
    assert payload["params"]["is_final"] is False


def test_bridge_stream_chunk_is_final_true(bridge):
    """stream_chunk() with is_final=True writes the flag correctly."""
    mod, tmp_path = bridge
    ipc_base = str(tmp_path / "ipc")

    mod.stream_chunk(
        group="main",
        chat_id="-1001234567890",
        text="last chunk",
        is_final=True,
        ipc_base=ipc_base,
    )

    outbox = tmp_path / "ipc" / "main" / "outbox"
    files = list(outbox.glob("*.json"))
    payload = json.loads(files[0].read_text())
    assert payload["params"]["is_final"] is True


def test_bridge_atomic_write_no_tmp_files_remain(bridge):
    """After atomic write, no .tmp files should remain in outbox."""
    mod, tmp_path = bridge
    ipc_base = str(tmp_path / "ipc")

    mod.send_message(group="main", chat_id="c1", text="atomic", ipc_base=ipc_base)
    mod.stream_chunk(group="main", chat_id="c1", text="chunk", is_final=True, ipc_base=ipc_base)

    outbox = tmp_path / "ipc" / "main" / "outbox"
    tmp_files = list(outbox.glob("*.tmp"))
    assert tmp_files == [], f"Tmp files still present: {tmp_files}"


def test_bridge_unique_uuids_per_call(bridge):
    """Each ipc_bridge call generates a unique ID."""
    mod, tmp_path = bridge
    ipc_base = str(tmp_path / "ipc")

    ids = set()
    for _ in range(10):
        ids.add(mod.send_message(group="main", chat_id="c1", text="x", ipc_base=ipc_base))

    assert len(ids) == 10, "All 10 IDs must be unique"


def test_bridge_multiple_calls_multiple_files(bridge):
    """Multiple calls produce multiple distinct files."""
    mod, tmp_path = bridge
    ipc_base = str(tmp_path / "ipc")

    for i in range(3):
        mod.send_message(group="main", chat_id="c1", text=f"msg{i}", ipc_base=ipc_base)

    outbox = tmp_path / "ipc" / "main" / "outbox"
    files = list(outbox.glob("*.json"))
    assert len(files) == 3


# ---------------------------------------------------------------------------
# _safe_delete utility
# ---------------------------------------------------------------------------

def test_safe_delete_existing_file(tmp_path):
    """_safe_delete removes an existing file without raising."""
    f = tmp_path / "test.json"
    f.write_text("{}")
    _safe_delete(f)
    assert not f.exists()


def test_safe_delete_nonexistent_file(tmp_path):
    """_safe_delete is a no-op for nonexistent files (missing_ok)."""
    f = tmp_path / "ghost.json"
    _safe_delete(f)  # must not raise
