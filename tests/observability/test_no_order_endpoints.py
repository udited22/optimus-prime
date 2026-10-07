"""READ-ONLY BY CONSTRUCTION: the dashboard cannot place, modify or cancel an order.

The only state-changing capability is MANUAL_MASTER_KILL, behind a two-step arm/confirm, delivered to a kill-only
port. These tests pin that down from four sides: the route table, live HTTP probing (with a real SIMULATED kernel
behind the server, whose journal must show no new order), the server module's imports, and the kill gate itself.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import time
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from project100c.journal import Journal
from project100c.observability.dashboard import server as server_mod
from project100c.observability.dashboard.events import DashboardError, EventBus, EventKind
from project100c.observability.dashboard.server import (
    ARM_TTL_S,
    CONFIRM_HEADER,
    CONFIRM_WORD,
    ROUTES,
    KillGate,
    match_route,
)
from project100c.observability.dashboard.simulator import REPLAY_SCENARIOS, Kernel, KillPort, SimulatedSession

from .helpers import StubKill, serve

ORDERISH = re.compile(r"order|trade|buy|sell|place|modify|cancel|exit|position|flatten|broker|execute|submit", re.I)
HDR = {CONFIRM_HEADER: CONFIRM_WORD}
PROBE_PATHS = [
    "/api/order", "/api/orders", "/api/orders/1", "/api/place_order", "/api/trade", "/api/trades", "/api/buy",
    "/api/sell", "/api/modify", "/api/cancel", "/api/cancel_all", "/api/exit", "/api/exit_all", "/api/positions",
    "/api/flatten", "/api/broker", "/api/broker/orders", "/api/intent", "/api/kill", "/api/kill/reset",
    "/api/reset", "/api/unkill", "/api/admin", "/order", "/v2/order/place", "/api/topology/order",
]  # fmt: skip


def test_route_table_has_no_order_like_path_and_only_the_kill_pair_is_post() -> None:
    for r in ROUTES:
        assert not ORDERISH.search(r.path), r
        assert r.method in ("GET", "POST")
    assert {r.path for r in ROUTES if r.method == "POST"} == {"/api/kill/arm", "/api/kill/confirm"}
    assert all(r.path.startswith("/api/kill/") for r in ROUTES if r.method != "GET")


@pytest.mark.parametrize("path", PROBE_PATHS)
def test_order_like_paths_are_not_routed_under_any_method(path: str) -> None:
    for m in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        assert match_route(m, path)[0] is None


@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz_/0123456789", min_size=1, max_size=30))
@settings(max_examples=300, deadline=None)
def test_any_post_path_other_than_the_kill_pair_is_unrouted(suffix: str) -> None:
    path = "/api/" + suffix
    r, _ = match_route("POST", path)
    assert r is None or path in ("/api/kill/arm", "/api/kill/confirm")


def test_server_module_cannot_reach_the_broker_or_the_kernel() -> None:
    tree = ast.parse(Path(server_mod.__file__).read_text())
    mods: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module)
    banned = ("project100c.broker", "project100c.kernel", "project100c.journal", "urllib.request", "socket")
    assert not [m for m in mods if m.startswith(banned)], mods
    assert "requests" not in mods and "http.client" not in mods


def test_kill_port_exposes_only_the_kill() -> None:
    p = KillPort(lambda r, b: {"ok": True})
    assert [a for a in dir(p) if not a.startswith("_")] == ["manual_master_kill"]
    with pytest.raises(AttributeError):
        p.submit = lambda: None  # type: ignore[attr-defined]  # __slots__: nothing can be attached


@pytest.fixture
def sim_server(tmp_path: Path, kernel: Kernel):  # type: ignore[no-untyped-def]
    """A real SIMULATED kernel on its own thread (as in live mode), fast-forwarded to 10:30 after its first trade,
    then held there (one step per ~4 hours of wall time), behind the HTTP server via the kill-only port."""
    from project100c.observability.dashboard.live import LiveRunner

    bus = EventBus()
    runner = LiveRunner(bus, kernel, speed=0.001, days=1, workdir=tmp_path, join_at=time(10, 30))
    runner.start_thread()
    at_1030 = lambda: any(e.kind is EventKind.TICK and e.ts.time() >= time(10, 30) for e in bus.tail(EventKind.TICK, 1))  # noqa: E731
    assert runner.wait_for(at_1030, timeout=30)
    sess = runner.session
    assert sess is not None
    try:
        with serve(bus, runner.kill_port()) as s:
            yield s, sess, tmp_path / f"journal-{sess.scenario.name}.sqlite"
    finally:
        runner.stop()


def _records(path: Path, event_type: str) -> list[dict[str, object]]:
    """Read the simulator's journal through a separate connection (the session's own belongs to its thread)."""
    j = Journal(path)
    try:
        return [r.payload for r in j.records() if r.event_type == event_type]
    finally:
        j.close()


def _orders(path: Path) -> int:
    return len(_records(path, "ORDER_SUBMITTED"))


def test_http_probing_never_creates_an_order(sim_server) -> None:  # type: ignore[no-untyped-def]
    s, sess, jpath = sim_server
    before = _orders(jpath)
    assert before >= 2  # the session did trade (entry + protective) before we started probing
    body = json.dumps({"side": "BUY", "qty": 65, "instrument_key": "NSE_FO|X", "price": "1"}).encode()
    for path in PROBE_PATHS:
        for m in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"):
            st_, _, _ = s.request(m, path, body if m != "GET" else None, {**HDR, "Content-Type": "application/json"})
            if m in ("GET", "POST"):
                assert st_ in (404, 405), (m, path, st_)
            else:
                assert st_ == 501, (m, path, st_)
    for r in ROUTES:  # every real route, fed an order-shaped body, still sends nothing
        path = r.path + ("x" if r.prefix else "")
        s.request(r.method, path + ("?once=1" if "stream" in path else ""), body, HDR)
    assert _orders(jpath) == before
    assert all(sw.value != "MANUAL_MASTER_KILL" for sw, _ in sess.rt.state.kills)


def test_kill_flow_over_http_latches_manual_master_in_the_simulated_kernel(sim_server) -> None:  # type: ignore[no-untyped-def]
    s, sess, jpath = sim_server
    assert s.json("POST", "/api/kill/arm")[0] == 400  # no custom header: a plain cross-site form cannot arm
    assert s.json("POST", "/api/kill/confirm", {"nonce": "guess", "confirm": CONFIRM_WORD}, HDR)[0] == 400
    st_, armed = s.json("POST", "/api/kill/arm", None, HDR)
    assert st_ == 200 and armed["expires_in_s"] == ARM_TTL_S
    st_, err = s.json("POST", "/api/kill/confirm", {"nonce": armed["nonce"], "confirm": "yes"}, HDR)
    assert st_ == 400 and CONFIRM_WORD in err["error"]
    # the nonce was single-use: the wrong word burned it
    assert s.json("POST", "/api/kill/confirm", {"nonce": armed["nonce"], "confirm": CONFIRM_WORD}, HDR)[0] == 400
    assert not sess.rt.state.kills
    nonce = s.json("POST", "/api/kill/arm", None, HDR)[1]["nonce"]
    st_, res = s.json("POST", "/api/kill/confirm", {"nonce": nonce, "confirm": CONFIRM_WORD}, HDR)
    assert st_ == 200 and res["latched"] is True and res["label"] == "SIMULATED", res
    assert {sw.value for sw, _ in sess.rt.state.kills} == {"MANUAL_MASTER_KILL"}
    rec = _records(jpath, "KILL_LATCHED")[-1]
    assert rec["switch"] == "MANUAL_MASTER_KILL" and "owner" in str(rec["reason"])
    # the second press is idempotent
    nonce = s.json("POST", "/api/kill/arm", None, HDR)[1]["nonce"]
    assert s.json("POST", "/api/kill/confirm", {"nonce": nonce, "confirm": CONFIRM_WORD}, HDR)[1]["already_latched"]


def test_a_kill_from_a_foreign_thread_is_refused_not_swallowed(kernel: Kernel, tmp_path: Path) -> None:
    import threading

    sess = SimulatedSession(REPLAY_SCENARIOS[0], kernel, tmp_path)
    next(iter(sess.run()))
    err: list[BaseException] = []

    def other() -> None:
        try:
            sess.manual_master_kill("from the wrong thread", "owner")
        except DashboardError as e:
            err.append(e)

    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert err and "own thread" in str(err[0])
    assert not sess.rt.state.kills


def test_kill_gate_expiry_and_single_use() -> None:
    now = [100.0]
    port = StubKill()
    g = KillGate(port, now=lambda: now[0])
    n = g.arm()["nonce"]
    now[0] += ARM_TTL_S + 0.1
    with pytest.raises(DashboardError, match="expired"):
        g.confirm(n, CONFIRM_WORD, "")
    n = g.arm()["nonce"]
    assert g.confirm(n, CONFIRM_WORD, "")["latched"] is True
    with pytest.raises(DashboardError):
        g.confirm(n, CONFIRM_WORD, "")
    assert len(port.calls) == 1 and port.calls[0][1] == "owner"


def test_live_runner_kill_end_to_end(kernel: Kernel, tmp_path: Path) -> None:
    from project100c.observability.dashboard.live import LiveRunner

    bus = EventBus()
    runner = LiveRunner(bus, kernel, speed=15 * 400, days=1, workdir=tmp_path, join_at=time(9, 25))
    runner.start_thread()
    try:
        assert runner.wait_for(lambda: bus.last_seq > 200)
        with serve(bus, runner.kill_port()) as s:
            nonce = s.json("POST", "/api/kill/arm", None, HDR)[1]["nonce"]
            st_, res = s.json("POST", "/api/kill/confirm", {"nonce": nonce, "confirm": CONFIRM_WORD}, HDR)
            assert st_ == 200 and res["latched"] is True and res["simulated"] is True
            snap = s.json("GET", "/api/snapshot")[1]
        latched = [k["id"] for k in snap["latest"]["KILLS"]["data"]["switches"] if k["latched"]]
        assert latched == ["MANUAL_MASTER_KILL"]
    finally:
        runner.stop()
