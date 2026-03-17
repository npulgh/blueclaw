# Lynxclaw — 技术验证 Spike

> 来源：[多引擎综合审视](../../notes/multi-engine-review.md)
> 状态：待执行
> 预计耗时：1-2 天
>
> 本 Spike 的目的是在写任何宿主代码之前，验证三个致命技术假设。
> 任何一项失败都需要重新评估对应模块的技术路线。
> 执行结果记录在 [findings.md](findings.md)。

---

## 为什么需要 Spike

Lynxclaw 的架构建立在三个未经验证的核心假设上：

1. **Python Claude Agent SDK** 的 hooks / resume / MCP 机制可用且行为符合预期
2. **watchdog + Docker Volume** 的文件事件通知在 Windows Docker Desktop (WSL2) 上可靠
3. **Unix Socket** 跨 Docker 容器边界传递在目标平台上可行（Proxy Sidecar 前提）

这三个假设如果错误，影响范围分别是：安全模型、IPC 通信层、网络代理层。
在 3000 行代码写完后才发现假设错误，返工成本远高于提前 1-2 天验证。

---

## Spike 索引

| Spike | 验证内容 | 阻塞阶段 | Spec |
| ----- | -------- | -------- | ---- |
| S1 | Python Agent SDK — hooks / resume / MCP | Phase 1 | [s1-agent-sdk.md](s1-agent-sdk.md) |
| S2 | watchdog + Docker Volume — 事件可靠性与延迟 | Phase 1 | [s2-watchdog-ipc.md](s2-watchdog-ipc.md) |
| S3 | Unix Socket 跨容器通信（可选） | Phase 3 | [s3-unix-socket.md](s3-unix-socket.md) |

---

## 执行清单

- [ ] S1: Python Agent SDK 验证 → [spec](s1-agent-sdk.md)
- [ ] S2: watchdog + Docker Volume 验证 → [spec](s2-watchdog-ipc.md)
- [ ] （可选）S3: Unix Socket 跨容器验证 → [spec](s3-unix-socket.md)
- [ ] 汇总所有结果，做出 Go/No-Go 决策
- [ ] 如有失败项，更新 TASKS.md 中受影响的任务设计
