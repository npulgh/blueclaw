# ADR-005: Container Hardening — 实战经验与遗留问题

> 状态: **已接受**
> 日期: 2026-03-19
> 背景: Phase 4 完成后，首次在完整 hardening 标志下运行 E2E 测试，暴露了多个隐性假设错误。

---

## 背景

Phase 4 实现了容器安全加固（`--read-only`、`--cap-drop ALL`、`--security-opt no-new-privileges:true`、`--pids-limit`、`--user 1000:1000`）。但 E2E 测试在 hardening 下全部失败，需要系统性排查。

---

## 发现的问题与修复

### 问题 1：Claude Code CLI 在 `--read-only` 下静默退出

**现象**: SDK 报 `Control request timeout: initialize`，CLI 进程在 0.5 秒内 exit 0，不写任何 stdout。

**根因**: Claude Code CLI 启动时无条件写 `~/.claude.json` 和 `~/.claude/backups/`。在 `--read-only` 下写失败，Node.js 静默退出（exit 0，无错误输出）。

**诊断路径**:
1. 先怀疑 proxy 路径问题 → 排除（proxy 200 OK）
2. 怀疑 stdin 问题 → 排除（pipe stdin 时 CLI 保持运行）
3. 对比有/无 `--read-only` → 确认根因
4. `find / -newer /proc/1 -type f` 找出 CLI 写的文件

**修复**: Dockerfile + `_build_command` 加 `--tmpfs /home/agent:rw,nosuid,size=64m,uid=1000,gid=1000`。

**关键细节**: tmpfs 必须指定 `uid=1000,gid=1000`，否则 agent 用户无写权限（默认 root 所有）。

---

### 问题 2：Volume 挂载点在 `--read-only` 下不存在

**现象**: `[Errno 30] Read-only file system: '/workspace/ipc'`，容器无法写 IPC outbox。

**根因**: Docker 在 `--read-only` 模式下，volume 挂载点必须在镜像中预先存在。`/workspace/ipc`、`/workspace/global` 等目录未在 Dockerfile 中创建，Docker 无法在只读根文件系统上动态创建挂载点。

**修复**: Dockerfile 加 `RUN mkdir -p /workspace/group /workspace/ipc /workspace/global /workspace/project /workspace/global_memory`。

**教训**: `--read-only` 容器的所有 volume 挂载点必须在构建时存在于镜像中。

---

### 问题 3：容器标识符 UUID 被误用为 Claude Code Session ID

**现象**: `No conversation found with session ID: <uuid>`，CLI 用 `--resume <uuid>` 失败。

**根因**: `ContainerManager.spawn()` 生成 UUID 作为容器追踪标识符（`sid`），并将其传给 `LYNXCLAW_SESSION_ID`。`container/agent-runner/main.py` 把任何非空的 `LYNXCLAW_SESSION_ID` 都当作 Claude Code session ID 来 `--resume`。

**修复**: `_build_command` 只在传入的 `session_id` 是真实的 Claude Code session ID（从 DB 读取）时才设置 `LYNXCLAW_SESSION_ID`；容器追踪 UUID 仅用于日志。

---

### 问题 4：ANTHROPIC_BASE_URL 未转发给容器

**现象**: 容器使用系统默认 Anthropic endpoint，忽略 `.env` 中配置的第三方兼容 API。

**根因**: `src/main.py` 构建 `env_vars` 时只传了 `ANTHROPIC_API_KEY`，未传 `ANTHROPIC_BASE_URL`。

**修复**: `src/main.py` 读取宿主机 `ANTHROPIC_BASE_URL` 并转发给容器。

---

## 遗留问题

### 遗留 1：`test_adapter_receives_reply` — API 返回空响应

**状态**: 未修复（外部依赖问题）

**现象**: 容器 exit 0，`agent done (streaming) chars=0`，`input=0 output=0`，outbox 无文件写入，adapter 收不到回复。

**根因**: 当前配置的 API proxy endpoint 接受请求但返回空响应体（0 tokens）。这是 API 端行为，不是代码 bug。

**复现条件**: `ANTHROPIC_BASE_URL` 指向某些第三方 proxy 时出现。

**解决方向**:
- 换用能正常返回内容的 API endpoint
- 或在 `container/agent-runner/main.py` 中检测 `chars=0` 并写一个错误 IPC 消息，避免 adapter 永久等待

---

### 遗留 2：Windows watchdog 对 Docker volume 的可靠性

**状态**: 已知风险，有 5 秒 periodic scan 兜底

**现象**: watchdog `on_moved`/`on_created` 事件在 Windows + Docker volume 挂载目录上可能不触发。

**当前缓解**: `ipc.py` 有 5 秒 periodic scan 作为 fallback。

**风险**: 5 秒延迟在高频场景下不可接受；periodic scan 在测试超时前可能来不及触发。

**解决方向**: 考虑将 IPC 从文件系统改为 Unix socket 或 TCP（但需要 ADR 评审，因为当前 IPC=filesystem 是架构约束）。

---

### 遗留 3：streaming chars=0 时无 IPC 写入

**状态**: ✅ 已修复（2026-03-20）

**现象**: API 返回空响应时，streaming 路径不写任何 outbox 文件，host 端 consumer 永久等待 IPC 消息，最终超时。

**影响**: 任何导致空响应的情况（API 错误、空 prompt、token 耗尽）都会导致 adapter 无响应。

**修复**: 在 `container/agent-runner/main.py` 的 streaming 路径末尾，若 `response` 为空，强制写一个 `send_message` IPC 文件（内容 `[Agent returned empty response]`）。同时修复了 ephemeral 和 persistent 两条路径。

---

### 问题 5（新增）：IPC 挂载路径双层 group

**状态**: ✅ 已修复（2026-03-20）

**现象**: 容器写 IPC 文件到 `data/ipc/{group}/{group}/outbox/`，host watcher 监听 `data/ipc/{group}/outbox/`，路径不匹配导致 adapter 收不到消息。

**根因**: host 端 `ipc_dir` = `data/ipc/{group}` 挂载到容器 `/workspace/ipc`。容器内 `ipc_bridge._get_outbox()` 拼接 `{ipc_base}/{group}/outbox` = `/workspace/ipc/{group}/outbox`，实际对应 host 的 `data/ipc/{group}/{group}/outbox`——多了一层 group。

**修复**: `src/main.py` 中 `ipc_dir` 从 `data/ipc/{group}` 改为 `data/ipc`（不带 group），挂载整个 IPC 根目录到 `/workspace/ipc`。容器内 `ipc_bridge` 拼接 `/{group}/outbox` 后路径与 host watcher 完全对齐。

**教训**: 容器挂载路径与容器内代码的路径拼接逻辑必须端到端验证。单独测试任一端都无法发现此类 off-by-one-directory 错误。

---

## 经验提炼

### Spec Coding 视角

1. **验收标准必须包含 hardening 环境**: 验收标准不能只要求"测试通过"，必须指定在完整 hardening 标志下运行。即：`docker run --read-only --cap-drop ALL ...` 下容器能正常启动和执行。

2. **第三方依赖的假设需显式记录**: Claude Code CLI 写 `~/.claude.json` 是未文档化的行为。类似的隐性假设（"CLI 需要可写 home 目录"）应在 spike 阶段发现并记录在 `spike/findings.md`。

3. **错误路径的 IPC 契约**: `container/agent-runner/main.py` 的错误路径（`sys.exit(1)`）会写 IPC 错误消息，但空响应路径不会。IPC 契约应规定：**任何执行路径结束时都必须写至少一个 outbox 文件**（成功或失败），否则 host 端无法区分"容器正在运行"和"容器已完成但无输出"。

5. **IPC 路径端到端验证**: 容器挂载路径与容器内代码的路径拼接逻辑必须端到端验证。host 传 `data/ipc/{group}` 挂载到 `/workspace/ipc`，容器内又拼 `/{group}/outbox`，导致双层 group。单独测试任一端都无法发现此类 off-by-one-directory 错误。必须在 E2E 测试中验证实际文件路径。

6. **空响应的 IPC 契约**: streaming 路径在 API 返回空响应时不写任何 outbox 文件，导致 host 端永久等待。IPC 契约应规定：**任何执行路径结束时都必须写至少一个 outbox 文件**（成功或失败），否则 host 端无法区分"容器正在运行"和"容器已完成但无输出"。

### Docker --read-only 检查清单

在为新容器添加 `--read-only` 之前，必须确认：

- [ ] 所有 volume 挂载点在 Dockerfile 中 `mkdir -p` 预创建
- [ ] 所有需要写入的目录有对应的 `--tmpfs` 挂载（含正确的 `uid=`/`gid=`）
- [ ] 第三方二进制（Node.js、Python 等）的写文件行为已通过 `find / -newer /proc/1` 验证
- [ ] tmpfs 的 `noexec` 标志与运行时需求兼容（Node.js JIT 需要可执行内存映射）

---

## Phase 5 实战经验（2026-03-21）

> Phase 5 引入 Credential Proxy（ADR-006）、Skills 系统（ADR-007）等功能。
> 手动 E2E 测试暴露了三个关键问题。

### 问题 6：Credential Proxy 绑定 127.0.0.1 + 容器 --network none

**现象**: 容器 exit_code=1，stdout 58 字节 `missing ANTHROPIC_API_KEY`。

**根因**: Credential Proxy 设计为容器通过 `http://host.docker.internal:3001` 访问 host 上的代理。但：
1. Credential Proxy 绑定 `127.0.0.1`，Docker bridge 网络无法访问 loopback
2. 容器 `--network none` 完全隔离，无法访问任何网络地址

**修复**:
- Credential Proxy 绑定改为 `0.0.0.0`
- 当 Credential Proxy 启用时，容器网络从 `none` 改为 `bridge`

**教训**: 设计容器→host 通信时，必须同时考虑 proxy 绑定地址和容器网络模式。`127.0.0.1` 只对 host 本地进程可达。

---

### 问题 7：Docker Desktop (Windows/WSL2) 无法解析 host.docker.internal

**现象**: 修复问题 6 后，容器仍然失败。`Name or service not known` — 容器内无法解析 `host.docker.internal`。`--add-host=host.docker.internal:host-gateway` 超时，`172.17.0.1`（bridge 网关）Connection refused。

**根因**: Docker Desktop for Windows 使用 WSL2 后端，网络拓扑与 Linux 原生 Docker 不同。容器→host 的网络路径不可靠。

**修复**: Credential Proxy 改为 opt-in（`LYNXCLAW_CREDENTIAL_PROXY=1`），默认关闭，回退到直接传 API key 的方式。

**教训**:
1. **跨平台网络假设是危险的** — `host.docker.internal` 在 Linux 原生 Docker 上可靠，在 Docker Desktop (Windows/macOS) 上不一定。架构设计必须有 fallback。
2. **安全增强功能必须可降级** — Credential Proxy 是安全增强，但不能因为它导致核心功能不可用。opt-in + fallback 是正确模式。

---

### 问题 8：容器内 API key 前置检查与 Proxy 模式冲突

**现象**: 修复网络问题后，容器仍然 exit_code=1，`missing ANTHROPIC_API_KEY`。

**根因**: `container/agent-runner/main.py` 在 `main()` 和 `persistent_loop()` 两处检查 `ANTHROPIC_API_KEY`，为空则 `sys.exit(1)`。Credential Proxy 模式下容器不再有 API key（by design），但前置检查不知道这一点。

**修复**: 当 `ANTHROPIC_BASE_URL` 存在时，用占位符 `sk-placeholder-credential-proxy` 绕过检查。SDK 只需要一个非空值来初始化，实际认证由 Credential Proxy 注入。

**教训**: 引入新的运行模式时，必须审查所有前置检查（`sys.exit`、`raise`、`assert`），确认它们在新模式下仍然合理。搜索 `sys.exit` 是最快的审查方式。

---

### 问题 9：Agent 回复泄露内部推理

**现象**: Bot 回复开头出现 `The user asked "天津在哪里"... Please provide a brief, accurate answer`，这是 agent 的内部思考，不应暴露给用户。

**根因**: `run_agent()` 没有设置 `system_prompt`，Claude Agent SDK 默认行为下 agent 会把推理过程混入输出文本。

**修复**: `ClaudeAgentOptions` 加 `system_prompt`，明确要求直接回复用户、不输出内部推理。

**教训**: 容器化 agent 必须有明确的 system prompt。没有 system prompt 的 agent 行为不可预测，尤其是输出格式。这不是"可选优化"，是必要配置。

---

## 经验提炼（更新）

### 容器→Host 通信检查清单

在设计容器访问 host 服务的架构时，必须确认：

- [ ] Host 服务绑定 `0.0.0.0`（不是 `127.0.0.1`）
- [ ] 容器网络模式允许访问 host（`bridge` 或 `host`，不能是 `none`）
- [ ] `host.docker.internal` 在目标平台上可解析（Linux 原生 Docker 需要 `--add-host`）
- [ ] 有 fallback 方案应对网络不通的情况（opt-in + 降级）
- [ ] 防火墙/安全组不阻止容器→host 端口

### 新运行模式引入检查清单

引入新的运行模式（如 Credential Proxy 模式）时：

- [ ] `grep -r "sys.exit" container/` — 审查所有前置检查
- [ ] 确认新模式下环境变量的存在/缺失不会触发意外退出
- [ ] 容器 stdout/stderr 在错误路径下有足够的诊断信息
- [ ] 错误日志同时打印 stdout 和 stderr（不只打印 stderr）

---

## 飞书渠道接入经验（2026-03-21）

> 飞书 WebSocket 长连接模式（ADR-003）首次实际接入。修复 3 个 bug，发现 1 个平台间歇性问题。

### 问题 10：lark_oapi 模块级 event loop 缓存导致 RuntimeError

**现象**: daemon thread 中 `ws.Client.start()` 抛 `RuntimeError: This event loop is already running`。

**根因**: `lark_oapi/ws/client.py` 第 26 行在模块加载时执行 `loop = asyncio.get_event_loop()` 缓存为模块级变量。后续 `start()` 用此缓存 loop 调用 `run_until_complete()`，而该 loop 已在主线程运行中。在子线程内 `asyncio.new_event_loop()` + `set_event_loop()` 无效，因为 SDK 不重新读取 `get_event_loop()`。

**修复**: Monkey-patch `lark_oapi.ws.client.loop = fresh_loop`。

**教训**: 第三方 SDK 模块级缓存 event loop 是常见陷阱。排查时先 `grep "get_event_loop\|run_until_complete"` SDK 源码。

---

### 问题 11：飞书 PATCH 消息类型不匹配 400 错误

**现象**: `code=230001, This message is NOT a card`。流式 edit_message 返回 400。

**根因**: `send_message()` 发纯文本，`edit_message()` 用 Interactive Card 格式 PATCH。飞书不允许跨类型修改。

**修复**: `_build_content()` 始终返回 Interactive Card，确保首条消息和后续编辑类型一致。

**教训**: 飞书消息编辑 API 要求消息类型前后一致。要支持流式编辑，首条消息必须是目标格式（卡片）。

---

### 问题 12：飞书 WebSocket 事件投递间歇性失败

**现象**: WebSocket Handshake OK，ping/pong 正常，但发消息后无事件到达。重启后有时恢复。

**根因**: 多因素叠加——
1. 飞书后台事件订阅需**手动单独添加**（权限导入不含事件订阅）
2. 每次修改权限/事件订阅后需**重新发布版本**
3. 飞书 WebSocket 平台侧存在间歇性事件投递延迟

**修复**: 确认配置完整 + 重新发布 + 重启 bot。

**教训**: 飞书的权限系统和事件订阅是**两个独立系统**。"权限已开通" ≠ "事件订阅已配置"。每次变更后必须重新发布版本。

---

### 飞书 Adapter 开发检查清单

- [ ] `lark_oapi.ws.client.loop` 已被 monkey-patch 为独立 event loop
- [ ] `_build_content()` 始终返回 Interactive Card（支持流式编辑）
- [ ] 飞书后台事件订阅已启用 + `im.message.receive_v1` 已添加
- [ ] 接收方式选择"长连接"
- [ ] 应用已发布（不是"开发中"）
- [ ] 修改配置后已重新发布版本
