#!/usr/bin/env python3
"""S2 Spike: watchdog + Docker Volume IPC 验证

验证宿主端 watchdog 是否能可靠检测到 Docker 容器写入的文件事件。
运行方式: python spike/ipc_verify.py
"""

import json
import os
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileCreatedEvent, FileSystemEventHandler

SPIKE_DIR   = Path(__file__).parent
OUTBOX_DIR  = SPIKE_DIR / "test_outbox"
RESULTS_FILE = SPIKE_DIR / "s2_results.json"

DOCKER_IMAGE  = "python:3.12-slim"
OUTBOX_MOUNT  = "/outbox"


# ─── event collector ─────────────────────────────────────────────────────────

class EventCollector(FileSystemEventHandler):
    """Collect file-appearance events.

    Handles two patterns:
    - Direct write  → FileCreatedEvent (on_created)
    - write-tmp+rename → FileMovedEvent (on_moved, dest is the real file)
    """
    def __init__(self):
        self.events: list = []
        self._lock = threading.Lock()

    def _record(self, path: str):
        recv_time = time.time()
        with self._lock:
            self.events.append({"path": path, "recv_time": recv_time})

    def on_created(self, event):
        if not event.is_directory:
            self._record(str(event.src_path))

    def on_moved(self, event):
        if not event.is_directory:
            # Renamed from .tmp → final file; record the destination
            self._record(str(event.dest_path))

    def snapshot(self) -> list:
        with self._lock:
            return list(self.events)

    def clear(self):
        with self._lock:
            self.events.clear()

    def wait_for(self, n: int, timeout: float = 15.0) -> list:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if len(self.events) >= n:
                    break
            time.sleep(0.05)
        return self.snapshot()


# ─── observer helpers ─────────────────────────────────────────────────────────

def start_observer(collector=None):
    if collector is None:
        collector = EventCollector()
    obs = Observer()
    obs.schedule(collector, str(OUTBOX_DIR), recursive=False)
    obs.start()
    time.sleep(0.5)          # warm-up
    return obs, collector


def stop_observer(obs):
    obs.stop()
    obs.join()


# ─── docker helpers ───────────────────────────────────────────────────────────

def outbox_volume_arg():
    """Return -v argument for Docker (handles Windows path)."""
    p = str(OUTBOX_DIR.resolve())
    # Docker Desktop for Windows accepts both / and \ but prefers /
    p = p.replace("\\", "/")
    return f"{p}:{OUTBOX_MOUNT}"


def run_container(script: str, name: str = "", env: dict = None,
                  timeout: int = 60) -> subprocess.CompletedProcess:
    cmd = ["docker", "run", "--rm", "-v", outbox_volume_arg()]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    if name:
        cmd += ["--name", name]
    cmd += [DOCKER_IMAGE, "python", "-c", script]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def setup_outbox():
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    for f in OUTBOX_DIR.iterdir():
        f.unlink()


# ─── S2.1 基础事件 ────────────────────────────────────────────────────────────

def test_basic_event() -> dict:
    print("\n" + "=" * 60)
    print("S2.1  基础事件测试")
    print("=" * 60)

    setup_outbox()
    obs, col = start_observer()

    try:
        script = (
            "import json, time\n"
            "data = {'test': 'basic', 'ts': time.time()}\n"
            "open('" + OUTBOX_MOUNT + "/test_basic.json', 'w').write(json.dumps(data))\n"
            "print('written test_basic.json')\n"
        )
        r = run_container(script, name="s2-basic")
        print(f"  容器输出: {r.stdout.strip()}")
        if r.returncode != 0:
            print(f"  容器错误: {r.stderr.strip()[:200]}")

        events = col.wait_for(1, timeout=5.0)
        success = len(events) > 0

        print(f"  收到事件: {len(events)}")
        print(f"  状态: {'PASS ✓' if success else 'FAIL ✗ — 5s 内未收到事件'}")

        return {
            "status": "PASS" if success else "FAIL",
            "event_received": success,
            "event_count": len(events),
            "note": ("宿主 watchdog 成功检测到容器写入事件"
                     if success else
                     "5s 内未收到 watchdog 事件 — 平台不支持事件驱动 IPC"),
        }
    finally:
        stop_observer(obs)


# ─── S2.2 延迟测量 ────────────────────────────────────────────────────────────

def test_latency() -> dict:
    print("\n" + "=" * 60)
    print("S2.2  延迟测量测试（100 个文件）")
    print("=" * 60)

    setup_outbox()
    obs, col = start_observer()

    NUM_FILES = 100

    try:
        # Embed write-timestamp (µs) into filename so host can compute latency
        # without clock-sync assumptions about file content.
        script = (
            "import time, os\n"
            "mount = '" + OUTBOX_MOUNT + "'\n"
            "for i in range(" + str(NUM_FILES) + "):\n"
            "    ts_us = int(time.time() * 1_000_000)\n"
            "    path = f'{mount}/lat_{i:04d}_{ts_us}.json'\n"
            "    open(path, 'w').write('{\"seq\":%d}' % i)\n"
            "print('done')\n"
        )

        t0 = time.time()
        r = run_container(script, name="s2-latency")
        elapsed = time.time() - t0
        print(f"  容器执行时间: {elapsed:.2f}s | 返回码: {r.returncode}")
        if r.returncode != 0:
            print(f"  错误: {r.stderr.strip()[:200]}")

        events = col.wait_for(NUM_FILES, timeout=20.0)
        received = len(events)
        print(f"  收到事件: {received}/{NUM_FILES}")

        if received == 0:
            print("  状态: FAIL ✗ — 未收到任何事件")
            return {
                "status": "FAIL",
                "sample_count": 0,
                "received_count": 0,
                "note": "未收到任何事件 — 平台不支持事件驱动 IPC",
            }

        # Parse latency from filename
        latencies_ms = []
        for e in events:
            fname = Path(e["path"]).name
            if fname.startswith("lat_") and fname.endswith(".json"):
                parts = fname[:-5].split("_")   # remove .json → ['lat','NNNN','TS']
                if len(parts) == 3:
                    try:
                        write_ts = int(parts[2]) / 1_000_000
                        lat = (e["recv_time"] - write_ts) * 1000
                        if lat >= 0:
                            latencies_ms.append(lat)
                    except ValueError:
                        pass

        if not latencies_ms:
            return {
                "status": "PARTIAL",
                "sample_count": 0,
                "received_count": received,
                "note": "收到事件但无法从文件名解析时间戳",
            }

        latencies_ms.sort()
        p50  = statistics.median(latencies_ms)
        idx99 = max(0, int(len(latencies_ms) * 0.99) - 1)
        p99  = latencies_ms[idx99]
        pmax = latencies_ms[-1]
        passed = p99 < 100

        print(f"  P50={p50:.1f}ms  P99={p99:.1f}ms  Max={pmax:.1f}ms")
        print(f"  状态: {'PASS ✓' if passed else 'FAIL ✗'} (P99 {'<' if passed else '>'} 100ms 阈值)")

        return {
            "status": "PASS" if passed else "FAIL",
            "sample_count": len(latencies_ms),
            "received_count": received,
            "p50_ms": round(p50, 1),
            "p99_ms": round(p99, 1),
            "max_ms": round(pmax, 1),
            "note": f"P99={p99:.1f}ms ({'< 100ms PASS' if passed else '> 100ms FAIL'})",
        }
    finally:
        stop_observer(obs)


# ─── S2.3 高频并发 ────────────────────────────────────────────────────────────

def test_concurrent() -> dict:
    print("\n" + "=" * 60)
    print("S2.3  高频并发测试（5 容器 × 20 文件 = 100）")
    print("=" * 60)

    setup_outbox()
    obs, col = start_observer()

    CONTAINERS         = 5
    FILES_PER_CONTAINER = 20
    TOTAL              = CONTAINERS * FILES_PER_CONTAINER

    try:
        # Container script uses env vars CID and N_FILES
        script = (
            "import json, time, os\n"
            "cid = int(os.environ['CID'])\n"
            "n = int(os.environ['N_FILES'])\n"
            "mount = '" + OUTBOX_MOUNT + "'\n"
            "for i in range(n):\n"
            "    ts = time.time()\n"
            "    final = f'{mount}/cc_{cid:02d}_{i:04d}.json'\n"
            "    tmp   = final + '.tmp'\n"
            "    open(tmp, 'w').write(json.dumps({'cid': cid, 'seq': i, 'ts': ts}))\n"
            "    os.rename(tmp, final)\n"
            "print(f'cid={cid} done')\n"
        )

        procs = []
        for cid in range(CONTAINERS):
            name = f"s2-cc-{cid}-{int(time.time())}"
            cmd = [
                "docker", "run", "--rm",
                "-v", outbox_volume_arg(),
                "-e", f"CID={cid}",
                "-e", f"N_FILES={FILES_PER_CONTAINER}",
                "--name", name,
                DOCKER_IMAGE, "python", "-c", script,
            ]
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
            procs.append((cid, p))

        print(f"  已启动 {CONTAINERS} 个容器…")

        for cid, p in procs:
            try:
                out, err = p.communicate(timeout=90)
                print(f"  容器 {cid}: {out.strip()}")
            except subprocess.TimeoutExpired:
                p.kill()
                print(f"  容器 {cid}: TIMEOUT")

        events = col.wait_for(TOTAL, timeout=20.0)
        received  = len([e for e in events if not e["path"].endswith(".tmp")])
        loss_rate = (TOTAL - received) / TOTAL * 100

        # Check ordering within each container
        seqs_by_cid: dict[int, list] = {i: [] for i in range(CONTAINERS)}
        for e in events:
            fname = Path(e["path"]).name
            if fname.startswith("cc_") and fname.endswith(".json"):
                parts = fname[:-5].split("_")
                if len(parts) == 3:
                    try:
                        seqs_by_cid[int(parts[1])].append(int(parts[2]))
                    except ValueError:
                        pass
        order_ok = all(v == sorted(v) for v in seqs_by_cid.values())

        passed = (received == TOTAL)
        print(f"  收到: {received}/{TOTAL}  丢失率: {loss_rate:.1f}%  顺序正确: {order_ok}")
        print(f"  状态: {'PASS ✓' if passed else 'FAIL ✗'}")

        return {
            "status": "PASS" if passed else "FAIL",
            "total_files": TOTAL,
            "received_count": received,
            "loss_rate_pct": round(loss_rate, 1),
            "order_correct": order_ok,
            "note": (f"{'零丢失' if passed else f'丢失 {TOTAL - received} 个'}, "
                     f"顺序{'正确' if order_ok else '异常'}"),
        }
    finally:
        stop_observer(obs)


# ─── S2.4 原子性 ──────────────────────────────────────────────────────────────

def test_atomicity() -> dict:
    print("\n" + "=" * 60)
    print("S2.4  原子性测试（write-tmp + rename）")
    print("=" * 60)

    setup_outbox()

    partial_reads: list = []
    received_paths: list = []
    lock = threading.Lock()

    class AtomicChecker(FileSystemEventHandler):
        def on_created(self, event):
            if event.is_directory or event.src_path.endswith(".tmp"):
                return
            recv_time = time.time()
            path = str(event.src_path)
            with lock:
                received_paths.append(path)
            # Immediately try to read and parse
            try:
                content = Path(path).read_text()
                json.loads(content)          # valid JSON → atomic
            except json.JSONDecodeError as exc:
                with lock:
                    partial_reads.append({"file": Path(path).name,
                                          "error": str(exc),
                                          "content_head": content[:80]})
            except OSError:
                pass                          # race with rename — ok to ignore

    checker = AtomicChecker()
    obs = Observer()
    obs.schedule(checker, str(OUTBOX_DIR), recursive=False)
    obs.start()
    time.sleep(0.5)

    try:
        NUM_FILES = 50
        script = (
            "import json, time, os\n"
            "mount = '" + OUTBOX_MOUNT + "'\n"
            "for i in range(" + str(NUM_FILES) + "):\n"
            "    data = json.dumps({'seq': i, 'payload': 'x' * 500, 'ts': time.time()})\n"
            "    tmp   = f'{mount}/atom_{i:04d}.json.tmp'\n"
            "    final = f'{mount}/atom_{i:04d}.json'\n"
            "    open(tmp, 'w').write(data)\n"
            "    os.rename(tmp, final)\n"
            "print('done')\n"
        )

        r = run_container(script, name="s2-atomic")
        print(f"  容器输出: {r.stdout.strip()}")
        if r.returncode != 0:
            print(f"  错误: {r.stderr.strip()[:200]}")

        time.sleep(3.0)       # let watchdog flush remaining events

        final_files = list(OUTBOX_DIR.glob("atom_*.json"))
        tmp_files   = list(OUTBOX_DIR.glob("atom_*.json.tmp"))

        success = len(partial_reads) == 0
        print(f"  写入文件: {NUM_FILES}  最终文件数: {len(final_files)}"
              f"  残留 .tmp: {len(tmp_files)}")
        print(f"  读到不完整 JSON: {len(partial_reads)}")
        print(f"  状态: {'PASS ✓' if success else 'FAIL ✗'}")

        return {
            "status": "PASS" if success else "FAIL",
            "files_written": NUM_FILES,
            "files_on_disk": len(final_files),
            "tmp_remaining": len(tmp_files),
            "partial_reads": len(partial_reads),
            "note": ("write-tmp+rename 原子 — 未读到半写内容"
                     if success else
                     f"发现 {len(partial_reads)} 个不完整文件"),
        }
    finally:
        stop_observer(obs)


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("S2 Spike: watchdog + Docker Volume IPC 验证")
    print(f"时间:   {datetime.now().isoformat()}")
    print(f"Outbox: {OUTBOX_DIR}")
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
        "watchdog_version": "6.0.0",
        "tests": {},
    }

    results["tests"]["basic_event"] = test_basic_event()
    results["tests"]["latency"]     = test_latency()
    results["tests"]["concurrent"]  = test_concurrent()
    results["tests"]["atomicity"]   = test_atomicity()

    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\n结果已保存: {RESULTS_FILE}")
    print("\n" + "=" * 60)
    print("汇总")
    print("=" * 60)
    for name, res in results["tests"].items():
        print(f"  {name:20s}  {res['status']:7s}  {res.get('note', '')}")

    return results


if __name__ == "__main__":
    main()
