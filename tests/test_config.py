"""Tests for src/config.py — T1.2 Config System."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from src.config import Config, ConfigError, load_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "test.config.yaml"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


def _write_env(tmp_path: Path, content: str) -> Path:
    p = tmp_path / ".env"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


MINIMAL_YAML = """\
    host:
      log_level: info
    telegram:
      enabled: false
    feishu:
      enabled: false
"""


# ---------------------------------------------------------------------------
# Test: load valid config
# ---------------------------------------------------------------------------

def test_load_valid_config(tmp_path, monkeypatch):
    yaml_file = _write_yaml(tmp_path, MINIMAL_YAML)
    env_file = _write_env(tmp_path, "ANTHROPIC_API_KEY=sk-test-key\n")

    # Ensure no stale env vars from the real environment interfere
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    cfg = load_config(yaml_file, dotenv_path=env_file)

    assert isinstance(cfg, Config)
    assert cfg.anthropic_api_key == "sk-test-key"
    assert cfg.host.log_level == "info"


# ---------------------------------------------------------------------------
# Test: default values
# ---------------------------------------------------------------------------

def test_default_values(tmp_path, monkeypatch):
    yaml_file = _write_yaml(tmp_path, MINIMAL_YAML)
    env_file = _write_env(tmp_path, "ANTHROPIC_API_KEY=sk-test-key\n")

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("DB_PATH", raising=False)

    cfg = load_config(yaml_file, dotenv_path=env_file)

    assert cfg.container.image == "lynxclaw-agent:latest"
    assert cfg.container.memory == "512m"
    assert cfg.container.cpus == 1.0
    assert cfg.container.max_concurrent == 5
    assert cfg.container.lifecycle == "ephemeral"
    assert cfg.router.default_trigger == "@bot"
    assert cfg.router.history_limit == 50
    assert cfg.streaming.enabled is True
    assert cfg.streaming.debounce_ms == 500
    assert cfg.streaming.debounce_chars == 200
    assert cfg.proxy.enabled is False
    assert cfg.db_path == "data/store/messages.db"
    assert cfg.telegram.enabled is False
    assert cfg.feishu.enabled is False


# ---------------------------------------------------------------------------
# Test: YAML values override defaults
# ---------------------------------------------------------------------------

def test_yaml_overrides_defaults(tmp_path, monkeypatch):
    yaml_file = _write_yaml(tmp_path, """\
        host:
          log_level: debug
        container:
          max_concurrent: 3
          memory: 256m
        router:
          history_limit: 20
        telegram:
          enabled: false
        feishu:
          enabled: false
    """)
    env_file = _write_env(tmp_path, "ANTHROPIC_API_KEY=sk-test-key\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    cfg = load_config(yaml_file, dotenv_path=env_file)

    assert cfg.host.log_level == "debug"
    assert cfg.container.max_concurrent == 3
    assert cfg.container.memory == "256m"
    assert cfg.router.history_limit == 20


# ---------------------------------------------------------------------------
# Test: env var overrides YAML
# ---------------------------------------------------------------------------

def test_env_var_overrides_yaml(tmp_path, monkeypatch):
    yaml_file = _write_yaml(tmp_path, """\
        host:
          log_level: info
        telegram:
          enabled: false
        feishu:
          enabled: false
    """)
    env_file = _write_env(tmp_path, "ANTHROPIC_API_KEY=sk-from-env\nLOG_LEVEL=WARNING\nDB_PATH=/custom/db.sqlite\n")

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("DB_PATH", raising=False)

    cfg = load_config(yaml_file, dotenv_path=env_file)

    assert cfg.anthropic_api_key == "sk-from-env"
    assert cfg.host.log_level == "warning"   # lowercased
    assert cfg.db_path == "/custom/db.sqlite"


# ---------------------------------------------------------------------------
# Test: missing ANTHROPIC_API_KEY raises ConfigError
# ---------------------------------------------------------------------------

def test_missing_api_key_raises(tmp_path, monkeypatch):
    yaml_file = _write_yaml(tmp_path, MINIMAL_YAML)
    env_file = _write_env(tmp_path, "# no key here\n")

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        load_config(yaml_file, dotenv_path=env_file)


# ---------------------------------------------------------------------------
# Test: telegram enabled but no token raises ConfigError
# ---------------------------------------------------------------------------

def test_telegram_enabled_no_token_raises(tmp_path, monkeypatch):
    yaml_file = _write_yaml(tmp_path, """\
        telegram:
          enabled: true
        feishu:
          enabled: false
    """)
    env_file = _write_env(tmp_path, "ANTHROPIC_API_KEY=sk-test-key\n")

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        load_config(yaml_file, dotenv_path=env_file)


# ---------------------------------------------------------------------------
# Test: feishu enabled but missing credentials raises ConfigError
# ---------------------------------------------------------------------------

def test_feishu_enabled_no_credentials_raises(tmp_path, monkeypatch):
    yaml_file = _write_yaml(tmp_path, """\
        telegram:
          enabled: false
        feishu:
          enabled: true
    """)
    env_file = _write_env(tmp_path, "ANTHROPIC_API_KEY=sk-test-key\n")

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    with pytest.raises(ConfigError, match="FEISHU_APP_ID"):
        load_config(yaml_file, dotenv_path=env_file)


# ---------------------------------------------------------------------------
# Test: telegram token from env var
# ---------------------------------------------------------------------------

def test_telegram_token_from_env(tmp_path, monkeypatch):
    yaml_file = _write_yaml(tmp_path, """\
        telegram:
          enabled: true
        feishu:
          enabled: false
    """)
    env_file = _write_env(tmp_path, "ANTHROPIC_API_KEY=sk-test-key\nTELEGRAM_BOT_TOKEN=bot123:abc\n")

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    cfg = load_config(yaml_file, dotenv_path=env_file)

    assert cfg.telegram.enabled is True
    assert cfg.telegram.bot_token == "bot123:abc"


# ---------------------------------------------------------------------------
# Test: missing config file raises ConfigError
# ---------------------------------------------------------------------------

def test_missing_config_file_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nonexistent.yaml", dotenv_path=None)


# ---------------------------------------------------------------------------
# Test: security defaults are populated
# ---------------------------------------------------------------------------

def test_security_defaults(tmp_path, monkeypatch):
    yaml_file = _write_yaml(tmp_path, MINIMAL_YAML)
    env_file = _write_env(tmp_path, "ANTHROPIC_API_KEY=sk-test-key\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    cfg = load_config(yaml_file, dotenv_path=env_file)

    assert "sudo" in cfg.security.blocked_commands
    assert ".ssh" in cfg.security.blocked_patterns


# ---------------------------------------------------------------------------
# Test: external security config
# ---------------------------------------------------------------------------

def test_security_config_from_external_file(tmp_path, monkeypatch):
    """Security config loaded from external file when present."""
    yaml_file = _write_yaml(tmp_path, MINIMAL_YAML)
    env_file = _write_env(tmp_path, "ANTHROPIC_API_KEY=sk-test-key\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    sec_dir = tmp_path / "config_home" / "lynxclaw"
    sec_dir.mkdir(parents=True)
    sec_file = sec_dir / "security.yaml"
    sec_file.write_text(
        "blocked_patterns:\n  - '*.pem'\n  - '*.key'\n  - '.secret'\n",
        encoding="utf-8",
    )

    cfg = load_config(yaml_file, dotenv_path=env_file, security_config_path=str(sec_file))
    assert "*.pem" in cfg.security.blocked_patterns
    assert ".secret" in cfg.security.blocked_patterns


def test_security_config_fallback_to_inline(tmp_path, monkeypatch):
    """When external security config is absent, fall back to inline config."""
    yaml_content = """\
    host:
      log_level: info
    telegram:
      enabled: false
    feishu:
      enabled: false
    security:
      blocked_patterns:
        - '*.env'
    """
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
    # Hardcoded defaults from SecurityConfig dataclass
    assert ".ssh" in cfg.security.blocked_patterns
    assert "sudo" in cfg.security.blocked_commands
