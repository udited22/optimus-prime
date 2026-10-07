# Trading calendar configs (backlog D-03)

| File | What | Sources |
|---|---|---|
| `nse_fo_holidays.toml` | NSE F&O trading holidays and special sessions for **2021–2026** (not regular days for us); settlement holidays for 2024–2026 only | **2021–2023:** NSE circulars NSE/CMTR/46623 (2021, via touchbroking mirror), NSE/CMTR/50560 (2022), NSE/FAOP/54759 (2023) cross-checked against the BSE lists (BSE notices 20201210-7 and 20221208-31; the 2022 BSE list via Kuvera). The 2023 Bakri Id move (28 → 29-Jun-2023) is from Zerodha bulletin 353722 and NSE/CD/57298. **2024–2026:** Upstox public market-holidays API (no auth; raw responses in `lake/raw/upstox_holidays/`, gitignored) cross-checked against NSE circulars (NSE/CMTR/59722, 60338, 61518, circular 154/2024 for 2024; NSE/CMTR/65587 for 2025; NSE/BSE 2026 circular + niftyindices.com for 2026) |
| `nifty_expiry_rules.toml` | Versioned expiry weekday rules (Thursday → Tuesday from 1-Sep-2025) + previous-trading-day holiday shift | NSE/FAOP/68747; the Monday move (NSE/FAOP/66938) was deferred by NSE/FAOP/67338 |
| `events.yaml` (EV-2026-10-02.3) | Scheduled market-moving events: RBI MPC decisions, the Union Budget, and US FOMC statements and US CPI releases | RBI press releases; indiabudget.gov.in; FOMC: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm (curl, 2-Oct-2026); US CPI: https://www.bls.gov/schedule/news_release/cpi.htm (read with the web fetch tool on 2-Oct-2026, because box curl gets HTTP 403) |
| `nifty_expiry_golden.csv` | **65** independently sourced NIFTY expiry dates used as a golden test table | NSE circulars (FAOP 65336, 66938, 68747, the lot-size circulars 64625/70616 and the Apr-2024 revision), NSE FOSett_prce_04012021.csv and _02012024.csv, Zerodha bulletins 320750/353722, CNBC-TV18 (14-Aug-2024), and the 30-Sep-2026 Upstox + Kite instrument files |

Verification policy: a holiday is `verified = true` only if ≥ 2 independent public sources agree. Settlement holidays and weekend
special sessions come from Upstox only and are marked `verified = false`; they do not change whether we trade (settlement holidays
are normal trading days; weekends are non-trading anyway). Muhurat sessions are verified as sessions, but their exact times come
from Upstox only.

Raw download SHA-256 (30-Sep-2026): `per_date_2024_2025.jsonl` a2c503f8…e16e39a; `list_current_year_20260930T181055Z.json` eff843d4…945d7954a.

**Gaps (UNVERIFIED / missing):**
- Years before 2021 and after 2026 are not covered. Queries raise `CalendarCoverageError`. Provisional mode applies only fixed-date national holidays.
- The golden table has 65 dates (target ≥ 60 met). Most 2021–2023 weekly expiries are not in it: NSE bhavcopies return HTTP 403 from the box, so only dates stated in a fetched document were added (no dates derived from our own rule).
- 2021–2023 settlement holidays are not listed (they do not change whether F&O trades).
- Muhurat timings for 2021–2023 are not recorded (2021: 18:15 start per ET, single source).
- Known disrupted session: 24-Feb-2021 (NSE outage, trading halted ~11:40, extended session to 17:00; news reports, UNVERIFIED detail). It is a trading day in the book; expect DQ gaps.
- **US events land after the NSE close and move the next Indian session (`impact: next_session`):**
  - An FOMC statement at 2:00 pm ET is 23:30 or 00:30 IST.
  - A CPI release at 8:30 am ET is 18:00 or 19:00 IST.
  - `EventBook.to_calendar(trading_calendar)` puts EVENT_REGIME on the first NSE trading day strictly after the US date.
  - The 2027 sessions are **provisional**: the holiday book stops at 2026, so only fixed-date holidays apply. Re-map them when the 2027 book lands.
  - FOMC dates are tentative until confirmed at the preceding meeting.
  - BLS has not yet published its 2027 CPI schedule, so there are no 2027 CPI entries.
  - NSE pages returned HTTP 403 again on 2-Oct-2026 and were skipped.
- NSE can declare ad-hoc holidays (e.g. elections). Refresh this book whenever NSE issues a holiday circular; the pre-flight check should compare it with the broker's holiday API each morning (future work).
- NSE/FAOP/68747 planned the Mar-2026 quarterly expiry for 31-Mar-2026. That date later became a holiday (Mahavir Jayanti, 2026 circular), so the rule gives 30-Mar-2026. The planned date was removed from the golden table, and 30-Mar-2026 is **UNVERIFIED** (derived, with no exchange listing available here).
