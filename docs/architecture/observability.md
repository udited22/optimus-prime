# Observability and command-centre contract

The Optimus Prime command centre is a **read-only projection of system state**. Visual state is derived from journaled events and deterministic mappings; the UI is not an alternate execution or discretionary trading surface.

## Principles

- Every displayed state must be traceable to a real system event or explicitly labelled simulated state.
- The frontend cannot create, modify or cancel orders.
- Safety and failure states must remain legible rather than being hidden behind visual polish.
- Motion and visual density degrade gracefully for reduced-motion and label-disabled modes.

## Event → visual mapping

The **COGNITIVE CONNECTOME** renderer maps domain events through `commandsFor`, while aggregate visual state is derived through `fieldState`. The public UI uses deterministic `cognitiveState` and `strategyPattern` mappings; it does not invent confidence or trading authority.

| Event kind | Public visual meaning |
|---|---|
| `tick` | market/data activity pulse |
| `regime` | market-context transition |
| `budget` | portfolio/risk-budget state update |
| `intent` | strategy/allocator proposed an action |
| `approve` | deterministic RISK layer admitted the bounded intent |
| `order` | EXECUTION lifecycle began |
| `ack` | execution interface acknowledged lifecycle state |
| `fill` | simulated/paper execution state changed |
| `report` | system evidence/report state updated |
| `promote` | research artefact changed lifecycle state |
| `kill` | safety state entered; pathway moves toward terminate / halted presentation |

The cognitive-state projection is one of: `WATCHING`, `EVALUATING`, `REJECTED`, `EXECUTING`, or `HALTED`. These labels describe what the system is doing; they do not grant control authority.

Strategy-pattern state is similarly explicit: `dormant`, `researching`, `candidate`, `live`, `paused`, `killed`, `evaluating`, or `rejected`. In this public repository, a `live` visual state is part of the mapping vocabulary only; the public build itself does not enable live trading.

`RISK` and `EXECUTION` are distinct visual regions because they are distinct authority boundaries in the architecture. A condition such as `BROKER_CONNECTIVITY_KILL` must render as an explicit failure/safety state rather than as ordinary activity.

In the public build, execution visuals are **simulate-only** or paper-derived. Live order authority is **not produced** by the command centre.

Transient event energy may leave an `afterglow` so recent activity remains understandable without falsifying current state. The query option `labels=off` changes presentation only; it does not change event mapping, policy or system behaviour.

## Read-only guarantee

HTTP/API routes exposed by the command centre provide status, evidence or event streams only. Order-shaped request bodies must not create economic actions. The authoritative path remains strategy/portfolio → deterministic risk → execution state machine → adapter → reconciliation.

## Accessibility and degradation

The interface supports reduced-motion behaviour and a labels-off view without changing semantics. Rendering fallbacks may change graphics technology, but the underlying event-to-state mapping stays deterministic.

## Public/private boundary

This document records the testable visual contract only. Internal design-review iterations, deployment topology and operator runbooks are intentionally excluded from the public repository.
