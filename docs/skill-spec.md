# Lynxclaw Skills 规范

> 参见 [ADR-007](adr/007-skills-system.md)

## 概述

Skills 是 Lynxclaw 的无代码扩展机制。用户通过编写 Markdown 文件为 Agent 添加领域知识、工具使用规则和行为指令，无需修改核心代码。

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

（以下为 Agent 可读的 Markdown 内容：领域知识、工具使用规则、行为指令等）
```

Frontmatter 为可选。如果省略，目录名作为 skill name。

## 加载规则

1. Agent Runner 启动时扫描 `/workspace/global/skills/` 和 `/workspace/group/skills/`
2. 全局 skills 先加载，Group skills 后加载
3. 同名 skill（目录名相同）时，Group 版本覆盖全局版本
4. SKILL.md 内容注入到 system prompt 中，位于 CLAUDE.md 之后
5. 空 skills 目录不报错

## 命名规范

- 目录名使用 kebab-case（如 `code-review`、`doc-writer`）
- SKILL.md 文件名固定为大写
