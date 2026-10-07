"""K-B2: the Upstox **sandbox** contract suite. It runs only when a sandbox access token is in the environment;
without one every networked test is skipped (the CI default). It never touches the live API: the base URL must be
a sandbox host, or the suite refuses before any request.

Run it (on the research or CI host, never the trading host, docs/engineering/security.md §16.3)::

    export UPSTOX_SANDBOX_ACCESS_TOKEN=...      # from the Upstox developer console, sandbox app (30 days)
    export UPSTOX_SANDBOX_INSTRUMENT='NSE_EQ|INE669E01016'   # optional; the SDK's own example
    .venv/bin/python -m pytest -q tests/contract -rs

What the sandbox can verify (verified 3-Oct-2026 from Upstox's sandbox page and the official Python SDK, whose
sandbox host is ``https://api-sandbox.upstox.com`` and whose sandbox endpoints are v2/v3 place, modify and cancel
and v2 multi-place): the v3 place, modify and cancel request shapes this adapter sends (LIMIT and SL, intraday,
DAY, ``slice=false``, the hashed tag), the response shape (``data.order_ids``), and how errors come back (HTTP
status, ``errors[].errorCode``) for a bad instrument and a bad token.

What it cannot verify: the order book, trades, positions and funds reads, order statuses after placement, and
**Exit-All** (not sandbox-enabled). Those stay UNVERIFIED until the read-only live session suite (K-B3) and the
live plumbing test (docs/risk/canary-criteria.md §15.3).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import pytest

from project100c.broker import OrderRequest, OrderType
from project100c.broker.upstox import UpstoxBroker, UpstoxConfig, UpstoxSession
from project100c.broker.upstox.transport import HttpResponse, HttpTransport
from project100c.core_types import OrderSide
from project100c.errors import BrokerDisconnectedError, BrokerRejectError
from project100c.sessions import IST

ENV_ACCESS = "UPSTOX_SANDBOX_ACCESS_TOKEN"
ENV_BASE = "UPSTOX_SANDBOX_BASE_URL"
ENV_INSTRUMENT = "UPSTOX_SANDBOX_INSTRUMENT"
ENV_PRICE = "UPSTOX_SANDBOX_PRICE"
DEFAULT_BASE = "https://api-sandbox.upstox.com"
DEFAULT_INSTRUMENT = "NSE_EQ|INE669E01016"  # the instrument in Upstox's own SDK sandbox example

needs_sandbox = pytest.mark.skipif(not os.environ.get(ENV_ACCESS), reason=f"{ENV_ACCESS} not set (no sandbox key)")


def sandbox_config(env: Mapping[str, str]) -> UpstoxConfig:
    """The adapter config for the sandbox. Refuses any host that is not an Upstox sandbox host."""
    base = env.get(ENV_BASE, DEFAULT_BASE).rstrip("/")
    u = urlsplit(base)
    host = u.hostname or ""
    if u.scheme != "https" or not host.endswith(".upstox.com") or "sandbox" not in host:
        raise ValueError(f"refusing {base!r}: the contract suite runs only against an https Upstox sandbox host")
    return UpstoxConfig(base_url=base, hft_base_url=base, timeout_s=10.0)


class Recording:
    """Wraps the real transport and keeps (method, path, status, top-level keys, error codes): never headers."""

    def __init__(self) -> None:
        self._t = HttpTransport()
        self.calls: list[tuple[str, str, int, tuple[str, ...], tuple[str, ...]]] = []

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
        r = self._t.request(
            method, url, headers=headers, params=params, json_body=json_body, form_body=form_body, timeout_s=timeout_s
        )
        raw = r.body.get("errors")
        errs: list[Any] = raw if isinstance(raw, list) else []
        codes = tuple(str(e.get("errorCode") or e.get("error_code") or "") for e in errs if isinstance(e, dict))
        self.calls.append((method, urlsplit(url).path, r.status, tuple(sorted(r.body)), codes))
        return r


def _broker(token: str | None = None) -> tuple[UpstoxBroker, Recording]:
    rec = Recording()
    session = UpstoxSession(token or os.environ[ENV_ACCESS], datetime.now(IST))
    b = UpstoxBroker(session, clock=lambda: datetime.now(IST), transport=rec, config=sandbox_config(os.environ))
    return b, rec


def _req(order_type: OrderType = OrderType.LIMIT, *, instrument: str | None = None) -> OrderRequest:
    """A one-unit BUY LIMIT, or a SELL stop-limit shaped like our protective stop (trigger above the limit)."""
    price = Decimal(os.environ.get(ENV_PRICE, "9.10"))
    sl = order_type is OrderType.SL
    return OrderRequest(
        f"K-B2-{uuid.uuid4().hex[:10]}",
        instrument or os.environ.get(ENV_INSTRUMENT, DEFAULT_INSTRUMENT),
        OrderSide.SELL if sl else OrderSide.BUY,
        1,
        order_type,
        price,
        price + Decimal("0.05") if sl else None,
        tag="PROTECTIVE" if sl else "ENTRY",
    )


# ------------------------------------------------------------------ always run (no key needed)
def test_the_guard_refuses_anything_but_a_sandbox_host() -> None:
    assert sandbox_config({}).hft_base_url == DEFAULT_BASE
    for bad in (
        "https://api.upstox.com",
        "https://api-hft.upstox.com",
        "http://api-sandbox.upstox.com",
        "https://api-sandbox.upstox.com.evil.example",
        "https://sandbox.example.com",
    ):
        with pytest.raises(ValueError, match="refusing"):
            sandbox_config({ENV_BASE: bad})


# ------------------------------------------------------------------ sandbox (skipped without a key)
@needs_sandbox
def test_v3_place_limit_returns_one_order_id() -> None:
    b, rec = _broker()
    oid = b.place(_req())
    assert isinstance(oid, str) and oid
    (call,) = rec.calls
    assert call[:3] == ("POST", "/v3/order/place", 200) and "data" in call[3]


@needs_sandbox
def test_v3_modify_and_cancel_accept_our_shapes() -> None:
    b, rec = _broker()
    oid = b.place(_req())
    b.modify(oid, price=Decimal(os.environ.get(ENV_PRICE, "9.10")) - Decimal("0.05"))
    b.cancel(oid)
    assert [(m, p, s) for m, p, s, _, _ in rec.calls] == [
        ("POST", "/v3/order/place", 200),
        ("PUT", "/v3/order/modify", 200),
        ("DELETE", "/v3/order/cancel", 200),
    ]


@needs_sandbox
def test_v3_place_sell_stop_limit_like_a_protective_stop() -> None:
    b, _ = _broker()
    assert b.place(_req(OrderType.SL))


@needs_sandbox
def test_a_bad_instrument_is_a_reject_with_an_error_code() -> None:
    b, rec = _broker()
    with pytest.raises(BrokerRejectError):
        b.place(_req(instrument="NSE_FO|0"))
    assert rec.calls[-1][2] in (400, 404, 422) and rec.calls[-1][4], rec.calls[-1]


@needs_sandbox
def test_a_bad_token_is_a_disconnect() -> None:
    b, rec = _broker(token="not-a-real-sandbox-value")
    with pytest.raises(BrokerDisconnectedError):
        b.place(_req())
    assert rec.calls[-1][2] == 401 or rec.calls[-1][4], rec.calls[-1]
