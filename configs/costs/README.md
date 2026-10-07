# Cost configuration (versioned)

- `nse_fo_index_options.toml`: dated statutory and exchange charge schedules for NSE index options (premium-based).
  - Each schedule has `effective_from` / `effective_to`, `verified`, `sources` (IDs in `docs/engineering/sources.md`) and notes.
  - Values are quoted strings and are parsed as `Decimal`. Floats are rejected.
- `brokerage_plans.toml`: brokerage plans. `default_live_plan = "upstox-options"` (₹20/executed order), per owner decision OD-004.
- **Unverified entries are refused by default.** `CostModel(..., allow_unverified=True)` is an explicit opt-in, and every resulting `ChargeBreakdown` carries `uses_unverified=True`.
- **Changing a rate:** add a new schedule with a new `version` and `effective_from`. Never edit a past schedule's numbers. If a past entry is wrong, supersede it and note why in the git commit.
- **Coverage gap:** there is no schedule before 1-Oct-2024. Backtests over earlier dates raise `NoScheduleForDateError` until verified historical schedules are added (backlog D-05).
- **Tests:** `tests/costs/` reproduces the design-pack feasibility tables (docs/architecture/architecture.md §A2–A3) to ₹0.01.
