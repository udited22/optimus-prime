# Technology stack

The committed stack is intentionally small. Optimus Prime optimises for **determinism, testability, reproducibility and clarity under failure** before throughput or low-latency optimisation.

This page describes what is actually present in the public repository rather than an aspirational infrastructure catalogue.

## Core runtime

| Concern | Committed choice | Rationale |
|---|---|---|
| Language | **Python 3.12+** | Strong data/research ecosystem and a compact, auditable implementation surface. |
| Domain contracts | **Pydantic v2** | Typed trade intents, specifications and configuration with explicit validation. |
| Configuration | **TOML / YAML** | Human-reviewable, version-controlled policy and research inputs. |
| Columnar data | **PyArrow / Parquet** | Portable historical-data representation with explicit schemas and lineage support. |
| Monetary arithmetic | **`Decimal` / explicit numeric policy** | Avoids implicit float-equality behaviour in safety-critical calculations. |

The deterministic risk and execution-domain logic is kept separable from I/O so it can be exercised against replay, fake and paper interfaces.

## Research

The optional research environment adds **NumPy** and **Matplotlib** for analysis and experiment tooling. The repository deliberately avoids making a large modelling framework mandatory for the core engine.

The research philosophy is to prefer simpler, inspectable models until evidence demonstrates that additional complexity survives out-of-sample validation and realistic costs.

## Testing and static analysis

| Tool | Role |
|---|---|
| **pytest** | unit and integration tests |
| **Hypothesis** | property-oriented testing of safety invariants |
| **mypy --strict** | static type checking across source and tests |
| **Ruff** | linting and formatting checks |
| **GitHub Actions** | reproducible public CI gate |
| **Gitleaks** | repository secret scanning |

Safety-critical components are tested around failure conditions and invariants, not only successful scenarios.

## Command centre

The read-only command centre is a separate frontend built with:

- **TypeScript**
- **Vite**
- **Vitest**
- **Three.js** for selected visualisation elements
- variable Inter and JetBrains Mono font packages

The UI consumes system state; it is not an alternate discretionary trading surface.

## Cryptography and credentials

The optional host dependency includes **`cryptography`** for encrypted local credential handling. Production secret-management details are intentionally outside the public repository.

## Design constraints

A few rules matter more than individual libraries:

- risk policy is deterministic and independently testable;
- external I/O is kept behind adapters;
- research and execution authority are separated;
- configuration is version-controlled and validated;
- bad data/state produces explicit failure rather than a plausible fallback;
- the system should be understandable from code and tests without requiring proprietary infrastructure.

## What is intentionally not claimed

The public repository does not claim to run a low-latency institutional execution stack, distributed data platform or production cloud estate. Components are added when they solve an evidenced engineering problem, not to make the architecture diagram look larger.

That constraint is deliberate: the credibility of Optimus Prime should come from the behaviour that is implemented and tested, not from technologies listed in a design document.
