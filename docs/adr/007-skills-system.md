# ADR-007: Skills 扩展系统 — 无代码扩展 Agent 能力

**状态**：已接受
**日期**：2026-03-21

---

## 决策

采用 Markdown 注入方案：用户通过编写 `SKILL.md` 文件为 Agent 添加领域知识和行为指令。Agent Runner 启动时自动扫描 skills 目录，将内容注入 system prompt。

## 背景

Agent 能力完全由 `container/agent-runner/main.py` + hooks 决定。用户无法在不改代码的情况下扩展 Agent 行为（如添加领域知识、自定义工具使用规则）。NanoClaw 通过 skills 目录实现了无代码扩展。

## 目录结构

```text
groups/
  skills/                          # 全局技能（所有 Group 可用）
    {skill-name}/SKILL.md
  {group-name}/
    skills/                        # Group 专属技能
      {skill-name}/SKILL.md
```

## SKILL.md 格式

```markdown
---
name: skill-name
description: 一句话描述技能用途
---

（Agent 可读的 Markdown 内容：领域知识、工具使用规则、行为指令等）
```

## 加载规则

1. Agent Runner 启动时扫描 `/workspace/global/skills/` 和 `/workspace/group/skills/`
2. 全局 skills 先加载，Group skills 后加载
3. 同名 skill（目录名相同）时，Group 版本覆盖全局版本
4. SKILL.md 内容注入到 system prompt 中，位于 CLAUDE.md 之后
5. 空 skills 目录不报错；frontmatter 可选

## 考虑的方案

| 方案 | 优势 | 劣势 |
| ---- | ---- | ---- |
| **A: Markdown 注入（选定）** | 零代码、用户友好、与 CLAUDE.md 体系一致 | 能力受限于 prompt |
| **B: MCP Tool 注册** | 结构化、可编程 | 需要写代码、增加复杂度 |
| **C: Git Branch（NanoClaw）** | 可改核心代码 | 过于复杂、不适合非开发者用户 |

选择方案 A：与 Lynxclaw 的 CLAUDE.md 记忆体系一脉相承，用户学习成本最低。

## 后果

- 用户可通过写 Markdown 文件扩展 Agent 能力，无需修改核心代码
- `src/memory.py` 需要在 `ensure_group_dirs()` 中播种 `skills/` 目录
- `container/agent-runner/main.py` 需要增加 skill 加载和 prompt 注入逻辑
- 挂载路径已被现有 `global_dir` 和 `group_dir` 覆盖，无需额外挂载配置

## 影响范围

- `src/memory.py`（skills 目录播种）
- `container/agent-runner/main.py`（skill 加载 + prompt 注入）
- `docs/skill-spec.md`（新增规范文档）
- `groups/skills/`（示例 skill）
