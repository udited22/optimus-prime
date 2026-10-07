# 16 — Security and Credential Architecture

## 16.1 Threat model (top risks)
1. **Credential theft**: API key/secret, access token, TOTP seed, broker password. Leads to unauthorised trading or fund withdrawal.
2. **Agent overreach**: an LLM agent (or prompt-injected content it reads) tries to place orders or change limits.
3. **Supply chain**: a malicious or compromised Python package.
4. **Host compromise** of the trading VM.
5. **Operator error**: a wrong config deployed during market hours.

## 16.2 Controls
| Control | Design |
|---|---|
| Separation of hosts | Trading host (static IP, kernel only) vs research host (agents, lake). No shared secrets. Research → trading only via signed release artefacts |
| Least privilege | Kernel runs as `p100c` service user; secrets readable only by `oms_gateway`'s user; no sudo for service users |
| Secret storage | Broker API secret + any permitted TOTP seed in an encrypted store (e.g. `age`/`sops`-encrypted file unlocked by a key held in a cloud KMS/secret manager, or the OS keyring). Never in git, env dumps, logs, crash reports or agent context. Redaction filter on all loggers |
| Secret storage, as built (3-Oct-2026, K-S1) | `ops/redact.py`, `ops/credstore.py`. **Encrypted store (built 3-Oct-2026 evening):** one Fernet-encrypted file (AES-128-CBC + HMAC-SHA256, `cryptography` package) outside the repo, mode 0600, with its key from `P100C_CREDSTORE_KEY` or a 0600 key file named by `P100C_CREDSTORE_KEY_FILE` (a Docker or systemd secret). A wrong key or an edited file is refused; only allowed names are read; an environment variable overrides the file. Not built: a KMS-held key (the key file is the root of trust, so it must never sit in the image, the repo or a backup next to the store). The daily access token is never stored. Credentials may also come straight from the environment, set by systemd from a file that `check_private_file` accepts only as a regular file owned by the service user with mode 0600 (`deploy/project100c-host.service`). `Redactor` masks the exact values of the known credential variables, any value registered at run time (the daily token), and credential-shaped text (Bearer headers, Telegram `bot<id>:<secret>` URLs, JWTs, `code=`/`access_token=`/`client_secret=`/`api_key=` fields). `install_log_redaction` rewrites log messages, arguments, tracebacks and stack text before any handler formats them; `RedactingAlerts` wraps any alert sink |
| Session tokens | Daily access tokens held only in gateway process memory; written to disk only encrypted if restart continuity is needed; revoked/logged out at end of day (required, S2) |
| 2FA | **Upstox (OD-004)** documents three sanctioned routes: interactive OAuth; a semi-automated request that the owner approves on mobile, with the token delivered to our notifier URL; and a TOTP + PIN token API. **Recommended: semi-automated owner approval.** Nothing sensitive is stored, and the human stays in the loop. TOTP + PIN would require storing the PIN and TOTP seed on the trading host (higher theft impact), so it needs an explicit owner decision. No automation beyond what Upstox documents |
| No withdrawal capability | Use API scopes/permissions without fund-withdrawal where the broker allows (e.g. never call payout APIs; block those endpoints in the adapter by allow-list) |
| Network | Trading host: inbound only SSH from the owner's allow-listed IP with key + optional hardware key; outbound allow-list to broker API/WS domains, NTP, package mirror (build time only), metrics push endpoint |
| Signed releases | Git tags signed; CI builds a wheel with a pinned lockfile (`uv` hashes); the trading host verifies the signature and hash before install; deploys blocked 08:45–16:30 IST on trading days |
| Config integrity | Risk limits file signed; the kernel verifies the signature + hash at start and every minute; mismatch → SYSTEM_INTEGRITY_KILL |
| Journal integrity | Hash-chained rows; daily digest copied off-host (research host + cloud bucket) |
| Dependency hygiene | Minimal dependency set on the trading host; `pip-audit` in CI; no packages installed at runtime |
| Agent sandbox | Agents run with tool allow-lists: read lake, run backtests in a container, write to git branches, write reports. No shell on the trading host, no broker SDKs installed on the research host |
| Broker agent tooling | The **Upstox MCP server and Upstox Agent Skill are prohibited** on all Project 100C hosts and agent tool lists. Only the deterministic Gateway holds an Upstox token (directive §10) |
| Static-IP changes | The Upstox IP can change once per calendar week, and a change invalidates all tokens. Changes happen only on non-trading days, through a runbook with owner sign-off |
| Prompt-injection hygiene | News/web content given to agents is treated as untrusted data; agent outputs never feed the kernel directly |
| Backups | Journal + lake manifests daily; restore drill monthly |
| Owner kill path | Out-of-band: (1) CLI on the trading host; (2) signed "HALT" message via a minimal authenticated endpoint; (3) the broker's own app/website manual exit + broker-side kill switch (Upstox Kill Switch API, S55: usable only when flat, 12 h cooling) |

## 16.3 Credentials inventory (to be filled at Phase 1; no credential is handled in this design phase)
| Secret | Owner | Storage | Rotation |
|---|---|---|---|
| Upstox API key/secret (live app) | The owner creates it **only at go-live** (OD-004) | encrypted store on trading host | on suspicion / yearly |
| Upstox sandbox app token (30-day) | The owner creates it when the K-B2 contract tests start | encrypted store on the **research/CI** host only | 30 days (forced) |
| Daily Upstox access token (expires 03:30 IST) | system | gateway memory | daily (forced by broker) |
| Upstox PIN + TOTP seed (**only if** the owner chooses the TOTP-login route) | The owner | encrypted store | on device change |
| Upstox Analytics Token (read-only, 1 yr), optional | The owner | research host keyring | yearly |
| Cloud account (VPS/static IP) | The owner | password manager + hardware 2FA | — |
| Data API tokens (research) | The owner | research host keyring | per vendor policy |
