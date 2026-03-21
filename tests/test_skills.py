"""Tests for skill loading in container/agent-runner/main.py (ADR-007)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add agent-runner to path so we can import load_skills
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "agent-runner"))

from main import load_skills  # type: ignore


def _create_skill(base: Path, name: str, content: str) -> None:
    """Helper: create a SKILL.md file."""
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")


class TestLoadSkills:
    def test_loads_global_skills(self, tmp_path):
        global_skills = tmp_path / "global" / "skills"
        _create_skill(global_skills, "review", "---\nname: review\ndescription: Code review\n---\nReview content")
        result = load_skills(str(global_skills), "")
        assert len(result) == 1
        assert result[0]["name"] == "review"
        assert "Review content" in result[0]["content"]

    def test_loads_group_skills(self, tmp_path):
        group_skills = tmp_path / "group" / "skills"
        _create_skill(group_skills, "helper", "---\nname: helper\n---\nHelper content")
        result = load_skills("", str(group_skills))
        assert len(result) == 1
        assert "Helper content" in result[0]["content"]

    def test_group_overrides_global(self, tmp_path):
        global_skills = tmp_path / "global" / "skills"
        group_skills = tmp_path / "group" / "skills"
        _create_skill(global_skills, "review", "---\nname: review\n---\nGlobal version")
        _create_skill(group_skills, "review", "---\nname: review\n---\nGroup version")
        result = load_skills(str(global_skills), str(group_skills))
        assert len(result) == 1
        assert "Group version" in result[0]["content"]

    def test_empty_dirs_no_error(self, tmp_path):
        result = load_skills(str(tmp_path / "nonexistent"), "")
        assert result == []

    def test_missing_frontmatter_still_loads(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skill_dir = skills_dir / "simple"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("Just plain content", encoding="utf-8")
        result = load_skills(str(skills_dir), "")
        assert len(result) == 1
        assert result[0]["name"] == "simple"
        assert "Just plain content" in result[0]["content"]

    def test_multiple_skills_sorted(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(skills_dir, "beta", "Beta skill")
        _create_skill(skills_dir, "alpha", "Alpha skill")
        result = load_skills(str(skills_dir), "")
        assert len(result) == 2
        assert result[0]["name"] == "alpha"
        assert result[1]["name"] == "beta"

    def test_description_parsed(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(skills_dir, "test", "---\nname: test\ndescription: A test skill\n---\nContent")
        result = load_skills(str(skills_dir), "")
        assert result[0]["description"] == "A test skill"
