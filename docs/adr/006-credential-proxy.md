# ADR-006: Credential Proxy — API Key 永不入容器

**状态**：已接受
**日期**：2026-03-21

---

## 决策

在宿主侧新增 Credential Proxy（`src/credential_proxy.py`），作为独立 HTTP 服务运行。容器不再接收 `ANTHROPIC_API_KEY` 环境变量，改为通过 `ANTHROPIC_BASE_URL` 指向宿主侧 Proxy，由 Proxy 注入真实凭证后转发到上游。

## 背景

当前 `ANTHROPIC_API_KEY` 通过 `-e` 直接注入容器环境变量。容器运行期间，Agent 可通过 `env` 命令或 `/proc/self/environ` 读取明文 key。虽然容器是临时的（`--rm`），但 prompt injection 攻击窗口存在。

NanoClaw 项目采用 HTTP Credential Proxy 方案：真实 API key 从未进入容器，从根本上消除了凭证泄露风险。

## 架构

```text
容器内 Agent
  └─ ANTHROPIC_BASE_URL=http://127.0.0.1:9099  (容器内 api_proxy)
       └─ 转发到 http://host.docker.internal:3001  (宿主侧 credential proxy)
            └─ 注入 x-api-key header
            └─ 转发到真实 upstream (api.anthropic.com 或第三方镜像)
```

- 容器内 `api_proxy.py` 的模型验证拦截功能保持不变
- 宿主侧 Credential Proxy 作为 daemon thread 运行，随主进程启停

## 考虑的方案

| 方案 | 优势 | 劣势 |
| ---- | ---- | ---- |
| **A: 环境变量直传（现状）** | 简单、零额外组件 | key 在容器内可见 |
| **B: Docker Secret** | Docker 原生支持 | 需要 Swarm 模式；文件仍可读 |
| **C: Credential Proxy（选定）** | key 永不入容器；兼容所有 OCI 运行时 | 多一个 HTTP 服务 |

选择方案 C：安全收益最大，且可复用现有 Proxy Sidecar 架构。

## 后果

- 容器环境变量中不再包含 `ANTHROPIC_API_KEY`
- 安全模型从"容器销毁后 key 消失"升级为"key 从未进入容器"
- 需要确保 `host.docker.internal` 在 Linux/macOS/Windows 上均可达
- 第三方镜像场景：Proxy 同时处理 base_url 转发 + 凭证注入
- 同步引入环境变量白名单（`_ALLOWED_ENV_PREFIXES`），防止其他敏感变量泄露

## 影响范围

- `src/credential_proxy.py`（新增）
- `src/container_manager.py`（环境变量白名单）
- `src/main.py`（启动 Proxy、移除 API key 传递）
- `container/agent-runner/api_proxy.py`（上游地址适配）
