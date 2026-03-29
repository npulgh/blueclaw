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
