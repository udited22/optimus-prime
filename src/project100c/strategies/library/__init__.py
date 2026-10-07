"""The long-option strategy library (docs/research/strategy-hypotheses.md). Every spec is RESEARCH; nothing here is an
edge. Private plug-ins from the alpha library (project100c.alpha) are added when that library is present."""

from project100c.alpha import register_plugins
from project100c.strategies.library.base import (
    BasePlugin,
    EntrySignal,
    LibraryParams,
    LongOptionStrategy,
    OpenTrade,
    Session,
    SignalPlugin,
    library_metadata,
)
from project100c.strategies.library.signals import PLUGINS

PRIVATE_PLUGIN_IDS = register_plugins(PLUGINS)

__all__ = [
    "PLUGINS",
    "PRIVATE_PLUGIN_IDS",
    "BasePlugin",
    "EntrySignal",
    "LibraryParams",
    "LongOptionStrategy",
    "OpenTrade",
    "Session",
    "SignalPlugin",
    "library_metadata",
]
