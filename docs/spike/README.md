# Blueclaw — 技术验证 Spike

> 来源：[多引擎综合审视](../../notes/multi-engine-review.md)
> 状态：待执行
> 预计耗时：1-2 天
>
> 本 Spike 的目的是在写任何宿主代码之前，验证三个致命技术假设。
> 任何一项失败都需要重新评估对应模块的技术路线。
> 执行结果记录在 [findings.md](findings.md)。

---

## 为什么需要 Spike

Blueclaw 的架构建立在三个未经验证的核心假设上：

1. **Python Claude Agent SDK** 的 hooks / resume / MCP 机制可用且行为符合预期
2. **watchdog + Docker Volume** 的文件事件通知在 Windows Docker Desktop (WSL2) 上可靠
3. **Unix Socket** 跨 Docker 容器边界传递在目标平台上可行（Proxy Sidecar 前提）

这三个假设如果错误，影响范围分别是：安全模型、IPC 通信层、网络代理层。
在 3000 行代码写完后才发现假设错误，返工成本远高于提前 1-2 天验证。

---

## S1: Python Agent SDK 验证

### 验证目标

确认 `claude-agent-sdk` Python 包的三个关键能力与 TypeScript 版行为一致。

### 验证方法

创建临时脚本 `spike/sdk_verify.py`，在本地（非容器）环境中执行：

```python
# 伪代码结构，实际 API 以 SDK 文档为准
from claude_agent_sdk import Agent, hooks

# --- S1.1 Hooks 验证 ---
# 注册 PreToolUse hook，拦截包含 "rm -rf" 的 Bash 命令
# 触发一个会调用 Bash 的 prompt
# 验证 hook 回调被触发，命令被拦截

# --- S1.2 Resume 验证 ---
# 第一次 query()，获取 session_id
# 第二次 query()，传入 resume=session_id
# 验证第二次回复能引用第一次的上下文

# --- S1.3 MCP 验证 ---
# 注册一个自定义 MCP stdio tool（如 "write_file"）
# 发送一个会触发该 tool 的 prompt
# 验证 tool 被调用，结果正确返回
```

### 判定标准

| 子项 | 通过 | 失败 |
| ---- | ---- | ---- |
| S1.1 hooks | PreToolUse 回调被触发，返回 `block` 后命令未执行 | 回调未触发，或 block 无效 |
| S1.2 resume | 第二次回复明确引用第一次对话内容 | 第二次回复无上下文，或 `resume` 参数报错 |
| S1.3 MCP | 自定义 tool 被 Agent 调用，输出写入文件 | tool 未被识别，或 stdio 通信失败 |

### 失败决策矩阵

| 失败项 | 影响范围 | 替代方案 | 决策 |
| ---- | ---- | ---- | ---- |
| S1.1 hooks | 安全模型（T1.9 Agent Runner） | 容器外拦截：宿主在 IPC 层过滤危险 tool 调用 | 可继续，安全层下移到宿主 |
| S1.2 resume | Resumable 会话（T2.1） | 宿主自行管理对话历史，每次注入 system prompt | 可继续，但 token 消耗增加 |
| S1.3 MCP | IPC Bridge（T1.7） | Agent 直接写文件到 outbox（不经 MCP） | 可继续，但失去 tool 调用的结构化语义 |
| S1.1 + S1.3 同时失败 | 安全 + IPC 两个核心层 | — | **重新评估是否使用 Python SDK** |

---

## S2: watchdog + Docker Volume 验证

### 验证目标

确认 watchdog 文件事件在 Docker Volume 挂载场景下的可靠性和延迟。

### 验证方法

创建临时脚本 `spike/ipc_verify.py`：

**宿主端**：启动 watchdog Observer 监听 `spike/test_outbox/` 目录，记录每个文件创建事件的时间戳。

**容器端**：启动一个最小 Docker 容器，挂载 `spike/test_outbox/`，执行以下测试：

| 测试 | 方法 | 预期 |
| ---- | ---- | ---- |
| 基础事件 | 容器内 `echo > outbox/test.json` | 宿主 watchdog 收到事件 |
| 延迟测量 | 容器写入 100 个文件，记录写入时间和宿主接收时间 | P99 延迟 < 100ms |
| 高频并发 | 5 个容器同时各写 20 个文件（模拟 stream_chunk） | 零丢失，顺序正确 |
| 原子性 | 容器用 write-tmp + rename 模式写入 | 宿主不读到半写文件 |
| 平台差异 | 在 Windows Docker Desktop (WSL2) 上重复以上测试 | 结果与 Linux 一致 |

### 判定标准

| 指标 | 通过 | 失败 |
| ---- | ---- | ---- |
| 事件丢失率 | 0% | > 0% |
| P99 延迟 | < 100ms | > 500ms |
| 高频并发 | 100 个文件全部收到 | 有遗漏 |
| 原子性 | 未读到半写内容 | 读到不完整 JSON |

### 失败决策矩阵

| 失败项 | 影响范围 | 替代方案 | 决策 |
| ---- | ---- | ---- | ---- |
| 事件偶尔丢失（< 1%） | IPC 可靠性 | 增加 5 秒定时扫描兜底（已纳入 T1.7 设计修订） | 可继续，双保险模式 |
| 事件频繁丢失（> 5%） | IPC 不可用 | 回退到 NanoClaw 的轮询模式（每 1-2 秒扫描） | 可继续，放弃事件驱动 |
| 延迟过高（> 500ms） | 流式体验 | 轮询模式 + 更短间隔（500ms） | 可继续，流式体验降级 |
| Windows 特有问题 | 跨平台承诺 | 文档标注 Windows 需要轮询兜底 | 可继续，平台差异化处理 |

---

## S3: Unix Socket 跨容器验证（可选）

> 此项为 Phase 3 Proxy Sidecar 的前提，非 Phase 1 阻塞项。
> 如果时间允许，在 Spike 期间一并验证；否则推迟到 Phase 3 前。

### 验证目标

确认 `--network none` 容器通过 Docker Volume 挂载的 Unix Socket 能与宿主通信。

### 验证方法

宿主启动一个简单的 Unix Socket HTTP 代理，容器通过 `HTTP_PROXY=socks5h://...` 发起请求。

### 判定标准

| 指标 | 通过 | 失败 |
| ---- | ---- | ---- |
| 容器 → 宿主 Socket 通信 | 请求到达宿主代理 | 连接被拒或超时 |
| Windows Docker Desktop | 行为与 Linux 一致 | Socket 文件不可见或不可连接 |

### 失败决策矩阵

| 失败项 | 替代方案 |
| ---- | ---- |
| Unix Socket 不可用 | 改用 `--network` 自定义网络 + iptables 白名单（安全性降级，需更新 ADR-004） |

---

## 执行清单

- [ ] 安装 `claude-agent-sdk` Python 包，确认版本
- [ ] 执行 S1.1 hooks 验证 → 记录到 findings.md
- [ ] 执行 S1.2 resume 验证 → 记录到 findings.md
- [ ] 执行 S1.3 MCP 验证 → 记录到 findings.md
- [ ] 执行 S2 watchdog 基础事件 → 记录到 findings.md
- [ ] 执行 S2 延迟测量 → 记录到 findings.md
- [ ] 执行 S2 高频并发 → 记录到 findings.md
- [ ] 执行 S2 原子性 → 记录到 findings.md
- [ ] （可选）执行 S3 Unix Socket → 记录到 findings.md
- [ ] 汇总所有结果，做出 Go/No-Go 决策
- [ ] 如有失败项，更新 TASKS.md 中受影响的任务设计
