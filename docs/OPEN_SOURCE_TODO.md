# 开源准备任务清单

> 可执行的任务追踪列表，与 [OPEN_SOURCE_READINESS.md](./OPEN_SOURCE_READINESS.md) 配套使用。  
> 状态：✅ P0/P1 已完成，待手动提交  
> 创建时间：2026-04-05  
> 更新时间：2026-04-06

---

## 使用说明

- `[ ]` 未开始
- `[/]` 进行中
- `[x]` 已完成

---

## P0 必须完成 ⚠️（开源前硬性要求）

### 文档修正
- [x] 1. README 测试数字修正（381 pass → 417 pass）
  - 完成时间：2026-04-06
  - 修改位置：`README.md` 第 173 行、第 238 行

### GitHub 社区基础设施
- [x] 2. 创建 `.github/ISSUE_TEMPLATE/bug_report.md`
  - 完成时间：2026-04-06
- [x] 3. 创建 `.github/ISSUE_TEMPLATE/feature_request.md`
  - 完成时间：2026-04-06
- [x] 4. 创建 `.github/ISSUE_TEMPLATE/config.yml`（禁用空白 issue）
  - 完成时间：2026-04-06
- [x] 5. 创建 `.github/PULL_REQUEST_TEMPLATE.md`
  - 完成时间：2026-04-06
- [x] 6. 创建 `.github/workflows/ci.yml`（Python 3.11/3.12 矩阵）
  - 完成时间：2026-04-06

### 社区规范文档
- [x] 7. 创建 `CONTRIBUTING.md`（开发环境、PR 流程、Channel Adapter 开发指南）
  - 完成时间：2026-04-06
- [x] 8. 创建 `CHANGELOG.md`（Keep a Changelog 格式，含 v0.1.0 初始内容）
  - 完成时间：2026-04-06
- [x] 9. 创建 `SECURITY.md`（漏洞报告、SLA、安全边界说明）
  - 完成时间：2026-04-06
- [x] 10. 创建 `CODE_OF_CONDUCT.md`（Contributor Covenant v2.1）
  - 完成时间：2026-04-06

**P0 验收标准**：
- [ ] GitHub Insights → Community Standards 全绿
- [ ] CI 在测试 PR 中正常运行并通过
- [ ] Issue/PR 模板在 GitHub 网页可正常加载

---

## P1 强烈建议 🟡（提升专业度）

- [x] 11. 创建英文 README（`README.en.md`）
  - 完成时间：2026-04-06
- [x] 12. README 添加徽章（CI / License / Python 3.11+）
  - 完成时间：2026-04-06
  - 备注：中文 README 顶部添加徽章 + 双语链接
- [x] 13. 创建 `.github/dependabot.yml`（pip weekly + actions monthly）
  - 完成时间：2026-04-06
- [/] 14. 创建 Git 标签 `v0.1.0` + GitHub Release
  - 状态：待手动执行（见下方"提交与发布"章节）
- [x] 15. 文档化分支策略（main=稳定, develop=开发, PR 合入 develop）
  - 完成时间：2026-04-06
  - 备注：已包含在 `CONTRIBUTING.md` 中

**P1 验收标准**：
- [ ] 徽章正常显示
- [ ] Release 页面有 v0.1.0 条目
- [ ] Dependabot 正常工作

---

## P2 可选优化 🟢（后续迭代）

- [x] 16. 创建 `.github/CODEOWNERS`
  - 完成时间：2026-04-06
- [x] 17. 完善 `pyproject.toml` 元数据（keywords、classifiers、作者信息）
  - 完成时间：2026-04-06
- [ ] 18. 集成代码覆盖率（pytest-cov + Codecov）
  - 状态：待后续迭代
  - 备注：需要在 CI 中添加覆盖率上传步骤
- [ ] 19. 配置 GHCR 镜像自动推送
  - 状态：待后续迭代
  - 备注：需要创建 `.github/workflows/docker.yml`

**P2 验收标准**：
- [ ] `pip install lynxclaw` 可用
- [ ] Codecov 报告上传正常
- [ ] `ghcr.io/lynxpurr/lynxclaw-agent` 镜像可拉取

---

## 最小可开源集合

以下 **7 项** 已 **全部完成**：

1. [x] README 数字修正
2. [x] Bug 报告模板
3. [x] PR 模板
4. [x] CI 工作流
5. [x] CONTRIBUTING.md
6. [x] CHANGELOG.md
7. [x] SECURITY.md

---

## 提交与发布（需手动执行）

### 1. 提交所有更改

```bash
# 确认所有文件已暂存
git status

# 提交
git add -A
git commit -m "chore: prepare for open source release

- Add GitHub community files (issue templates, PR template, CI)
- Add CONTRIBUTING.md, CHANGELOG.md, SECURITY.md, CODE_OF_CONDUCT.md
- Add English README with badges and Chinese/English links
- Update pyproject.toml with metadata (classifiers, keywords, URLs)
- Fix README test count (381 -> 417)
- Add Dependabot and CODEOWNERS configuration"

# 推送
git push origin develop
```

### 2. 创建版本标签

```bash
# 创建附注标签
git tag -a v0.1.0 -m "MVP Release: AI agent runtime with Telegram/Feishu support

Features:
- AI agent containerized runtime with hardened Docker security
- Telegram (aiogram) and Feishu (Lark) channel support
- Streaming responses with debouncing
- Web Dashboard with Bearer Token auth
- Credential Proxy for secure API key handling
- Skills system via Markdown injection
- 417 unit/integration tests"

# 推送标签到远程
git push origin v0.1.0
```

### 3. GitHub Release

推送标签后，前往 https://github.com/lynxpurr/lynxclaw/releases 创建 Release：

- **标题**: `v0.1.0 - MVP Release`
- **内容**: 复制 `CHANGELOG.md` 中 `[0.1.0]` 章节内容
- **Attach binaries**: 不需要（Python 项目）

---

## 最终开源检查清单

仓库设为 Public 前逐项确认：

```
□ 代码已 commit 并 push 到 develop 分支
□ 创建了 v0.1.0 标签并 push
□ GitHub Release 已创建
□ CI 状态为绿色（417+ tests pass）
□ CHANGELOG.md 中 v0.1.0 日期已填写
□ README 所有链接可正常跳转
□ .env.example 无真实凭证
□ LICENSE 文件存在且为 AGPL-3.0
□ Git 工作区 clean（无未提交修改）
□ GitHub 仓库设置为 Public
□ 仓库描述和 topics 已填写（建议: ai-agent docker telegram feishu python mcp）
□ Community Standards（Insights 页）所有项目为绿色
```

---

## 后续工作（开源后）

### 短期（1-2 周内）
- [ ] 配置代码覆盖率（Codecov）
- [ ] 配置 GHCR 镜像自动推送
- [ ] 添加更多 Channel Adapter（Discord、Slack 等）

### 中期（1-3 个月）
- [ ] PyPI 发布（`pip install lynxclaw`）
- [ ] 完善文档（更多示例、视频教程）
- [ ] 社区建设（讨论区、贡献者指南细化）

### 长期
- [ ] 多 Agent 协作（Swarm）增强
- [ ] 更多 IM 平台支持
- [ ] 可视化工作流编辑器

---

## 相关文档

- [OPEN_SOURCE_READINESS.md](./OPEN_SOURCE_READINESS.md) — 详细说明与参考模板
- [CLAUDE.md](/CLAUDE.md) — 开发指南
- [CONTRIBUTING.md](/CONTRIBUTING.md) — 贡献指南
- [CHANGELOG.md](/CHANGELOG.md) — 版本历史
- [SECURITY.md](/SECURITY.md) — 安全政策
