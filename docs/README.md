# Optimus Prime — technical notes

The public documentation is intentionally curated around enduring engineering ideas and reproducible evidence. Operational account runbooks, personal capital decisions, live candidate parameters and dated internal decision logs are kept outside the public repository.

## Start here

| Area | Document | What it shows |
|---|---|---|
| **System design** | [Architecture](architecture/architecture.md) | Trust boundaries, Research → Portfolio → Risk → Execution, event-sourced state and fail-closed behaviour. |
| **Execution** | [Execution engine](architecture/execution-engine.md) | Typed intents, order state machine, idempotency, adapter boundary and reconciliation. |
| **Risk** | [Risk engine](risk/risk-engine.md) | Independent deterministic admission, kill-state model and safety invariants. |
| **Research method** | [Methodology](research/methodology.md) | How hypotheses, data and experiments are structured. |
| **Promotion standard** | [Validation](research/validation.md) | Out-of-sample discipline and evidence required before promotion. |
| **Evidence** | [Negative results](research/negative-results.md) | Research the system has rejected rather than presenting only successful-looking backtests. |
| **Costs** | [Cost-drag study](research/cost-drag-study.md) | Why execution friction is treated as part of strategy economics. |
| **Engineering** | [Technology stack](engineering/tech-stack.md) | What is actually committed and why the stack stays deliberately small. |
| **Security** | [Security model](engineering/security.md) | Threat model and authority boundaries without deployment secrets. |
| **Public/private boundary** | [Private alpha](engineering/private-alpha.md) | How strategy IP stays private without bypassing public engine contracts. |

## Additional implementation notes

The `architecture/` and `data/` directories contain deeper notes for parts of the committed engine such as historical-data handling, market-data interfaces, backtesting and strategy specifications.

These documents are supporting material rather than a product roadmap. The source code and tests remain authoritative when a document and implementation disagree.

## What is intentionally not here

The public repository does not contain:

- broker-account identifiers or live credentials;
- authentication/token operating procedures;
- production host access instructions;
- personal capital or income targets;
- current live-candidate strategy parameters;
- trading journals;
- chat/prompt transcripts;
- dated internal decision logs and experiment scratchpads.

That information is either private operational state or research working material, not part of the public engineering portfolio.
