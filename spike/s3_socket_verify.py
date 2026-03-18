#!/usr/bin/env python3
"""S3 Spike: Unix Socket 跨容器验证

验证 --network none 容器通过 Docker Volume 挂载的 Unix Socket 能与
其他容器或宿主进程通信。

运行方式: python spike/s3_socket_verify.py

测试矩阵:
  S3.1  Container-to-Container — 容器间 Unix Socket（同 WSL2 内核）
  S3.2  Host-to-Container      — Windows 宿主进程 → --network none 容器
  S3.3  RTT 延迟测量            — 20 次 round-trip，统计 P50/P99/Max
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

SPIKE_DIR    = Path(__file__).parent
SOCKET_DIR   = SPIKE_DIR / "test_socket"
RESULTS_FILE = SPIKE_DIR / "s3_results.json"

DOCKER_IMAGE  = "python:3.12-slim"
SOCKET_MOUNT  = "/socket"


# ─── helpers ─────────────────────────────────────────────────────────────────

def socket_volume_arg() -> str:
    """Return -v argument for Docker (handles Windows path)."""
    p = str(SOCKET_DIR.resolve()).replace("\\", "/")
    return f"{p}:{SOCKET_MOUNT}"


def setup_socket_dir():
    SOCKET_DIR.mkdir(parents=True, exist_ok=True)
    for f in SOCKET_DIR.iterdir():
        try:
            f.unlink()
        except PermissionError:
            pass


def run_container(script: str, name: str = "", extra_flags: list = None,
                  timeout: int = 60) -> subprocess.CompletedProcess:
    cmd = ["docker", "run", "--rm", "-v", socket_volume_arg()]
    cmd += (extra_flags or [])
    if name:
        cmd += ["--name", name]
    cmd += [DOCKER_IMAGE, "python", "-c", script]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def run_container_detached(script: str, name: str = "",
                            extra_flags: list = None) -> subprocess.Popen:
    cmd = ["docker", "run", "--rm", "-v", socket_volume_arg()]
    cmd += (extra_flags or [])
    if name:
        cmd += ["--name", name]
    cmd += [DOCKER_IMAGE, "python", "-c", script]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


# ─── S3.1  Container-to-Container ────────────────────────────────────────────

_SERVER_SCRIPT = r"""
import socket, os, time, json

SOCK = '/socket/test.sock'
READY = '/socket/.ready'

if os.path.exists(SOCK):
    os.unlink(SOCK)

srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(SOCK)
os.chmod(SOCK, 0o777)
srv.listen(1)

with open(READY, 'w') as f:
    f.write('ready')

srv.settimeout(30.0)
try:
    conn, _ = srv.accept()
    data = conn.recv(4096)
    conn.sendall(b'echo:' + data)
    conn.close()
    print('server:ok')
except socket.timeout:
    print('server:timeout')
finally:
    srv.close()
"""

_CLIENT_SCRIPT = r"""
import socket, os, time, json

SOCK = '/socket/test.sock'
READY = '/socket/.ready'

deadline = time.time() + 20.0
while time.time() < deadline:
    if os.path.exists(READY):
        break
    time.sleep(0.1)
else:
    print(json.dumps({'success': False, 'error': 'server_ready_timeout'}))
    raise SystemExit(1)

time.sleep(0.1)   # let server reach accept()

try:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    t0 = time.time()
    client.connect(SOCK)
    msg = b'hello_from_network_none'
    client.sendall(msg)
    resp = client.recv(4096)
    rtt_ms = round((time.time() - t0) * 1000, 2)
    client.close()
    ok = (resp == b'echo:' + msg)
    print(json.dumps({'success': ok, 'response': resp.decode(), 'rtt_ms': rtt_ms}))
except Exception as e:
    print(json.dumps({'success': False, 'error': str(e)}))
"""


def test_container_to_container() -> dict:
    print("\n" + "=" * 60)
    print("S3.1  Container-to-Container Unix Socket")
    print("      服务端容器 → shared volume socket → --network none 客户端")
    print("=" * 60)

    setup_socket_dir()

    ts = int(time.time())
    srv_proc = run_container_detached(
        _SERVER_SCRIPT,
        name=f"s3-server-{ts}",
    )
    print("  服务端容器已启动（detached）…")

    # Wait for .ready sentinel
    ready_file = SOCKET_DIR / ".ready"
    deadline = time.time() + 20.0
    while time.time() < deadline:
        if ready_file.exists():
            break
        time.sleep(0.1)

    if not ready_file.exists():
        srv_proc.kill()
        print("  状态: FAIL ✗ — 服务端 20s 内未就绪")
        return {"status": "FAIL", "note": "服务端未在 20s 内写入 .ready 文件"}

    print("  服务端已就绪，启动 --network none 客户端…")

    cli_result = run_container(
        _CLIENT_SCRIPT,
        name=f"s3-client-{ts}",
        extra_flags=["--network", "none"],
        timeout=30,
    )

    srv_out, srv_err = srv_proc.communicate(timeout=10)

    print(f"  客户端输出: {cli_result.stdout.strip()}")
    print(f"  服务端输出: {srv_out.strip()}")
    if cli_result.returncode != 0:
        print(f"  客户端 stderr: {cli_result.stderr.strip()[:200]}")

    try:
        cli_data = json.loads(cli_result.stdout.strip())
        success = cli_data.get("success", False)
        rtt_ms  = cli_data.get("rtt_ms")
        error   = cli_data.get("error", "")
    except (json.JSONDecodeError, ValueError):
        success = False
        rtt_ms  = None
        error   = f"parse error: {cli_result.stdout.strip()[:100]}"

    print(f"  成功: {success}  RTT: {rtt_ms} ms  错误: {error or '—'}")
    print(f"  状态: {'PASS ✓' if success else 'FAIL ✗'}")

    return {
        "status": "PASS" if success else "FAIL",
        "rtt_ms": rtt_ms,
        "server_output": srv_out.strip(),
        "error": error,
        "note": (f"容器间 Unix Socket 通信成功 RTT={rtt_ms}ms"
                 if success else f"失败: {error}"),
    }


# ─── S3.2  Host-to-Container ─────────────────────────────────────────────────

_HOST_CLIENT_SCRIPT = r"""
import socket, os, time, json

SOCK = '/socket/host.sock'

deadline = time.time() + 20.0
while time.time() < deadline:
    if os.path.exists(SOCK):
        break
    time.sleep(0.1)
else:
    print(json.dumps({'success': False, 'error': 'socket_file_not_visible'}))
    raise SystemExit(1)

try:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5.0)
    t0 = time.time()
    client.connect(SOCK)
    msg = b'hello_from_container_to_host'
    client.sendall(msg)
    resp = client.recv(4096)
    rtt_ms = round((time.time() - t0) * 1000, 2)
    client.close()
    ok = (resp == b'echo:' + msg)
    print(json.dumps({'success': ok, 'response': resp.decode(), 'rtt_ms': rtt_ms}))
except Exception as e:
    print(json.dumps({'success': False, 'error': str(e)}))
"""


def test_host_to_container() -> dict:
    print("\n" + "=" * 60)
    print("S3.2  Host-to-Container Unix Socket")
    print("      Windows 宿主 Python 进程 → volume mount → --network none 容器")
    print("      (预期: Windows Docker Desktop WSL2 可能 FAIL)")
    print("=" * 60)

    setup_socket_dir()

    host_sock_path = SOCKET_DIR / "host.sock"

    # Create Unix socket server on host (Windows Python)
    if not hasattr(socket, "AF_UNIX"):
        print("  宿主 Python 无 AF_UNIX 属性（Windows 平台限制）")
        return {
            "status": "SKIP",
            "note": "宿主 Python 不支持 AF_UNIX（无 socket.AF_UNIX 属性）— Windows 平台已知限制",
        }

    try:
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as e:
        print(f"  宿主 AF_UNIX socket 创建失败: {e}")
        return {
            "status": "SKIP",
            "note": f"宿主不支持 AF_UNIX socket: {e}",
        }

    try:
        if host_sock_path.exists():
            host_sock_path.unlink()
        srv.bind(str(host_sock_path))
        srv.listen(1)
        srv.settimeout(25.0)
        print(f"  宿主 socket 已绑定: {host_sock_path}")
    except OSError as e:
        srv.close()
        print(f"  宿主 socket bind 失败: {e}")
        return {
            "status": "SKIP",
            "note": f"宿主 socket bind 失败: {e}",
        }

    # Start container in background
    ts = int(time.time())
    cli_proc = run_container_detached(
        _HOST_CLIENT_SCRIPT,
        name=f"s3-hostcli-{ts}",
        extra_flags=["--network", "none"],
    )
    print("  --network none 客户端容器已启动…")

    # Accept connection from container
    connection_received = False
    rtt_ms = None
    error = ""
    success = False

    try:
        conn, _ = srv.accept()
        connection_received = True
        data = conn.recv(4096)
        t0 = time.time()
        conn.sendall(b"echo:" + data)
        conn.close()
        print("  宿主: 收到连接并应答")
    except socket.timeout:
        error = "host_accept_timeout — 容器未能连接到宿主 socket"
        print(f"  宿主: accept() 超时 — {error}")
    except OSError as e:
        error = f"host_socket_error: {e}"
        print(f"  宿主: {error}")
    finally:
        srv.close()

    try:
        cli_out, cli_err = cli_proc.communicate(timeout=30)
        print(f"  客户端输出: {cli_out.strip()}")
        if cli_proc.returncode != 0:
            print(f"  客户端 stderr: {cli_err.strip()[:200]}")
        if cli_out.strip():
            cli_data = json.loads(cli_out.strip())
            success = cli_data.get("success", False)
            rtt_ms  = cli_data.get("rtt_ms")
            if not success and not error:
                error = cli_data.get("error", "")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError) as e:
        cli_proc.kill()
        if not error:
            error = str(e)

    print(f"  连接到达宿主: {connection_received}  成功: {success}  RTT: {rtt_ms} ms")
    print(f"  状态: {'PASS ✓' if success else ('FAIL ✗ (预期)' if not success else 'FAIL ✗')}")

    return {
        "status": "PASS" if success else "FAIL",
        "connection_received": connection_received,
        "rtt_ms": rtt_ms,
        "error": error,
        "note": (f"宿主→容器 Unix Socket 通信成功 RTT={rtt_ms}ms"
                 if success else
                 f"失败（Windows Docker Desktop 已知限制）: {error}"),
    }


# ─── S3.3  RTT Latency ────────────────────────────────────────────────────────

_LATENCY_SERVER = r"""
import socket, os, time, json

SOCK = '/socket/lat.sock'
READY = '/socket/.lat_ready'
N = 20

if os.path.exists(SOCK):
    os.unlink(SOCK)

srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(SOCK)
os.chmod(SOCK, 0o777)
srv.listen(1)

with open(READY, 'w') as f:
    f.write('ready')

srv.settimeout(30.0)
conn, _ = srv.accept()
conn.settimeout(10.0)
for _ in range(N):
    data = conn.recv(1024)
    if not data:
        break
    conn.sendall(data)   # echo back
conn.close()
srv.close()
print('latency_server:done')
"""

_LATENCY_CLIENT = r"""
import socket, os, time, json

SOCK = '/socket/lat.sock'
READY = '/socket/.lat_ready'
N = 20

deadline = time.time() + 20.0
while time.time() < deadline:
    if os.path.exists(READY):
        break
    time.sleep(0.05)

time.sleep(0.1)

client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.connect(SOCK)

rtts = []
for i in range(N):
    msg = f'ping{i:04d}'.encode()
    t0 = time.perf_counter()
    client.sendall(msg)
    resp = client.recv(1024)
    rtt = (time.perf_counter() - t0) * 1000
    rtts.append(round(rtt, 3))

client.close()

rtts.sort()
p50  = rtts[len(rtts) // 2]
p99  = rtts[max(0, int(len(rtts) * 0.99) - 1)]
pmax = rtts[-1]
print(json.dumps({'rtts': rtts, 'p50': p50, 'p99': p99, 'max': pmax}))
"""


def test_latency() -> dict:
    print("\n" + "=" * 60)
    print("S3.3  RTT 延迟测量（20 次 round-trip, --network none 客户端）")
    print("=" * 60)

    setup_socket_dir()

    ts = int(time.time())
    srv_proc = run_container_detached(
        _LATENCY_SERVER,
        name=f"s3-latsrv-{ts}",
    )

    ready_file = SOCKET_DIR / ".lat_ready"
    deadline = time.time() + 20.0
    while time.time() < deadline:
        if ready_file.exists():
            break
        time.sleep(0.1)

    if not ready_file.exists():
        srv_proc.kill()
        return {"status": "FAIL", "note": "延迟测试服务端未就绪"}

    print("  延迟服务端就绪，启动客户端…")

    cli_result = run_container(
        _LATENCY_CLIENT,
        name=f"s3-latcli-{ts}",
        extra_flags=["--network", "none"],
        timeout=30,
    )
    srv_out, _ = srv_proc.communicate(timeout=10)

    print(f"  服务端: {srv_out.strip()}")
    print(f"  客户端: {cli_result.stdout.strip()}")

    try:
        data = json.loads(cli_result.stdout.strip())
        p50  = data["p50"]
        p99  = data["p99"]
        pmax = data["max"]
        passed = p99 < 50   # 50ms 阈值（Unix socket 应远低于此）
        print(f"  P50={p50:.3f}ms  P99={p99:.3f}ms  Max={pmax:.3f}ms")
        print(f"  状态: {'PASS ✓' if passed else 'FAIL ✗'}")
        return {
            "status": "PASS" if passed else "FAIL",
            "sample_count": len(data["rtts"]),
            "p50_ms": p50,
            "p99_ms": p99,
            "max_ms": pmax,
            "note": f"P50={p50:.3f}ms P99={p99:.3f}ms Max={pmax:.3f}ms",
        }
    except (json.JSONDecodeError, KeyError) as e:
        print(f"  结果解析失败: {e}")
        return {
            "status": "FAIL",
            "note": f"结果解析失败: {e} | 原始输出: {cli_result.stdout.strip()[:100]}",
        }


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("S3 Spike: Unix Socket 跨容器验证")
    print(f"时间:       {datetime.now().isoformat()}")
    print(f"Socket 目录: {SOCKET_DIR}")
    print("=" * 60)

    # Preflight
    r = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
    if r.returncode != 0:
        print("ERROR: Docker 未运行"); sys.exit(1)
    print("Docker: OK")

    print(f"预拉取镜像 {DOCKER_IMAGE}…")
    subprocess.run(["docker", "pull", DOCKER_IMAGE],
                   capture_output=True, timeout=120)

    results = {
        "timestamp": datetime.now().isoformat(),
        "platform":  "Windows 11 Pro / Docker Desktop WSL2",
        "docker_version": "29.2.1",
        "tests": {},
    }

    results["tests"]["container_to_container"] = test_container_to_container()
    results["tests"]["host_to_container"]      = test_host_to_container()
    results["tests"]["latency"]                = test_latency()

    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\n结果已保存: {RESULTS_FILE}")
    print("\n" + "=" * 60)
    print("汇总")
    print("=" * 60)
    for name, res in results["tests"].items():
        status = res.get("status", "?")
        note   = res.get("note", "")
        print(f"  {name:30s}  {status:7s}  {note}")

    return results


if __name__ == "__main__":
    main()
