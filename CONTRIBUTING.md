# Contributing and development rules

Optimus Prime is a personal research project. These rules apply to every change, whether a human or an agent makes it. The project constitution (kept private) always wins over this file.

## Setup

```bash
uv venv -p 3.13 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
(cd dashboard && npm ci)   # only needed for the dashboard and its build test
```

## The gate: every commit must pass all four

```bash
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/mypy                 # strict, covers src/ and tests/
.venv/bin/python -m pytest -q
```

There is no CI workflow in this repository yet: run the four checks and `gitleaks git --redact .` locally before every push.

## Rules

1. **Tests with every module.** New behaviour ships with tests in the same commit. Safety-relevant code (Governor, mandate, kills, journal, broker adapters, data ingest) also needs failure-injection tests, and Hypothesis property tests where a property exists, for example "never net short" or "every order is LIMIT or SL-limit on the tick grid".
2. **Strict typing.** `mypy --strict` over `src/` and `tests/`. No `Any` leaks and no `type: ignore` without a reason in a comment. Money and prices are `Decimal`, never `float`. Timestamps are timezone-aware (IST).
3. **No silent failures.** Raise the typed errors in [`src/project100c/errors.py`](src/project100c/errors.py) (`ConfigError`, `DataQualityInputError`, `MissingCredentialError`, ...). Never swallow an exception, never "fix" data quietly, never fall back to a default. Unknown or untrustworthy state disables entries. DQ problems become explicit issues, quarantine or `NO_DATA` records.
4. **One module per commit.** Each commit implements one backlog item or one module and names it in the title (for example `K-08: ...` or `D-06: ...`). Policy changes (OD-xxx rules, see [docs/risk/policy-rules.md](docs/risk/policy-rules.md)) get their own commit. Never rewrite published history.
5. **Versioned configuration, not constants.** Costs, sessions, trading windows, risk limits and the calendar live in `configs/` with version IDs and effective dates. Old versions are kept so historical runs stay reproducible.
6. **Honest labels.** Synthetic or fixture data is labelled **SIMULATED**. Modelled quantities, such as synthetic spreads, are labelled **ASSUMED**. Behaviour not yet confirmed against a real broker or vendor is labelled **UNVERIFIED**. Never present a backtest as evidence without the validation in docs/research/validation.md.
7. **Safety boundaries.**
   - No LLM or agent path to orders. Every order passes the deterministic Risk Governor.
   - Long options only (OD-006).
   - The trading window and the hard flat are enforced in code (OD-002/008/009).
   - The Dhan client is data only (OD-011).
   - Changes to the constitution, risk limits or OD-xxx policy rules need the owner's explicit approval.
8. **No secrets in git, ever.**
   - Credentials come only from environment variables (for example `DHAN_ACCESS_TOKEN`) or the gitignored `configs/local/`.
   - Never log, print or persist a token.
   - Run `gitleaks git --redact .` before pushing.
   - If a secret is ever committed, stop, rotate it, and tell the owner. Do not just delete it in a new commit.
9. **Docs follow code, briefly.** No per-commit commentary or conversation records in this repository.
   - **Public-repo rule:** no wealth or income targets, prompts, chat history, personal instructions, decision transcripts, capital or broker-account details, secrets, alpha parameters of live candidates or trading journals. Live candidates belong in the private alpha library ([docs/engineering/private-alpha.md](docs/engineering/private-alpha.md)).
10. **The README states only what is committed, and every push refreshes its status.**
    - Keep the README's short "Current status" table accurate (date and test counts from an actual run).
    - Describe only what exists on `main`.
    - Market data (`lake/`) and credentials (`configs/local/`) are never committed.
