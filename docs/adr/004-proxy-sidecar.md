# ADR-004: 使用 Proxy Sidecar 而非 --network bridge

**状态**：已接受
**日期**：2026-03-17

---

## 决策

容器需要联网时，通过**宿主侧 Proxy Sidecar + Unix Socket** 提供受控出站访问，而非使用 Docker `--network bridge` 模式。

## 背景

Agent 容器默认 `--network none` 完全断网。部分场景（如 Web 搜索）需要有限的网络访问能力。需要在安全隔离和功能需求之间找到平衡。

## 考虑的方案

| 方案 | 优势 | 劣势 |
| ---- | ---- | ---- |
| **Proxy Sidecar** | 域名粒度控制、请求日志、速率限制 | 实现复杂度略高 |
| `--network bridge` | Docker 原生支持、零额外代码 | 容器可访问任意地址、无法细粒度控制 |
| iptables 规则 | Linux 原生、高性能 | Windows 不兼容、规则管理复杂 |
| VPN / 网络命名空间 | 强隔离 | 过度工程化、增加运维复杂度 |

## 理由

1. **域名级粒度控制**：Proxy 可配置白名单（如只允许 `api.anthropic.com`、`*.google.com`），精确到域名。
2. **全量请求日志**：每个出站请求的 URL、大小、耗时都可记录，便于审计和排查。
3. **防数据外泄**：即使 Agent 被 prompt injection 劫持，也无法向白名单外的域名发送数据。
4. **速率限制**：可在 Proxy 层实施请求频率限制，防止 Agent 滥用网络。
5. **跨平台一致性**：Unix Socket 挂载在 Linux 和 Windows Docker 上行为一致（通过 Docker Volume）。

## 权衡

- 增加一个宿主进程组件（Proxy Sidecar），但实现相对简单（约 200 行 Python）。
- Unix Socket 通信有轻微性能开销，但对 Web 搜索这类场景可忽略。
- 容器内需要配置 `HTTP_PROXY` 环境变量，但这是一次性设置。
- 白名单配置需要维护，但提供了显式的安全边界。
