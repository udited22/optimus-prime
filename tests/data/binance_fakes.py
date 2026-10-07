"""Fake data.binance.vision archive (S3 listing + zips + .CHECKSUM) for offline Binance tests."""

from __future__ import annotations

import hashlib
import io
import zipfile
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from project100c.data.binance import BinanceArchive, BinanceConfig, CryptoDownloader, CryptoJobStore
from project100c.data.binance.config import load_binance_config
from project100c.data.http import HttpRequest, HttpResponse, ScriptedTransport
from project100c.data.lake import Lake
from project100c.data.public_http import PublicGetter, RetryPolicy
from project100c.data.ratelimit import MemoryQuotaStore, RateLimiter

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 3, 16, 0, tzinfo=UTC)


def kline_rows(day: date, *, micro: bool, minutes: Iterable[int] | None = None, price: str = "100") -> list[str]:
    unit = 1000 if micro else 1
    start = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)
    out = []
    for m in minutes if minutes is not None else range(1440):
        ot = (start + m * 60_000) * unit
        ct = ot + 60_000 * unit - 1
        out.append(f"{ot},{price},101,99,100.5,2.5,{ct},250,10,1.25,125,0")
    return out


def kline_csv(rows: list[str], *, header: bool) -> bytes:
    head = (
        "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,"
        "taker_buy_quote_volume,ignore\n"
    )
    return ((head if header else "") + "\n".join(rows) + "\n").encode()


def zip_one(name: str, payload: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, payload)
    return buf.getvalue()


class FakeArchive:
    """Serves listing pages (``page_size`` keys each), zips and checksums; ``bad_sum`` keys get a wrong digest."""

    def __init__(self, *, page_size: int = 1000) -> None:
        self.files: dict[str, bytes] = {}
        self.bad_sum: set[str] = set()
        self.page_size = page_size
        self.requests: list[str] = []

    def add(self, key: str, csv: bytes) -> None:
        self.files[key] = zip_one(key.rsplit("/", 1)[1].replace(".zip", ".csv"), csv)

    def __call__(self, req: HttpRequest) -> HttpResponse:
        self.requests.append(req.url)
        u = urlsplit(req.url)
        if u.hostname == "s3-ap-northeast-1.amazonaws.com":
            q = parse_qs(u.query, keep_blank_values=True)
            pre, marker = q["prefix"][0], q.get("marker", [""])[0]
            keys = sorted(k for k in self.files if k.startswith(pre) and k > marker)
            page, more = keys[: self.page_size], len(keys) > self.page_size
            body = (
                '<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                + f"<IsTruncated>{'true' if more else 'false'}</IsTruncated>"
                + "".join(
                    f"<Contents><Key>{k}</Key></Contents><Contents><Key>{k}.CHECKSUM</Key></Contents>" for k in page
                )
                + "</ListBucketResult>"
            )
            return HttpResponse(200, body.encode())
        key = u.path.lstrip("/")
        if key.endswith(".CHECKSUM"):
            z = key.removesuffix(".CHECKSUM")
            if z not in self.files:
                return HttpResponse(404, b"")
            digest = hashlib.sha256(self.files[z] + (b"x" if z in self.bad_sum else b"")).hexdigest()
            return HttpResponse(200, f"{digest}  {z.rsplit('/', 1)[1]}\n".encode())
        if key in self.files:
            return HttpResponse(200, self.files[key])
        return HttpResponse(404, b"")


def config() -> BinanceConfig:
    return load_binance_config(REPO / "configs" / "data" / "binance.toml")


def rig(tmp: Path, fake: FakeArchive) -> tuple[BinanceArchive, Lake, CryptoJobStore, CryptoDownloader]:
    cfg = config()
    clock = [0.0]

    def _sleep(s: float) -> None:
        clock[0] += s

    lim = RateLimiter(
        per_second=100,
        per_day=100_000,
        clock=lambda: clock[0],
        sleep=_sleep,
        wall_clock=lambda: NOW,
        quota=MemoryQuotaStore(),
    )
    getter = PublicGetter(
        transport=ScriptedTransport(fake),
        limiter=lim,
        policy=RetryPolicy(
            server_max_attempts=3, rate_limit_max_attempts=3, backoff_base_s=1, backoff_cap_s=4, timeout_s=5
        ),
    )
    archive = BinanceArchive(config=cfg, getter=getter)
    lake = Lake(tmp / "lake")
    store = CryptoJobStore(tmp / "lake" / "jobs" / "crypto.sqlite", wall_clock=lambda: NOW)
    dl = CryptoDownloader(
        archive=archive,
        lake=lake,
        store=store,
        config_version=cfg.version,
        max_missing_fraction=Decimal("0.01"),
        thresholds_version="test",
        wall_clock=lambda: NOW,
    )
    return archive, lake, store, dl


def day_key(market: str, sym: str, d: date) -> str:
    base = "data/spot" if market == "spot" else "data/futures/um"
    return f"{base}/daily/klines/{sym}/1m/{sym}-1m-{d.isoformat()}.zip"


def month_key(market: str, sym: str, month: str) -> str:
    base = "data/spot" if market == "spot" else "data/futures/um"
    return f"{base}/monthly/klines/{sym}/1m/{sym}-1m-{month}.zip"


def days(start: date, n: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]
