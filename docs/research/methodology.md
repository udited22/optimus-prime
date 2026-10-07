# Research methodology

The research plane decides whether an opportunity is economically real. It has no authority to place orders. A
strategy earns capital only with evidence: positive expectancy after realistic costs, out-of-sample robustness, a known
minimum viable capital and an acceptable risk of ruin.

## The pipeline

1. **Hypothesis first.** Every idea is a typed `StrategySpec` (or a `StructureSpec` for multi-leg research) with an
   economic rationale, the data it needs, parameter ranges, eligible regimes and explicit **falsification criteria**,
   written before any run ([strategy-hypotheses.md](strategy-hypotheses.md), [../architecture/strategyspec.md](../architecture/strategyspec.md)).
2. **Pre-registration.** The rules, the test windows and the pass/kill thresholds are committed before the run.
   Nothing is edited after a result is read; a change is a new version and a new trial.
3. **Trial accounting.** Every evaluation on real data is a trial in the append-only, hash-chained experiment registry
   (`registry/`). The trial count feeds the deflated Sharpe ratio (V9), so data mining is paid for.
4. **Realistic backtests.** Event-driven replay with a look-ahead guard, dated and verified statutory charges and
   brokerage, ASSUMED synthetic spreads until real spreads are measured, conservative fill models and latency
   ([../architecture/backtesting.md](../architecture/backtesting.md)). Data-quality failures quarantine data; they are never "fixed" quietly.
5. **Validation gates V1–V18** ([validation.md](validation.md)): leakage audit, cost and slippage stress, minimum
   trade counts, walk-forward out-of-sample, bootstrap confidence intervals, DSR, regime and parameter stability,
   capacity, event-day certification. Synthetic data can never be VALIDATED.
6. **Untouched holdout.** The most recent six months plus a hashed 20% of earlier weeks
   (`configs/validation/holdout.toml`), read once per version; every look is counted.
7. **Capital requirement.** Each candidate gets a minimum viable capital (`economics/mvc.py`): the binding constraint
   among cash or margin, per-trade risk at the stop, stress loss and drawdown. Research uses the capital a strategy
   needs, not the live canary's.
8. **Forward confirmation.** A survivor is tracked forward on PAPER with frozen rules and a single evaluation at the
   end of the window; then shadow; then tiny real capital to check that execution matches the model. Only then does
   scaling become a question.

Negative results narrow the search space: [negative-results.md](negative-results.md).
