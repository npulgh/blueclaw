"""Channel adapters package.

Re-exports the public API for convenience.
"""

from src.channels.registry import ChannelAdapter, ChannelRegistry

__all__ = ["ChannelAdapter", "ChannelRegistry"]
