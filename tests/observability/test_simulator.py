"""The SIMULATED session: synthetic prices through the real Risk Governor + kernel runtime harness + fake broker."""

from __future__ import annotations

import json
import re
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from project100c.observability.dashboard.events import DashboardError, EventKind
from project100c.observability.dashboard.market import synthetic_day
from project100c.observability.dashboard.simulator import REPLAY_SCENARIOS, Kernel, SimulatedSession, build_replay
from project100c.observability.dashboard.topology import EDGE_SET, NODE_IDS, NODES, STRATEGIES, topology_json

Replays = dict[str, list[dict[str, Any]]]


def _t(e: dict[str, Any]) -> time:
    return time.fromisoformat(e["ts"][11:19])


def test_topology_is_consistent() -> None:
    assert len(NODE_IDS) == len(NODES)
    assert all(a in NODE_IDS and b in NODE_IDS for a, b in EDGE_SET)
    t = topology_json()
    ids = {n["id"] for n in t["nodes"]}
    need = {"cio", "market_intel", "data_quality", "strategy_factory", "validation", "allocator", "risk_governor"}
    assert need | {"execution", "broker", "post_trade"} <= ids
    assert {f"strat:{s.id}" for s in STRATEGIES} <= ids
    # every path from a strategy to the broker goes through the Risk Governor
    for s in STRATEGIES:
        assert (s.node, "execution") not in EDGE_SET and (s.node, "broker") not in EDGE_SET
    assert ("allocator", "execution") not in EDGE_SET and ("allocator", "broker") not in EDGE_SET
    assert {a for a, b in EDGE_SET if b == "execution"} <= {"risk_governor", "broker"}


def test_synthetic_prices_are_deterministic_and_bounded() -> None:
    sc = REPLAY_SCENARIOS[0]
    a = synthetic_day(sc.day, sc.seed, 15)
    assert a == synthetic_day(sc.day, sc.seed, 15)
    assert a != synthetic_day(sc.day, sc.seed + 1, 15)
    assert len(a) == 1501 and a[0].ts.time() == time(9, 15) and a[-1].ts.time() == time(15, 30)
    lo, hi = min(p.spot for p in a), max(p.spot for p in a)
    assert (hi - lo) / lo < 0.05  # a plausible intraday range


def test_replay_is_deterministic(kernel: Kernel, replays: Replays) -> None:
    assert build_replay(REPLAY_SCENARIOS[0], kernel) == replays[REPLAY_SCENARIOS[0].name]


def test_every_event_is_simulated_and_every_flow_is_a_declared_edge(replays: Replays) -> None:
    for evs in replays.values():
        assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1))
        for e in evs:
            assert e["simulated"] is True and e["label"] == "SIMULATED"
            for a, b, _ in e["flows"]:
                assert (a, b) in EDGE_SET
        assert evs[0]["kind"] == "SESSION" and "SIMULATED" in evs[0]["data"]["notice"]
        assert evs[-1]["kind"] == "LOG" and any(e["kind"] == "DAY_END" for e in evs[-3:])


def test_every_entry_order_follows_an_approval_inside_the_entry_window(replays: Replays) -> None:
    for evs in replays.values():
        approved: set[str] = set()
        for e in evs:
            d = e["data"]
            if e["kind"] == "DECISION" and d["verdict"] == "APPROVE" and not d["simulate_only"]:
                approved.add(d["intent_id"])
            if e["kind"] == "ORDER" and d["kind"] == "ENTRY":
                assert any(d["client_order_id"].startswith(i) for i in approved), d
                assert time(9, 20) <= _t(e) < time(14, 0)


def test_normal_day_trades_with_a_broker_side_stop_and_ends_flat(replays: Replays) -> None:
    evs = replays["normal-1"]
    kinds = [e["data"]["kind"] for e in evs if e["kind"] == "ORDER"]
    assert "ENTRY" in kinds and "PROTECTIVE" in kinds
    assert any(e["kind"] == "FILL" for e in evs)
    decisions = [e["data"] for e in evs if e["kind"] == "DECISION"]
    assert any(d["verdict"] == "APPROVE" for d in decisions)
    assert any(d["verdict"] == "REJECT" for d in decisions)  # the SHADOW ATM intents do not fit the 2% budget
    last_pos = [e for e in evs if e["kind"] == "POSITION"][-1]
    assert last_pos["data"]["open"] is False and _t(last_pos) < time(15, 0)
    day_end = next(e for e in evs if e["kind"] == "DAY_END")
    assert day_end["data"]["kills"] == [] and day_end["data"]["trades"] >= 1


def test_broker_drop_day_latches_broker_connectivity_kill(replays: Replays) -> None:
    evs = replays["broker-drop"]
    kills = [e for e in evs if e["kind"] == "KILLS"][-1]["data"]["switches"]
    assert len(kills) == 9
    latched = {k["id"] for k in kills if k["latched"]}
    assert "BROKER_CONNECTIVITY_KILL" in latched
    t_kill = next(_t(e) for e in evs if e["kind"] == "LOG" and "BROKER_CONNECTIVITY_KILL LATCHED" in e["data"]["text"])
    assert time(11, 40, 10) <= t_kill <= time(11, 41)
    after = [e["data"] for e in evs if e["kind"] == "DECISION" and _t(e) > t_kill]
    assert after and all("KILL_ACTIVE" in d["reasons"] for d in after)
    assert not [e for e in evs if e["kind"] == "ORDER" and e["data"]["kind"] == "ENTRY" and _t(e) > t_kill]


def test_manual_master_kill_mid_session_blocks_new_entries(kernel: Kernel, tmp_path: Path) -> None:
    sess = SimulatedSession(REPLAY_SCENARIOS[0], kernel, tmp_path)
    it = sess.run()
    for e in it:
        if e.kind is EventKind.TICK and e.ts.time() >= time(9, 25):
            break
    res = sess.manual_master_kill("test: owner pressed the button", "owner")
    assert res["latched"] is True
    assert sess.manual_master_kill("again", "owner")["already_latched"] is True
    drained = sess.drain_after_kill()
    assert any(e.kind is EventKind.KILLS for e in drained)
    rest = list(it)
    assert not [e for e in rest if e.kind is EventKind.ORDER and e.data["kind"] == "ENTRY"]
    decs = [e for e in rest if e.kind is EventKind.DECISION]
    assert decs and all("KILL_ACTIVE" in e.data["reasons"] for e in decs)
    with pytest.raises(DashboardError, match="ended"):
        sess.manual_master_kill("too late", "owner")


def test_economics_card_is_simulated_advisory_and_reconciles(replays: Replays) -> None:
    """docs/risk/system-economics.md on the dashboard: an ECONOMICS event at session start, after each completed round
    trip and at
    day end, built from the kernel's own journal. Advisory only, SIMULATED, no new endpoint."""
    for name, evs in replays.items():
        econ = [e for e in evs if e["kind"] == "ECONOMICS"]
        assert len(econ) >= 2, name
        assert evs.index(econ[0]) < 4  # at session start
        end = next(i for i, e in enumerate(evs) if e["kind"] == "DAY_END")
        assert evs[end - 1]["kind"] == "ECONOMICS"
        for e in econ:
            d = e["data"]
            assert e["simulated"] is True and d["simulated"] is True
            assert d["advisory_only"] is True and d["blocks_trading"] is False
            assert d["fixed_monthly"] == "1163.81" and d["nav_for_fixed_threshold"] == "116381"
            assert d["raised"] is True and "ADVISORY" in d["headline"]  # Rs 10k canary: structural, as expected
            assert any("not a kill switch" in r for r in d["recommendations"])
            assert d["tax_status"] == "ASSUMED" and d["month"] == "2026-10"
            t = d["today"]
            assert t["month_trading_days"] == 20  # Oct-2026: Gandhi Jayanti (2nd) and Dussehra (20th) are holidays
            gross, charges, share, tax = (Decimal(t[k]) for k in ("gross", "charges", "fixed_share", "tax"))
            assert share == (Decimal("1163.80909") / 20).quantize(Decimal("0.01"))
            assert tax >= 0 and Decimal(t["net"]) == gross - charges - share - tax
        last = econ[-1]["data"]["today"]
        day_end = evs[end]["data"]
        assert last["round_trips"] == day_end["trades"]
        # repricing per executed order is never dearer than the kernel's per-fill charging
        assert Decimal(last["gross"]) - Decimal(last["charges"]) >= Decimal(day_end["realised"]) - Decimal("0.01")


ACTIVITY_TONES = {"info", "ok", "warn", "reject", "bad", "muted"}


def test_every_log_carries_human_readable_activity(replays: Replays) -> None:
    """System Activity (v3): the backend writes the sentence; the raw text stays for the Raw-logs view."""
    for evs in replays.values():
        logs = [e["data"] for e in evs if e["kind"] == "LOG"]
        assert logs
        for d in logs:
            a = d["activity"]
            assert a["actor"] in NODE_IDS and a["tone"] in ACTIVITY_TONES and a["text"] and d["text"]
        texts = [d["activity"]["text"] for d in logs]
        assert any(t.startswith("Risk Governor") for t in texts)
        assert any(t.startswith("Order executed") for t in texts)


def _tony(evs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in evs if e["kind"] == "TONY"]


def test_tony_is_simulated_deterministic_and_emitted_on_change_not_every_tick(replays: Replays) -> None:
    for evs in replays.values():
        tonys = _tony(evs)
        ticks = [e for e in evs if e["kind"] == "TICK"]
        assert tonys and len(tonys) < len(ticks) / 2
        assert evs.index(tonys[0]) < 6  # the panel is populated from the first seconds of a session
        assert all(e["simulated"] is True and e["data"]["labels"] == ["SIMULATED"] for e in tonys)
        assert all("no language model" in e["data"]["generated_by"] for e in tonys)
        for e in tonys:
            assert e["data"]["now"] and e["data"]["why"] and e["data"]["next"]
            assert e["data"]["attention"]["level"] in {"NORMAL", "ATTENTION", "INTERVENTION"}
        # nothing in Tony claims a model confidence the system does not produce
        assert not any(re.search(r"[Cc]onfidence\s*:?\s*\d", json.dumps(e["data"])) for e in tonys)


def _levels(evs: list[dict[str, Any]]) -> list[tuple[time, str]]:
    return [(_t(e), e["data"]["attention"]["level"]) for e in _tony(evs)]


def test_attention_states_follow_the_scenarios(replays: Replays) -> None:
    assert {lvl for _, lvl in _levels(replays["normal-1"])} == {"NORMAL"}
    n2 = _levels(replays["normal-2"])
    first_att = next(t for t, lvl in n2 if lvl == "ATTENTION")
    assert time(11, 59) <= first_att <= time(12, 1)
    att = next(e for e in _tony(replays["normal-2"]) if e["data"]["attention"]["level"] == "ATTENTION")
    assert att["data"]["attention"]["concerns"][0]["code"] == "REJECTION_STREAK"
    bd = _levels(replays["broker-drop"])
    first_int = next(t for t, lvl in bd if lvl == "INTERVENTION")
    assert time(11, 40, 10) <= first_int <= time(11, 41)
    assert all(lvl == "INTERVENTION" for t, lvl in bd if t >= first_int)
    assert any(lvl == "ATTENTION" for t, lvl in bd if t < first_int)  # the broker link was down before the kill
    last = _tony(replays["broker-drop"])[-1]["data"]
    assert last["now"].startswith("Trading halted: Broker link kill switch")
    # attention changes are themselves logged as activity from Tony
    acts = [e["data"]["activity"] for e in replays["broker-drop"] if e["kind"] == "LOG"]
    assert any(a["actor"] == "cio" and a["text"].startswith("Intervention required") for a in acts)


def test_manual_kill_drain_reports_intervention(kernel: Kernel, tmp_path: Path) -> None:
    sess = SimulatedSession(REPLAY_SCENARIOS[0], kernel, tmp_path)
    it = sess.run()
    for e in it:
        if e.kind is EventKind.TICK and e.ts.time() >= time(9, 25):
            break
    sess.manual_master_kill("test", "owner")
    drained = sess.drain_after_kill()
    t = [e for e in drained if e.kind is EventKind.TONY]
    assert t and t[-1].data["attention"]["level"] == "INTERVENTION"
    assert "Manual master kill" in t[-1].data["now"] or "MANUAL" in t[-1].data["now"].upper()


def test_regime_events_come_from_the_k11_classifier_and_say_unvalidated(replays: Replays, kernel: Kernel) -> None:
    for name, evs in replays.items():
        regs = [e["data"] for e in evs if e["kind"] == "REGIME"]
        assert regs, name
        assert regs[0]["warmup"] is True
        assert regs[0]["tags"] == ["NO_EDGE"]  # nothing classified before the first minute closes
        for d in regs:
            assert d["classifier"] == kernel.regime.version
            assert d["status"] == "UNVALIDATED"
            assert d["inputs"] == "SIMULATED"
            assert "not a confidence" in d["classifier_agreement_note"]
        live = [d for d in regs if not d["warmup"]]
        assert live, name
        for d in live:
            assert {"trend", "volatility", "gap", "opening"} <= d.keys()
            assert d["trend"] in {"UP", "DOWN", "RANGE"}
            assert "NO_EDGE" not in d["tags"]
        # the gap is known: the simulator supplies the synthetic previous close
        assert all(d["gap"] != "UNKNOWN" for d in live)


def test_the_regime_labels_are_reproducible(kernel: Kernel) -> None:
    a = [e["data"] for e in build_replay(REPLAY_SCENARIOS[1], kernel) if e["kind"] == "REGIME"]
    b = [e["data"] for e in build_replay(REPLAY_SCENARIOS[1], kernel) if e["kind"] == "REGIME"]
    assert a == b
