# ADR-004: 使用 Proxy Sidecar 容器而非 --network bridge

**状态**：已接受（Spike S3 验证，2026-03-18 更新）
**日期**：2026-03-17

---

## 决策

容器需要联网时，通过**独立 Proxy Sidecar 容器 + Unix Socket**提供受控出站访问，而非使用 Docker `--network bridge` 模式。

> **Spike S3 关键更新**（2026-03-18）：Sidecar 必须是容器（Linux 环境），不能是 Windows 宿主进程。原因：Windows Python 3.12.10 (win32) 无 `socket.AF_UNIX`，宿主进程无法创建 Unix Socket 服务端。

## 背景

Agent 容器默认 `--network none` 完全断网。部分场景（如 Web 搜索）需要有限的网络访问能力。需要在安全隔离和功能需求之间找到平衡。

## 考虑的方案

| 方案 | 优势 | 劣势 |
| ---- | ---- | ---- |
| **Proxy Sidecar 容器** | 域名粒度控制、请求日志、速率限制、跨平台兼容 | 多一个容器组件 |
| `--network bridge` | Docker 原生支持、零额外代码 | 容器可访问任意地址、无法细粒度控制 |
| iptables 规则 | Linux 原生、高性能 | Windows 不兼容、规则管理复杂 |
| VPN / 网络命名空间 | 强隔离 | 过度工程化、增加运维复杂度 |

## 理由

1. **域名级粒度控制**：Proxy 可配置白名单（如只允许 `api.anthropic.com`、`*.google.com`），精确到域名。
2. **全量请求日志**：每个出站请求的 URL、大小、耗时都可记录，便于审计和排查。
3. **防数据外泄**：即使 Agent 被 prompt injection 劫持，也无法向白名单外的域名发送数据。
4. **速率限制**：可在 Proxy 层实施请求频率限制，防止 Agent 滥用网络。
5. **跨平台兼容（Spike 验证）**：Proxy Sidecar 作为容器（Linux 内核）运行，Unix Socket 通过共享 Docker Volume 暴露，容器间通信 P50=0.060ms / P99=0.064ms。Windows 宿主 Python 无 AF_UNIX，必须用容器而非宿主进程。

## 权衡

- 增加一个 Proxy Sidecar 容器组件，但实现相对简单（约 200 行 Python）。
- Unix Socket 通信有轻微性能开销，Spike 实测 P99=0.064ms，对 Web 搜索场景可忽略。
- 容器内需要配置 `HTTP_PROXY` 环境变量指向 socket 路径，但这是一次性设置。
- 白名单配置需要维护，但提供了显式的安全边界。

## 实现拓扑

```text
agent 容器 (--network none)
    └─ HTTP_PROXY=http://proxy → /proxy/proxy.sock
              ↑ Docker Volume 共享（-v proxy_vol:/proxy）
Proxy Sidecar 容器 (lynxclaw-proxy, --network bridge)
    └─ 监听 /proxy/proxy.sock
    └─ 出站请求 → 域名白名单过滤 → Internet
```

Proxy Sidecar 与 Agent 容器共享 `proxy_vol` Volume，socket 文件在该 Volume 中创建。

## 实验验证（Spike S3，2026-03-18）

| 指标 | 结果 |
| ---- | ---- |
| 容器→容器 Unix Socket 通信 | **PASS** — P50=0.060ms，P99=0.064ms |
| Windows 宿主 Python AF_UNIX | **无** — `socket.AF_UNIX` 属性缺失（win32 平台） |
| Sidecar 必须是容器 | **确认** — 宿主进程方案在 Windows 上不可行 |

