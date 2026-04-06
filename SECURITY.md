# 安全政策

## 支持的版本

| 版本 | 支持状态 |
|------|----------|
| 0.1.x | ✅ 接收安全更新 |
| < 0.1.0 | ❌ 不再支持 |

---

## 报告漏洞

**请勿**在公开的 Issue 中报告安全漏洞。

请通过以下方式私下报告：

1. **GitHub Security Advisories**（推荐）：
   - 前往 https://github.com/lynxpurr/lynxclaw/security/advisories/new
   - 创建私有安全建议

2. **邮件**（备用）：
   - security@lynxpurr.dev

请在报告中包含：
- 问题描述
- 复现步骤
- 影响版本
- 可能的修复建议（如有）

---

## 响应时间线

| 严重等级 | 确认时间 | 修复时间 |
|----------|----------|----------|
| 严重（远程代码执行、数据泄露） | 48 小时内 | 7 天内 |
| 高（权限绕过、拒绝服务） | 72 小时内 | 14 天内 |
| 中/低（其他安全问题） | 7 天内 | 30 天内 |

---

## 安全边界与架构

### 容器隔离

- 每个 Agent 调用在独立 Docker 容器中运行
- 默认参数：`--cap-drop ALL --read-only --network none --user 1000:1000`
- 资源限制：`--memory 512m --cpus 1.0 --pids-limit 256`

### 凭证管理

- **IM 凭证**（Telegram Token、飞书 App ID/Secret）**永不**进入容器
- **API Key**：支持两种模式
  - **Credential Proxy 模式**（推荐）：设置 `LYNXCLAW_CREDENTIAL_PROXY=1` 启用，API Key 由宿主侧 HTTP Proxy 注入请求头，**不进入容器环境变量**（参见 ADR-006）
  - **直传模式**（默认）：API Key 通过 `-e ANTHROPIC_API_KEY` 注入容器环境变量。容器为临时实例（`--rm`），销毁后 key 随之消失，但运行期间容器内进程可读取
- 用户自托管时必须自行保管 `.env` 文件

### 网络访问

- 默认无网络（`--network none`）
- 需要联网时通过 Proxy Sidecar，受限于域名白名单
- 容器内无法直接访问宿主机网络

### 文件系统

- 只读根文件系统（`--read-only`）
- 敏感路径被禁止挂载：`.ssh`, `.aws`, `.gnupg`, `.env`, `*.pem`, `*.key`
- 挂载使用 tmpfs，容器销毁后数据清除（持久化数据通过 IPC 传出）

### AGPL-3.0 须知

本项目采用 AGPL-3.0 许可证：

- **用户自托管**：修改代码后向用户提供服务，必须公开修改后的源代码
- **网络使用即分发**：通过网络使用本软件即视为分发，触发开源义务

---

## 安全更新

安全修复将通过以下渠道发布：

1. GitHub Security Advisories
2. GitHub Releases（标记为 `security`）
3. CHANGELOG.md 中的 `Security` 章节

建议开启 Watch → Releases 以接收通知。

---

## 致谢

感谢以下安全研究者：

*（预留 - 第一位报告有效漏洞者将在此列出）*
