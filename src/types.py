# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Global dataclasses for Lynxclaw.

Shared types used across channels, router, and IPC layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class IncomingMessage:
    """A message received from any channel adapter."""

    message_id: str        # Platform-native ID (used for idempotency)
    chat_id: str           # Conversation / group identifier
    sender_id: str
    sender_name: str
    text: str
    channel: str           # Channel name, e.g. "telegram" or "feishu"
    attachments: list = field(default_factory=list)
    timestamp: int = 0     # Unix epoch seconds
    raw: Any = None        # Original platform payload (for debugging)


@dataclass
class OutgoingMessage:
    """A message to be sent via a channel adapter."""

    text: Optional[str] = None
    rich_text: Optional[dict] = None
    attachments: Optional[list] = None


@dataclass
class ChannelCapabilities:
    """Declares what features a channel adapter supports."""

    supports_edit: bool = False
    supports_rich_text: bool = False
    supports_attachments: bool = False
