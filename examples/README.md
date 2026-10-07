# Examples (SYNTHETIC)

Everything here is a **synthetic demonstration**, not a research candidate and not a recommendation. Parameters are
arbitrary round numbers chosen to exercise the schema, the payoff maths and the PAPER structure book.

- `structures/X-DEMO-*.yaml`: defined-risk multi-leg StructureSpecs (an expiry-day call credit spread, an iron
  condor, a trend-side credit spread and a wide-winged strangle).
- `configs/paper_structures.example.toml`: a PAPER structure-book config (overnight holds refused).

Real candidates (exact rules and parameters) live in the private alpha library; see `project100c.alpha` and
[docs/engineering/private-alpha.md](../docs/engineering/private-alpha.md).
