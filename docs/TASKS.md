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

## Phase 5：架构升级（NanoClaw 借鉴）

> 来源：NanoClaw 架构分析（2026-03-21）。详见 [ARCHITECTURE.md §十](ARCHITECTURE.md)。
> 原则：只引入 Lynxclaw 缺失且收益明确的设计，不照搬。

### 5.0 前置：ADR 编写

| # | 任务 | 验收标准 |
| ---- | ---- | ---- |
| 5.0.1 | 编写 ADR-006: Credential Proxy 凭证隔离 | 记录决策动机、方案对比（env var vs proxy）、安全分析、迁移路径 |
| 5.0.2 | 编写 ADR-007: Skills 扩展系统 | 记录设计选型（Markdown 注入 vs MCP tool vs git branch）、目录结构、加载机制 |

---

### 5.1 Credential Proxy——凭证永不入容器（P0）

| # | 任务 | 文件 | 验收标准 |
| ---- | ---- | ---- | ---- |
| 5.1.1 | 扩展 Proxy Sidecar 支持凭证注入 | `src/proxy.py` | Proxy 接收请求 → 注入 `Authorization: Bearer {key}` → 转发上游；key 从宿主环境变量读取，不写入任何文件 |
| 5.1.2 | ContainerManager 移除 API key 环境变量 | `src/container_manager.py` | 容器不再收到 `ANTHROPIC_API_KEY`；改为 `ANTHROPIC_BASE_URL=http://proxy-sidecar:port` |
| 5.1.3 | 容器内 api_proxy 适配 | `container/agent-runner/api_proxy.py` | 模型验证拦截保持不变；上游地址改为 credential proxy endpoint |
| 5.1.4 | 环境变量白名单 | `src/container_manager.py` | `_ALLOWED_ENV_PREFIXES` 白名单过滤，不匹配的 key 被拦截并 log warning |
| 5.1.5 | 测试：凭证不可达验证 | `tests/test_credential_proxy.py` | 容器内 `env` 不含 `ANTHROPIC_API_KEY`；`/proc/self/environ` 不含 key；API 调用仍正常工作 |
| 5.1.6 | E2E 验证 | `tests/test_e2e_local.py` | 现有 E2E 测试在 credential proxy 模式下全部通过 |

---

### 5.2 Skills 扩展系统——无代码扩展 Agent 能力（P0）

| # | 任务 | 文件 | 验收标准 |
| ---- | ---- | ---- | ---- |
| 5.2.1 | 定义 SKILL.md 规范 | `docs/skill-spec.md` | 文档描述 SKILL.md 格式（name、description、trigger、content 四段）、加载顺序、优先级规则 |
| 5.2.2 | Memory Manager 增加 skills 目录播种 | `src/memory.py` | `ensure_group_dirs()` 为每个 Group 创建 `skills/` 子目录；全局 `groups/skills/` 目录自动创建 |
| 5.2.3 | Agent Runner 加载 skills | `container/agent-runner/main.py` | 启动时扫描 `/workspace/global/skills/` + `/workspace/group/skills/`，将 SKILL.md 内容注入 system prompt |
| 5.2.4 | ContainerManager 挂载 skills 目录 | `src/container_manager.py` | 全局 skills 通过 global_dir 挂载（已覆盖）；Group skills 通过 group_dir 挂载（已覆盖）；确认路径可达 |
| 5.2.5 | 内置示例 skill | `groups/skills/code-review/SKILL.md` | 提供一个 code-review 示例技能，验证加载链路 |
| 5.2.6 | 测试：skill 加载与注入 | `tests/test_skills.py` | 验证 skill 文件被正确发现、解析、注入 system prompt；空目录不报错 |

---

### 5.3 安全配置外置（P1）

| # | 任务 | 文件 | 验收标准 |
| ---- | ---- | ---- | ---- |
| 5.3.1 | 新增安全配置文件 | `~/.config/lynxclaw/security.yaml` | 包含 `blocked_patterns`、`blocked_commands`、`allowed_env_prefixes` |
| 5.3.2 | Config 加载合并 | `src/config.py` | 优先读取外置安全配置；不存在时 fallback 到 `lynxclaw.config.yaml` 中的 security 段 |
| 5.3.3 | 从主配置移除安全段 | `lynxclaw.config.yaml` | security 段标记为 deprecated，保留作为 fallback |
| 5.3.4 | 测试 | `tests/test_config.py` | 外置配置优先；fallback 正常；两者都不存在时使用硬编码默认值 |

---

### 5.4 Sender Allowlist——消息预过滤（P2）

| # | 任务 | 文件 | 验收标准 |
| ---- | ---- | ---- | ---- |
| 5.4.1 | Group 配置增加 allowed_senders | `src/config.py` | `GroupConfig` 新增 `allowed_senders: list[str]`，默认空列表（不限制） |
| 5.4.2 | Router 增加 sender 检查 | `src/router.py` | trigger 匹配后、入队前检查 sender_id；不在白名单返回 `RouteResult.UNAUTHORIZED` |
| 5.4.3 | 测试 | `tests/test_router.py` | 白名单为空时所有 sender 通过；白名单非空时仅允许列表内 sender |

---

### 5.5 Channel 自注册（P2）

| # | 任务 | 文件 | 验收标准 |
| ---- | ---- | ---- | ---- |
| 5.5.1 | Adapter 模块增加 `create_adapter()` 工厂函数 | `src/channels/telegram.py`、`feishu.py` | 每个模块导出 `CHANNEL_NAME: str` 和 `create_adapter(config) -> Optional[ChannelAdapter]` |
| 5.5.2 | `discover_adapters()` 改为动态扫描 | `src/channels/registry.py` | 扫描 `src/channels/` 目录，动态导入含 `create_adapter` 的模块；缺少凭证返回 None 自动跳过 |
| 5.5.3 | 测试 | `tests/test_registry.py` | 新 adapter 只需创建文件 + 实现 `create_adapter()`，无需改其他文件 |

**触发条件**：Channel 数量增长到 4+ 时实施。

---

### Phase 5 依赖关系

```text
5.0.1 ──→ 5.1.* (Credential Proxy)
5.0.2 ──→ 5.2.* (Skills)
5.1.4 ──→ 5.3.* (安全配置外置，复用白名单机制)
5.1.* 和 5.2.* 互不依赖，可并行
5.4.* 和 5.5.* 独立，可随时实施
```

---

## 后续开发方向（待规划）

> 以下为潜在扩展方向，尚未立项。开始前需先写 ADR 或更新 ARCHITECTURE.md。

- **更多 IM 渠道**：Discord、Slack、微信（参考 [channel-development.md](channel-development.md)）
- **Web Dashboard**：可视化 Group 管理、任务调度、审计日志
- **Agent 市场**：预置 Agent 模板（代码助手、文档助手等）
- **多模型支持**：在 `run_agent()` 抽象层接入非 Claude 模型
- **Docker Sandbox / DinD 双层隔离**：高安全场景可选的 hypervisor 级隔离（参考 NanoClaw）
