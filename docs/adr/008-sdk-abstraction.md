# ADR-008: SDK 抽象与可替换性

**状态**：已采纳
**日期**：2026-03-29
**决策者**：架构团队

---

## 背景

Lynxclaw 当前使用 Claude Agent SDK 作为容器内 AI Agent 的实现。随着项目发展，需要明确：

1. SDK 在架构中的定位（核心依赖 vs 可替换组件）
2. 是否支持未来更换为其他 AI SDK（OpenAI、Gemini 等）
3. 如何在保持灵活性的同时避免过度抽象

## 决策

**SDK 定位为"容器内可替换实现"，而非平台核心**：

1. **IPC 协议与 SDK 解耦**：Host ↔ Container 通信通过文件系统 JSON-RPC，协议与 SDK 无关
2. **单一抽象点**：容器内 `run_agent()` 函数是唯一的 SDK 交互点
3. **容器镜像可插拔**：未来可提供多个镜像变体（`lynxclaw-agent-claude`, `lynxclaw-agent-openai`）
4. **当前不做抽象层**：不引入通用 Agent 接口，避免过度设计

## 理由

### 为什么 IPC 解耦很重要

- Host 进程（router, container_manager, channels）完全不依赖 SDK
- 更换 SDK 只需修改容器内代码，Host 代码零改动
- 测试时可以用 mock 容器替代真实 SDK

### 为什么不做通用抽象层

Claude Agent SDK 提供的高级功能（工具循环、MCP、session resume、hooks）是其核心价值。如果抽象成通用接口：

- 需要定义最小公约数，丢失 SDK 特有能力
- 其他 SDK（OpenAI Agents、LangChain）需要大量适配代码
- 维护成本高，收益不明确

### 当前策略

**保留 Claude Agent SDK 作为默认实现**，原因：

- 开箱即用的工具循环（tool_use → 执行 → tool_result → 继续）
- 内置工具（Bash, Read, Write, Edit）
- MCP 支持（可加载外部工具）
- Session resume（自动管理对话历史）
- Hooks 系统（PreToolUse / PostToolUse 用于安全拦截）

**未来扩展路径**：

如需支持其他模型，采用"多镜像变体"策略：

```yaml
# lynxclaw.config.yaml
container:
  image: lynxclaw-agent-claude    # 或 lynxclaw-agent-openai
```

每个镜像独立实现 `run_agent()` + IPC bridge，无需共享抽象层。

## 更换成本分析

| 组件 | 是否需要改 | 改动量 |
|------|-----------|--------|
| Host 进程（router, container_manager, ipc） | ❌ 不需要 | 0 |
| IPC 协议 | ❌ 不需要 | 0 |
| Channel adapters（Telegram/Feishu） | ❌ 不需要 | 0 |
| 容器 `run_agent()` | ✅ 需要重写 | 中等（~100-200 行） |
| 容器 requirements.txt + Dockerfile | ✅ 需要更新 | 小 |

**关键障碍**：

- 需要重新实现工具循环（tool_use → 执行 → tool_result）
- 需要自己管理 session 历史
- 需要实现 hooks 等价机制（安全拦截）

## 影响

### 文档表述

- README：移除"以 Claude Agent SDK 为核心"的表述
- ARCHITECTURE.md：明确 SDK 是"容器内可替换实现"
- 技术栈表格：标注"当前实现（可替换）"

### 代码结构

- `container/agent-runner/main.py` 中 `run_agent()` 保持为单一抽象点
- 注释说明："更换 SDK 只需修改此函数"
- IPC 协议保持稳定，不随 SDK 变化

### 未来工作

如需支持其他 SDK：

1. 创建新目录 `container/agent-runner-openai/`
2. 实现 `run_agent()` + `ipc_bridge.py`
3. 构建新镜像 `lynxclaw-agent-openai:latest`
4. 配置文件新增 `container.image` 选项

## 相关决策

- [ADR-001: 文件系统 IPC](001-file-ipc.md) — IPC 解耦是 SDK 可替换的基础
- [ADR-006: Credential Proxy](006-credential-proxy.md) — 凭证注入与 SDK 无关
- [ADR-007: Skills 系统](007-skills-system.md) — Skills 通过文件注入，不依赖 SDK

## 参考

- Claude Agent SDK 文档：https://docs.anthropic.com/en/agent-sdk
- OpenAI Agents SDK：https://platform.openai.com/docs/assistants
