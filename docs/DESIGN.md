# Lynxclaw — 设计文档索引

> **Lynxclaw** 是一个轻量级 AI 智能体运行平台，以 Anthropic Claude Agent SDK（Python）为核心，将 Claude Agent 安全地运行在 Docker 容器中，通过 IM（Telegram / 飞书）与用户交互。

---

## 文档结构

本项目的设计文档已按职责拆分为以下独立文件，便于 AI Agent 和人类开发者按需阅读：

| 文档 | 内容 | 关注点 |
| ---- | ---- | ---- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 系统架构、组件设计、安全模型、数据模型 | **怎么建** |
| [TASKS.md](TASKS.md) | 分阶段开发任务清单、验收标准、依赖关系 | **做什么** |
| [adr/](adr/) | 架构决策记录（ADR） | **为什么这么选** |

---

## 架构决策记录（ADR）

| ADR | 决策 |
| ---- | ---- |
| [ADR-001](adr/001-file-ipc.md) | IPC 使用文件系统 JSON-RPC（不换 gRPC/Socket） |
| [ADR-002](adr/002-aiogram.md) | Telegram SDK 使用 aiogram（不换 python-telegram-bot） |
| [ADR-003](adr/003-long-connection.md) | 长连接优先于 Webhook |
| [ADR-004](adr/004-proxy-sidecar.md) | 联网走 Proxy Sidecar（不用 --network bridge） |
| [ADR-005](adr/005-resumable-containers.md) | 默认 Ephemeral 容器 + 可选 Resumable |

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

## 技术栈

| 类别 | 技术 |
| ---- | ---- |
| 运行时 | Python 3.11+ |
| AI SDK | `claude-agent-sdk` |
| 容器 | Docker Engine |
| 数据库 | SQLite + `aiosqlite` |
| Telegram | `aiogram` v3 |
| 飞书 | `lark-oapi`（WebSocket 长连接） |
| 文件监听 | `watchdog` |
| 日志 | `structlog` |
| 配置 | `pyyaml` + `python-dotenv` |

---

## 阅读指南

- **首次了解项目**：先读本文件 → 再读 [ARCHITECTURE.md](ARCHITECTURE.md)
- **开始开发**：读 [TASKS.md](TASKS.md)，从 T1.1 开始逐任务实现
- **理解设计决策**：读 [adr/](adr/) 中对应的 ADR 文件
- **修改已确定的架构**：必须先更新对应 ADR，再修改代码
