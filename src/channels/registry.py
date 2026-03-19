"""Channel adapter ABC and registry.

ChannelAdapter defines the interface every channel (Telegram, Feishu, …) must implement.
ChannelRegistry manages adapter instances: register, lookup, start_all, stop_all.
discover_adapters() is the factory used by main.py to instantiate enabled adapters.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from src.types import ChannelCapabilities, IncomingMessage, OutgoingMessage

if TYPE_CHECKING:
    from src.config import Config

logger = logging.getLogger(__name__)

# Type alias for the message handler callback
MessageHandler = Callable[[IncomingMessage], Awaitable[None]]


class ChannelAdapter(ABC):
    """Abstract base class for all channel adapters."""

    @abstractmethod
    async def init(self, config) -> None:
        """Initialise the adapter with channel-specific config."""

    @abstractmethod
    async def start(self) -> None:
        """Start receiving messages (connect, begin polling, etc.)."""

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully stop the adapter and release resources."""

    @abstractmethod
    def on_message(self, handler: MessageHandler) -> None:
        """Register the callback invoked for every incoming message."""

    @abstractmethod
    async def send_message(self, chat_id: str, content: OutgoingMessage) -> str:
        """Send a message; returns the platform-assigned message ID."""

    @abstractmethod
    async def edit_message(
        self, chat_id: str, msg_id: str, content: OutgoingMessage
    ) -> None:
        """Edit an already-sent message (used for streaming updates)."""

    @abstractmethod
    def capabilities(self) -> ChannelCapabilities:
        """Return the feature set supported by this adapter."""


class ChannelRegistry:
    """Simple dict-backed registry for channel adapters.

    Usage::

        registry = ChannelRegistry()
        registry.register("telegram", TelegramAdapter())
        await registry.start_all()
        ...
        await registry.stop_all()
    """

    def __init__(self) -> None:
        self._adapters: dict[str, ChannelAdapter] = {}

    def register(self, name: str, adapter: ChannelAdapter) -> None:
        """Register *adapter* under *name*. Raises ValueError on duplicate."""
        if name in self._adapters:
            raise ValueError(f"Adapter '{name}' is already registered.")
        self._adapters[name] = adapter
        logger.debug("Registered channel adapter: %s", name)

    def get(self, name: str) -> ChannelAdapter:
        """Return the adapter registered under *name*. Raises KeyError if absent."""
        try:
            return self._adapters[name]
        except KeyError:
            raise KeyError(f"No adapter registered for channel '{name}'.")

    async def start_all(self) -> None:
        """Call start() on every registered adapter (insertion order)."""
        for name, adapter in self._adapters.items():
            logger.info("Starting channel adapter: %s", name)
            await adapter.start()

    async def stop_all(self) -> None:
        """Call stop() on every registered adapter in reverse insertion order."""
        for name, adapter in reversed(list(self._adapters.items())):
            logger.info("Stopping channel adapter: %s", name)
            await adapter.stop()

    @property
    def names(self) -> list[str]:
        """Return registered adapter names in insertion order."""
        return list(self._adapters.keys())


# ---------------------------------------------------------------------------
# Adapter factory / auto-discovery
# ---------------------------------------------------------------------------

def discover_adapters(config: "Config") -> dict[str, "ChannelAdapter"]:
    """Instantiate and return all adapters that are enabled in *config*.

    Returns a dict of ``{channel_name: uninitialised_adapter}``.  The caller
    is responsible for calling ``adapter.init(channel_config)`` on each entry
    before registering it with a ``ChannelRegistry``.

    Adding a new adapter requires only:
    1. Creating the adapter class in ``src/channels/<name>.py``.
    2. Adding a config section to ``src/config.py``.
    3. Adding one ``if config.<name>.enabled`` block below.

    No other files need to change.
    """
    # Import here to avoid circular imports at module load time.
    from src.channels.feishu import FeishuAdapter
    from src.channels.telegram import TelegramAdapter

    adapters: dict[str, ChannelAdapter] = {}

    if config.telegram.enabled:
        adapters["telegram"] = TelegramAdapter()
        logger.debug("discover_adapters: telegram enabled")

    if config.feishu.enabled:
        adapters["feishu"] = FeishuAdapter()
        logger.debug("discover_adapters: feishu enabled")

    # To add a new channel (e.g. Discord), append:
    #
    #   if config.discord.enabled:
    #       from src.channels.discord import DiscordAdapter
    #       adapters["discord"] = DiscordAdapter()

    logger.info("discover_adapters: found %d adapter(s): %s", len(adapters), list(adapters))
    return adapters
