# ADR-005: 默认 Ephemeral + 可选 Resumable 容器策略

**状态**：已接受
**日期**：2026-03-17

---

## 决策

容器默认使用 **Ephemeral 模式**（`--rm`，用完即毁），通过 Claude Agent SDK 的 `resume=session_id` 机制支持 **Resumable 模式**（会话可恢复），**Persistent 模式**（常驻容器）留待 Phase 4。

## 背景

AI Agent 的容器生命周期直接影响安全性、资源消耗和用户体验。需要在「每次新建容器的安全性」和「保持上下文连续性的用户体验」之间找到平衡。

## 考虑的方案

| 方案 | 优势 | 劣势 |
| ---- | ---- | ---- |
| **Ephemeral + Resumable** | 安全（容器即用即毁）、资源友好、上下文可恢复 | 恢复时有冷启动延迟 |
| 纯 Ephemeral | 最安全、最简单 | 多轮对话丢失上下文 |
| 纯 Persistent | 零冷启动、状态完整保留 | 安全风险大（长驻容器可能被利用）、资源浪费 |

## 理由

1. **安全优先**：Ephemeral 容器每次请求后销毁，攻击面窗口极小。即使 Agent 被劫持，危害仅限于单次请求。
2. **Claude Agent SDK 原生支持**：SDK 提供 `resume=session_id` 参数，可在新容器中恢复之前的会话上下文，无需保持容器运行。
3. **资源高效**：不占用空闲容器资源。5 个并发 Group 不意味着 5 个常驻容器。
4. **简化运维**：无需处理容器健康检查、心跳保活、OOM 恢复等长驻容器问题。
5. **渐进式演进**：Ephemeral → Resumable → Persistent，每一步都在前一步的基础上增量添加。

## 权衡

- Resumable 模式依赖 Claude Agent SDK 的 session 持久化能力，如果 SDK API 变化需要适配。
- 每次请求有容器冷启动开销（约 1-3 秒），但 IM 场景下用户可接受。
- 会话恢复时 SDK 需要重建上下文（消耗 token），但比维护常驻容器的安全风险更合算。
- Persistent 模式适用于高频交互、长时间运行的特殊场景，在 Phase 4 中作为可选功能引入。

## 实现要点

- 宿主 `sessions` 表存储 `(group_name, session_id, last_active)`
- Agent Runner 执行完毕后返回 `session_id` → 宿主持久化到 DB
- 下次同 Group 消息 → 读取 `session_id` → 注入容器环境变量 → Agent SDK `resume=`
