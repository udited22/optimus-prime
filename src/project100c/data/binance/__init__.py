"""Binance public-archive downloader (data.binance.vision; no key, DATA ONLY). Spot and USD-M perpetual 1m klines,
USD-M premium-index rate history and 5-minute metrics, checksum-verified, DQ-checked, written to the Lake."""

from project100c.data.binance.archive import ArchiveFile, ArchiveKind, BinanceArchive, period_of, prefix
from project100c.data.binance.config import BinanceConfig, load_binance_config
from project100c.data.binance.dq import DQ_VERSION, CheckResult, check_klines, check_series
from project100c.data.binance.jobs import (
    CryptoDownloader,
    CryptoJobStore,
    IngestOut,
    PlanResult,
    RunSummary,
    Task,
    TaskStatus,
    ingest_file,
    plan_series,
)
from project100c.data.binance.parse import Parsed, parse_klines, parse_metrics, parse_rate, unzip_single
from project100c.data.binance.universe import CryptoUniverse, UniverseSymbol, load_universe, rank_snapshot

__all__ = [
    "DQ_VERSION",
    "ArchiveFile",
    "ArchiveKind",
    "BinanceArchive",
    "BinanceConfig",
    "CheckResult",
    "CryptoDownloader",
    "CryptoJobStore",
    "CryptoUniverse",
    "IngestOut",
    "Parsed",
    "PlanResult",
    "RunSummary",
    "Task",
    "TaskStatus",
    "UniverseSymbol",
    "check_klines",
    "check_series",
    "ingest_file",
    "load_binance_config",
    "load_universe",
    "parse_klines",
    "parse_metrics",
    "parse_rate",
    "period_of",
    "plan_series",
    "prefix",
    "rank_snapshot",
    "unzip_single",
]
