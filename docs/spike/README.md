# Lynxclaw — 技术验证 Spike

> 状态：**已完成 ✓** — 全部 GO，Phase 1 开发可启动
> 执行日期：2026-03-17 / 18

---

## 快速入口

| 文档 | 内容 |
| ---- | ---- |
| **[SPIKE-REPORT.md](SPIKE-REPORT.md)** | **综合报告**：关键发现、可复用 API 用法、架构影响（从这里开始） |
| [findings.md](findings.md) | 完整执行记录：每项测试的原始数据、表格、备注 |
| [archive/](archive/) | 过程性文档：Spike 规格、任务清单（已存档，供历史参考） |

---

## 背景

Lynxclaw 的架构建立在三个未经验证的核心假设上，在写任何实现代码之前先行验证：

| Spike | 核心假设 | 判定 |
| ----- | -------- | ---- |
| S1 | Python Agent SDK hooks / resume / MCP 可用 | **GO ✓** |
| S2 | watchdog + Docker Volume 文件事件在 Windows 下可靠 | **GO ✓** |
| S3 | Unix Socket 跨 Docker 容器边界可行（Proxy Sidecar 前提） | **GO ✓** |
