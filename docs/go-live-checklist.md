# Go-live checklist

`ops/host_main` refuses `live` mode unless this file's SHA-256 matches the signed `checklist_sha256` in
`configs/host/host.toml`, the config lock is opened, the owner's sign-off is recorded, and the build allows live
(this build never does). It complements the canary gate in [docs/risk/canary-criteria.md](risk/canary-criteria.md).

**Status: NOT READY.** No strategy is VALIDATED, no broker credential exists and no order has reached a real broker.

## 1. Before the first live order

- At least one strategy VALIDATED on real data (all gates in [docs/research/validation.md](research/validation.md)),
  capital-eligible at the canary NAV, and confirmed by forward paper and shadow runs.
- Broker sandbox contract suite run (`tests/contract/`), a read-only live session (positions, orders, funds,
  recorded feed sessions with a gap report) and a readiness report (latency, feed stability, reconciliation).
- The trading host provisioned with a broker-registered static IP, time sync, TLS for the token webhook, the
  encrypted credential store, journal backups and the restart drill; research code and agents never run on it.
- The alert channel healthy; the daily broker-token gate wired (fail closed: no token, no trading).

## 2. Limits on the first live day

All from `configs/risk/limits.toml` and the trading window config; the Governor enforces them and none can be raised
from the alert channel: 2% of NAV per trade including costs; 4% daily and 8% weekly loss (latched); drawdown
suspension at 12.5% of the high-water mark; 1 lot (a long straddle may use 2, OD-013); at most 10 entries a day and
each spec's own cap; entries 09:20 to before 14:00 IST, forced flatten from 14:50, hard flat by 15:00; abnormal-market,
broker-error and slippage kills (OD-017); long only (OD-006).

## 3. Every trading morning

1. The daily broker token is approved before the gate time, or the system does not trade that day.
2. The alert channel is healthy.
3. The journal hash chain verifies; the rebuilt kernel state shows no open position and no unexpected kill.
4. Reconciliation at start: the broker shows nothing the journal does not know.
5. Instrument master and lot sizes refreshed; the calendar says today is a session.
6. Clock skew under 1 second.
7. If any check fails: no trading today. The system stays flat and alerts.
