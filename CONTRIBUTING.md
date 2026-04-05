# 贡献指南

感谢你对 Lynxclaw 的兴趣！本指南将帮助你快速开始贡献。

---

## 开发环境搭建

### 前置条件

- Python 3.11+
- Docker Engine 20.10+
- Git

### 快速开始

```bash
# 1. Fork 并克隆仓库
git clone https://github.com/YOUR_USERNAME/lynxclaw.git
cd lynxclaw

# 2. 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

# 3. 安装依赖
pip install -e ".[dev]"

# 4. 运行测试
python -m pytest tests/ --ignore=tests/test_e2e_local.py

# 5. 构建 Agent 镜像
docker build -t lynxclaw-agent:latest -f container/agent-runner/Dockerfile .
```

---

## 代码规范

### 风格指南

- 遵循 PEP 8
- 使用有意义的变量名
- 函数和类添加 docstring
- 复杂逻辑添加注释

### 测试要求

- 所有 PR 必须通过现有测试（417+ pass）
- 新功能需附带测试
- E2E 测试需要 Docker + API key，本地开发可跳过

```bash
# 单元/集成测试（必须通过）
python -m pytest tests/ --ignore=tests/test_e2e_local.py

# 完整测试（需要 Docker + API key）
python -m pytest tests/test_e2e_local.py
```

---

## 提交 PR 流程

### 分支策略

| 分支 | 用途 | 备注 |
|------|------|------|
| `main` | 稳定发布 | 只接受从 `develop` 合并 |
| `develop` | 开发集成 | PR 默认目标分支 |
| `feature/*` | 功能开发 | 从 `develop` 切出 |
| `fix/*` | Bug 修复 | 从 `develop` 切出 |

### 步骤

1. **创建功能分支**
   ```bash
   git checkout develop
   git pull origin develop
   git checkout -b feature/your-feature-name
   ```

2. **开发并提交**
   ```bash
   # 编写代码
   # 编写/更新测试
   # 运行测试确保通过
   git add .
   git commit -m "feat: 简短描述"
   ```

3. **保持同步**
   ```bash
   git fetch origin
   git rebase origin/develop
   ```

4. **推送并创建 PR**
   ```bash
   git push origin feature/your-feature-name
   ```
   然后在 GitHub 创建 PR，目标分支选 `develop`。

### Commit 规范

- `feat:` 新功能
- `fix:` Bug 修复
- `docs:` 文档更新
- `refactor:` 代码重构
- `test:` 测试相关
- `chore:` 构建/工具相关

---

## 架构约束

修改以下关键设计时，**必须**同步更新对应 ADR：

| 组件 | ADR 文件 |
|------|----------|
| IPC 文件系统通信 | `docs/adr/001-file-ipc.md` |
| Telegram aiogram 选择 | `docs/adr/002-aiogram.md` |
| 长连接 vs Webhook | `docs/adr/003-long-connection.md` |
| Proxy Sidecar 网络 | `docs/adr/004-proxy-sidecar.md` |
| 容器生命周期模式 | `docs/adr/005-resumable-containers.md` |
| Credential Proxy | `docs/adr/006-credential-proxy.md` |
| Skills 系统 | `docs/adr/007-skills-system.md` |
| SDK 抽象层 | `docs/adr/008-sdk-abstraction.md` |

---

## 添加新的 Channel Adapter

如果你想支持新的 IM 平台（如 Discord、Slack）：

1. 阅读 `docs/channel-development.md`
2. 在 `src/channels/` 创建新适配器，继承 `ChannelAdapter`
3. 在 `src/channels/registry.py` 注册
4. 在 `lynxclaw.config.yaml` 添加配置示例
5. 添加单元测试

---

## 获取帮助

- 查看 [CLAUDE.md](CLAUDE.md) 了解项目架构
- 查看 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 了解系统设计
- 在 [Discussions](https://github.com/lynxpurr/lynxclaw/discussions) 提问

---

## 行为准则

本项目遵循 [Contributor Covenant](CODE_OF_CONDUCT.md)。参与即表示你同意遵守。
