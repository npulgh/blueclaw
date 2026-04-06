# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later

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
    """Auto-discover adapters by scanning channel modules for create_adapter().

    Each adapter module can define:
    - ``CHANNEL_NAME: str`` — the registry key (falls back to module name)
    - ``create_adapter(config) -> Optional[ChannelAdapter]`` — factory function

    Modules without ``create_adapter`` are silently skipped. Factories that
    return ``None`` (disabled/missing credentials) are also skipped.

    Adding a new adapter requires only:
    1. Creating the adapter class in ``src/channels/<name>.py``.
    2. Adding ``CHANNEL_NAME`` and ``create_adapter()`` to the module.
    3. Adding a config section to ``src/config.py``.

    No other files need to change.
    """
    import importlib
    import pkgutil

    import src.channels as channels_pkg

    adapters: dict[str, ChannelAdapter] = {}

    for _importer, modname, _ispkg in pkgutil.iter_modules(channels_pkg.__path__):
        if modname in ("registry", "__init__", "example_adapter"):
            continue
        try:
            mod = importlib.import_module(f"src.channels.{modname}")
        except ImportError as exc:
            logger.warning("discover_adapters: failed to import %s: %s", modname, exc)
            continue

        factory = getattr(mod, "create_adapter", None)
        if factory is None:
            continue

        name = getattr(mod, "CHANNEL_NAME", modname)
        try:
            adapter = factory(config)
        except Exception as exc:
            logger.warning("discover_adapters: %s.create_adapter() failed: %s", name, exc)
            continue

        if adapter is not None:
            adapters[name] = adapter
            logger.debug("discover_adapters: %s enabled", name)

    logger.info("discover_adapters: found %d adapter(s): %s", len(adapters), list(adapters))
    return adapters
