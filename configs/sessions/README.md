# Session configuration: two separate, versioned things

1. `exchange_sessions.toml` holds **NSE's** hours (coverage from 1-Jan-2021; the 2021-01-01..2024-09-30 versions set only the normal-market hours, not trade-modification or closing-VWAP times, which were not sourced for that period).
   - F&O normal market: 09:15–15:30 until 2-Aug-2026, then 09:15–**15:40** from 3-Aug-2026 (NSE/FAOP/74467).
   - Cash normal market: 09:15–15:30, with the closing auction session 15:15–15:35 from 3-Aug-2026. The CAS times come from a secondary source and are flagged `closing_auction_verified = false`.
2. `trading_window.toml` holds **our** policy (owner decisions OD-002 / OD-007 / OD-008 / OD-009; current version `TW-2026-10-01.2` (OD-009); superseded `TW-2026-10-01` and `TW-2026-09-30` are kept).
   - Order activity only in [09:15, 15:00).
   - No new entries at or after 14:00 (OD-008; was 14:45 under OD-003).
   - Forced flatten from 14:50 (OD-008, confirmed).
   - Residual position at 15:00: `BROKER_EXIT_ALL` (OD-007). Exit-All pricing is UNVERIFIED.
   - Entries from 09:20 (OD-009, confirmed).
   - Hard flat 15:00.

`SessionCalendar` refuses to answer (it raises `SessionError`) when:
- the timestamp is naive;
- the date is not covered;
- our window would fall outside the F&O normal market on that date;
- the caller has not confirmed the date is a trading day. The holiday calendar is backlog D-03.
