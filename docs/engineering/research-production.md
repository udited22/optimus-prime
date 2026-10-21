# Research Production v1

Research Production v1 is the October 2026 operating milestone of Optimus Prime: running the research system as a continuous, auditable process (live data capture, prospective paper evidence and a read-only operator view) while keeping capital execution switched off.

**Status: in progress.** The components below are running in the private deployment, but the milestone's production acceptance gate is still open. They are not yet accepted as production.

**Live autonomous capital execution is not production-enabled yet.**

> Scope of this page: these components run in the private production deployment of Optimus Prime. Their source is **not** part of this public snapshot, which publishes the reusable engine only (see [Private alpha](private-alpha.md)). This page describes behaviour and boundaries, not code you can run from this repository.

## What Research Production v1 consists of

| Component | What it does | What it cannot do |
|---|---|---|
| **Live option-chain recorder** | Captures index option-chain snapshots on a fixed intraday schedule through a data-only vendor client whose endpoint allowlist contains no order or account endpoint. A supervised loop is guarded by a single-instance lock, a heartbeat and liveness checks. A capture day is only as good as the host's uptime; partial days are recorded as such, never filled in. | Place, modify or cancel orders. Backfill a missed slot. |
| **Fail-closed preflight** | Before any (re)start, the candidate code checks that the data credential covers the *next* session's close plus a margin, that the data plan is active and that a small data call succeeds. If any check fails, the running process is left untouched and an operator alert is raised. | Print or transmit the credential. |
| **Explicit missing-data register** | Sessions that were not captured are recorded permanently as `MISSING_DATA`, distinct from `MARKET_CLOSED`. Backfill is forbidden, so they are never treated as empty or zero-activity observations. | Rewrite history. |
| **Per-exchange holiday calendars** | Each exchange's trading calendar is sourced from that exchange's own notices and checked by exact-equality tests. | Infer one exchange's holidays from another's. |
| **Prospective evidence guard** | A paper-forward session counts as evidence only if it is recorded by a fixed deadline after the session and is not in the missing-data register. Late or gap sessions go to a separate tree and never reach the ledger, kill rule or pass statistics. Forward records are write-once, and strategy definitions are hash-frozen before the window starts. | Promote a strategy, retune parameters or count retrospective results. |
| **Off-box backup** | Each finished capture day, complete or not, is packed reproducibly, uploaded with a per-file checksum manifest and verified by re-download. | Overwrite or delete an existing backup. |
| **Read-only production view** | Shows release vs running code, recorder and data-credential health (validity time only), the missing-data register, backup state, the experiment registry and forward evidence; blind rounds show counts only. Values that do not exist are shown as `NOT_AVAILABLE`, never estimated. It is served on loopback only, GET-only. | Write to any store, call a broker or vendor, or change a strategy, holdout or experiment. |

## Boundaries of this milestone

- **No broker-order path.** The production build hard-disables live mode. The engine host is wired only to the paper broker, research code has no import path to broker or execution modules, and boundary tests enforce this.
- **No AI component has economic authority.** Agents and language models are not part of any order, risk or capital path.
- **Evidence integrity.** Missing, late and diagnostic data are kept mechanically separate from prospective evidence. Incidents are logged openly and are never "repaired" by reruns or catch-up.
- **Release discipline.** The deployment runs tagged releases. Merges use merge commits with green CI and independent review, and the history is never rewritten.

## Operating model

Scheduled routines run a morning preflight, intraday liveness checks, a post-close backup and verify, and a post-close paper-forward run. Each routine alerts the operator on failure, and the credential refresh is always a human step.

## What this milestone does not claim

- It does not claim that Research Production v1 is complete or accepted: its acceptance gate is open.
- No strategy has passed the validation standard. Forward paper evidence is still being accumulated and is evaluated once, at the end of its pre-registered window.
- Paper results are simulated after modelled costs. They are not evidence of future returns.
- Live execution, capital allocation and autonomous operation remain future milestones, gated on validated evidence and an explicit go-live checklist.
