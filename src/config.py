# LynxClaw - AI Coding Agent Framework
# Copyright (C) 2026
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Lynxclaw configuration loader.

Loads YAML config file + .env, merges env var overrides, validates required fields.
Usage: config = load_config("lynxclaw.config.yaml")
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv


class ConfigError(ValueError):
    """Raised when required config fields are missing or invalid."""


# ---------------------------------------------------------------------------
# Dataclasses — one per YAML section
# ---------------------------------------------------------------------------

@dataclass
class HostConfig:
    log_level: str = "info"


@dataclass
class ContainerConfig:
    image: str = "lynxclaw-agent:latest"
    memory: str = "512m"
    cpus: float = 1.0
    network: str = "none"
    timeout: int = 300
    max_concurrent: int = 5
    lifecycle: str = "ephemeral"
    runtime: str = "docker"


@dataclass
class TelegramConfig:
    enabled: bool = False
    mode: str = "polling"
    bot_token: Optional[str] = None


@dataclass
class FeishuConfig:
    enabled: bool = False
    mode: str = "websocket"
    app_id: Optional[str] = None
    app_secret: Optional[str] = None


@dataclass
class RouterConfig:
    default_trigger: str = "@bot"
    history_limit: int = 50
    group_queue_max: int = 10


@dataclass
class GroupConfig:
    name: str
    channel: str
    chat_id: str
    is_main: bool = False
    trigger: str = "@bot"
    token_budget: int = 0  # 0 = unlimited; otherwise max tokens per calendar month
    container_mode: str = "ephemeral"  # "ephemeral" or "persistent"
    allowed_senders: list[str] = field(default_factory=list)  # empty = no restriction


@dataclass
class ProxyConfig:
    enabled: bool = False
    allowed_domains: list[str] = field(default_factory=lambda: [
        "api.anthropic.com",
    ])


@dataclass
class StreamingConfig:
    enabled: bool = True
    debounce_ms: int = 500
    debounce_chars: int = 200


@dataclass
class SecurityConfig:
    blocked_commands: list[str] = field(default_factory=lambda: [
        "rm -rf /", "sudo", "chmod 777", ":(){:|:&};:",
    ])
    blocked_patterns: list[str] = field(default_factory=lambda: [
        ".ssh", ".aws", ".gnupg", ".env", "*.pem", "*.key",
    ])


@dataclass
class DashboardConfig:
    enabled: bool = False


@dataclass
class Config:
    # Credentials (from env, not YAML)
    anthropic_api_key: str = ""
    db_path: str = "data/store/messages.db"

    host: HostConfig = field(default_factory=HostConfig)
    container: ContainerConfig = field(default_factory=ContainerConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    router: RouterConfig = field(default_factory=RouterConfig)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    streaming: StreamingConfig = field(default_factory=StreamingConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    groups: list[GroupConfig] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Loader helpers
# ---------------------------------------------------------------------------

def _merge_section(dataclass_instance, raw: dict) -> None:
    """Overwrite dataclass fields with values from a raw dict (in-place)."""
    for key, value in raw.items():
        if hasattr(dataclass_instance, key):
            setattr(dataclass_instance, key, value)


def _load_yaml(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {path} must be a YAML mapping, got {type(data).__name__}")
    return data


def load_config(
    yaml_path: str | Path = "lynxclaw.config.yaml",
    *,
    dotenv_path: str | Path | None = ".env",
    security_config_path: str | Path | None = None,
) -> Config:
    """Load config from YAML file + .env, apply env var overrides, validate.

    Args:
        yaml_path: Path to the YAML config file.
        dotenv_path: Path to .env file (None to skip). Defaults to ".env".
        security_config_path: Path to external security config YAML. When present
            and the file exists, its contents override the inline security section.
            Defaults to ``~/.config/lynxclaw/security.yaml``.

    Returns:
        Populated Config dataclass.

    Raises:
        ConfigError: When required fields are missing or the file is unreadable.
    """
    # 1. Load .env (silently skip if file doesn't exist)
    if dotenv_path is not None:
        load_dotenv(dotenv_path=dotenv_path, override=False)

    # 2. Load YAML
    try:
        raw = _load_yaml(yaml_path)
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {yaml_path}")
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {yaml_path}: {exc}") from exc

    # 3. Build config with defaults, then merge YAML sections
    cfg = Config()
    section_map = {
        "host": cfg.host,
        "container": cfg.container,
        "telegram": cfg.telegram,
        "feishu": cfg.feishu,
        "router": cfg.router,
        "proxy": cfg.proxy,
        "streaming": cfg.streaming,
        "security": cfg.security,
        "dashboard": cfg.dashboard,
    }
    for section_name, section_obj in section_map.items():
        if section_name in raw and isinstance(raw[section_name], dict):
            _merge_section(section_obj, raw[section_name])

    # 3b. External security config (overrides inline security section)
    _sec_path = security_config_path
    if _sec_path is None:
        _sec_path = Path.home() / ".config" / "lynxclaw" / "security.yaml"
    try:
        sec_raw = _load_yaml(_sec_path)
        _merge_section(cfg.security, sec_raw)
    except (FileNotFoundError, ConfigError):
        pass  # fallback to inline or defaults

    # Parse groups list
    groups_raw = raw.get("groups")
    if groups_raw and isinstance(groups_raw, list):
        cfg.groups = [
            GroupConfig(
                name=g["name"],
                channel=g["channel"],
                chat_id=str(g["chat_id"]),
                is_main=bool(g.get("is_main", False)),
                trigger=g.get("trigger", cfg.router.default_trigger),
                token_budget=int(g.get("token_budget", 0)),
                container_mode=g.get("container_mode", "ephemeral"),
                allowed_senders=g.get("allowed_senders", []),
            )
            for g in groups_raw
            if isinstance(g, dict) and "name" in g and "channel" in g and "chat_id" in g
        ]

    # 4. Apply env var overrides
    if log_level := os.environ.get("LOG_LEVEL"):
        cfg.host.log_level = log_level.lower()
    if db_path := os.environ.get("DB_PATH"):
        cfg.db_path = db_path

    cfg.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if token := os.environ.get("TELEGRAM_BOT_TOKEN"):
        cfg.telegram.bot_token = token
    if app_id := os.environ.get("FEISHU_APP_ID"):
        cfg.feishu.app_id = app_id
    if app_secret := os.environ.get("FEISHU_APP_SECRET"):
        cfg.feishu.app_secret = app_secret

    # 5. Validate required fields
    _validate(cfg)

    return cfg


def _validate(cfg: Config) -> None:
    """Raise ConfigError for any missing required fields."""
    if not cfg.anthropic_api_key:
        raise ConfigError(
            "ANTHROPIC_API_KEY is required. Set it in your .env file or environment."
        )
    if cfg.telegram.enabled and not cfg.telegram.bot_token:
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN is required when telegram.enabled = true."
        )
    if cfg.feishu.enabled:
        if not cfg.feishu.app_id:
            raise ConfigError(
                "FEISHU_APP_ID is required when feishu.enabled = true."
            )
        if not cfg.feishu.app_secret:
            raise ConfigError(
                "FEISHU_APP_SECRET is required when feishu.enabled = true."
            )
