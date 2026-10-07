# 02 — Technology Stack

Selection criteria, in priority order: **determinism and testability → correctness under failure → reproducibility → operational simplicity for one owner → performance**. We are not competing on latency (see 01 §B6), so developer velocity and verifiability matter more than microseconds.

| Layer | Choice | Why | Alternatives considered |
|---|---|---|---|
| Language (all) | **Python 3.12/3.13**, `mypy --strict`, `ruff` | Richest quant/data ecosystem; every Indian broker ships a Python SDK (S23, S29, S33, S35) | — |
| Risk Governor + order state machine | **Python pure functions, frozen dataclasses/pydantic v2, no I/O, no floats for money** (`Decimal` or integer paise) | Can be tested exhaustively (property tests). Can later be ported 1:1 to Rust behind the same interface if profiling or assurance needs it | Rust now: better guarantees, but slower iteration and harder for the owner to audit. Revisit at Phase 5 |
| Schemas / contracts | **pydantic v2** models + JSON Schema export; `StrategySpec` in YAML validated against schema | One source of truth for TradeIntent, RiskTicket, OrderEvent, StrategySpec | protobuf (overkill for single host) |
| Data lake | **Parquet** (Hive-partitioned by `date/underlying/expiry`) + **DuckDB** for SQL + **Polars** for transforms | Columnar, fast, zero-server, reproducible file hashes | TimescaleDB/ClickHouse (ops burden); kdb+ (cost/licence) |
| Data versioning | Content hash (BLAKE3/SHA-256) manifest per partition, stored in git; optional DVC | §13 lineage: data version → research → strategy → trades | LakeFS (heavier) |
| Journal / OMS state | **SQLite in WAL mode, append-only event tables, hash-chained rows**, one DB per trading day + a durable copy | Crash-safe, simple, replayable; no DB server to fail | PostgreSQL (fine later; more moving parts) |
| Inter-process bus | **ZeroMQ** or Unix-domain sockets carrying msgpack-encoded typed messages; sequence numbers + checksums | Simple, local, deterministic ordering per topic | Redis Streams, NATS (acceptable later) |
| Async I/O | `asyncio` + `uvloop` for WebSocket ingest; kernel decision loop single-threaded | One writer per state = no races | multithreading (rejected) |
| Backtester / replay | **Custom event-driven engine** sharing the live strategy/risk/OMS code, with pluggable fill models | Must model NSE specifics (tick 0.05, price bands, SL-limit behaviour, lot 65, STT on sell, expiry rules) and reuse production code exactly | NautilusTrader (strong design; evaluate as a reference or even a base; its Indian broker adapters and NSE cost model would need building); vectorbt (research-only screening, never for promotion) |
| Options math | `py_vollib`/own Black-76 implementation, cross-checked against the broker/NSE IV where available | Greeks for risk and features | QuantLib (heavy) |
| Statistics / validation | numpy, scipy, statsmodels, `arch` (bootstrap), scikit-learn (only for simple, regularised models) | Standard, auditable | Deep learning (not until there is evidence and data to justify it) |
| Experiment registry | **MLflow tracking (local file store)** or a DuckDB table; every run logs code SHA, data manifest hash, params, seed, metrics | Reproducibility; multiple-testing accounting (counts every trial) | W&B (SaaS) |
| Testing | `pytest`, **Hypothesis** (property-based), `pytest-benchmark`, fault-injection harness (fake broker with scripted failures), golden-replay tests | "No capital until kernel is deterministic under simulated failures" (§23) | — |
| Orchestration (research) | Makefile/`just` + cron / `prefect` (optional) | Simple | Airflow (overkill) |
| Process supervision | **systemd** units with `WatchdogSec`, `Restart=on-failure` *for data processes only*; the kernel does **not** auto-restart into trading. It restarts into HALTED and needs pre-flight | Prefer stopping over an uncertain state | Docker Compose (fine for research host) |
| Observability | **Prometheus** + **Grafana**; logs as structured JSON → Loki (or files + DuckDB); owner report via **Streamlit** (read-only) | Standard, free, self-hosted | Datadog (cost) |
| Alerts | Grafana alerting → email/Telegram (the owner sets up the channel; no messages are sent without owner configuration) | Fast human awareness | SMS gateway |
| Secrets | See 16-security: OS keyring / `age`-encrypted file / cloud secret manager; never in git, env dumps or logs | — | — |
| Packaging | `uv` for env/lock; pinned hashes; reproducible builds | Deterministic deployments | poetry, pip-tools |
| LLM agents | Any capable LLM via API, **only on the research host**, tool access limited to read-only data, the backtester sandbox, git branches and report writing | §10: LLM never holds broker creds | — |

### Money and numeric rules
- Money is stored in integer paise or `Decimal`. Prices are in ticks of ₹0.05 (NSE options tick; verify from the instrument master daily).
- No float equality in risk checks. All comparisons include explicit tolerances, which are documented and tested.

### Why not Rust for the kernel from day one?
The kernel's risk logic is small: tens of rules, all pure functions. Python with strict typing, immutability and property-based testing gives high assurance and the owner can audit it. The risk of a Python kernel is in I/O concurrency, not in the arithmetic. The architecture isolates that by making the governor a pure function called from one single-threaded loop. A Rust port is an explicit Phase 5 backlog item (18-backlog, K-R1).
