"""Dhan instrument list on a REAL public fixture subset (tests/fixtures/dhan/README.md)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from project100c.data.dhan import download_instrument_list, futures_security_ids, load_dhan_config
from project100c.data.http import HttpResponse, ScriptedTransport
from project100c.data.lake import Lake, Zone
from project100c.errors import InstrumentMasterError, VendorServerError
from project100c.instruments import InstrumentKind, cross_check, parse_dhan_csv, parse_kite_csv, parse_upstox_json
from project100c.sessions import IST
from tests.data.dhan_fakes import CONFIGS

AS_OF = datetime(2026, 9, 30, 23, 27, tzinfo=IST)
NIFTY = frozenset({"NIFTY"})


@pytest.fixture(scope="module")
def dhan_csv(fixtures_dir: Path) -> Path:
    return fixtures_dir / "dhan" / "dhan_scrip_master_subset_20260930.csv"


def test_parse_real_subset(dhan_csv: Path) -> None:
    m, rep = parse_dhan_csv(dhan_csv, as_of=AS_OF, underlyings=NIFTY)
    assert rep.rows_total == 82 and rep.rows_used == 77
    assert rep.skipped == {"underlying_filtered": 2, "not_NSE_derivative": 3}
    assert m.lot_size("NIFTY") == 65
    assert m.tick_size("NIFTY", InstrumentKind.OPTION) == Decimal("0.05")
    assert m.tick_size("NIFTY", InstrumentKind.FUTURE) == Decimal("0.1")
    assert m.future_expiries("NIFTY") == (date(2026, 10, 27), date(2026, 11, 23), date(2026, 12, 29))


def test_cross_check_against_kite_and_upstox(dhan_csv: Path, fixtures_dir: Path) -> None:
    d, _ = parse_dhan_csv(dhan_csv, as_of=AS_OF, underlyings=NIFTY)
    k, _ = parse_kite_csv(fixtures_dir / "instruments" / "kite_NFO_subset_20260930.csv", as_of=AS_OF, underlyings=NIFTY)
    u, _ = parse_upstox_json(
        fixtures_dir / "instruments" / "upstox_NSE_subset_20260930.json", as_of=AS_OF, underlyings=NIFTY
    )
    # strict: any kind/right/expiry/strike/lot/tick disagreement raises. Dhan SECURITY_ID == NSE exchange token.
    assert [x for x in cross_check(d, u, "NIFTY") if x.field != "presence"] == []
    assert [x for x in cross_check(d, k, "NIFTY") if x.field != "presence"] == []


def test_bad_header_rejected(tmp_path: Path) -> None:
    p = tmp_path / "x.csv"
    p.write_text("EXCH_ID,SEGMENT\nNSE,D\n")
    with pytest.raises(InstrumentMasterError, match="missing columns"):
        parse_dhan_csv(p, as_of=AS_OF)


def test_download_sends_no_credentials_and_writes_ref(tmp_path: Path, dhan_csv: Path) -> None:
    cfg = load_dhan_config(CONFIGS / "data" / "dhan.toml")
    t = ScriptedTransport([HttpResponse(200, dhan_csv.read_bytes())])
    lake = Lake(tmp_path)
    snap = download_instrument_list(transport=t, config=cfg, lake=lake, wall_clock=lambda: AS_OF)
    req = t.requests[0]
    assert req.url == "https://images.dhan.co/api-data/api-scrip-master-detailed.csv" and req.method == "GET"
    assert {k.lower() for k in req.headers} == {"accept"}  # no access-token / client-id on a public file
    assert snap.part.rows == 77 and lake.parts(Zone.REF, "dhan_instruments") == [snap.part.path]
    assert lake.lineage_of(snap.part.path)["raw_sha256"] == snap.raw.sha256
    assert futures_security_ids(snap.master)[0] == ("48704", "2026-10-27")
    again = download_instrument_list(
        transport=ScriptedTransport([HttpResponse(200, dhan_csv.read_bytes())]),
        config=cfg,
        lake=lake,
        wall_clock=lambda: AS_OF,
    )
    assert again.part.sha256 == snap.part.sha256  # deterministic


def test_download_http_error_raises(tmp_path: Path) -> None:
    cfg = load_dhan_config(CONFIGS / "data" / "dhan.toml")
    with pytest.raises(VendorServerError):
        download_instrument_list(
            transport=ScriptedTransport([HttpResponse(503, b"")]),
            config=cfg,
            lake=Lake(tmp_path),
            wall_clock=lambda: AS_OF,
        )


def test_config_guards(tmp_path: Path) -> None:
    from project100c.errors import ConfigError

    src = (CONFIGS / "data" / "dhan.toml").read_text()
    for bad in (
        src.replace("requests_per_second = 4", "requests_per_second = 6"),
        src.replace('api_base = "https://api.dhan.co/v2"', 'api_base = "http://api.dhan.co/v2"'),
        src.replace('api_base = "https://api.dhan.co/v2"', 'api_base = "https://evil.example/v2"'),
        src.replace('http_timeout_seconds = "90"', "http_timeout_seconds = 90.0"),
    ):
        p = tmp_path / "d.toml"
        p.write_text(bad)
        with pytest.raises(ConfigError):
            load_dhan_config(p)
    with pytest.raises(ConfigError):
        load_dhan_config(tmp_path / "missing.toml")
