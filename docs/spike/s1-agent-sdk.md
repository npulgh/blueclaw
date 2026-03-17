# S1: Python Agent SDK 验证

> 状态：**已完成 — GO** ✓（2026-03-17）
> 执行结果记录在 [findings.md](findings.md) § S1

---

## 验证目标

确认 `claude-agent-sdk` Python 包的三个关键能力与 TypeScript 版行为一致。

## 验证方法

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

## 判定标准

| 子项 | 通过 | 失败 |
| ---- | ---- | ---- |
| S1.1 hooks | PreToolUse 回调被触发，返回 `block` 后命令未执行 | 回调未触发，或 block 无效 |
| S1.2 resume | 第二次回复明确引用第一次对话内容 | 第二次回复无上下文，或 `resume` 参数报错 |
| S1.3 MCP | 自定义 tool 被 Agent 调用，输出写入文件 | tool 未被识别，或 stdio 通信失败 |

## 失败决策矩阵

| 失败项 | 影响范围 | 替代方案 | 决策 |
| ---- | ---- | ---- | ---- |
| S1.1 hooks | 安全模型（T1.9 Agent Runner） | 容器外拦截：宿主在 IPC 层过滤危险 tool 调用 | 可继续，安全层下移到宿主 |
| S1.2 resume | Resumable 会话（T2.1） | 宿主自行管理对话历史，每次注入 system prompt | 可继续，但 token 消耗增加 |
| S1.3 MCP | IPC Bridge（T1.7） | Agent 直接写文件到 outbox（不经 MCP） | 可继续，但失去 tool 调用的结构化语义 |
| S1.1 + S1.3 同时失败 | 安全 + IPC 两个核心层 | — | **重新评估是否使用 Python SDK** |

## 执行清单

- [x] 安装 `claude-agent-sdk` Python 包，确认版本
- [x] 执行 S1.1 hooks 验证 → 记录到 findings.md
- [x] 执行 S1.2 resume 验证 → 记录到 findings.md
- [x] 执行 S1.3 MCP 验证 → 记录到 findings.md
