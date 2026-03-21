"""Tests for src/memory.py — Memory management utilities."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.memory import (
    ensure_group_dirs,
    get_global_memory_path,
    get_group_memory_path,
)


# ---------------------------------------------------------------------------
# ensure_group_dirs
# ---------------------------------------------------------------------------

def test_ensure_group_dirs_creates_groups_dir(tmp_path: Path) -> None:
    """groups/ directory is created when it does not exist."""
    groups_dir = str(tmp_path / "groups")
    ensure_group_dirs(groups_dir, [])
    assert Path(groups_dir).is_dir()


def test_ensure_group_dirs_creates_group_subdirs(tmp_path: Path) -> None:
    """A subdirectory is created for each group name supplied."""
    groups_dir = str(tmp_path / "groups")
    ensure_group_dirs(groups_dir, ["main", "support"])
    assert (Path(groups_dir) / "main").is_dir()
    assert (Path(groups_dir) / "support").is_dir()


def test_ensure_group_dirs_creates_session_and_files_subdirs(tmp_path: Path) -> None:
    """session/ and files/ subdirectories are created inside each group dir."""
    groups_dir = str(tmp_path / "groups")
    ensure_group_dirs(groups_dir, ["main"])
    assert (Path(groups_dir) / "main" / "session").is_dir()
    assert (Path(groups_dir) / "main" / "files").is_dir()


def test_ensure_group_dirs_creates_skills_dirs(tmp_path: Path) -> None:
    """skills/ directories are created for global and each group (ADR-007)."""
    groups_dir = str(tmp_path / "groups")
    ensure_group_dirs(groups_dir, ["main", "support"])
    assert (Path(groups_dir) / "skills").is_dir()
    assert (Path(groups_dir) / "main" / "skills").is_dir()
    assert (Path(groups_dir) / "support" / "skills").is_dir()


# ---------------------------------------------------------------------------
# CLAUDE.md template seeding
# ---------------------------------------------------------------------------

def test_global_claude_md_is_seeded(tmp_path: Path) -> None:
    """groups/CLAUDE.md is created from the template on first run."""
    groups_dir = str(tmp_path / "groups")
    ensure_group_dirs(groups_dir, [])
    global_md = Path(groups_dir) / "CLAUDE.md"
    assert global_md.exists()
    content = global_md.read_text(encoding="utf-8")
    assert "Lynxclaw Global Memory" in content
    assert "Main Group" in content


def test_group_claude_md_is_seeded(tmp_path: Path) -> None:
    """groups/{name}/CLAUDE.md is created from the template on first run."""
    groups_dir = str(tmp_path / "groups")
    ensure_group_dirs(groups_dir, ["main"])
    group_md = Path(groups_dir) / "main" / "CLAUDE.md"
    assert group_md.exists()
    content = group_md.read_text(encoding="utf-8")
    assert "main" in content


def test_group_claude_md_contains_group_name(tmp_path: Path) -> None:
    """The group name appears in the seeded CLAUDE.md."""
    groups_dir = str(tmp_path / "groups")
    ensure_group_dirs(groups_dir, ["support"])
    group_md = Path(groups_dir) / "support" / "CLAUDE.md"
    content = group_md.read_text(encoding="utf-8")
    assert "support" in content


# ---------------------------------------------------------------------------
# Existing files are NOT overwritten
# ---------------------------------------------------------------------------

def test_existing_global_claude_md_is_not_overwritten(tmp_path: Path) -> None:
    """Pre-existing groups/CLAUDE.md is left intact on subsequent calls."""
    groups_dir = tmp_path / "groups"
    groups_dir.mkdir()
    existing_content = "# My custom global memory\n"
    (groups_dir / "CLAUDE.md").write_text(existing_content, encoding="utf-8")

    ensure_group_dirs(str(groups_dir), [])

    result = (groups_dir / "CLAUDE.md").read_text(encoding="utf-8")
    assert result == existing_content


def test_existing_group_claude_md_is_not_overwritten(tmp_path: Path) -> None:
    """Pre-existing groups/{name}/CLAUDE.md is left intact on subsequent calls."""
    groups_dir = tmp_path / "groups"
    (groups_dir / "main").mkdir(parents=True)
    existing_content = "# Agent-written notes\n"
    (groups_dir / "main" / "CLAUDE.md").write_text(existing_content, encoding="utf-8")

    ensure_group_dirs(str(groups_dir), ["main"])

    result = (groups_dir / "main" / "CLAUDE.md").read_text(encoding="utf-8")
    assert result == existing_content


def test_idempotent_on_repeated_calls(tmp_path: Path) -> None:
    """Calling ensure_group_dirs twice does not raise and preserves files."""
    groups_dir = str(tmp_path / "groups")
    ensure_group_dirs(groups_dir, ["main"])
    # Second call — must not raise
    ensure_group_dirs(groups_dir, ["main"])
    assert (Path(groups_dir) / "main" / "CLAUDE.md").exists()


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def test_get_global_memory_path(tmp_path: Path) -> None:
    """get_global_memory_path returns the correct path string."""
    groups_dir = str(tmp_path / "groups")
    result = get_global_memory_path(groups_dir)
    assert result == str(Path(groups_dir) / "CLAUDE.md")


def test_get_group_memory_path(tmp_path: Path) -> None:
    """get_group_memory_path returns the correct path string for a named group."""
    groups_dir = str(tmp_path / "groups")
    result = get_group_memory_path(groups_dir, "main")
    assert result == str(Path(groups_dir) / "main" / "CLAUDE.md")
