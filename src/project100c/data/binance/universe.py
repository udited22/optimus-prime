"""Point-in-time crypto universe: BTC plus the top N by market cap, excluding stablecoins and wrapped/staked
duplicates (configs/data/crypto_universe.toml). ``rank_snapshot`` re-derives the list from a keyless ranking
snapshot (CoinLore ticker JSON) so the pinned config can be checked against the stored raw bytes."""

from __future__ import annotations

import json
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from project100c.errors import ConfigError, VendorResponseError


class UniverseSymbol(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    asset: str = Field(pattern=r"^[A-Z0-9]{2,12}$")
    rank: int = Field(gt=0)
    cap_usd_bn: Decimal
    note: str = ""


class CryptoUniverse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    as_of_utc: str
    ranking_source: str
    quote: str
    top_n: int = Field(gt=0, le=50)
    exclude_stablecoins: tuple[str, ...]
    exclude_wrapped_or_staked: tuple[str, ...]
    symbols: tuple[UniverseSymbol, ...]

    def pairs(self) -> list[str]:
        return [f"{s.asset}{self.quote}" for s in self.symbols]


def load_universe(path: Path) -> CryptoUniverse:
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        u = CryptoUniverse.model_validate(raw)
    except (FileNotFoundError, tomllib.TOMLDecodeError, ValidationError) as e:
        raise ConfigError(f"crypto universe {path}: {e}") from e
    if not u.symbols or u.symbols[0].asset != "BTC":
        raise ConfigError("the universe must start with BTC")
    if len(u.symbols) != u.top_n + 1:
        raise ConfigError(f"expected BTC + {u.top_n} symbols, got {len(u.symbols)}")
    return u


def rank_snapshot(body: bytes, *, top_n: int, excluded: set[str]) -> list[dict[str, Any]]:
    """BTC plus the next ``top_n`` non-excluded assets by market cap from a CoinLore ``/api/tickers/`` reply."""
    try:
        data = json.loads(body, parse_float=Decimal)["data"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise VendorResponseError(f"ranking snapshot: {e}") from None
    rows = sorted(data, key=lambda x: -Decimal(str(x["market_cap_usd"])))
    up = {e.upper() for e in excluded}
    out: list[dict[str, Any]] = []
    for x in rows:
        sym = str(x["symbol"]).upper()
        if sym in up:
            continue
        out.append({"asset": sym, "rank": int(x["rank"]), "cap_usd": str(x["market_cap_usd"]), "name": x["name"]})
        if len(out) == top_n + 1:
            break
    if not out or out[0]["asset"] != "BTC":
        raise VendorResponseError("ranking snapshot: BTC is not first")
    return out
