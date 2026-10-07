"""CLI: planning works with no token; 'run' without DHAN_ACCESS_TOKEN fails loudly (exit 2) before any network I/O."""

from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.data.dhan_fakes import REPO


def _cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dhan_cli", REPO / "scripts" / "dhan_download.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dhan_cli"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_plan_without_token(tmp_path: Path, capsys: pytest.CaptureFixture[str], no_network: None) -> None:
    rc = _cli().main(["plan", "rolling", "--from", "2021-10-01", "--to", "2026-10-01", "--lake", str(tmp_path)], {})
    assert rc == 0 and "2562 requests" in capsys.readouterr().out


def test_run_without_token_exits_2_and_sends_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_network: None
) -> None:
    rc = _cli().main(["run", "rolling", "--lake", str(tmp_path)], {})
    err = capsys.readouterr().err
    assert rc == 2 and "DHAN_ACCESS_TOKEN is not set" in err and "no download happened" in err
    assert not (tmp_path / "raw").exists()
