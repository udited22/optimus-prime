"""Backfill plan for the first real Dhan pull (D-06): which download jobs, in which order.

The plan is pure and deterministic: the same anchor date always yields the same job specs (and therefore the same
job ids and chunk keys), so a re-run with a fresh token resumes exactly where the last one stopped. The SQLite job
store (``jobs.JobStore``) is the checkpoint; this module only decides the order.

Order (most recent first, then backwards, as the owner asked):

1. Tier A, the most recent ``recent_days`` (default 365): NIFTY index and India VIX 1-minute (90-day windows),
   the active NIFTY futures, nearest-weekly options ATM±10 CE+PE, next-weekly options ATM±3 CE+PE.
2. Tier B, older history back to ``anchor - years``, newest window first, interleaving the series so that each step
   back in time is complete across series before the next one starts.

Every window is its own job, so the backtest can select exactly the windows it needs (the lake reader re-plans the
chunks of the job specs it is given). Facts behind the choices (verified on the first real pull, 2-Oct-2026):

* rolling ``expiryCode`` 1 = the nearest expiry on or after the trade date (expiry day included), 2 = the next one;
  0 is rejected by Dhan with DH-905 "expiryCode is required".
* ATM±10 is served for the nearest expiry; for code 2 the docs promise only ATM±3 (ATM+10 for code 2 came back
  incomplete in the probe), so the plan asks for ATM±3 there.
* Intraday candles: at most 90 days per call (DH-905 beyond), 1-minute data back to at least 2020-10. Expired
  futures are not reachable (no security id for expired contracts in the public scrip master).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from project100c.data.dhan.jobs import CandleJobSpec, RollingOptionJobSpec

INDEX_LABEL = "NIFTY-INDEX"
INDEX_SECURITY_ID = "13"
VIX_LABEL = "INDIA-VIX"
VIX_SECURITY_ID = "21"  # public scrip master: NSE,I,21,INDIA VIX (verified on the first pull: 375 bars/day)
NEAR_OFFSETS = tuple(range(-10, 11))
NEXT_OFFSETS = tuple(range(-3, 4))

Series = Literal["index", "vix", "futures", "options_near", "options_next"]


@dataclass(frozen=True, slots=True)
class ActiveFuture:
    label: str
    security_id: str


@dataclass(frozen=True, slots=True)
class BackfillItem:
    tier: Literal["A", "B"]
    series: Series
    spec: RollingOptionJobSpec | CandleJobSpec


# Active NIFTY futures from the public scrip master of 30-Sep-2026 (expired contracts have no reachable id).
ACTIVE_FUTURES = (
    ActiveFuture("NIFTY-FUT-2026-10-27", "48704"),
    ActiveFuture("NIFTY-FUT-2026-11-23", "61471"),
    ActiveFuture("NIFTY-FUT-2026-12-29", "58875"),
)
DEFAULT_ANCHOR = "2026-10-02"  # exclusive; fixed so the plan (and every job id) is stable across re-runs


def _windows_back(anchor: date, start: date, days: int) -> list[tuple[date, date]]:
    """[from, to) windows of ``days`` ending at ``anchor`` (exclusive), newest first, the last clipped at ``start``."""
    out: list[tuple[date, date]] = []
    hi = anchor
    while hi > start:
        lo = max(start, hi - timedelta(days=days))
        out.append((lo, hi))
        hi = lo
    return out


def _candle(label: str, sid: str, w: tuple[date, date]) -> CandleJobSpec:
    return CandleJobSpec(
        label=label,
        security_id=sid,
        exchange_segment="IDX_I",
        instrument="INDEX",
        from_date=w[0],
        to_date=w[1],
        window_days=90,
    )


def _opt(w: tuple[date, date], code: int, offsets: Sequence[int]) -> RollingOptionJobSpec:
    return RollingOptionJobSpec(
        from_date=w[0], to_date=w[1], window_days=30, expiry_codes=(code,), strike_offsets=tuple(offsets)
    )


def plan_backfill(
    *,
    anchor: date,
    years: int = 5,
    recent_days: int = 365,
    futures: Sequence[ActiveFuture] = (),
    futures_from: date | None = None,
) -> list[BackfillItem]:
    """The ordered backfill. ``anchor`` is exclusive (normally today). Deterministic for a given input."""
    if years < 1 or recent_days < 1:
        raise ValueError("years and recent_days must be >= 1")
    start = date(anchor.year - years, anchor.month, anchor.day)
    recent = max(start, anchor - timedelta(days=recent_days))
    cw = _windows_back(anchor, start, 90)
    ow = _windows_back(anchor, start, 30)
    items: list[BackfillItem] = []
    # Tier A: candles (cheap) first, then futures, then options newest-first
    for w in cw:
        if w[1] > recent:
            items.append(BackfillItem("A", "index", _candle(INDEX_LABEL, INDEX_SECURITY_ID, w)))
            items.append(BackfillItem("A", "vix", _candle(VIX_LABEL, VIX_SECURITY_ID, w)))
    f0 = futures_from or (anchor - timedelta(days=90))
    for fut in futures:
        items.append(
            BackfillItem(
                "A",
                "futures",
                CandleJobSpec(
                    label=fut.label,
                    security_id=fut.security_id,
                    exchange_segment="NSE_FNO",
                    instrument="FUTIDX",
                    oi=True,
                    from_date=f0,
                    to_date=anchor,
                    window_days=90,
                ),
            )
        )
    for w in ow:
        if w[1] > recent:
            items.append(BackfillItem("A", "options_near", _opt(w, 1, NEAR_OFFSETS)))
    for w in ow:
        if w[1] > recent:
            items.append(BackfillItem("A", "options_next", _opt(w, 2, NEXT_OFFSETS)))
    # Tier B: older history, newest first: the cheap candle series, then nearest-weekly, then next-weekly options
    for w in cw:
        if w[1] <= recent:
            items.append(BackfillItem("B", "index", _candle(INDEX_LABEL, INDEX_SECURITY_ID, w)))
            items.append(BackfillItem("B", "vix", _candle(VIX_LABEL, VIX_SECURITY_ID, w)))
    for w in ow:
        if w[1] <= recent:
            items.append(BackfillItem("B", "options_near", _opt(w, 1, NEAR_OFFSETS)))
    for w in ow:
        if w[1] <= recent:
            items.append(BackfillItem("B", "options_next", _opt(w, 2, NEXT_OFFSETS)))
    return items


# ---------------------------------------------------------------------------------------------- SENSEX (BSE)
SENSEX_INDEX_LABEL = "SENSEX-INDEX"
SENSEX_INDEX_SECURITY_ID = "51"  # public scrip master: BSE,I,51,...,SENSEX (IDX_I)
# SENSEX derivatives relaunch (BSE notice 20230327-65): the first day Dhan's rolling option data has bars
SENSEX_OPTIONS_FROM = date(2023, 5, 15)


def _sensex_opt(w: tuple[date, date], code: int, offsets: Sequence[int]) -> RollingOptionJobSpec:
    return RollingOptionJobSpec(
        underlying="SENSEX",
        underlying_security_id=int(SENSEX_INDEX_SECURITY_ID),
        exchange_segment="BSE_FNO",
        from_date=w[0],
        to_date=w[1],
        window_days=30,
        expiry_codes=(code,),
        strike_offsets=tuple(offsets),
    )


def plan_sensex_backfill(*, anchor: date, years: int = 5, recent_days: int = 365) -> list[BackfillItem]:
    """SENSEX backfill, same shape and order as ``plan_backfill``: the SENSEX index 1-minute (IDX_I 51) for
    ``years``; nearest-weekly options ATM±10 and next-weekly ATM±3, CE+PE, on BSE_FNO from the 15-May-2023
    relaunch (no earlier rolling data exists). No India-VIX analogue and no futures (Dhan has no expired
    futures; active SENSEX futures can be added like NIFTY's when needed). Deterministic for a given input."""
    if years < 1 or recent_days < 1:
        raise ValueError("years and recent_days must be >= 1")
    start = date(anchor.year - years, anchor.month, anchor.day)
    recent = max(start, anchor - timedelta(days=recent_days))
    cw = _windows_back(anchor, start, 90)
    ow = _windows_back(anchor, max(start, SENSEX_OPTIONS_FROM), 30)
    items: list[BackfillItem] = []
    tiers: tuple[Literal["A", "B"], ...] = ("A", "B")
    for t in tiers:
        cws = [w for w in cw if (w[1] > recent) == (t == "A")]
        ows = [w for w in ow if (w[1] > recent) == (t == "A")]
        items += [BackfillItem(t, "index", _candle(SENSEX_INDEX_LABEL, SENSEX_INDEX_SECURITY_ID, w)) for w in cws]
        items += [BackfillItem(t, "options_near", _sensex_opt(w, 1, NEAR_OFFSETS)) for w in ows]
        items += [BackfillItem(t, "options_next", _sensex_opt(w, 2, NEXT_OFFSETS)) for w in ows]
    return items
