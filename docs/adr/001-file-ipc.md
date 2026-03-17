# ADR-001: 文件系统 JSON-RPC 作为 IPC 机制

**状态**：已接受
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
| Unix Socket | 低延迟 | Windows 兼容性差、需要额外 Volume 挂载 |
| TCP Socket | 标准化 | 需要开放网络、与 `--network none` 冲突 |

## 理由

1. **与 `--network none` 完美兼容**：文件 IPC 不需要网络栈，容器可以完全断网运行，不影响通信。
2. **天然审计能力**：JSON 文件可直接查看、归档，方便事后审查 Agent 行为。
3. **零版本耦合**：宿主和容器只需约定 JSON schema，无需共享 protobuf/gRPC 版本。
4. **跨平台**：Docker Volume 在 Linux 和 Windows 上行为一致。
5. **延迟可接受**：watchdog 文件事件驱动，实测延迟在个位数毫秒，对 IM 场景完全足够。

## 权衡

- 文件 I/O 吞吐量低于内存通信，但 IM 消息频率远未达到瓶颈。
- 需要 watchdog 库做事件驱动，增加一个依赖（但 watchdog 是成熟的跨平台库）。
- 并发写入需要原子文件操作（写临时文件 → rename），避免读到半写文件。
