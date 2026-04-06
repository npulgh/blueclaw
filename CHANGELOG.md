# 变更日志

所有显著变更都将记录在此文件。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，  
版本遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [Unreleased]

## [0.1.0] - 2026-04-05

### Added

- **MVP 核心平台**：AI Agent 安全运行在 Docker 容器中
- **IM 双通道支持**：Telegram（aiogram v3 Long Polling）+ 飞书（lark-oapi WebSocket）
- **流式响应**：实时推送，500ms / 200 字符去抖机制
- **容器安全加固**：`--cap-drop ALL`、`--read-only`、非 root 用户、资源限制
- **容器生命周期**：支持 ephemeral（一次性）和 resumable（多轮复用）模式
- **定时任务调度**：基于 croniter 的 cron 表达式支持
- **Web Dashboard**：7 个只读 API + Alpine.js SPA 前端，Bearer Token 认证
- **Proxy Sidecar**：网络隔离架构，可控域名白名单访问
- **Credential Proxy**（ADR-006）：API Key 不再通过环境变量注入容器
- **Skills 系统**（ADR-007）：通过 Markdown 文件注入领域知识
- **SDK 抽象层**（ADR-008）：容器内 SDK 与 Host 解耦，可替换
- **SQLite 持久化**：消息、会话、任务、审计日志
- **可观测性**：structlog JSON 日志 + Prometheus 指标 + 审计日志

### Security

- 容器默认 `--network none`，联网必须通过 Proxy Sidecar
- IM 凭证（Telegram Token、飞书密钥）永不进入容器
- 挂载白名单机制，敏感路径（.ssh, .aws, .env 等）禁止访问
- 发送者白名单（Sender Allowlist）防止未授权触发

---

## 计划中的功能

详见 GitHub Issues。
