# Public engine, private alpha

Optimus Prime separates **reusable trading infrastructure** from **current candidate strategy IP**.

The public repository contains the engine: data ingestion, cost modelling, backtesting, validation, experiment tracking, portfolio primitives, deterministic risk, execution state machines, paper adapters and observability.

Current candidate strategies may live in a separate private alpha library. That library can contribute strategy plug-ins, specifications and research configuration, but it receives no special authority.

## Boundary rules

1. **The public engine must work without the private library.** Tests and examples use public/synthetic stand-ins.
2. **Private strategies use the same contracts.** They are loaded through the same typed schemas as public research specifications.
3. **Private code cannot replace core safety components.** It may provide strategy/research artefacts, not an alternate Risk Governor, execution gateway or reconciliation path.
4. **No candidate bypasses validation.** Being private does not make a strategy promoted or capital-eligible.
5. **No candidate bypasses risk.** Every resulting trade intent is evaluated by the same deterministic admission layer.
6. **Secrets and account configuration are not part of alpha.** The private strategy library is not a credential store or deployment runbook.

## Why this split exists

Publishing the infrastructure makes the engineering model reviewable without turning a public portfolio repository into a catalogue of live hypotheses and parameters.

It also forces a useful architectural discipline: if the engine only works when hidden strategy code is present, then the public/private boundary is not real. Optimus Prime therefore keeps synthetic examples and public research artefacts sufficient to exercise the interfaces and safety model independently.
