"""An in-memory Upstox REST simulation for adapter tests. No network; SIMULATED fills only.

It models the documented request/response shapes (place v3, order book, Exit-All) and the UNVERIFIED ones the
adapter assumes (modify, cancel, trades, positions, funds), matches LIMIT and SL orders against a quote the test
sets, and can inject the failures the gateway must survive.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from project100c.broker.upstox.transport import HttpResponse
from project100c.errors import BrokerDisconnectedError, BrokerTimeoutError
from project100c.sessions import IST

TOKEN = "fake-session-token"


def _err(status: int, code: str, msg: str, **extra: Any) -> HttpResponse:
    return HttpResponse(status, {"status": "error", "errors": [{"errorCode": code, "message": msg, **extra}]})


@dataclass
class Faults:
    place: list[str] = field(default_factory=list)  # per place call: "timeout_after_accept", "timeout", "5xx", ...
    reads_disconnected: bool = False
    exit_all_skip: set[str] = field(default_factory=set)


class FakeUpstox:
    def __init__(self, clock: Callable[[], datetime], *, funds: Decimal = Decimal("10000")) -> None:
        self.clock = clock
        self.funds = funds
        self.faults = Faults()
        self.quotes: dict[str, tuple[Decimal, Decimal]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.trades: list[dict[str, Any]] = []
        self.pos: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[Mapping[str, Any]] = []
        self.headers: list[Mapping[str, str]] = []
        self._ids = itertools.count(261005000000001)
        self._tids = itertools.count(1)

    # -- simulation -----------------------------------------------------------------------------------------
    def set_quote(self, key: str, bid: str, ask: str) -> None:
        self.quotes[key] = (Decimal(bid), Decimal(ask))
        self.match()

    def _ts(self) -> str:
        return self.clock().astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")

    def _fill(self, o: dict[str, Any], px: Decimal) -> None:
        q = o["quantity"] - o["filled_quantity"]
        o["filled_quantity"] += q
        o["pending_quantity"] = 0
        o["average_price"] = float(px)
        o["status"] = "complete"
        o["exchange_timestamp"] = self._ts()
        self.trades.append(
            {
                "trade_id": f"T{next(self._tids)}",
                "order_id": o["order_id"],
                "instrument_token": o["instrument_token"],
                "transaction_type": o["transaction_type"],
                "quantity": q,
                "average_price": float(px),
                "exchange_timestamp": self._ts(),
                "order_timestamp": o["order_timestamp"],
            }
        )
        p = self.pos.setdefault(
            o["instrument_token"],
            {
                "instrument_token": o["instrument_token"],
                "quantity": 0,
                "day_buy_quantity": 0,
                "day_sell_quantity": 0,
                "day_buy_value": 0.0,
                "day_sell_value": 0.0,
                "product": "I",
            },
        )
        if o["transaction_type"] == "BUY":
            p["quantity"] += q
            p["day_buy_quantity"] += q
            p["day_buy_value"] += float(px) * q
        else:
            p["quantity"] -= q
            p["day_sell_quantity"] += q
            p["day_sell_value"] += float(px) * q

    def match(self) -> None:
        for o in self.orders.values():
            if o["status"] not in ("open", "trigger pending") or o["instrument_token"] not in self.quotes:
                continue
            bid, ask = self.quotes[o["instrument_token"]]
            px, trig = Decimal(str(o["price"])), Decimal(str(o["trigger_price"]))
            if o["status"] == "trigger pending":
                hit = bid <= trig if o["transaction_type"] == "SELL" else ask >= trig
                if not hit:
                    continue
                o["status"] = "open"
            if o["transaction_type"] == "BUY" and ask <= px:
                self._fill(o, ask)
            elif o["transaction_type"] == "SELL" and bid >= px:
                self._fill(o, bid)

    # -- Transport ------------------------------------------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        form_body: Mapping[str, str] | None = None,
        timeout_s: float,
    ) -> HttpResponse:
        u = urlsplit(url)
        path, host = u.path, u.netloc
        self.calls.append((method, path))
        self.headers.append(dict(headers))
        self.bodies.append(dict(json_body or {}))
        p = dict(params or {})
        if path.startswith("/v3/login/auth/token/request/"):
            return HttpResponse(
                200,
                {
                    "status": "success",
                    "data": {"authorization_expiry": "1791325800000", "notifier_url": "https://example.invalid/hook"},
                },
            )
        if headers.get("Authorization") != f"Bearer {TOKEN}":
            return _err(401, "UDAPI100050", "Invalid token used to access API")
        if path.startswith("/v3/order/") and host != "api-hft.upstox.com":
            return _err(404, "UDAPI100060", "Resource not Found.")
        self.match()
        if method == "POST" and path == "/v3/order/place":
            return self._place(dict(json_body or {}))
        if self.faults.reads_disconnected:
            raise BrokerDisconnectedError("injected: connection reset")
        if method == "PUT" and path == "/v3/order/modify":
            b = dict(json_body or {})
            o = self.orders.get(str(b.get("order_id")))
            if o is None or o["status"] not in ("open", "trigger pending"):
                return _err(400, "UDAPI100040", "Order not modifiable")
            o["price"], o["trigger_price"] = b["price"], b["trigger_price"]
            self.match()
            return HttpResponse(200, {"status": "success", "data": {"order_id": o["order_id"]}})
        if method == "DELETE" and path == "/v3/order/cancel":
            o = self.orders.get(p.get("order_id", ""))
            if o is None or o["status"] not in ("open", "trigger pending"):
                return _err(400, "UDAPI100041", "Order not cancellable")
            o["status"] = "cancelled"
            return HttpResponse(200, {"status": "success", "data": {"order_id": o["order_id"]}})
        if method == "GET" and path == "/v2/order/retrieve-all":
            return HttpResponse(200, {"status": "success", "data": [dict(o) for o in self.orders.values()]})
        if method == "GET" and path == "/v2/order/trades/get-trades-for-day":
            return HttpResponse(200, {"status": "success", "data": list(self.trades)})
        if method == "GET" and path == "/v2/portfolio/short-term-positions":
            return HttpResponse(200, {"status": "success", "data": [dict(v) for v in self.pos.values()]})
        if method == "GET" and path == "/v2/user/get-funds-and-margin":
            return HttpResponse(
                200,
                {"status": "success", "data": {"equity": {"available_margin": float(self.funds), "used_margin": 0.0}}},
            )
        if method == "POST" and path == "/v2/order/positions/exit":
            return self._exit_all()
        return _err(404, "UDAPI100060", f"Resource not Found: {method} {path}")

    def _place(self, b: dict[str, Any]) -> HttpResponse:
        fault = self.faults.place.pop(0) if self.faults.place else None
        if fault == "timeout":
            raise BrokerTimeoutError("injected: read timed out")
        if fault == "disconnect":
            raise BrokerDisconnectedError("injected: connection refused")
        if fault == "5xx":
            return HttpResponse(503, {"_raw": "Service Unavailable"})
        if fault == "static_ip":
            return _err(403, "UDAPI1154", "Orders are allowed only from a registered static IP")
        if fault == "reject":
            return _err(400, "UDAPI1004", "Valid order type is required")
        assert len(b["tag"]) <= 40 and b["product"] == "I" and b["validity"] == "DAY"
        assert b["order_type"] in ("LIMIT", "SL") and b["slice"] is False and b["is_amo"] is False
        oid = str(next(self._ids))
        self.orders[oid] = {
            "order_id": oid,
            "exchange_order_id": f"X{oid}",
            "tag": b["tag"],
            "instrument_token": b["instrument_token"],
            "transaction_type": b["transaction_type"],
            "quantity": b["quantity"],
            "order_type": b["order_type"],
            "price": b["price"],
            "trigger_price": b["trigger_price"],
            "product": "I",
            "validity": "DAY",
            "status": "trigger pending" if b["order_type"] == "SL" else "open",
            "filled_quantity": 0,
            "pending_quantity": b["quantity"],
            "average_price": 0,
            "status_message": None,
            "order_timestamp": self._ts(),
            "exchange_timestamp": self._ts(),
            "variety": "SIMPLE",
            "is_amo": False,
        }
        self.match()
        if fault == "timeout_after_accept":
            raise BrokerTimeoutError("injected: accepted, then the response was lost")
        return HttpResponse(200, {"status": "success", "data": {"order_ids": [oid]}, "metadata": {"latency": 9}})

    def _exit_all(self) -> HttpResponse:
        open_pos = [p for p in self.pos.values() if p["quantity"] != 0]
        if not open_pos:
            return _err(400, "UDAPI1111", "No open positions to exit")
        ids: list[str] = []
        errors: list[dict[str, Any]] = []
        for p in open_pos:
            key = p["instrument_token"]
            if key in self.faults.exit_all_skip:
                errors.append({"errorCode": "UDAPI1113", "message": "exit failed", "instrument_key": key})
                continue
            oid = str(next(self._ids))
            bid, ask = self.quotes[key]
            side = "SELL" if p["quantity"] > 0 else "BUY"
            o = {
                "order_id": oid,
                "exchange_order_id": f"X{oid}",
                "tag": None,
                "instrument_token": key,
                "transaction_type": side,
                "quantity": abs(p["quantity"]),
                "order_type": "MARKET",
                "price": 0,
                "trigger_price": 0,
                "product": "I",
                "validity": "DAY",
                "status": "open",
                "filled_quantity": 0,
                "pending_quantity": abs(p["quantity"]),
                "average_price": 0,
                "status_message": None,
                "order_timestamp": self._ts(),
                "exchange_timestamp": self._ts(),
                "variety": "SIMPLE",
                "is_amo": False,
            }
            self.orders[oid] = o
            self._fill(o, bid if side == "SELL" else ask)  # SIMULATED: MARKET with protection fills at the touch
            ids.append(oid)
        if errors:
            return HttpResponse(
                207,
                {
                    "status": "partial_success",
                    "data": {"order_ids": ids},
                    "errors": errors,
                    "summary": {"total": len(open_pos), "success": len(ids), "error": len(errors)},
                },
            )
        return HttpResponse(
            200,
            {
                "status": "success",
                "data": {"order_ids": ids},
                "summary": {"total": len(ids), "success": len(ids), "error": 0},
            },
        )
