# Lynxclaw — 任务清单

> MVP 已完成（2026-03-19）。Phase 0–4 全部实现，381 测试通过。
>
> 关联文档：
> - [ARCHITECTURE.md](ARCHITECTURE.md) — 系统架构
> - [adr/](adr/) — 架构决策记录
> - [archive/TASKS-v1-mvp.md](archive/TASKS-v1-mvp.md) — MVP 开发历史记录（Phase 0–4 完整任务清单）

---

## 架构约束（DO NOT CHANGE）

> 以下决策已在 ADR 中确认，修改前必须先更新对应 ADR。

| 约束 | ADR |
| ---- | ---- |
| IPC 使用文件系统 JSON-RPC（不换 gRPC/Socket） | [ADR-001](adr/001-file-ipc.md) |
| Telegram SDK 使用 aiogram v3（不换 python-telegram-bot） | [ADR-002](adr/002-aiogram.md) |
| 长连接优先于 Webhook | [ADR-003](adr/003-long-connection.md) |
| 联网走 Proxy Sidecar（不用 --network bridge） | [ADR-004](adr/004-proxy-sidecar.md) |
| 默认 Ephemeral + 可选 Resumable + 可选 Persistent | [ADR-005](adr/005-resumable-containers.md) |
| 容器加固参数（cap-drop ALL 等）不可删减 | [ARCHITECTURE.md §3.4](ARCHITECTURE.md) |
| 数据库使用 SQLite（不换 PostgreSQL 等） | 设计哲学——零部署依赖 |

---

## 已知问题（Backlog）

### BUG-002: E2E 测试断言条件过严

**文件**: `tests/test_e2e_local.py`

**现象**: `test_adapter_receives_reply` 等待 `len(adapter.sent) > initial_sent + 1`（需要 thinking placeholder + 真实回复），当 API 返回空响应时只有 placeholder，测试超时。

**根因**: 测试假设 API 总是返回非空内容。使用第三方 proxy 时可能返回空响应。

**修复方向**: 断言改为 `len(adapter.sent) > initial_sent`（至少一条新消息），或换用可靠的 API endpoint。

---

## 后续开发方向（待规划）

> 以下为潜在扩展方向，尚未立项。开始前需先写 ADR 或更新 ARCHITECTURE.md。

- **更多 IM 渠道**：Discord、Slack、微信（参考 [channel-development.md](channel-development.md)）
- **Web Dashboard**：可视化 Group 管理、任务调度、审计日志
- **Agent 市场**：预置 Agent 模板（代码助手、文档助手等）
- **多模型支持**：在 `run_agent()` 抽象层接入非 Claude 模型
