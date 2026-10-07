"""Binance public data archive (https://data.binance.vision): listing, download and checksum verification.

Layout (https://github.com/binance/binance-public-data):

* ``data/spot/{monthly|daily}/klines/{SYMBOL}/1m/{SYMBOL}-1m-{YYYY-MM|YYYY-MM-DD}.zip``
* ``data/futures/um/{monthly|daily}/klines/{SYMBOL}/1m/...``  (USD-M perpetuals)
* ``data/futures/um/monthly/fundingRate/{SYMBOL}/{SYMBOL}-fundingRate-{YYYY-MM}.zip``
* ``data/futures/um/daily/metrics/{SYMBOL}/{SYMBOL}-metrics-{YYYY-MM-DD}.zip`` (open interest, long/short ratios)

Every zip has a ``.CHECKSUM`` sidecar ("<sha256>  <file name>"). ``fetch_verified`` downloads both and raises
``ChecksumMismatchError`` unless the SHA-256 of the zip matches; a mismatch is retried once (CDN hiccup) first.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote

from project100c.data.binance.config import BinanceConfig
from project100c.data.lake import sha256_hex
from project100c.data.public_http import PublicGetter
from project100c.errors import ChecksumMismatchError, VendorResponseError

_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_SYMBOL = re.compile(r"^[A-Z0-9]{2,20}$")


class ArchiveKind(StrEnum):
    SPOT_KLINES = "spot_klines"
    UM_KLINES = "um_klines"
    UM_RATE = "um_fundingrate"  # 8-hourly (or 4-hourly) perpetual rate settlements
    UM_METRICS = "um_metrics"  # 5-minute open interest and long/short ratios


def _check_symbol(symbol: str) -> str:
    if not _SYMBOL.match(symbol):
        raise ValueError(f"bad symbol {symbol!r}")
    return symbol


def prefix(kind: ArchiveKind, symbol: str, period: str) -> str:
    """Archive key prefix for one (kind, symbol, period) where period is 'monthly' or 'daily'."""
    s = _check_symbol(symbol)
    if period not in ("monthly", "daily"):
        raise ValueError(f"bad period {period!r}")
    if kind is ArchiveKind.SPOT_KLINES:
        return f"data/spot/{period}/klines/{s}/1m/"
    if kind is ArchiveKind.UM_KLINES:
        return f"data/futures/um/{period}/klines/{s}/1m/"
    if kind is ArchiveKind.UM_RATE:
        if period != "monthly":
            raise ValueError("the rate archive is monthly only")
        return f"data/futures/um/monthly/fundingRate/{s}/"
    if period != "daily":
        raise ValueError("the metrics archive is daily only")
    return f"data/futures/um/daily/metrics/{s}/"


_PERIOD_RE = re.compile(r"-(\d{4}-\d{2}(?:-\d{2})?)\.zip$")


def period_of(key: str) -> str:
    """'2021-01' (monthly) or '2021-01-31' (daily) from an archive key."""
    m = _PERIOD_RE.search(key)
    if m is None:
        raise VendorResponseError(f"archive key without a period: {key}")
    return m.group(1)


@dataclass(frozen=True, slots=True)
class ArchiveFile:
    key: str
    body: bytes
    sha256: str
    checksum_line: str
    attempts: int


class BinanceArchive:
    def __init__(self, *, config: BinanceConfig, getter: PublicGetter) -> None:
        self._cfg = config
        self._get = getter

    def list_zips(self, key_prefix: str) -> list[str]:
        """Every ``.zip`` key under the prefix (S3 ListObjects v1, paginated with ``marker``), sorted."""
        keys: list[str] = []
        marker = ""
        for _ in range(1000):
            url = f"{self._cfg.listing_base}?prefix={quote(key_prefix)}&marker={quote(marker)}"
            res = self._get.get(url, accept="application/xml")
            if res is None:
                return []
            try:
                root = ET.fromstring(res.response.body)
            except ET.ParseError as e:
                raise VendorResponseError(f"listing {key_prefix}: not XML: {e}") from None
            page = [el.text or "" for el in root.iter(f"{_S3_NS}Key")]
            keys += [k for k in page if k.endswith(".zip")]
            truncated = (root.findtext(f"{_S3_NS}IsTruncated") or "false").lower() == "true"
            if not truncated or not page:
                return sorted(set(keys))
            marker = page[-1]
        raise VendorResponseError(f"listing {key_prefix}: more than 1000 pages")

    def fetch_verified(self, key: str) -> ArchiveFile | None:
        """Download a zip and its .CHECKSUM; ``None`` if the zip is not published (404)."""
        if not key.startswith("data/") or not key.endswith(".zip") or ".." in key:
            raise ValueError(f"bad archive key {key!r}")
        url = f"{self._cfg.archive_base}/{key}"
        attempts = 0
        for _try in range(2):
            ck = self._get.get(url + ".CHECKSUM", accept="text/plain")
            if ck is None:
                raise ChecksumMismatchError(f"{key}: no .CHECKSUM published")
            line = ck.response.body.decode("ascii", "replace").strip()
            parts = line.split()
            if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]) or parts[1] != key.rsplit("/", 1)[1]:
                raise ChecksumMismatchError(f"{key}: malformed .CHECKSUM {line[:120]!r}")
            got = self._get.get(url, accept="application/zip")
            attempts += ck.attempts + (0 if got is None else got.attempts)
            if got is None:
                return None
            digest = sha256_hex(got.response.body)
            if digest == parts[0]:
                return ArchiveFile(key, got.response.body, digest, line, attempts)
        raise ChecksumMismatchError(f"{key}: sha256 {digest} != published {parts[0]} (twice)")
