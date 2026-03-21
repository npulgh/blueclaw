# Phase 5 架构升级实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实施 NanoClaw 借鉴的 P0-P2 架构升级：Credential Proxy、Skills 系统、安全配置外置、环境变量白名单、Sender Allowlist、Channel 自注册。

**Architecture:** 六个独立模块按优先级实施。P0（Credential Proxy + Skills）可并行；P1（安全配置外置 + 环境变量白名单）依赖 P0 的白名单机制；P2（Sender Allowlist + Channel 自注册）独立。

**Tech Stack:** Python 3.11 / asyncio / Docker / SQLite / structlog / pytest

---

## File Structure

### New Files
- `src/credential_proxy.py` — 宿主侧 HTTP 凭证注入代理
- `tests/test_credential_proxy.py` — 凭证代理测试
- `tests/test_skills.py` — Skills 加载测试
- `docs/skill-spec.md` — SKILL.md 格式规范
- `docs/adr/006-credential-proxy.md` — ADR-006
- `docs/adr/007-skills-system.md` — ADR-007
- `groups/skills/code-review/SKILL.md` — 示例 skill

### Modified Files
- `src/proxy.py` — 扩展支持凭证注入模式
- `src/container_manager.py` — 环境变量白名单 + credential proxy URL
- `src/main.py` — 移除直接传递 API key，改用 credential proxy URL
- `src/config.py` — 新增 GroupConfig.allowed_senders + 安全配置外置
- `src/memory.py` — 新增 skills/ 目录播种
- `src/router.py` — 新增 RouteResult.UNAUTHORIZED + sender 检查
- `src/channels/registry.py` — 自注册模式
- `src/channels/telegram.py` — 增加 create_adapter() 工厂
- `src/channels/feishu.py` — 增加 create_adapter() 工厂
- `container/agent-runner/main.py` — 加载 skills 注入 system prompt
- `tests/test_router.py` — sender allowlist 测试
- `tests/test_config.py` — 安全配置外置测试
- `tests/test_memory.py` — skills 目录测试
- `tests/test_container.py` — 环境变量白名单测试
- `tests/test_registry.py` — 自注册测试

---

## Task 1: ADR-006 Credential Proxy

**Files:**
- Create: `docs/adr/006-credential-proxy.md`

- [ ] **Step 1: Write ADR-006**

```markdown
# ADR-006: Credential Proxy — API Key 永不入容器

## 状态
已接受 (2026-03-21)

## 背景
当前 `ANTHROPIC_API_KEY` 通过 `-e` 直接注入容器环境变量。容器运行期间，Agent 可通过
`env` 命令或 `/proc/self/environ` 读取明文 key。虽然容器是临时的（`--rm`），但 prompt
injection 攻击窗口存在。

NanoClaw 项目采用 HTTP Credential Proxy 方案：真实 API key 从未进入容器。

## 决策
在宿主侧新增 Credential Proxy（`src/credential_proxy.py`），作为独立 HTTP 服务运行：
1. 监听 `127.0.0.1:<port>`（默认 3001）
2. 容器收到 `ANTHROPIC_BASE_URL=http://host.docker.internal:<port>`
3. Proxy 拦截请求，注入 `x-api-key` header，转发到真实上游
4. 容器内 `api_proxy.py` 的模型验证拦截功能保持不变

## 方案对比
| 方案 | 优点 | 缺点 |
| ---- | ---- | ---- |
| A: 环境变量直传（现状） | 简单 | key 在容器内可见 |
| B: Docker Secret | 原生支持 | 需要 Swarm 模式；文件仍可读 |
| C: Credential Proxy（选定） | key 永不入容器 | 多一个 HTTP 服务 |

## 后果
- 容器环境变量中不再包含 `ANTHROPIC_API_KEY`
- 安全模型从"容器销毁后 key 消失"升级为"key 从未进入容器"
- 需要确保 `host.docker.internal` 在 Linux/macOS/Windows 上均可达
- 第三方镜像场景：Proxy 同时处理 base_url 转发 + 凭证注入
```

- [ ] **Step 2: Commit ADR-006**

```bash
git add docs/adr/006-credential-proxy.md
git commit -m "docs: ADR-006 credential proxy — API key 永不入容器"
```

---

## Task 2: ADR-007 Skills System

**Files:**
- Create: `docs/adr/007-skills-system.md`

- [ ] **Step 1: Write ADR-007**

```markdown
# ADR-007: Skills 扩展系统 — 无代码扩展 Agent 能力

## 状态
已接受 (2026-03-21)

## 背景
Agent 能力完全由 `container/agent-runner/main.py` + hooks 决定。用户无法在不改代码的
情况下扩展 Agent 行为。NanoClaw 通过 `.claude/skills/` 目录实现了 Markdown 驱动的技能扩展。

## 决策
采用 Markdown 注入方案：
1. 技能文件为 `SKILL.md`，存放在 `groups/skills/`（全局）和 `groups/{name}/skills/`（Group 级）
2. Agent Runner 启动时扫描 skills 目录，将内容注入 system prompt
3. 加载顺序：全局 skills → Group skills（同名时 Group 覆盖全局）

## 方案对比
| 方案 | 优点 | 缺点 |
| ---- | ---- | ---- |
| A: Markdown 注入（选定） | 零代码、用户友好 | 能力受限于 prompt |
| B: MCP Tool 注册 | 结构化、可编程 | 需要写代码 |
| C: Git Branch（NanoClaw） | 可改核心代码 | 过于复杂 |

## SKILL.md 格式
```
---
name: skill-name
description: 一句话描述
---
# Skill Content
（Agent 可读的 Markdown 内容）
```

## 后果
- 用户可通过写 Markdown 文件扩展 Agent 能力
- 核心代码保持精简
- 需要在 Agent Runner 中增加 skill 加载逻辑
```

- [ ] **Step 2: Commit ADR-007**

```bash
git add docs/adr/007-skills-system.md
git commit -m "docs: ADR-007 skills 扩展系统 — 无代码扩展 Agent 能力"
```

---
## Task 3: Credential Proxy 实现

**Files:**
- Create: `src/credential_proxy.py`
- Create: `tests/test_credential_proxy.py`

- [ ] **Step 1: Write failing tests for CredentialProxy**

File: `tests/test_credential_proxy.py`

Tests to write:
- `test_injects_api_key`: Proxy injects `x-api-key` header into forwarded requests
- `test_strips_incoming_auth`: Proxy strips any auth header the container sends
- `test_preserves_path`: Request path forwarded to upstream unchanged
- `test_base_url_for_container`: Returns URL with `host.docker.internal` and port
- `test_start_stop`: Proxy starts and stops cleanly

Test fixtures:
- `_start_fake_upstream(port)`: HTTPServer that echoes headers back as JSON
- `fake_upstream` fixture on port 19876
- `proxy` fixture: `CredentialProxy(upstream, api_key="sk-real-secret-key", port=19877)`

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_credential_proxy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.credential_proxy'`

- [ ] **Step 3: Implement CredentialProxy**

File: `src/credential_proxy.py`

Class `CredentialProxy`:
- `__init__(self, upstream: str, api_key: str, port: int = 3001, auth_token: str = "")`
- `start(self) -> None`: Start HTTPServer in daemon thread, wait for port ready
- `stop(self) -> None`: Shutdown server
- `port` property: Return configured port
- `base_url_for_container` property: Return `http://host.docker.internal:{port}`

Internal `_ProxyHandler(BaseHTTPRequestHandler)`:
- `do_GET` / `do_POST` / `do_PUT` / `do_DELETE`: All delegate to `_proxy(method)`
- `_proxy(method)`: Read body, strip `Authorization`/`x-api-key` headers, inject real `x-api-key`, forward to upstream, relay response back
- `log_message`: Silence access logs, use structlog instead

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_credential_proxy.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/credential_proxy.py tests/test_credential_proxy.py
git commit -m "feat: credential proxy — API key never enters containers"
```

---

## Task 4: Integrate Credential Proxy into Host

**Files:**
- Modify: `src/main.py:342-366`
- Modify: `src/container_manager.py:324-337`
- Modify: `tests/test_container.py`

- [ ] **Step 1: Write failing test for env var whitelist**

File: `tests/test_container.py` — add new test:

```python
def test_env_whitelist_blocks_unknown_vars(manager):
    """Environment variables not matching allowed prefixes are blocked."""
    cmd = manager._build_command(
        group_name="test",
        env_vars={
            "LYNXCLAW_GROUP": "test",
            "ANTHROPIC_BASE_URL": "http://proxy:3001",
            "AWS_SECRET_KEY": "should-be-blocked",
            "RANDOM_VAR": "also-blocked",
        },
        mounts={},
        session_id="sid",
    )
    env_str = " ".join(cmd)
    assert "LYNXCLAW_GROUP" in env_str
    assert "ANTHROPIC_BASE_URL" in env_str
    assert "AWS_SECRET_KEY" not in env_str
    assert "RANDOM_VAR" not in env_str
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_container.py::test_env_whitelist_blocks_unknown_vars -v`
Expected: FAIL — `AWS_SECRET_KEY` found in command

- [ ] **Step 3: Add env var whitelist to ContainerManager**

File: `src/container_manager.py` — add after line 293:

```python
# Environment variable prefix whitelist — only vars matching these prefixes
# are forwarded to containers. Prevents accidental credential leakage.
_ALLOWED_ENV_PREFIXES = (
    "ANTHROPIC_",
    "LYNXCLAW_",
    "CLAUDE_CODE_DISABLE_",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "IPC_BASE_DIR",
)
```

Modify `_build_command` env section (line ~336):

```python
for k, v in {**builtin_env, **env_vars}.items():
    if not any(k.startswith(p) for p in _ALLOWED_ENV_PREFIXES):
        log.warning("container.env_blocked", key=k, group=group_name)
        continue
    cmd += ["-e", f"{k}={v}"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_container.py -v`
Expected: All PASS

- [ ] **Step 5: Modify main.py to use credential proxy**

File: `src/main.py` — in `_group_consumer`, replace env_vars block (lines 342-366):

Before:
```python
env_vars = {
    "ANTHROPIC_API_KEY": config.anthropic_api_key,
    "LYNXCLAW_CHAT_ID": msg.chat_id,
}
base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
if base_url:
    env_vars["ANTHROPIC_BASE_URL"] = base_url
```

After:
```python
env_vars = {
    "LYNXCLAW_CHAT_ID": msg.chat_id,
}
# Credential proxy provides API key — container never sees it directly
if hasattr(config, '_credential_proxy') and config._credential_proxy:
    env_vars["ANTHROPIC_BASE_URL"] = config._credential_proxy.base_url_for_container
else:
    # Fallback: direct key (for testing or when proxy is disabled)
    env_vars["ANTHROPIC_API_KEY"] = config.anthropic_api_key
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    if base_url:
        env_vars["ANTHROPIC_BASE_URL"] = base_url
```

Also in `_start()` function, add credential proxy startup before channel start:

```python
# Start credential proxy
from src.credential_proxy import CredentialProxy
upstream = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
cred_proxy = CredentialProxy(
    upstream=upstream,
    api_key=config.anthropic_api_key,
    auth_token=os.environ.get("ANTHROPIC_AUTH_TOKEN", ""),
)
cred_proxy.start()
config._credential_proxy = cred_proxy
```

Forward remaining env vars (auth_token, disable_nonessential) through proxy, not container.

- [ ] **Step 6: Run full test suite**

Run: `python -m pytest tests/ --ignore=tests/test_e2e_local.py -v`
Expected: All existing tests PASS

- [ ] **Step 7: Commit**

```bash
git add src/credential_proxy.py src/container_manager.py src/main.py tests/test_credential_proxy.py tests/test_container.py
git commit -m "feat: integrate credential proxy — ANTHROPIC_API_KEY removed from containers"
```
---

## Task 5: Skills 系统 — SKILL.md 规范 + 目录播种

**Files:**
- Create: `docs/skill-spec.md`
- Modify: `src/memory.py`
- Modify: `tests/test_memory.py`
- Create: `groups/skills/code-review/SKILL.md`

- [ ] **Step 1: Write SKILL.md 规范文档**

File: `docs/skill-spec.md`

```markdown
# Lynxclaw Skills 规范

## 概述
Skills 是 Lynxclaw 的无代码扩展机制。用户通过编写 Markdown 文件为 Agent 添加领域知识、
工具使用规则和行为指令，无需修改核心代码。

## 目录结构
groups/
  skills/                          # 全局技能（所有 Group 可用）
    {skill-name}/SKILL.md
  {group-name}/
    skills/                        # Group 专属技能
      {skill-name}/SKILL.md

## SKILL.md 格式
---
name: skill-name
description: 一句话描述技能用途
---

（以下为 Agent 可读的 Markdown 内容：领域知识、工具使用规则、行为指令等）

## 加载规则
1. Agent Runner 启动时扫描 `/workspace/global/skills/` 和 `/workspace/group/skills/`
2. 全局 skills 先加载，Group skills 后加载
3. 同名 skill（目录名相同）时，Group 版本覆盖全局版本
4. SKILL.md 内容注入到 system prompt 中，位于 CLAUDE.md 之后
5. 空 skills 目录不报错

## 命名规范
- 目录名使用 kebab-case（如 `code-review`、`doc-writer`）
- SKILL.md 文件名固定为大写
```

- [ ] **Step 2: Write failing test for skills directory seeding**

File: `tests/test_memory.py` — add:

```python
def test_ensure_group_dirs_creates_skills_dirs(tmp_path: Path) -> None:
    """skills/ directories are created for global and each group."""
    groups_dir = str(tmp_path / "groups")
    ensure_group_dirs(groups_dir, ["main", "support"])
    assert (Path(groups_dir) / "skills").is_dir()
    assert (Path(groups_dir) / "main" / "skills").is_dir()
    assert (Path(groups_dir) / "support" / "skills").is_dir()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_memory.py::test_ensure_group_dirs_creates_skills_dirs -v`
Expected: FAIL — `skills` directory not created

- [ ] **Step 4: Update memory.py to create skills directories**

File: `src/memory.py` — in `ensure_group_dirs()`, add after global CLAUDE.md seeding (line 72):

```python
# Seed global skills directory
(groups_path / "skills").mkdir(exist_ok=True)
```

And inside the per-group loop, after `(group_path / "files").mkdir(...)` (line 81):

```python
(group_path / "skills").mkdir(exist_ok=True)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_memory.py -v`
Expected: All PASS

- [ ] **Step 6: Create example skill**

File: `groups/skills/code-review/SKILL.md`

```markdown
---
name: code-review
description: 代码审查助手，提供结构化的代码审查反馈
---

# Code Review Skill

你是一个代码审查助手。当用户请求代码审查时，按以下结构提供反馈：

## 审查维度
1. **正确性**：逻辑错误、边界条件、空值处理
2. **安全性**：注入风险、凭证泄露、权限问题
3. **可维护性**：命名、复杂度、重复代码
4. **性能**：不必要的循环、内存泄露、阻塞调用

## 输出格式
- 每个问题标注严重级别：🔴 Critical / 🟡 Warning / 🔵 Suggestion
- 引用具体行号
- 提供修复建议（不只是指出问题）
```

- [ ] **Step 7: Commit**

```bash
git add docs/skill-spec.md src/memory.py tests/test_memory.py groups/skills/code-review/SKILL.md
git commit -m "feat: skills directory seeding + SKILL.md spec + example skill"
```

---

## Task 6: Agent Runner 加载 Skills

**Files:**
- Modify: `container/agent-runner/main.py`
- Create: `tests/test_skills.py`

- [ ] **Step 1: Write failing tests for skill loading**

File: `tests/test_skills.py`

```python
"""Tests for skill loading in agent runner."""

from __future__ import annotations

from pathlib import Path

import pytest


def _create_skill(base: Path, name: str, content: str) -> None:
    """Helper: create a SKILL.md file."""
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")


# Import the function we'll create
def _try_import():
    from container.agent_runner_skills import load_skills
    return load_skills


class TestLoadSkills:
    def test_loads_global_skills(self, tmp_path):
        global_skills = tmp_path / "global" / "skills"
        _create_skill(global_skills, "review", "---\nname: review\n---\nReview content")
        load_skills = _try_import()
        result = load_skills(str(global_skills), "")
        assert len(result) == 1
        assert "Review content" in result[0]["content"]

    def test_loads_group_skills(self, tmp_path):
        group_skills = tmp_path / "group" / "skills"
        _create_skill(group_skills, "helper", "---\nname: helper\n---\nHelper content")
        load_skills = _try_import()
        result = load_skills("", str(group_skills))
        assert len(result) == 1
        assert "Helper content" in result[0]["content"]

    def test_group_overrides_global(self, tmp_path):
        global_skills = tmp_path / "global" / "skills"
        group_skills = tmp_path / "group" / "skills"
        _create_skill(global_skills, "review", "---\nname: review\n---\nGlobal version")
        _create_skill(group_skills, "review", "---\nname: review\n---\nGroup version")
        load_skills = _try_import()
        result = load_skills(str(global_skills), str(group_skills))
        assert len(result) == 1
        assert "Group version" in result[0]["content"]

    def test_empty_dirs_no_error(self, tmp_path):
        load_skills = _try_import()
        result = load_skills(str(tmp_path / "nonexistent"), "")
        assert result == []

    def test_missing_frontmatter_still_loads(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skill_dir = skills_dir / "simple"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("Just plain content", encoding="utf-8")
        load_skills = _try_import()
        result = load_skills(str(skills_dir), "")
        assert len(result) == 1
        assert "Just plain content" in result[0]["content"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_skills.py -v`
Expected: FAIL — import error

- [ ] **Step 3: Implement load_skills in agent runner**

File: `container/agent-runner/main.py` — add function before `run_agent()`:

```python
def load_skills(global_skills_dir: str, group_skills_dir: str) -> list[dict]:
    """Load SKILL.md files from global and group skills directories.

    Returns list of {"name": str, "description": str, "content": str}.
    Group skills override global skills with the same directory name.
    """
    skills: dict[str, dict] = {}  # name -> skill dict

    for skills_dir in [global_skills_dir, group_skills_dir]:
        if not skills_dir:
            continue
        skills_path = Path(skills_dir)
        if not skills_path.is_dir():
            continue
        for skill_dir in sorted(skills_path.iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                continue
            raw = skill_file.read_text(encoding="utf-8")
            name = skill_dir.name
            description = ""
            content = raw

            # Parse optional YAML frontmatter
            if raw.startswith("---"):
                parts = raw.split("---", 2)
                if len(parts) >= 3:
                    for line in parts[1].strip().splitlines():
                        if line.startswith("name:"):
                            name = line.split(":", 1)[1].strip()
                        elif line.startswith("description:"):
                            description = line.split(":", 1)[1].strip()
                    content = parts[2].strip()

            skills[name] = {
                "name": name,
                "description": description,
                "content": content,
            }

    return list(skills.values())
```

- [ ] **Step 4: Inject skills into system prompt**

File: `container/agent-runner/main.py` — in `main()`, before `run_agent()` call:

```python
# Load skills from mounted directories
global_skills_dir = "/workspace/global/skills"
group_skills_dir = "/workspace/group/skills"
skills = load_skills(global_skills_dir, group_skills_dir)

if skills:
    skills_prompt = "\n\n# Active Skills\n"
    for s in skills:
        skills_prompt += f"\n## {s['name']}\n{s['content']}\n"
    # Prepend skills to the user prompt so they appear in system context
    prompt = skills_prompt + "\n\n---\n\n" + prompt
    log.info("skills.loaded", count=len(skills), names=[s["name"] for s in skills])
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_skills.py -v`
Expected: All PASS

- [ ] **Step 6: Run full test suite**

Run: `python -m pytest tests/ --ignore=tests/test_e2e_local.py -v`
Expected: All PASS (existing tests unaffected)

- [ ] **Step 7: Commit**

```bash
git add container/agent-runner/main.py tests/test_skills.py
git commit -m "feat: agent runner loads SKILL.md files into system prompt"
```
---

## Task 7: 安全配置外置

**Files:**
- Modify: `src/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing test for external security config**

File: `tests/test_config.py` — add:

```python
def test_security_config_from_external_file(tmp_path, monkeypatch):
    """Security config loaded from ~/.config/lynxclaw/security.yaml when present."""
    yaml_file = _write_yaml(tmp_path, MINIMAL_YAML)
    env_file = _write_env(tmp_path, "ANTHROPIC_API_KEY=sk-test-key\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # Write external security config
    sec_dir = tmp_path / "config_home" / "lynxclaw"
    sec_dir.mkdir(parents=True)
    sec_file = sec_dir / "security.yaml"
    sec_file.write_text(
        "blocked_patterns:\n  - '*.pem'\n  - '*.key'\n  - '.secret'\n"
        "allowed_env_prefixes:\n  - ANTHROPIC_\n  - LYNXCLAW_\n",
        encoding="utf-8",
    )

    cfg = load_config(yaml_file, dotenv_path=env_file, security_config_path=str(sec_file))
    assert "*.pem" in cfg.security.blocked_patterns
    assert ".secret" in cfg.security.blocked_patterns


def test_security_config_fallback_to_inline(tmp_path, monkeypatch):
    """When external security config is absent, fall back to inline config."""
    yaml_content = MINIMAL_YAML + "security:\n  blocked_patterns:\n    - '*.env'\n"
    yaml_file = _write_yaml(tmp_path, yaml_content)
    env_file = _write_env(tmp_path, "ANTHROPIC_API_KEY=sk-test-key\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    cfg = load_config(yaml_file, dotenv_path=env_file, security_config_path="/nonexistent/path")
    assert "*.env" in cfg.security.blocked_patterns


def test_security_config_defaults_when_both_absent(tmp_path, monkeypatch):
    """When neither external nor inline security config exists, use hardcoded defaults."""
    yaml_file = _write_yaml(tmp_path, MINIMAL_YAML)
    env_file = _write_env(tmp_path, "ANTHROPIC_API_KEY=sk-test-key\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    cfg = load_config(yaml_file, dotenv_path=env_file, security_config_path="/nonexistent/path")
    # Should have default blocked patterns
    assert len(cfg.security.blocked_patterns) > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_config.py::test_security_config_from_external_file -v`
Expected: FAIL — `load_config()` does not accept `security_config_path`

- [ ] **Step 3: Implement external security config loading**

File: `src/config.py` — modify `load_config()` signature:

```python
def load_config(
    yaml_path: str | Path,
    *,
    dotenv_path: str | Path | None = None,
    security_config_path: str | None = None,
) -> Config:
```

Add after YAML parsing, before env var overrides:

```python
# 4.5 Load external security config (priority: external > inline > defaults)
if security_config_path is None:
    # Default location: ~/.config/lynxclaw/security.yaml
    security_config_path = str(
        Path.home() / ".config" / "lynxclaw" / "security.yaml"
    )

_sec_path = Path(security_config_path)
if _sec_path.is_file():
    with open(_sec_path, encoding="utf-8") as f:
        sec_data = yaml.safe_load(f) or {}
    if "blocked_patterns" in sec_data:
        cfg.security.blocked_patterns = sec_data["blocked_patterns"]
    if "allowed_env_prefixes" in sec_data:
        cfg.security.allowed_env_prefixes = sec_data.get("allowed_env_prefixes", [])
```

Also add `allowed_env_prefixes` field to `SecurityConfig`:

```python
@dataclass
class SecurityConfig:
    blocked_patterns: list[str] = field(default_factory=lambda: [
        ".ssh", ".gnupg", ".aws", "*.pem", "*.key", ".env", "credentials",
    ])
    allowed_env_prefixes: list[str] = field(default_factory=lambda: [
        "ANTHROPIC_", "LYNXCLAW_", "CLAUDE_CODE_DISABLE_",
        "HTTP_PROXY", "HTTPS_PROXY", "IPC_BASE_DIR",
    ])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_config.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/config.py tests/test_config.py
git commit -m "feat: external security config — ~/.config/lynxclaw/security.yaml"
```

---

## Task 8: Sender Allowlist

**Files:**
- Modify: `src/config.py`
- Modify: `src/router.py`
- Modify: `tests/test_router.py`

- [ ] **Step 1: Write failing tests for sender allowlist**

File: `tests/test_router.py` — add:

```python
@pytest.mark.asyncio
async def test_route_unauthorized_sender(db):
    """Messages from senders not in allowlist are rejected."""
    cfg = _make_config(groups=[
        GroupConfig(
            name="restricted",
            channel="telegram",
            chat_id="12345",
            is_main=False,
            trigger="",
            allowed_senders=["user_100", "user_200"],
        )
    ])
    r = MessageRouter()
    await r.init(db, cfg)
    msg = _msg(chat_id="12345", sender_id="user_999", text="hello")
    result = await r.route(msg)
    assert result == RouteResult.UNAUTHORIZED


@pytest.mark.asyncio
async def test_route_allowed_sender(db):
    """Messages from senders in allowlist are accepted."""
    cfg = _make_config(groups=[
        GroupConfig(
            name="restricted",
            channel="telegram",
            chat_id="12345",
            is_main=False,
            trigger="",
            allowed_senders=["user_100"],
        )
    ])
    r = MessageRouter()
    await r.init(db, cfg)
    msg = _msg(chat_id="12345", sender_id="user_100", text="hello")
    result = await r.route(msg)
    assert result == RouteResult.QUEUED


@pytest.mark.asyncio
async def test_route_empty_allowlist_permits_all(db):
    """Empty allowed_senders list means no restriction."""
    cfg = _make_config(groups=[
        GroupConfig(
            name="open",
            channel="telegram",
            chat_id="12345",
            is_main=False,
            trigger="",
            allowed_senders=[],
        )
    ])
    r = MessageRouter()
    await r.init(db, cfg)
    msg = _msg(chat_id="12345", sender_id="anyone", text="hello")
    result = await r.route(msg)
    assert result == RouteResult.QUEUED
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_router.py::test_route_unauthorized_sender -v`
Expected: FAIL — `GroupConfig` has no `allowed_senders` field

- [ ] **Step 3: Add allowed_senders to GroupConfig**

File: `src/config.py` — modify `GroupConfig`:

```python
@dataclass
class GroupConfig:
    name: str
    channel: str
    chat_id: str
    is_main: bool = False
    trigger: str = "@bot"
    token_budget: int = 0
    container_mode: str = "ephemeral"
    allowed_senders: list[str] = field(default_factory=list)
```

Update `_parse_groups()` to read `allowed_senders` from YAML.

- [ ] **Step 4: Add UNAUTHORIZED to RouteResult and sender check to Router**

File: `src/router.py`:

```python
class RouteResult(Enum):
    QUEUED = auto()
    DUPLICATE = auto()
    NO_GROUP = auto()
    NO_TRIGGER = auto()
    BACKPRESSURE = auto()
    UNAUTHORIZED = auto()  # Sender not in allowed_senders list
```

In `route()` method, after trigger matching (line ~136), before backpressure check:

```python
# 3.5 Sender allowlist — if configured, only allow listed senders
allowed = await self._db.get_group_allowed_senders(group_name)
if allowed and msg.sender_id not in allowed:
    log.debug(
        "router.unauthorized_sender",
        group=group_name,
        sender_id=msg.sender_id,
    )
    return RouteResult.UNAUTHORIZED
```

Alternative (simpler, no DB change): store allowed_senders in router memory during init:

```python
# In init():
self._allowed_senders: dict[str, list[str]] = {}
for group_cfg in config.groups:
    self._allowed_senders[group_cfg.name] = group_cfg.allowed_senders

# In route(), after trigger matching:
allowed = self._allowed_senders.get(group_name, [])
if allowed and msg.sender_id not in allowed:
    log.debug("router.unauthorized_sender", group=group_name, sender_id=msg.sender_id)
    return RouteResult.UNAUTHORIZED
```

Use the simpler approach (no DB change needed).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_router.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/config.py src/router.py tests/test_router.py
git commit -m "feat: sender allowlist — per-group message pre-filtering"
```

---

## Task 9: Channel 自注册

**Files:**
- Modify: `src/channels/registry.py`
- Modify: `src/channels/telegram.py`
- Modify: `src/channels/feishu.py`
- Modify: `tests/test_registry.py`

- [ ] **Step 1: Write failing test for auto-discovery**

File: `tests/test_registry.py` — add:

```python
def test_discover_adapters_uses_create_adapter(monkeypatch):
    """discover_adapters calls create_adapter on each module."""
    from src.channels import registry
    from src.config import Config, TelegramConfig

    cfg = Config()
    cfg.anthropic_api_key = "sk-test"
    cfg.telegram = TelegramConfig(enabled=True, bot_token="tok")

    adapters = registry.discover_adapters(cfg)
    # Should find telegram adapter via create_adapter
    assert "telegram" in adapters
```

- [ ] **Step 2: Add create_adapter to telegram.py**

File: `src/channels/telegram.py` — add at module level:

```python
CHANNEL_NAME = "telegram"

def create_adapter(config: "Config") -> "Optional[ChannelAdapter]":
    """Factory function for auto-discovery. Returns None if not configured."""
    if not config.telegram.enabled:
        return None
    return TelegramAdapter()
```

- [ ] **Step 3: Add create_adapter to feishu.py**

File: `src/channels/feishu.py` — add at module level:

```python
CHANNEL_NAME = "feishu"

def create_adapter(config: "Config") -> "Optional[ChannelAdapter]":
    """Factory function for auto-discovery. Returns None if not configured."""
    if not config.feishu.enabled:
        return None
    return FeishuAdapter()
```

- [ ] **Step 4: Update discover_adapters to use create_adapter**

File: `src/channels/registry.py` — replace `discover_adapters()`:

```python
def discover_adapters(config: "Config") -> dict[str, "ChannelAdapter"]:
    """Auto-discover adapters by scanning channel modules for create_adapter().

    Each adapter module should define:
    - CHANNEL_NAME: str
    - create_adapter(config) -> Optional[ChannelAdapter]

    Returns None from create_adapter to skip (missing credentials, disabled).
    """
    import importlib
    import pkgutil
    import src.channels as channels_pkg

    adapters: dict[str, ChannelAdapter] = {}

    for importer, modname, ispkg in pkgutil.iter_modules(channels_pkg.__path__):
        if modname in ("registry", "__init__"):
            continue
        try:
            mod = importlib.import_module(f"src.channels.{modname}")
        except ImportError as e:
            logger.warning("discover_adapters: failed to import %s: %s", modname, e)
            continue

        factory = getattr(mod, "create_adapter", None)
        name = getattr(mod, "CHANNEL_NAME", modname)
        if factory is None:
            continue

        try:
            adapter = factory(config)
        except Exception as e:
            logger.warning("discover_adapters: %s.create_adapter() failed: %s", name, e)
            continue

        if adapter is not None:
            adapters[name] = adapter
            logger.debug("discover_adapters: %s enabled", name)

    logger.info("discover_adapters: found %d adapter(s): %s", len(adapters), list(adapters))
    return adapters
```

- [ ] **Step 5: Run all tests**

Run: `python -m pytest tests/ --ignore=tests/test_e2e_local.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/channels/registry.py src/channels/telegram.py src/channels/feishu.py tests/test_registry.py
git commit -m "feat: channel auto-registration via create_adapter() factory"
```

---

## Task 10: Final Integration + Docs Update

**Files:**
- Modify: `docs/TASKS.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ --ignore=tests/test_e2e_local.py -v`
Expected: All PASS (existing 381 + new tests)

- [ ] **Step 2: Update TASKS.md — mark Phase 5 tasks complete**

Mark all implemented tasks with checkmarks in TASKS.md.

- [ ] **Step 3: Update CLAUDE.md project status**

Update test count and note Phase 5 completion.

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "docs: Phase 5 架构升级完成 — credential proxy + skills + security config + sender allowlist + channel auto-registration"
git push origin develop
```
