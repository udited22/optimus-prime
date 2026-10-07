# Security model

Optimus Prime treats security as part of trading correctness. A strategy with good expected value is still unsafe if research tooling, credentials or external state can bypass the system's authority boundaries.

This document describes the public security model. Account-specific credentials, deployment topology, authentication procedures and production runbooks are intentionally private.

## Threat model

The design focuses on a small set of high-impact risks:

1. **Credential compromise** — an external API credential is exposed or used outside its intended component.
2. **Research-agent overreach** — an automated research process attempts to cross from advisory work into transactional authority.
3. **Prompt/data injection** — untrusted market/news/web content tries to influence an agent's tool behaviour rather than merely its analysis.
4. **Supply-chain compromise** — a dependency or build artefact is malicious or unexpectedly changed.
5. **Host/process compromise** — a runtime component gains authority it should not have.
6. **Operator/configuration error** — an invalid configuration expands risk instead of failing closed.
7. **Secret leakage through logs or diagnostics** — sensitive values escape through observability rather than source code.

## Core controls

### Separate research from execution authority

Research tools and language-model agents can inspect data, run experiments, propose code changes and produce reports. They do not hold execution credentials and do not directly submit orders.

A research artefact must cross explicit validation and deterministic risk boundaries before it can affect execution state.

### Least privilege

Components should receive only the capabilities required for their role. In particular:

- research processes do not require transactional credentials;
- observability is read-only;
- execution adapters expose only the operations required by the order lifecycle;
- account/funds-management operations outside the trading mandate are not part of the adapter surface.

### Secrets never belong in Git

The repository contains no live credentials. Runtime secrets are supplied through deployment-specific secret management and are redacted from logs, exception text and structured telemetry.

The public CI pipeline runs a repository secrets scan in addition to normal tests.

### Deterministic authority boundary

No agent, dashboard or research process receives an alternate route around the Risk Governor and execution state machine. An approved risk decision is bound to a specific intent rather than acting as a general permission to trade.

### Fail closed on integrity problems

Examples of conditions that reduce authority include:

- configuration/hash mismatch;
- failed journal writes or state reconstruction;
- stale/untrusted market data;
- unknown external execution state;
- reconciliation mismatch;
- unexpected credential/session failure.

The system does not convert an integrity failure into a permissive fallback.

## Logging and redaction

Structured logs are designed for diagnosis without exposing credentials. Known sensitive values and credential-shaped strings are redacted before handlers emit them.

A useful invariant is: **the data needed to debug a failure should not include the secret that authorised the operation.**

## Public/private boundary

The public repository intentionally documents security principles and testable boundaries, not production secrets or deployment instructions. The following stay outside Git:

- API keys, tokens and authentication factors;
- account identifiers;
- deployment IPs/host access rules;
- private notification endpoints;
- production secret-store configuration;
- operational authentication and emergency-response runbooks.

That separation is deliberate: public review should make the security architecture understandable without turning the repository into a deployment manual.
