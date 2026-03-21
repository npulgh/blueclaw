# Lynxclaw — 任务清单

> MVP 已完成（2026-03-19）。Phase 0–4 全部实现，381 测试通过。
> Phase 5 已完成（2026-03-21）。402 测试通过，手动 E2E 验证通过。
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
| Credential Proxy 默认关闭，opt-in 启用（Windows 网络限制） | [ADR-006](adr/006-credential-proxy.md) |
| Skills 通过 Markdown 注入 system prompt | [ADR-007](adr/007-skills-system.md) |

---

## 已知问题（Backlog）

### BUG-002: E2E 测试断言条件过严

**文件**: `tests/test_e2e_local.py`

**现象**: `test_adapter_receives_reply` 等待 `len(adapter.sent) > initial_sent + 1`（需要 thinking placeholder + 真实回复），当 API 返回空响应时只有 placeholder，测试超时。

**根因**: 测试假设 API 总是返回非空内容。使用第三方 proxy 时可能返回空响应。

**修复方向**: 断言改为 `len(adapter.sent) > initial_sent`（至少一条新消息），或换用可靠的 API endpoint。

---

## Phase 5：架构升级（已完成 ✅ 2026-03-21）

> 来源：NanoClaw 架构分析（2026-03-21）。详见 [ARCHITECTURE.md §十](ARCHITECTURE.md)。
> 分支: `feature-phase6` | 402 测试通过 | 手动 E2E 验证通过
> 原则：只引入 Lynxclaw 缺失且收益明确的设计，不照搬。

### 5.0 前置：ADR 编写 ✅

| # | 任务 | 验收标准 | 状态 |
| ---- | ---- | ---- | ---- |
| 5.0.1 | 编写 ADR-006: Credential Proxy 凭证隔离 | 记录决策动机、方案对比（env var vs proxy）、安全分析、迁移路径 | ✅ |
| 5.0.2 | 编写 ADR-007: Skills 扩展系统 | 记录设计选型（Markdown 注入 vs MCP tool vs git branch）、目录结构、加载机制 | ✅ |

---

### 5.1 Credential Proxy——凭证永不入容器（P0）✅

> 实现偏差：独立模块 `src/credential_proxy.py`（非扩展 `src/proxy.py`）。默认关闭（Windows Docker 网络限制）。

| # | 任务 | 文件 | 验收标准 | 状态 |
| ---- | ---- | ---- | ---- | ---- |
| 5.1.1 | ~~扩展 Proxy Sidecar~~ → 独立 Credential Proxy | `src/credential_proxy.py` | Proxy 接收请求 → 注入 `x-api-key` header → 转发上游 | ✅ |
| 5.1.2 | ContainerManager 移除 API key 环境变量 | `src/container_manager.py` | Credential Proxy 启用时容器不收到 `ANTHROPIC_API_KEY` | ✅ |
| 5.1.3 | 容器内 api_proxy 适配 | `container/agent-runner/api_proxy.py` | 模型验证拦截保持不变；上游地址改为 credential proxy endpoint | ✅ 无需改动，自动适配 |
| 5.1.4 | 环境变量白名单 | `src/container_manager.py` | `_ALLOWED_ENV_PREFIXES` 白名单过滤 | ✅ |
| 5.1.5 | 测试：凭证不可达验证 | `tests/test_credential_proxy.py` | Proxy 启停、header 注入、auth_token 转发 | ✅ 5 tests |
| 5.1.6 | E2E 验证 | 手动测试 | Credential Proxy 模式下端到端通过 | ⚠️ Windows 不可用，Linux 待验证 |

---

### 5.2 Skills 扩展系统——无代码扩展 Agent 能力（P0）✅

| # | 任务 | 文件 | 验收标准 | 状态 |
| ---- | ---- | ---- | ---- | ---- |
| 5.2.1 | 定义 SKILL.md 规范 | `docs/skill-spec.md` | 文档描述 SKILL.md 格式、加载顺序、优先级规则 | ✅ |
| 5.2.2 | Memory Manager 增加 skills 目录播种 | `src/memory.py` | `ensure_group_dirs()` 创建 `skills/` 子目录 | ✅ |
| 5.2.3 | Agent Runner 加载 skills | `container/agent-runner/main.py` | `load_skills()` + `_inject_skills()` 注入 system prompt | ✅ |
| 5.2.4 | ContainerManager 挂载 skills 目录 | `src/container_manager.py` | 通过 global_dir / group_dir 父目录挂载覆盖 | ✅ 隐式覆盖 |
| 5.2.5 | 内置示例 skill | `groups/skills/code-review/SKILL.md` | code-review 示例技能 | ✅ |
| 5.2.6 | 测试：skill 加载与注入 | `tests/test_skills.py` | 7 tests: 全局/Group/覆盖/空目录/多技能/frontmatter | ✅ |

---

### 5.3 安全配置外置（P1）✅

| # | 任务 | 文件 | 验收标准 | 状态 |
| ---- | ---- | ---- | ---- | ---- |
| 5.3.1 | 新增安全配置文件 | `~/.config/lynxclaw/security.yaml` | 包含 `blocked_patterns`、`blocked_commands` | ✅ |
| 5.3.2 | Config 加载合并 | `src/config.py` | 外置优先 → 内联 fallback → 硬编码默认值 | ✅ |
| 5.3.3 | 从主配置移除安全段 | `lynxclaw.config.yaml` | security 段标记为 deprecated | ⚠️ 未标记 deprecated 注释（功能正常） |
| 5.3.4 | 测试 | `tests/test_config.py` | 外置覆盖、fallback、默认值 3 个场景 | ✅ |

---

### 5.4 Sender Allowlist——消息预过滤（P2）✅

| # | 任务 | 文件 | 验收标准 | 状态 |
| ---- | ---- | ---- | ---- | ---- |
| 5.4.1 | Group 配置增加 allowed_senders | `src/config.py` | `GroupConfig.allowed_senders: list[str]`，默认空 | ✅ |
| 5.4.2 | Router 增加 sender 检查 | `src/router.py` | `RouteResult.DENIED`，trigger 匹配后、入队前检查 | ✅ |
| 5.4.3 | 测试 | `tests/test_router.py` | denied / allowed / empty-list 三种场景 | ✅ 3 tests |

---

### 5.5 Channel 自注册（P2）✅

| # | 任务 | 文件 | 验收标准 | 状态 |
| ---- | ---- | ---- | ---- | ---- |
| 5.5.1 | Adapter 模块增加 `create_adapter()` 工厂函数 | `telegram.py`、`feishu.py` | `CHANNEL_NAME` + `create_adapter()` | ✅ |
| 5.5.2 | `discover_adapters()` 改为动态扫描 | `src/channels/registry.py` | `pkgutil.iter_modules` 扫描 | ✅ |
| 5.5.3 | 测试 | `tests/test_registry.py` | 注册、查找、生命周期 | ✅ |

---

### 5.6 调试期间追加的修复 ✅

| # | 任务 | 文件 | 说明 | 状态 |
| ---- | ---- | ---- | ---- | ---- |
| 5.6.1 | Agent system_prompt | `container/agent-runner/main.py` | 防止内部推理泄露到用户回复 | ✅ |
| 5.6.2 | 错误日志增加 stdout | `src/main.py` | `consumer.nonzero_exit` 同时打印 stdout+stderr | ✅ |
| 5.6.3 | Credential Proxy 绑定 0.0.0.0 | `src/credential_proxy.py` | Docker bridge 网络可达 | ✅ |
| 5.6.4 | 容器网络模式联动 | `src/container_manager.py` | Credential Proxy 启用时自动切换 bridge | ✅ |
| 5.6.5 | Agent runner placeholder key | `container/agent-runner/main.py` | `ANTHROPIC_BASE_URL` 存在时用占位符绕过 key 检查 | ✅ |

---

### Phase 5 依赖关系

```text
5.0.1 ──→ 5.1.* (Credential Proxy)
5.0.2 ──→ 5.2.* (Skills)
5.1.4 ──→ 5.3.* (安全配置外置，复用白名单机制)
5.1.* 和 5.2.* 互不依赖，可并行
5.4.* 和 5.5.* 独立，可随时实施
```

### Phase 5 已知限制

| 限制 | 影响 | 解决方案 |
| ---- | ---- | -------- |
| Credential Proxy 在 Docker Desktop (Windows/WSL2) 不可用 | 容器无法通过 `host.docker.internal` 访问 host | 默认关闭，`LYNXCLAW_CREDENTIAL_PROXY=1` opt-in |
| Agent system_prompt 硬编码在 `run_agent()` | 无法通过配置文件自定义 | 后续可从 CLAUDE.md 或 Skills 注入 |

### Phase 5 实战经验

详见 [ADR-005 §Phase 5 实战经验](adr/ADR-005-container-hardening-lessons.md)。核心教训：

1. **Docker 网络拓扑因平台而异** — 设计容器→host 通信时必须考虑 Linux/macOS/Windows 三种 Docker 后端的差异，提供 fallback
2. **新运行模式必须审查所有 `sys.exit`** — 引入 Credential Proxy 模式后，容器内 `missing ANTHROPIC_API_KEY` 检查变成了误杀
3. **Agent 必须有 system prompt** — 没有 system prompt 的 LLM agent 输出不可预测，内部推理会泄露到用户回复

---

## 后续开发方向（待规划）

> 以下为潜在扩展方向，尚未立项。开始前需先写 ADR 或更新 ARCHITECTURE.md。

- **更多 IM 渠道**：Discord、Slack、微信（参考 [channel-development.md](channel-development.md)）
- **Web Dashboard**：可视化 Group 管理、任务调度、审计日志
- **Agent 市场**：预置 Agent 模板（代码助手、文档助手等）
- **多模型支持**：在 `run_agent()` 抽象层接入非 Claude 模型
- **Docker Sandbox / DinD 双层隔离**：高安全场景可选的 hypervisor 级隔离（参考 NanoClaw）
