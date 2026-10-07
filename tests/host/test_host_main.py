"""The composition root: config, the live lock, feed selection, and a replay day end to end (SIMULATED)."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from project100c.ops import host_main as hm
from project100c.ops.credstore import CredentialStoreError
from project100c.ops.status_server import PUBLIC_KEYS

ROOT = Path(__file__).resolve().parents[2]
CFG = ROOT / "configs" / "host" / "host.toml"
CHECKLIST = ROOT / "docs" / "go-live-checklist.md"


def cfg(tmp: Path, **kw: object) -> hm.HostConfig:
    c = hm.load_host_config(CFG, state_dir=tmp / "state")
    return dataclasses.replace(c, status_port=0, webhook_port=0, **kw)  # type: ignore[arg-type]


def test_the_shipped_config_is_replay_paper_safe_and_locked() -> None:
    c = hm.load_host_config(CFG)
    assert c.mode == "replay" and c.feed == "auto" and c.config_version
    assert not c.live_lock.unlocked and c.live_lock.owner_signoff == ""
    assert c.nav == 10000 and c.schedule.gate_deadline.isoformat() == "09:05:00"
    with pytest.raises(ValueError, match="mode must be"):
        dataclasses.replace(c, mode="real")


def test_live_is_refused_even_with_every_config_lock_open(tmp_path: Path) -> None:
    good = hm.LiveLock(True, "The owner, test", hashlib.sha256(CHECKLIST.read_bytes()).hexdigest())
    problems = hm.live_lock_problems(good, CHECKLIST)
    assert problems == ["live trading is disabled in this build (LIVE_ENABLED_IN_THIS_BUILD = False)"]
    assert not hm.build_allows_live()
    closed = hm.live_lock_problems(hm.LiveLock(), tmp_path / "missing.md")
    assert len(closed) == 4
    assert any(
        "checklist_sha256" in p
        for p in hm.live_lock_problems(dataclasses.replace(good, checklist_sha256="0"), CHECKLIST)
    )
    assert hm.main(["--config", str(CFG), "--root", str(ROOT), "--mode", "live", "--state-dir", str(tmp_path)]) == 3
    assert not (tmp_path / "journal").exists()  # refused before anything was built


def test_the_root_never_builds_an_order_placing_broker() -> None:
    src = Path(hm.__file__).read_text(encoding="utf-8")
    names = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
    names |= {a.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert "PaperBroker" in names
    assert not {"UpstoxBroker", "FakeBroker"} & names  # PaperLoop's rehearsal owns its own fake broker
    assert "broker.upstox.adapter" not in src


def test_feed_selection_fails_closed(tmp_path: Path) -> None:
    never = tmp_path / "never"
    with pytest.raises(CredentialStoreError, match="feed = upstox"):
        hm.Host(cfg(tmp_path, mode="paper", feed="upstox"), root=ROOT, env={}, forbid_under=never, serve=False)
    h = hm.Host(cfg(tmp_path, mode="paper"), root=ROOT, env={}, forbid_under=never, serve=False)
    try:
        assert h.feed_kind == "replay" and h.telegram is None  # auto without keys: SIMULATED replay feed
    finally:
        h.close()
    env = {
        "UPSTOX_API_KEY": "fake-key-123456",
        "UPSTOX_API_SECRET": "fake-secret-123456",
        "UPSTOX_REDIRECT_URI": "https://example.invalid/cb",
        "P100C_WEBHOOK_PATH": "/upstox/notify/abcdefghij123456",
    }
    h = hm.Host(cfg(tmp_path, mode="paper"), root=ROOT, env=env, forbid_under=never, serve=False)
    try:  # wired, not started: no token request is made before the 08:45 event
        assert h.feed_kind == "upstox" and h.gate.broker_name == "Upstox" and h.live_feed is not None
        assert h.live_feed.health(h.clock()) == "WAITING_FOR_TOKEN" and h.webhook is not None
        assert "fake-secret-123456" not in h.redactor("x fake-secret-123456 y")
    finally:
        h.close()


def test_a_replay_day_runs_end_to_end(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    h = hm.Host(c, root=ROOT, env={}, forbid_under=tmp_path / "never")
    try:
        assert h.run() == 0
        (rep,) = h.reports
        assert rep["day"] == "2026-10-05" and rep["flat"] and rep["kills"] == [] and rep["real_money"] == "OFF"
        assert [g[1] for g in rep["gate"]] == ["REQUESTED", "ACTIVE"]
        assert rep["shadow"]["bars"] == 375 and rep["shadow"]["signals"]
        assert rep["host"]["failed_steps"] == 0 and rep["host"]["steps"] > 1000
        reh = rep["order_rehearsal"]
        assert (
            reh["journal_verified"] and reh["flat_at_end"] and reh["orders_sent"] > 0 and "HYPOTHETICAL" in reh["nav"]
        )
        state = c.state_dir
        assert (state / "backups" / rep["backup"]).is_file()
        assert json.loads((state / "reports" / "2026-10-05.json").read_text())["day"] == "2026-10-05"
        pub = h.board.public()
        assert set(pub) <= PUBLIC_KEYS and pub["real_money"] == "OFF" and pub["phase"] == "AFTER_HOURS"
        assert "paper_realised_today" in h.board.private() and h.board.healthy()
        assert h.runtime.state.positions == {} and h.broker.orders() == []  # shadow: the host's own book is untouched
    finally:
        h.close()


def test_a_second_process_on_the_same_journal_does_not_restart_the_day(tmp_path: Path) -> None:
    c = cfg(tmp_path, replay=dataclasses.replace(hm.load_host_config(CFG).replay, rehearse_orders=False))
    a = hm.Host(c, root=ROOT, env={}, forbid_under=tmp_path / "never", serve=False)
    a.run(max_steps=200)
    a.close()
    b = hm.Host(c, root=ROOT, env={}, forbid_under=tmp_path / "never", serve=False)
    try:
        assert b.run() == 0
        assert any("restarted" in m for _, _, m in b.log_alerts.recent)
        assert b.reports and b.reports[0]["kills"] == []
    finally:
        b.close()
