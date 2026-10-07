# 07 — Unified StrategySpec

Every strategy is a **package** containing `spec.yaml` (this schema), deterministic code implementing the `Strategy` interface, tests, and an evidence folder. The spec is validated against a JSON Schema generated from pydantic models (backlog R-S1). **A strategy without a valid spec cannot be loaded by the runtime.**

## 7.1 Schema (YAML, with field semantics)

```yaml
id: "S-ORB-001"                 # stable identifier
version: "0.1.0"                # semver; any logic/param change = new version
status: RESEARCH                # RESEARCH|BACKTESTED|VALIDATED|PAPER|SHADOW|CANARY|PRODUCTION|DEGRADED|QUARANTINED|RETIRED
owner_agent: "research.momentum"
created: 2026-09-30
hypothesis: >                   # falsifiable statement, one paragraph
economic_rationale: >           # WHY a counterparty systematically loses / pays us; who is on the other side
required_data:                  # dataset ids + min granularity + point-in-time requirement
  - {dataset: nifty_fut_1m, granularity: 1m}
  - {dataset: nifty_opt_quotes, granularity: 1s, fields: [bid, ask, ltp, oi]}
signal:
  formula: >                    # exact, reproducible definition (or reference to function + params)
  params: {}                    # every tunable: {value, min, max, step?}; the range is used by perturbation tests
legs:                           # OD-006 LONG OPTIONS ONLY: every leg is side BUY; a SELL leg cannot be represented
  - {side: BUY, right: SIGNAL}  # right: CE | PE | SIGNAL (direction chosen by the signal); max 2 legs (one CE + one PE)
eligible_regimes: [TRENDING_UP, VOLATILITY_EXPANSION]
prohibited_regimes: [EVENT_REGIME, LOW_LIQUIDITY, ABNORMAL_MARKET, NO_EDGE]
instrument_selection: "NIFTY index options only"
expiry_selection: "nearest weekly with DTE >= 1 (no 0DTE unless spec says so)"
strike_selection: "delta 0.35–0.50 by own IV calc; spread <= 2 ticks; OI >= X"
entry:
  trigger: >
  window_start: "09:30:00"      # >= 09:15; must also fit the configured trading window (entry_start)
  window_end: "13:00:00"        # <= 14:00 (OD-008)
  order: {type: LIMIT, price_rule: "mid + 1 tick, cap at max_price", ttl_seconds: 10, max_chase_ticks: 2}
  max_entries_per_day: 1        # the spec's own daily entry cap (OD-014); default 1; a long straddle is ONE entry; system cap on top
exit:
  stop: {method: "premium % | underlying level | time", value: , order: SL_LIMIT, limit_offset_ticks: }
  profit_taking: >
  time_exit: "15:00 IST hard (OD-002; kernel-enforced) or earlier; no new entries at/after 14:00 (OD-008)"
  max_holding_minutes: 90
sizing:
  method: "fixed 1 lot if risk_at_stop <= risk_budget else NO TRADE"
  risk_at_stop_formula: "(entry - stop_fill_estimate) * lot + round_trip_costs + slippage_allowance"
expected_frequency: "trades/week (distribution)"
expected_slippage: {entry_ticks: , exit_ticks: , stop_ticks: , source: "measured|assumed"}
cost_assumptions: {cost_model_version: "CM-2026-04-01", brokerage_per_order: 20}
capacity: "max lots before impact > 1 tick (estimate + method)"
event_certified: false          # may enter on an EVENT_REGIME day only if true (OD-014 default; docs/research/validation.md §13.2a)
event_certification:            # required exactly when event_certified is true
  {gate_result_id: "VR-<12 hex>", gate_config_version: , subset: HISTORICAL_EVENT_DAYS, data_label: REAL, verdict: VALIDATED, event_days: , certified_on: }
failure_modes:
  - "regime shift to mean reversion"
  - "IV crush after entry"
dependencies: {weekly_expiry: true, min_capital_inr: }
evidence:                        # all 'pending' until produced by the pipeline; never hand-edited
  backtest_dataset: {manifest_hash: pending, period: pending}
  in_sample: pending
  out_of_sample: pending
  walk_forward: pending
  holdout: pending
  stress: pending
  paper: pending
  shadow: pending
  live: pending
confidence: {level: NONE, rationale: "no evidence yet"}   # NONE|LOW|MEDIUM|HIGH (set by Validation agent only)
#   (the implementation also carries sizing.max_lots: 1 and cost_assumptions.brokerage_plan instead of a raw ₹ figure)
retirement_criteria:
  - "live expectancy CI upper bound < 0 over >= 40 trades"
  - "realised slippage > 2x assumed for 20 trades"
  - "hypothesis-specific invalidation condition"
lineage: {code_sha: , data_manifest: , experiment_ids: [], approvals: []}
```

## 7.2 Rules
1. `evidence.*` and `confidence` are **written only by the pipeline and the Validation agent**. A research agent or human editing them makes the package invalid (checked by signature).
2. Every param has a range, and perturbation tests use that range. A spec with un-ranged params fails validation.
3. `risk_at_stop_formula` is computed by the Risk Governor with **its own cost model**, never the strategy's number. The strategy's number is only compared, for divergence alerts.
4. `dependencies.min_capital_inr` is computed automatically: the NAV at which one lot's risk at stop ≤ 2% of NAV at median premium. Strategies whose `min_capital_inr` > current NAV are **auto-ineligible** (this is how the 01 §A3 finding is enforced).
5. Strategies emit `TradeIntent` objects only (schema in 10-execution-engine). They never call the broker.
6. **Long options only (OD-006).** `legs[].side` is the literal `BUY`. Specs with SELL legs, spreads, or short straddles/strangles fail validation with `MANDATE_LONG_ONLY`. Two BUY legs (one CE + one PE, e.g. a long straddle) are representable; all Governor limits still apply to the combined risk.
7. **Implemented (Phase 0, R-S1 partial):** `src/project100c/spec/` (pydantic models, YAML/JSON loaders, config cross-checks) and the committed JSON Schema at `schemas/strategyspec.schema.json` (regenerate with `scripts/export_spec_schema.py`; a test fails on drift). Enforced: ranged params, OD-002/003 times, LIMIT/SL-LIMIT only, max 1 lot, NO_EDGE and ABNORMAL_MARKET always prohibited, evidence/confidence/min_capital pipeline-owned (`load_authored_spec` rejects hand-set values), lifecycle status requires the matching evidence, and legal lifecycle transitions only. **Not yet built:** signature verification of evidence, and automatic `min_capital_inr` computation.
8. **Regime policy, invalidation rules and data availability (2-Oct-2026).** `spec/models.py`, `spec/regime_policy.py`:
   - **`eligible_regimes`** constrains each regime *dimension* it names (trend, volatility, opening character; `REGIME_DIMENSIONS`): the current value must be one of the named ones. Dimensions it does not name are unconstrained; AND across dimensions, OR within one. A *condition* (gap, expiry, event, low liquidity) listed there is permitted, not required.
   - **`prohibited_regimes`**: any of these present blocks the entry. NO_EDGE and ABNORMAL_MARKET are always in it.
   - **`required_regimes`** (new): conditions that must all be present, e.g. EXPIRY_REGIME for an expiry-day strategy. Only conditions are accepted, and none may also be prohibited.
   - The pure `regime_permits(policy, tags)` returns OK, REGIME_UNKNOWN (no tags), REGIME_BLOCKED or REGIME_NOT_ALLOWED with the reason. The Governor applies it as check 17 (docs/risk/risk-engine.md §9.7).
   - **`invalidation_rules`** (new, at least one): `{kind: REGIME_CHANGE | PRICE_LEVEL | TIME | VOLATILITY | DATA, description, action: EXIT_POSITION | CANCEL_ENTRY | STAND_DOWN_DAY, regimes}`. They say the thesis is wrong *now*, before the stop is hit. REGIME_CHANGE rules list their trigger tags and are checked generically (`triggered_regime_invalidations`); the other kinds are evaluated by the strategy's code. An invalidation exit is a sell-to-close, never a short.
   - **`required_data[].availability`** (new): AVAILABLE / UNCERTAIN / UNAVAILABLE / UNVERIFIED (default). UNCERTAIN or UNAVAILABLE data needs a named **`substitute`**. This is where a futures-volume dependency is flagged (docs/research/strategy-hypotheses.md, "Strategy library").
   - **`falsification_criteria`** (new, at least one) and **`cost_sensitivity`** (new, a sentence or more): what result would kill the idea, and how the idea's economics react to costs and slippage (charges per round trip, the slippage multiple at which it breaks even). Both are written before any backtest, so a spec cannot be judged against criteria invented after the results.

