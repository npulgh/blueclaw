# Lynxclaw — 设计文档索引

> **Lynxclaw** 是一个轻量级 AI 智能体运行平台，以 Anthropic Claude Agent SDK（Python）为核心，将 Claude Agent 安全地运行在 Docker 容器中，通过 IM（Telegram / 飞书）与用户交互。
>
> **状态**：MVP 完成（2026-03-19）。381 测试通过，Telegram E2E 验证。

---

## 文档结构

| 文档 | 内容 | 关注点 |
| ---- | ---- | ---- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 系统架构、组件设计、安全模型、数据模型 | **怎么建** |
| [TASKS.md](TASKS.md) | 架构约束、已知问题、后续方向 | **做什么** |
| [channel-development.md](channel-development.md) | 新增 IM 渠道开发指南 | **如何扩展** |
| [E2E-TESTING.md](E2E-TESTING.md) | E2E 测试策略与运行手册 | **如何测试** |
| [DEBUG-API-MIRROR.md](DEBUG-API-MIRROR.md) | 第三方 API 镜像调试记录 | **运维参考** |
| [adr/](adr/) | 架构决策记录（ADR） | **为什么这么选** |

---

## 架构决策记录（ADR）

| ADR | 决策 |
| ---- | ---- |
| [ADR-001](adr/001-file-ipc.md) | IPC 使用文件系统 JSON-RPC（不换 gRPC/Socket） |
| [ADR-002](adr/002-aiogram.md) | Telegram SDK 使用 aiogram v3（不换 python-telegram-bot） |
| [ADR-003](adr/003-long-connection.md) | 长连接优先于 Webhook |
| [ADR-004](adr/004-proxy-sidecar.md) | 联网走 Proxy Sidecar（不用 --network bridge） |
| [ADR-005](adr/005-resumable-containers.md) | 默认 Ephemeral + 可选 Resumable + 可选 Persistent |
| [ADR-005 加固教训](adr/ADR-005-container-hardening-lessons.md) | 容器加固实施经验与 Bug 修复记录 |

---

## 归档文档

> 以下文档已完成历史使命，归档于 `archive/` 供参考。

| 文档 | 说明 |
| ---- | ---- |
| [archive/TASKS-v1-mvp.md](archive/TASKS-v1-mvp.md) | MVP Phase 0–4 完整开发任务清单 |
| [archive/notes/](archive/notes/) | 项目启动前的规划笔记与多引擎分析 |
| [archive/spike-data/](archive/spike-data/) | Spike 验证原始数据（S1/S2/S3 测试产物） |
| [spike/](spike/) | Spike 技术验证报告（findings、SPIKE-REPORT） |

---

## 设计哲学

| 原则 | 说明 |
| ---- | ---- |
| 容器即安全边界 | Agent 运行在最小权限 Docker 容器中，OS 级隔离 |
| 纵深防御 | 容器隔离 + 网络代理 + 工具钩子 + 挂载白名单，多层叠加 |
| 流式优先 | Agent 响应边生成边推送到 IM，用户无需等待完整回复 |
| 长连接优先 | 飞书 WebSocket / Telegram Long Polling 为首选 |
| 小而可审计 | 核心代码 ≤ 5,000 行 |
| IM 原生 | Telegram + 飞书为一等公民，不做通用网关 |
| IPC 解耦 | 宿主与容器通过文件系统 JSON-RPC 通信，不耦合 SDK 版本 |

---

## 阅读指南

- **首次了解项目**：先读本文件 → 再读 [ARCHITECTURE.md](ARCHITECTURE.md)
- **开发新功能**：读 [TASKS.md](TASKS.md) 了解约束和方向，读 [ARCHITECTURE.md](ARCHITECTURE.md) 了解现有结构
- **新增 IM 渠道**：读 [channel-development.md](channel-development.md)
- **理解设计决策**：读 [adr/](adr/) 中对应的 ADR 文件
- **修改已确定的架构**：必须先更新对应 ADR，再修改代码
