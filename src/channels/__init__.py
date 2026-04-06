# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Channel adapters package.

Re-exports the public API for convenience.
"""

from src.channels.registry import ChannelAdapter, ChannelRegistry

__all__ = ["ChannelAdapter", "ChannelRegistry"]
