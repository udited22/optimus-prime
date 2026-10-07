# Free and open US market-data sources (equities, ETFs, equity options, index and futures)

Research for Part 3, 3-Oct-2026 (IST). Every row was checked against the provider's own page on 3-Oct-2026
between 22:15 and 22:50 IST, and where possible by a live request from the box. "Probe" notes what the box
actually got back. Nothing here is legal advice. Where a point could not be confirmed on an official page, the
table says so.

## Summary

| Source | Key? | Coverage | History | Granularity | Limits (free) | Terms for personal research |
|---|---|---|---|---|---|---|
| **Alpaca Market Data (Basic)** | free key | US stocks and ETFs (SIP = all US exchanges; real-time only IEX); US options (indicative feed) | stocks **since 2016** | 1-minute to monthly bars, trades, quotes | **200 historical calls/min**; latest 15 min unavailable on SIP; 10,000 bars/page | Alpaca terms; free with any account (paper is enough) |
| Massive (formerly Polygon.io), Basic | free key | stocks, options, indices, forex/crypto, futures (separate subscriptions, each with a free tier) | **2 years** | minute and day aggregates, end-of-day only | **5 requests/min per asset class**; no trades, quotes, snapshots or flat files | individual use; professional use needs a business licence |
| Tiingo (Starter) | free key | 49k US/Chinese stocks, 60k ETFs/mutual funds; IEX intraday | EOD **30+ years** | daily; IEX intraday resampled down to 1 min | **50 requests/hour, 1,000/day, 500 unique symbols/month**, 1 GB/month | "Internal use only": personal use, no display or sharing |
| Yahoo Finance via the chart endpoint (what yfinance uses) | **no key** | global stocks, ETFs, indices, some futures (`ES=F`) | daily from listing (SPY 1993, ^GSPC 1927) | daily; **1-minute only for the last 30 days, at most 8 days per request** | unpublished | **unofficial**; Yahoo terms apply, "intended for personal use only", no redistribution |
| Stooq | **now needs a key** (CAPTCHA) | global daily and some intraday | decades (daily) | daily | n/a | since about March 2026 the CSV endpoint returns a page asking for an `apikey` |
| Nasdaq Data Link | free key (anonymous very limited) | free datasets are mostly macro and reference; the free US-equity price set (WIKI) has been discontinued | per dataset | per dataset | anonymous: 20 calls/10 min, 50/day; free key: 300/10 s, 2,000/10 min, 50,000/day | per-dataset licence |
| FRED (St. Louis Fed) | API: free key | 800k+ US/international macro series | per series (often decades) | daily to annual | API key required for every web-service request | FRED terms (most series public domain, some copyrighted) |
| SEC EDGAR (data.sec.gov) | **no key** | all filings since 1994; XBRL company facts; submissions | 1994 onward; XBRL about 2009 onward | per filing | **10 requests/s**, declared `User-Agent` with contact required | public data |
| Cboe index history | **no key** | VIX, VIX9D, VIX3M, VVIX, SKEW, VXO and other Cboe indices; current delayed option chains | VIX from 2 Jan 1990 | daily (chains: current snapshot only) | unpublished | "furnished without responsibility for accuracy"; DataShop sells history |
| Databento | key + payment details | US equities (exchange feeds), OPRA options, CME futures and more | per dataset | tick to daily | **$125 free credits** for historical data, once per team, expiring 6 months after sign-up | per-dataset licence; billed only above the credits |
| Interactive Brokers (TWS API) | **funded account** + market-data subscriptions | everything IB trades | bars of 30 s or less only for 6 months; expired futures 2 years; **no expired options** | 1 s to monthly | pacing: no more than 60 small-bar requests per 10 min, at most 50 open historical requests | account terms; historical data needs live Level-1 subscriptions |

## Per source

### Alpaca Market Data API v2 (best free source overall; needs a free key)
- Plans page: https://docs.alpaca.markets/docs/about-market-data-api. Basic (free) covers US stocks and ETFs.
  Real-time coverage is IEX only, "Historical data timeframe: Since 2016" and "Historical data limitation:
  latest 15 minutes", with **200 historical API calls per minute**. Options are on the indicative feed. Every
  endpoint except historical crypto needs the `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` header pair, created in
  the web dashboard.
- Bars endpoint: https://docs.alpaca.markets/reference/stockbars (`GET /v2/stocks/bars`).
  - `feed` is one of sip, iex, boats or otc; `limit` can be up to 10000 per page, with `next_page_token` for
    the next page.
  - `adjustment` is raw, split, dividend, spin-off or all.
  - `asof` handles symbol renames (FB to META).
  - `timeframe` runs from 1Min to 12Month.
- Why it is first: consolidated (SIP) **1-minute** bars for every US stock and ETF back to 2016, at a rate that
  covers the S&P 500 in about an hour. At 10,000 bars per page, one symbol-year of 1-minute bars is about 10
  calls, so 500 symbols × 10 years is about 50k calls, about 4–5 hours at 200 per minute.
- Limits: not tick data; options history on the free plan is the indicative feed, not OPRA. Option history
  depth was not confirmed on an official page.

### Massive (formerly Polygon.io), Basic
- https://massive.com/knowledge-base/article/how-quickly-can-i-access-massives-market-date says the free Basic
  tier of every product gives:
  - end-of-day aggregate bars by minute and by day, with two years of history;
  - reference data, splits and dividends, and corporate actions;
  - five requests a minute per asset class.

  Trades, quotes, snapshots, WebSockets and flat files need a paid tier.
- https://massive.com/knowledge-base/article/what-are-the-different-massive-subscriptions-i-can-use: Stocks,
  Options (including index options), Indices (history from 2023-02-14), Currencies and Futures are separate
  subscriptions. Professional use needs a business licence.
- Verdict: two years at 5 calls/min is too slow for a 500-symbol universe, but useful for a few option or
  futures contracts.

### Tiingo, Starter (free)
- https://www.tiingo.com/about/pricing lists:
  - 30+ years of price history and 5 years of fundamentals history;
  - **500 unique symbols per month, 50 requests per hour, 1,000 per day, 1 GB per month**;
  - licence "Internal Use Only: you may only use the data for your own personal use and you may not display or
    share the data".
- Docs: https://www.tiingo.com/documentation/end-of-day and https://www.tiingo.com/documentation/iex (IEX
  intraday, `resampleFreq` down to 1min).
- Verdict: the S&P 500 plus SPY and QQQ is 503 symbols, just over the monthly symbol cap. It is a good
  cross-check of Yahoo's long daily history for a subset.

### Yahoo Finance chart endpoint / yfinance (best no-key price source; unofficial)
- yfinance README (https://github.com/ranaroussi/yfinance):
  - "not affiliated, endorsed, or vetted by Yahoo";
  - "intended for research and educational purposes";
  - "the Yahoo! finance API is intended for personal use only";
  - refer to Yahoo's terms: https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html and
    https://policies.yahoo.com/us/en/yahoo/terms/product-atos/apiforydn/index.htm.
- Probe from the box: `https://query1.finance.yahoo.com/v8/finance/chart/<SYM>` answers without a key or cookie.
  - Daily bars run from each listing's first trade date (SPY 1993-01-29: 8,477 bars; ^GSPC 1927-12-30: 24,806),
    with split and dividend events and `adjclose`.
  - The endpoint's own 1-minute errors: "Only 8 days worth of 1m granularity data are allowed to be fetched per
    request" and "The requested range must be within the last 30 days".
  - Values are single-precision floats widened to double (501.9800109863281). Our parser restores the
    float32-exact decimal (501.98).
- Risks: no SLA, can change or block without notice, delisted tickers mostly return 404 (survivorship), and the
  terms forbid redistribution. Use it for personal research only and never ship this data.

### Stooq
- pandas-datareader issue #1012 (https://github.com/pydata/pandas-datareader/issues/1012, 13-Apr-2026) reports
  that as of March 2026 Stooq requires an API key: the CSV URL returns a page pointing to
  `https://stooq.com/q/d/?s=spy.us&get_apikey`, a CAPTCHA page. pandas-datareader has since listed Stooq among
  its removed readers.
- Probe: `stooq.com` and `stooq.pl` ended the TLS handshake from the box (curl error 35), so it is
  unreachable from here in any case.

### Nasdaq Data Link (formerly Quandl)
- Rate limits, per the official docs page indexed at https://docs.data.nasdaq.com/docs/rate-limits-1 (this
  page and getting-started returned 404 on 3-Oct-2026, so the figures are from the indexed copy):
  - anonymous: 20 calls per 10 minutes and 50 per day, shared;
  - free key: 300 per 10 s, 2,000 per 10 min and 50,000 per day.
- Probe: `data.nasdaq.com/api/v3/datasets/FRED/GDP.csv` returned HTTP 403 (bot wall) from the box.
- The free end-of-day US stock set (WIKI) has not been updated since 2018. The useful free content is now macro
  data, which FRED serves directly. Verdict: skip.

### FRED
- https://fred.stlouisfed.org/docs/api/api_key.html: "All web service requests require an API key", a 32-character
  lower-case key from a free fredaccount.stlouisfed.org login. API index: https://fred.stlouisfed.org/docs/api/fred/.
  ALFRED vintage dates are available through `fred/series/vintagedates`.
- Probe: the API without a key returned HTTP 400 "Variable api_key is not set". The keyless `fredgraph.csv`
  download timed out from the box (HTTP/2 reset, then a 40 s timeout).
- Verdict: request a free key for macro series (rates, CPI, unemployment and so on).

### SEC EDGAR
- https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data:
  - fair access is at most **10 requests/second**, with a declared `User-Agent` such as "Sample Company Name
    AdminContact@<domain>";
  - EDGAR starts in 1994/1995;
  - `company_tickers.json` maps tickers to CIKs.
- https://www.sec.gov/search-filings/edgar-application-programming-interfaces: data.sec.gov APIs "do not require
  any authentication or API keys". They serve submissions history and XBRL company facts and frames, updated in
  real time, with nightly bulk ZIPs.
- Probe: `company_tickers.json`, `submissions/CIK0000320193.json` and `api/xbrl/companyfacts/CIK0000320193.json`
  all returned 200.
- Verdict: the right no-key fundamentals source. It is not implemented yet (prices were the priority); the
  constituents list already carries each company's CIK, so the join is ready.

### Cboe
- https://www.cboe.com/tradable_products/vix/vix_historical_data: "download VIX Index data for 1990 to present
  (Updated Daily)", plus VXO history; the data "is compiled for the convenience of site visitors and is furnished
  without responsibility for accuracy".
- Probe: `cdn.cboe.com/api/global/us_indices/daily_prices/<SERIES>_History.csv` redirects to `cdn-api.cboe.com`,
  which serves these without a key:
  - VIX from 1990-01-02;
  - VIX9D from 2011;
  - VIX3M from 2009;
  - VVIX from 2006 (value only);
  - SKEW from 1990 (value only).

  `cdn.cboe.com/api/global/delayed_quotes/options/SPY.json` returns the **current** delayed SPY option chain
  (bid, ask, IV, Greeks, OI), but no history.
- Verdict: implemented for the VIX family. A daily chain snapshot job could build option history going forward
  (not built).

### Databento
- https://roadmap.databento.com/announcements/end-of-early-access-125-in-free-credits-for-all-users: new users get
  **$125 in free credits for historical data**, shared per team, expiring after 6 months, and are billed only
  above $125.
- https://databento.com/blog/why-payment-information-required says payment details are required at sign-up.
- Dataset coverage and history depth (US equities, OPRA, CME) differ per dataset. The pricing and catalog pages
  need JavaScript and could not be read from here, so check https://databento.com/pricing before spending.
- Verdict: best for a one-off, high-quality pull of OPRA option history or CME futures within the credit.

### Interactive Brokers
- https://interactivebrokers.github.io/tws-api/historical_limitations.html:
  - historical data needs the same Level-1 subscriptions as live data;
  - bars of 30 s or less are only available for 6 months;
  - expired futures only for 2 years past expiry;
  - **no data for expired options**;
  - pacing is no more than 60 small-bar requests per 10 minutes and at most 50 simultaneous open requests.
- Verdict: needs a funded account plus subscriptions, and is no help for option history. Not a research source
  here.

## Recommendation

1. **No-key starter (built now): Yahoo daily plus 1-minute, Cboe VIX family, S&P 500 current list.**
   `scripts/us_backfill.py run` fills `lake/us/`.
   - Daily full history for the 503 current constituents plus SPY, QQQ, ^GSPC and ^NDX, with split and
     dividend events.
   - 1-minute bars for the last 30 days, for the same set.
   - Five Cboe series.

   It sits behind the same seams as the Dhan and Binance downloaders: `HttpTransport`, `PublicGetter`,
   `RateLimiter` and `QuotaStore` with a persisted daily cap, `Lake` with raw, clean, quarantine and manifests,
   and `DQReport`/`DQCode`. Each re-run extends the 1-minute archive (days already done are never re-fetched),
   so a weekly cron builds history Yahoo itself does not keep. Coverage and DQ:
   `docs/data/us-coverage-2026-10-03.md`.
2. **The best source needs a free key: request an Alpaca key** (sign up at https://app.alpaca.markets; a
   paper-only account is enough). In "API Keys", create a key pair and export `ALPACA_API_KEY_ID` and
   `ALPACA_API_SECRET_KEY`. The client is built and tested offline (`src/project100c/data/us/alpaca.py`,
   `scripts/us_backfill.py alpaca-minute --symbols SPY,QQQ --days 7 --feed sip`). It writes the same per-session
   1-minute parts (`alpaca_minute_bars`) with the same DQ, and it is the path to **SIP 1-minute bars back to
   2016**.
3. **Also request a free FRED key** for macro data. Use SEC EDGAR (no key) for fundamentals next, keyed by the
   CIKs already in the constituents part.
4. For **expired option history** no free source is adequate. Use Databento's $125 credit for a targeted OPRA
   pull, or start a daily Cboe delayed-chain snapshot now to accumulate history going forward.
5. Skip Stooq (key and CAPTCHA, unreachable from the box), Nasdaq Data Link (free equity prices discontinued),
   Massive Basic (2 years at 5 calls/min) and IBKR (account plus subscriptions, no expired options) for this
   purpose.

## Survivorship bias and other caveats (read before any backtest)

- The constituents list is the S&P 500 **today** (Open Knowledge package https://github.com/datasets/s-and-p-500-companies,
  built from Wikipedia). Companies removed before today are missing, and Yahoo returns 404 for most delisted
  tickers. A backtest over this list overstates returns and understates drawdowns.
- History is per listing, not per membership: a stock appears from its IPO, not from when it joined the index.
- A point-in-time membership history needs a licensed source (CRSP, Norgate, Sharadar) or a reconstruction from
  S&P DJI announcements (Wikipedia's "selected changes" table is incomplete).
- Yahoo `open`/`high`/`low`/`close` are split-adjusted, `adj_close` also dividend-adjusted, and volume
  split-adjusted. Values are restated whenever a new split or dividend arrives, so a re-pull can differ from an
  older pull. Every pull keeps its raw reply and manifest, so this stays auditable.
- The Yahoo 1-minute window is rolling. Anything not fetched within 30 days is gone from this source.
