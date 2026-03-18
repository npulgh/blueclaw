# ADR-001: 文件系统 JSON-RPC 作为 IPC 机制

**状态**：已接受（Spike S2 验证，2026-03-18 更新）
**日期**：2026-03-17

---

## 决策

宿主进程与 Docker 容器之间的通信使用 **文件系统 JSON-RPC 2.0**（通过 Docker Volume 共享目录），而非 gRPC、Unix Socket 或 TCP Socket。

## 背景

容器内运行的 Agent Runner 需要向宿主发送消息（如 `send_message`、`stream_chunk`），宿主也可能需要向容器传递指令。需要一种可靠、简单且与容器安全策略兼容的通信方式。

## 考虑的方案

| 方案 | 优势 | 劣势 |
| ---- | ---- | ---- |
| **文件系统 JSON-RPC** | 零依赖、跨平台、天然持久化、易于调试审计 | 延迟略高于 Socket（毫秒级 vs 微秒级） |
| gRPC / protobuf | 高性能、强类型 | 需要额外依赖、与 `--network none` 冲突 |
| Unix Socket | 低延迟（P99=0.064ms） | Windows Python 无 `AF_UNIX`，宿主进程方案不可行 |
| TCP Socket | 标准化 | 需要开放网络、与 `--network none` 冲突 |

## 理由

1. **与 `--network none` 完美兼容**：文件 IPC 不需要网络栈，容器可以完全断网运行，不影响通信。
2. **天然审计能力**：JSON 文件可直接查看、归档，方便事后审查 Agent 行为。
3. **零版本耦合**：宿主和容器只需约定 JSON schema，无需共享 protobuf/gRPC 版本。
4. **跨平台**：Docker Volume 在 Linux 和 Windows 上行为一致（Spike S2 实测确认）。
5. **延迟可接受**：watchdog 文件事件驱动，**Spike S2 实测 P50=0.2ms / P99=0.4ms**，对 IM 场景完全足够。

## 权衡

- 文件 I/O 吞吐量低于内存通信，但 IM 消息频率远未达到瓶颈。
- 需要 watchdog 库做事件驱动，增加一个依赖（但 watchdog 是成熟的跨平台库）。
- 并发写入需要原子文件操作（写临时文件 → rename），避免读到半写文件。
- **⚠️ 关键实现细节**：宿主端 watchdog handler 必须**同时实现 `on_created` 和 `on_moved`**。容器使用 write-tmp + rename 原子写入时，在 Windows 和 Linux 上均触发 `FileMovedEvent`（`on_moved`），而非 `FileCreatedEvent`。仅实现 `on_created` 会导致 100% 原子写入事件丢失。`on_moved` 中应使用 `event.dest_path`（非 `src_path`）。

## 实验验证（Spike S2，2026-03-18）

| 指标 | 结果 |
| ---- | ---- |
| 事件丢失率 | **0%**（100 并发文件，5 容器） |
| P50 延迟 | **0.2 ms** |
| P99 延迟 | **0.4 ms** |
| 原子写入（write-tmp + rename） | **零读到半写内容** |
| Windows Docker Desktop WSL2 | **与 Linux 行为一致**，无平台分叉 |

