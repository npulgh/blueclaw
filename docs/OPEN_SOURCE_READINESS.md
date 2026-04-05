# 开源准备工作清单

> 本文档记录 Lynxclaw 开源前需要完成的准备工作及验收标准。
>
> 状态：🚧 进行中
> 最后更新：2026-04-05
> 目标版本：v0.1.0 (MVP)

---

## 一、当前状态速览

| 维度 | 状态 | 说明 |
|------|------|------|
| 项目定位 | ✅ 完成 | AI Agent 容器化运行平台，对接 Telegram/飞书 |
| 功能完整度 | ✅ 完成 | Phase 1-6 全部实现，417 个测试通过 |
| 代码规范 | ✅ 完成 | 26 个源文件，符合"小而可审计"原则 |
| 许可证 | ✅ 完成 | AGPL-3.0，已添加版权声明头 |
| 技术文档 | ✅ 完成 | 架构文档、8 份 ADR、调试指南齐全 |
| Git 仓库 | ✅ 完成 | 已配置远程 `github.com:lynxpurr/lynxclaw.git` |
| README 数字 | ⚠️ 需更新 | README 仍显示旧测试数（381），应更新为 417 |

---

## 二、必须完成 (P0) ⚠️

开源前**必须**完成的基础设施建设。

### 2.1 README 修正

- [ ] 将 README.md 中 `381 pass` 更新为 `417 pass`（`~22s`）

> 现状：README 的"运行测试"和"开发状态"两处数字滞后，外部贡献者第一眼就会看到不一致。

### 2.2 GitHub 社区基础设施

- [ ] 创建 `.github/ISSUE_TEMPLATE/bug_report.md`
- [ ] 创建 `.github/ISSUE_TEMPLATE/feature_request.md`
- [ ] 创建 `.github/ISSUE_TEMPLATE/config.yml`（关闭空白 issue，引导使用模板）
- [ ] 创建 `.github/PULL_REQUEST_TEMPLATE.md`
- [ ] 创建 `.github/workflows/ci.yml`（自动化测试）

**`ci.yml` 参考配置要点**：

```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
          cache: "pip"
      - run: pip install -e ".[dev]"
      - run: python -m pytest tests/ --ignore=tests/test_e2e_local.py -q
```

> 使用 `cache: "pip"` 加速依赖安装；排除 E2E 测试（依赖 Docker + API key）；矩阵覆盖最低和最新支持版本。

**验收标准**：
- Issue 模板能在 GitHub 网页正常加载
- PR 模板能自动填充
- CI 在每次 push/PR 时自动运行 pytest，结果可见

### 2.3 贡献指南

- [ ] 创建 `CONTRIBUTING.md`

**内容要求**：
- 开发环境搭建（Python 3.11+、Docker）
- 代码规范（遵循现有代码风格，参考 CLAUDE.md）
- 测试要求（`pytest tests/` 需 417+ pass，E2E 测试另需 Docker + API key）
- 提交 PR 流程（fork → feature branch → PR → CI green → review → merge）
- 如何添加新的 Channel Adapter（参考 `docs/channel-development.md`）
- 架构约束说明（修改关键设计须同步更新对应 ADR）

### 2.4 变更日志

- [ ] 创建 `CHANGELOG.md`

**格式**：采用 [Keep a Changelog](https://keepachangelog.com/) 规范，使用语义化版本。

**初始内容**：

```markdown
## [Unreleased]

## [0.1.0] - 2026-04-XX
### Added
- MVP 核心平台：Agent 容器化运行
- Telegram / 飞书双通道支持
- 流式响应（streaming，500ms / 200 chars 去抖）
- 定时任务调度（scheduler，croniter）
- Web Dashboard（只读 API + Alpine.js SPA）
- Proxy Sidecar 网络隔离架构
- Credential Proxy（ADR-006，API Key 不进容器环境变量）
- Skills 系统（Markdown 注入，ADR-007）
- 安全加固容器（cap-drop ALL, read-only rootfs, --network none）
```

### 2.5 安全政策

- [ ] 创建 `SECURITY.md`

**内容要求**：

- 支持的版本范围（明确哪些版本会收到安全更新）
- 漏洞报告方式（GitHub Security Advisories 私有报告，避免公开 issue）
- 响应 SLA（建议：确认 48h，严重漏洞修复 7 天，一般漏洞 30 天）
- 已知安全边界说明（容器隔离、IM 凭证不进入容器、AGPL 用户自托管须知）
- 致谢名单（预留 Hall of Fame）

### 2.6 行为准则

- [ ] 创建 `CODE_OF_CONDUCT.md`

GitHub 将其列为 [Community Health Files](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/creating-a-default-community-health-file) 之一，缺失时 Insights → Community Standards 会显示警告。

**建议**：直接采用 [Contributor Covenant v2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/)（业界标准，两段式：承诺 + 执行标准）。仅需填入项目联系邮件即可。

---

## 三、强烈建议 (P1) 🟡

显著提升项目专业度和用户体验。

### 3.1 英文文档支持

- [ ] 创建 `README.en.md`（英文版 README）
- [ ] 或：将主 README 改为英文，中文移至 `README.zh.md`

**建议**：采用方案 B（英文为主），更利于国际化。英文 README 应包含：

- 一句话定位 + 亮点列表
- Prerequisites / Quick Start（Telegram 路径）
- 架构图（可沿用现有 ASCII 图）
- 安全模型说明（这是项目差异化亮点，值得突出）
- 徽章区（见 3.2）

### 3.2 README 徽章

- [ ] CI 状态徽章
- [ ] 许可证徽章
- [ ] Python 版本徽章

```markdown
[![CI](https://github.com/lynxpurr/lynxclaw/actions/workflows/ci.yml/badge.svg)](https://github.com/lynxpurr/lynxclaw/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
```

> 注意：CI 徽章 URL 在 CI workflow 文件命名为 `ci.yml` 时才正确，与 2.2 节一致。

### 3.3 Dependabot 配置

- [ ] 创建 `.github/dependabot.yml`

自动提 PR 升级依赖，避免长期积累漏洞。

```yaml
version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "weekly"
    open-pull-requests-limit: 5
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "monthly"
```

> `open-pull-requests-limit: 5` 防止 Dependabot 刷屏；Actions 依赖变更频率低，月度即可。

### 3.4 版本标签

- [ ] 确认 `CHANGELOG.md` 日期后，创建 Git 标签 `v0.1.0`

```bash
git tag -a v0.1.0 -m "MVP Release: AI agent runtime with Telegram/Feishu support"
git push origin v0.1.0
```

> 建议在 GitHub 上同步创建 Release，附上 CHANGELOG 中的 [0.1.0] 条目作为 Release Notes。

### 3.5 默认分支策略

- [ ] 在 README（或 CONTRIBUTING.md）中说明分支策略
- [ ] 在 GitHub 仓库设置中开启 `develop` 为默认分支（或明确 `main` 的定位）

**当前现状**：代码在 `develop` 分支，建议在 README 中明确说明 `main` = 稳定发布，`develop` = 开发集成，PR 合入 `develop`。

---

## 四、可选优化 (P2) 🟢

锦上添花，可后续迭代。

### 4.1 CODEOWNERS

- [ ] 创建 `.github/CODEOWNERS`

自动指定 PR reviewer，防止 PR 无人处理。初始阶段可简单写 `* @lynxpurr`（全部文件指向维护者）。

### 4.2 PyPI 发布

- [ ] 完善 `pyproject.toml` 元数据（`keywords`、`homepage`、`classifiers`、作者信息）
- [ ] 注册 PyPI 项目名（抢占 `lynxclaw`）
- [ ] 配置 Trusted Publisher（GitHub Actions → PyPI，无需 token）

### 4.3 代码覆盖率

- [ ] 添加 `pytest-cov`
- [ ] 集成 Codecov（在 CI 中上传，免费 public repo）
- [ ] 在 README 添加覆盖率徽章

### 4.4 Docker Hub / GHCR

- [ ] 创建自动化工作流，在打 tag 时推送镜像到 GHCR（`ghcr.io/lynxpurr/lynxclaw-agent`）
- [ ] 在 README 添加镜像拉取命令

> GHCR 与 GitHub 仓库权限联动，比 Docker Hub 更简单，适合 GitHub 托管项目。

### 4.5 分支保护规则

- [ ] 在 GitHub 设置中为 `main`/`develop` 开启 Branch Protection：
  - Require status checks (CI pass) before merging
  - Require at least 1 approving review
  - Dismiss stale reviews when new commits are pushed

---

## 五、完成后验收

开源前逐项检查：

```
□ 所有 P0 任务完成（含 README 测试数字修正）
□ CI 测试通过（417+ tests pass，Python 3.11 和 3.12）
□ CHANGELOG.md 中 [0.1.0] 发布日期已填写
□ 文档链接有效（README 中所有相对路径链接可跳转）
□ .env.example 已脱敏（无真实凭证）
□ LICENSE 文件存在且为 AGPL-3.0
□ Git 仓库 clean（无未提交修改）
□ GitHub 仓库设置为 Public
□ 仓库描述和 topics 已填写（建议：ai-agent docker telegram feishu python）
□ Community Standards（Insights 页）所有项目为绿色
```

---

## 六、时间估算

| 优先级 | 任务 | 预计工作量 |
| ------ | ---- | --------- |
| P0 | README 修正 | 5 分钟 |
| P0 | GitHub 社区基础设施（模板 + CI） | 1.5–2 小时 |
| P0 | CONTRIBUTING.md | 1 小时 |
| P0 | CHANGELOG.md | 30 分钟 |
| P0 | SECURITY.md | 30 分钟 |
| P0 | CODE_OF_CONDUCT.md | 15 分钟 |
| P1 | 英文 README | 2–3 小时 |
| P1 | 徽章 + Dependabot | 30 分钟 |
| P1 | 版本标签 + Release | 15 分钟 |
| P2 | 各项优化 | 2–3 小时 |
| **总计** | | **8–11 小时** |

---

## 七、相关文档

- [CLAUDE.md](/CLAUDE.md) — 开发指南
- [ARCHITECTURE.md](/docs/ARCHITECTURE.md) — 系统架构
- [TASKS.md](/docs/TASKS.md) — 开发任务历史
- [channel-development.md](/docs/channel-development.md) — Channel Adapter 开发指南
- [LICENSE](/LICENSE) — AGPL-3.0 许可证
