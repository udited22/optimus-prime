"""The public-engine / private-alpha boundary.

The engine (data, costs, backtester, validation, Risk Governor, execution, paper book) is public. Live research
candidates are not: their exact parameters, signal plug-ins, structure specs and research-round configs live in a
separate, private *alpha library* that the engine loads only if it is present. Without it, the engine runs and the
test suite passes on the public research library and the synthetic examples alone.

Where the private library is looked for (first match wins):

1. ``$P100C_ALPHA_DIR`` (must exist if set; a typo fails loudly rather than silently running without it);
2. ``<repo>/private_alpha/`` (gitignored, never committed).

Layout of an alpha library directory (every part optional)::

    private_alpha/                 <- the directory itself
      private_alpha/__init__.py    <- Python package; may define PLUGINS: dict[str, type[BasePlugin]]
      specs/*.yaml               <- private StrategySpecs (loaded after the public specs/)
      specs/structures/*.yaml    <- private StructureSpecs
      configs/<rel path>         <- overrides for configs/<rel path> (e.g. research/forward_round.toml)

Rules: a private plug-in may add strategy ids but never replace a public one; private specs go through the same
loaders, schema checks and Governor as public ones; nothing here grants any live authority.
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import MutableMapping
from pathlib import Path
from types import ModuleType
from typing import Any

from project100c.errors import ConfigError

ENV_VAR = "P100C_ALPHA_DIR"
PACKAGE = "private_alpha"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = REPO_ROOT / "private_alpha"


def alpha_dir() -> Path | None:
    """The private alpha library directory, or None when it is absent (the public engine on its own)."""
    raw = os.environ.get(ENV_VAR, "").strip()
    if raw:
        d = Path(raw).expanduser()
        if not d.is_dir():
            raise ConfigError(f"{ENV_VAR}={raw!r} is not a directory")
        return d
    return DEFAULT_DIR if DEFAULT_DIR.is_dir() else None


def spec_dirs(public: Path) -> list[Path]:
    """Directories holding StrategySpecs: the public one, then the private library's ``specs/`` if present."""
    d = alpha_dir()
    extra = d / "specs" if d is not None else None
    return [public, *([extra] if extra is not None and extra.is_dir() else [])]


def structure_dirs(public: Path) -> list[Path]:
    """Directories holding StructureSpecs: the public one, then the private ``specs/structures/`` if present."""
    d = alpha_dir()
    extra = d / "specs" / "structures" if d is not None else None
    return [public, *([extra] if extra is not None and extra.is_dir() else [])]


def config_path(rel: str, public_configs: Path) -> Path:
    """``configs/<rel>`` from the private library when it has one, else the public file."""
    d = alpha_dir()
    if d is not None and (d / "configs" / rel).is_file():
        return d / "configs" / rel
    return public_configs / rel


def load_private_module() -> ModuleType | None:
    """Import the private ``private_alpha`` package if the library directory carries one."""
    d = alpha_dir()
    if d is None or not (d / PACKAGE / "__init__.py").is_file():
        return None
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
    return importlib.import_module(PACKAGE)


def register_plugins(registry: MutableMapping[str, Any]) -> list[str]:
    """Add the private library's signal plug-ins (``private_alpha.PLUGINS``) to ``registry``; returns the added ids.
    A private id that collides with a public one is refused (ConfigError)."""
    mod = load_private_module()
    extra = getattr(mod, "PLUGINS", None) if mod is not None else None
    if not extra:
        return []
    clash = sorted(k for k in extra if k in registry and registry[k] is not extra[k])
    if clash:
        raise ConfigError(f"private plug-ins may not replace public ones: {clash}")
    registry.update(extra)
    return sorted(extra)
