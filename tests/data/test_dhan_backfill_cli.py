"""Backfill + coverage CLIs: planning and reporting need no token and send nothing; 'run' without a token stops."""

from __future__ import annotations

import importlib.util
import socket
import sqlite3
import sys
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.data.dhan_fakes import REPO


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_backfill_plan_and_status_need_no_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_network: None
) -> None:
    bf = _load("dhan_backfill")
    assert bf.main(["plan", "--lake", str(tmp_path)], {}) == 0
    out = capsys.readouterr().out
    assert "total requests=3461" in out and out.index("tier A") < out.index("tier B")
    assert bf.main(["status", "--lake", str(tmp_path)], {}) == 0
    assert "no job store yet" in capsys.readouterr().out


def test_backfill_run_without_token_exits_2_and_sends_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_network: None
) -> None:
    rc = _load("dhan_backfill").main(["run", "--lake", str(tmp_path)], {})
    assert rc == 2 and "DHAN_ACCESS_TOKEN" in capsys.readouterr().err
    assert not (tmp_path / "raw").exists()


def test_coverage_report_on_an_empty_lake_is_all_pending(tmp_path: Path, no_network: None) -> None:
    cov = _load("dhan_coverage_report")
    out = tmp_path / "cov.md"
    assert cov.main(["--lake", str(tmp_path), "--out", str(out)]) == 0
    text = out.read_text()
    assert "PENDING=3461" in text and "0.0% finished" in text
    assert "OPT code1 ATM-10 CE" in text and "OPT code2 ATM+3 PE" in text and "OPT code2 ATM+4" not in text
    assert "access-token" not in text.lower() and "client" not in text.lower()


def test_newest_contiguous_stops_at_the_first_unfinished_window() -> None:
    cov = _load("dhan_coverage_report")
    d = date.fromisoformat
    w = {
        (d("2026-09-02"), d("2026-10-02")): True,
        (d("2026-08-03"), d("2026-09-02")): True,
        (d("2026-07-04"), d("2026-08-03")): False,
        (d("2026-06-04"), d("2026-07-04")): True,
    }
    assert cov.newest_contiguous(w) == (d("2026-08-03"), d("2026-10-02"))
    w[(d("2026-09-02"), d("2026-10-02"))] = False
    assert cov.newest_contiguous(w) is None


def test_claim_order_and_one_in_run_retry_of_failed_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bf = _load("dhan_backfill")
    db = sqlite3.connect(tmp_path / "s.sqlite", isolation_level=None)
    db.execute("CREATE TABLE chunks(job_id TEXT, status TEXT)")
    db.execute(bf.CLAIMS)
    db.executemany("INSERT INTO chunks VALUES(?,?)", [("new", "PENDING"), ("old", "PENDING"), ("done", "DONE")])
    seq = ["new", "done", "old"]  # plan order, newest first
    assert bf._claim(db, "r1", 0, seq) == ("r1", "new")
    assert bf._claim(db, "r1", 1, seq) == ("r1", "old")  # 'done' has nothing left
    assert bf._claim(db, "r1", 0, seq) is None
    # the first pass of 'new' ends with a FAILED chunk (gateway 504); it is retried once, before anything older
    db.execute("UPDATE chunks SET status='FAILED' WHERE job_id='new'")
    db.execute("UPDATE backfill_claims SET finished_utc='2000-01-01T00:00:00+00:00' WHERE job_id='new'")
    assert bf._claim(db, "r1", 0, seq) == ("r1#retry", "new")
    db.execute("UPDATE backfill_claims SET finished_utc='2000-01-01T00:00:00+00:00' WHERE job_id='new'")
    assert bf._claim(db, "r1", 0, seq) is None  # only one in-run retry; the next round or run takes it after that
    # a first pass that finished just now is not retried yet
    db.execute("UPDATE chunks SET status='FAILED' WHERE job_id='old'")
    monkeypatch.setattr(bf, "RETRY_AFTER_SECONDS", 10**9)
    db.execute("UPDATE backfill_claims SET finished_utc='2999-01-01T00:00:00+00:00' WHERE job_id='old'")
    assert bf._claim(db, "r1", 0, seq) is None
