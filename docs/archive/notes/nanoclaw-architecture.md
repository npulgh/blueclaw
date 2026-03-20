# NanoClaw 技术架构总结

> 来源：[qwibitai/nanoclaw](https://github.com/qwibitai/nanoclaw)
> 分析时间：2026-03-17
> 数据来源：DeepWiki + GitHub

---

## 一、项目定位

NanoClaw 是 **OpenClaw 的轻量级安全替代方案**，是一个基于 Anthropic Claude Agent SDK 的个人 AI 助手。

核心差异：

| 维度 | OpenClaw | NanoClaw |
|------|----------|----------|
| 代码量 | ~500,000 行，53 个配置文件，70+ 依赖 | ~3,900 行，15 个核心文件 |
| 安全模型 | 应用层权限检查 | OS 级容器隔离 |
| 运行方式 | 单 Node 进程，共享内存 | 每次 agent 调用独立容器 |
| 可审计性 | 难以全面理解 | 小到可以完整阅读 |

**设计哲学**：够用、可理解、容器级安全，而不是功能全面。

---

## 二、整体架构

```
┌─────────────────────────────────────────────────────┐
│                  Host Process (Node.js)              │
│                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │ Channel  │  │  Router  │  │ Task Scheduler   │  │
│  │ Registry │  │(router.ts)│  │(task-scheduler.ts│  │
│  └────┬─────┘  └────┬─────┘  └────────┬─────────┘  │
│       │             │                  │             │
│  ┌────▼─────────────▼──────────────────▼──────────┐ │
│  │            SQLite Database                      │ │
│  │         (store/messages.db)                     │ │
│  └──────────────────┬──────────────────────────────┘ │
│                     │                               │
│  ┌──────────────────▼──────────────────────────────┐ │
│  │          Container Runner (container-runner.ts)  │ │
│  └──────────────────┬──────────────────────────────┘ │
└─────────────────────┼───────────────────────────────┘
                      │ spawn
         ┌────────────▼──────────────┐
         │   Linux Container          │
         │  (Docker / Apple Container)│
         │                            │
         │  ┌────────────────────┐    │
         │  │  agent-runner      │    │
         │  │  (Claude Agent SDK)│    │
         │  └────────────────────┘    │
         │                            │
         │  /workspace/group/  (rw)   │
         │  /workspace/project/(ro)   │
         │  /workspace/global/ (ro)   │
         └────────────────────────────┘
```

---

## 三、核心组件详解

### 3.1 宿主编排器（Host Orchestrator）

**文件**：`src/index.ts`

单个 Node.js 进程，维护三个核心事件循环：

| 循环 | 频率 | 功能 |
|------|------|------|
| `startMessageLoop()` | 每 2 秒 | 轮询 SQLite，处理新消息 |
| `startScheduler()` | 每 60 秒 | 检查到期任务并触发 |
| `watchIPC()` | 每 1 秒 | 监听容器 IPC 目录的出站消息 |

启动时：初始化 Channel Registry → 从 SQLite 恢复状态 → 重放未处理消息。

### 3.2 消息处理管道

```
用户发消息
  → Channel 接收
  → 存入 SQLite
  → Message Loop 轮询（2s）
  → router.ts 检查 chat_jid 是否在注册组
  → 检查 trigger pattern 是否匹配
  → 拉取会话历史 + 格式化
  → container-runner.ts 在容器内调用 Claude Agent SDK
  → 容器输出响应
  → 通过原始 Channel 回复用户
```

### 3.3 Channel 系统

**文件**：`src/channels/registry.ts`、`src/channels/index.ts`

- 采用**工厂注册**模式，各 Channel 在启动时调用 `registerChannel()` 自注册
- 缺少凭证时自动跳过并打印警告，不影响其他 Channel
- Channel 作为 Claude Code **Skills** 实现，安装即扩展功能

已支持 Channel：WhatsApp、Telegram、Slack、Discord、Gmail

### 3.4 容器运行器（Container Runner）

**文件**：`src/container-runner.ts`

每次 Agent 调用：
1. 创建临时 Linux 容器（`--rm` 自动清理）
2. 设置工作目录：`groups/{group-name}/`
3. 挂载白名单内的目录
4. 以非 root 用户（`node`，uid 1000）运行
5. 通过 Claude Agent SDK 执行任务

容器内可用工具：Bash、文件操作、WebSearch、WebFetch、agent-browser（Chromium）、`mcp__nanoclaw__*`（任务调度）

### 3.5 IPC 通信

**文件**：`src/ipc.ts`，`container/agent-runner/src/ipc-mcp-stdio.ts`

- 通信机制：**文件系统 IPC**（`data/ipc/` 目录）
- 容器写入 IPC 文件 → 宿主 `watchIPC()` 轮询读取
- 支持操作：`send_message`（向 Group 发消息）、任务增删改查

### 3.6 数据库与状态管理

**文件**：`src/db.ts`

- 存储引擎：SQLite（`better-sqlite3`），路径 `store/messages.db`
- 存储内容：消息、注册组、Session、定时任务、Router 状态
- 启动时执行 Schema 迁移
- 消息游标（cursor）支持崩溃恢复，保证至少一次投递

---

## 四、安全模型

### 4.1 容器隔离（核心安全边界）

- **进程隔离**：容器进程无法影响宿主系统
- **文件系统隔离**：仅显式挂载的目录在容器内可见
- **非 root 运行**：uid 1000 的 `node` 用户
- **临时容器**：`--rm` 确保每次调用后清理

### 4.2 挂载安全

挂载白名单存于 `~/.config/nanoclaw/mount-allowlist.json`（项目根目录外，不会被挂入容器）。

默认阻断的路径模式：`.ssh`、`.aws`、`.gnupg`、`credentials`、`.env`、私钥文件

`validateMount()` 校验逻辑：
- 拒绝路径穿越（`..` 和绝对路径）
- 挂载前解析符号链接
- 非主 Group 强制只读

### 4.3 权限分级

| 能力 | Main Group | Non-Main Group |
|------|-----------|----------------|
| 项目根目录 | `/workspace/project`（只读） | 无 |
| Group 目录 | `/workspace/group`（读写） | `/workspace/group`（读写） |
| 全局内存 | 通过 project 隐式访问 | `/workspace/global`（只读） |
| 额外挂载 | 可配置 | 只读（需显式授权） |
| 跨 Group 消息 | 可管理所有 Group | 仅限本 Group |
| 任务调度 | 可调度任意 Group 任务 | 仅限本 Group |

### 4.4 凭证过滤

仅向容器暴露 `CLAUDE_CODE_OAUTH_TOKEN` 和 `ANTHROPIC_API_KEY`，其他环境变量不透传。

---

## 五、高级功能

### 5.1 分层内存系统（Hierarchical CLAUDE.md）

```
groups/
  CLAUDE.md                ← 全局内存（所有 Group 只读，main Group 可写）
  {group-name}/
    CLAUDE.md              ← 该 Group 的专属记忆
    notes.md               ← Agent 可创建的扩展文件
```

Agent 启动时 CWD 设为 `groups/{group-name}/`，自动加载 `../CLAUDE.md`（全局）和 `./CLAUDE.md`（组级），通过 `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD` 支持更多目录。

### 5.2 Agent Swarms（智能体群组）

通过设置环境变量 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` 启用，允许多个专门化 Agent 协作完成复杂任务，基于 Claude Code 的 sub-agent orchestration 实现。

### 5.3 技能系统（Skills）

技能存储：`.claude/skills/{skill-name}/SKILL.md`

示例：`/add-telegram` 技能可将 Telegram Channel 添加到 fork 中。技能是扩展 NanoClaw 功能的主要机制，而非修改核心代码。

### 5.4 定时任务调度

**文件**：`src/task-scheduler.ts`

支持三种任务类型：
- `cron`：基于 cron 表达式（如 `0 9 * * 1` 每周一 9 点）
- `interval`：固定间隔（如 `3600000` 每小时）
- `once`：一次性任务（ISO 时间戳）

特点：任务在对应 Group 的容器中运行，拥有完整 Agent 能力（WebSearch、文件操作等），可通过 `send_message` 推送消息或静默完成。

### 5.5 MCP 服务器

每次 Agent 调用时动态创建 `nanoclaw` MCP Server，向容器内 Agent 提供以下工具：

```
schedule_task      # 创建定时任务
list_tasks         # 列出任务
get_task           # 查询任务详情
update_task        # 更新任务
pause_task         # 暂停任务
resume_task        # 恢复任务
cancel_task        # 取消任务
send_message       # 向本 Group 发送消息
```

---

## 六、技术栈

| 类别 | 技术 |
|------|------|
| 运行时 | Node.js 20+ |
| 语言 | TypeScript |
| AI SDK | `@anthropic-ai/claude-agent-sdk` |
| 容器运行时 | Docker（Linux/Windows）/ Apple Container（macOS） |
| 数据库 | SQLite + `better-sqlite3` |
| WhatsApp | `baileys` |
| 日志 | `pino`（结构化 JSON） |
| 浏览器自动化 | `agent-browser`（Chromium） |

---

## 七、文件结构

```
nanoclaw/
├── src/
│   ├── index.ts              # 宿主编排器入口
│   ├── router.ts             # 消息路由
│   ├── container-runner.ts   # 容器生命周期管理
│   ├── ipc.ts                # IPC 文件监听
│   ├── db.ts                 # SQLite 操作 + Schema 迁移
│   ├── task-scheduler.ts     # 定时任务调度
│   ├── config.ts             # 配置加载
│   ├── types.ts              # 类型定义
│   └── channels/
│       ├── registry.ts       # Channel 工厂注册
│       └── index.ts          # Channel 自注册触发
├── container/agent-runner/
│   └── src/
│       ├── index.ts          # 容器内 Agent 入口
│       └── ipc-mcp-stdio.ts  # nanoclaw MCP Server 实现
├── groups/
│   ├── CLAUDE.md             # 全局记忆
│   └── {group-name}/
│       └── CLAUDE.md         # Group 专属记忆
├── store/
│   └── messages.db           # SQLite 数据库
├── data/
│   └── ipc/                  # 宿主-容器 IPC 目录
├── docs/
│   ├── SPEC.md               # 详细技术规格
│   ├── REQUIREMENTS.md       # 需求文档
│   └── SECURITY.md           # 安全文档
└── .claude/skills/           # 技能目录（Channel 扩展等）
```

---

## 八、与 OpenClaw 的核心差异总结

1. **代码规模**：3,900 行 vs 500,000 行 — 可被完整审计
2. **安全边界**：容器级 OS 隔离 vs 应用层权限检查
3. **进程模型**：每调用独立容器 vs 单进程共享内存
4. **扩展方式**：Claude Code Skills vs 插件框架
5. **依赖数量**：极少核心依赖 vs 70+ 依赖
6. **设计目标**：安全可审计的个人助手 vs 功能完备的通用平台

---

## 参考资料

- [GitHub: qwibitai/nanoclaw](https://github.com/qwibitai/nanoclaw)
- [NanoClaw 官网](https://nanoclaw.dev)
- [DeepWiki: qwibitai/nanoclaw](https://deepwiki.com/qwibitai/nanoclaw)
