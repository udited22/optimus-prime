# Contributing and development rules

Optimus Prime is a personal quantitative-research and systems-engineering project. Changes should preserve reproducibility, explicit uncertainty and strict separation between research logic and transactional authority.

## Setup

```bash
uv venv -p 3.13 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
(cd dashboard && npm ci)
```

## Quality gate

Every change should pass the same gates enforced by CI:

```bash
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/mypy
.venv/bin/python -m pytest -q
(cd dashboard && npm test && npm run build)
```

The repository also runs a secrets scan in CI.

## Engineering rules

1. **Tests travel with behaviour.** New behaviour requires tests in the same change. Safety-relevant components need negative-path and failure-injection coverage; property tests are preferred where an invariant can be expressed directly.
2. **Strict typing.** `mypy --strict` covers `src/` and `tests/`. Avoid unbounded `Any`; money and prices use `Decimal`; timestamps are timezone-aware.
3. **No silent repair.** Invalid data, configuration or external state must produce an explicit typed failure, quarantine or `NO_DATA` outcome. Unknown state must not quietly become a default value.
4. **Version configuration.** Costs, sessions, risk policy, calendars and research configuration are versioned so historical runs remain reproducible.
5. **Separate research from authority.** Research code may propose strategies and intents. It does not bypass deterministic risk or execution controls.
6. **Fail closed.** Untrusted market data, inconsistent state or failed reconciliation blocks new exposure until the condition is understood.
7. **Honest labels.** Synthetic/fixture output is `SIMULATED`; modelled quantities are `ASSUMED`; behaviour not confirmed against the relevant external interface is `UNVERIFIED`.
8. **No secrets in git.** Credentials come from environment variables or ignored local configuration. Never log or persist credentials. Rotate immediately if a secret is ever committed.
9. **Keep public docs enduring.** The public repository should contain architecture, methodology and reproducible engineering evidence — not chat transcripts, personal capital targets, broker-account details, deployment secrets, live strategy parameters or trading journals.
10. **Keep the README factual.** Describe only capabilities that exist in the current branch and do not imply validated edge or live deployment where neither exists.

## Public engine / private research

Exact rules and parameters for current candidate strategies belong outside the public repository. The public engine must remain testable with synthetic/public stand-ins and must enforce the same validation and risk boundaries regardless of where a candidate strategy is loaded from.

See [docs/engineering/private-alpha.md](docs/engineering/private-alpha.md).
