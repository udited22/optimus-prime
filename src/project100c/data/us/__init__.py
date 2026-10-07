"""US market data, DATA ONLY. No-key starter sources: Yahoo chart (daily full history, 1-minute last 30 days;
unofficial, personal research only), Cboe index history, the S&P 500 current-constituents list. Ready but keyed:
Alpaca Market Data v2 bars (free Basic plan). Research and recommendation: docs/research/us-market-data-sources.md.
"""

from project100c.data.us.alpaca import AlpacaBarsClient, AlpacaCredentials, bars_table, bars_url, parse_bars_page
from project100c.data.us.config import NyseSessions, UsMarketConfig, load_nyse_sessions, load_us_config
from project100c.data.us.dq import DQ_VERSION, check_daily, check_index_series, check_minute, report_status
from project100c.data.us.jobs import (
    DS_1M,
    DS_ACTIONS,
    DS_ALPACA_1M,
    DS_CBOE,
    DS_CONS,
    DS_DAILY,
    RunSummary,
    Status,
    UsDownloader,
    UsJobStore,
    minute_windows,
)
from project100c.data.us.parse import (
    chart_url,
    f32_decimal,
    lake_symbol,
    parse_cboe_csv,
    parse_chart,
    parse_constituents,
    vendor_symbol,
)

__all__ = [
    "DQ_VERSION",
    "DS_1M",
    "DS_ACTIONS",
    "DS_ALPACA_1M",
    "DS_CBOE",
    "DS_CONS",
    "DS_DAILY",
    "AlpacaBarsClient",
    "AlpacaCredentials",
    "NyseSessions",
    "RunSummary",
    "Status",
    "UsDownloader",
    "UsJobStore",
    "UsMarketConfig",
    "bars_table",
    "bars_url",
    "chart_url",
    "check_daily",
    "check_index_series",
    "check_minute",
    "f32_decimal",
    "lake_symbol",
    "load_nyse_sessions",
    "load_us_config",
    "minute_windows",
    "parse_bars_page",
    "parse_cboe_csv",
    "parse_chart",
    "parse_constituents",
    "report_status",
    "vendor_symbol",
]
