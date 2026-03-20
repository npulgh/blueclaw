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

1. **验收标准必须包含 hardening 环境**: 当前 TASKS.md 的验收标准只要求"测试通过"，未指定在完整 hardening 标志下运行。应在 Phase 4 的验收标准中明确：`docker run --read-only --cap-drop ALL ...` 下容器能正常启动和执行。

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
