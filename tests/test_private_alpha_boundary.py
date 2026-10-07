"""The public-engine / private-alpha boundary (project100c.alpha): the engine runs without the private library, loads
it when present, and a private plug-in can add strategy ids but never replace a public one."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from project100c import alpha
from project100c.errors import ConfigError
from project100c.strategies.library import PLUGINS, PRIVATE_PLUGIN_IDS

REPO = Path(__file__).resolve().parents[1]


def _fake_library(root: Path, body: str) -> Path:
    pkg = root / "lib" / alpha.PACKAGE
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(body)
    (root / "lib" / "specs" / "structures").mkdir(parents=True)
    (root / "lib" / "configs" / "research").mkdir(parents=True)
    (root / "lib" / "configs" / "research" / "x.toml").write_text("a = 1\n")
    return root / "lib"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(alpha.ENV_VAR, raising=False)
    monkeypatch.delitem(sys.modules, alpha.PACKAGE, raising=False)


def test_the_public_repo_ships_no_private_library() -> None:
    assert alpha.alpha_dir() is None and alpha.load_private_module() is None
    assert PRIVATE_PLUGIN_IDS == []
    assert alpha.spec_dirs(REPO / "specs") == [REPO / "specs"]
    assert alpha.config_path("risk/limits.toml", REPO / "configs") == REPO / "configs" / "risk" / "limits.toml"
    assert "private_alpha/" in (REPO / ".gitignore").read_text()


def test_a_present_library_adds_plugins_specs_and_config_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = _fake_library(tmp_path, "class P:\n    pass\nPLUGINS = {'S-PRIVATE-001': P}\n")
    monkeypatch.setenv(alpha.ENV_VAR, str(lib))
    monkeypatch.setattr(sys, "path", list(sys.path))
    reg: dict[str, object] = dict(PLUGINS)
    assert alpha.register_plugins(reg) == ["S-PRIVATE-001"] and "S-PRIVATE-001" in reg
    assert alpha.spec_dirs(REPO / "specs") == [REPO / "specs", lib / "specs"]
    assert alpha.structure_dirs(REPO / "examples") == [REPO / "examples", lib / "specs" / "structures"]
    assert alpha.config_path("research/x.toml", REPO / "configs") == lib / "configs" / "research" / "x.toml"


def test_a_private_plugin_may_not_replace_a_public_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = sorted(PLUGINS)[0]
    lib = _fake_library(tmp_path, f"class P:\n    pass\nPLUGINS = {{{sid!r}: P}}\n")
    monkeypatch.setenv(alpha.ENV_VAR, str(lib))
    monkeypatch.setattr(sys, "path", list(sys.path))
    with pytest.raises(ConfigError, match="replace public"):
        alpha.register_plugins(dict(PLUGINS))


def test_a_mistyped_library_path_fails_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(alpha.ENV_VAR, str(tmp_path / "nope"))
    with pytest.raises(ConfigError, match="not a directory"):
        alpha.alpha_dir()
