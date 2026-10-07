# 21. Alerts and the daily broker token (OD-017)

Status: **built (fake transports only)** on 3-Oct-2026. No bot token, chat ID or broker key exists yet; the owner supplies them at go-live through the secure form (see [`docs/go-live-checklist.md`](../go-live-checklist.md)).

## 21.1 What OD-017 decided

- **Telegram** carries every P1 (URGENT) alert and the daily broker-token prompt.
- Settings come from the environment only: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. They are never in git, a config file or a log line. The bot token is part of the Telegram API URL, so URLs are never logged either.
- Alert channels are adapters (OD-017 rule 4). The kernel only knows the `AlertSink` protocol (`send(severity, message)`); `project100c.notify` is never imported by the core (`tests/test_architecture_boundaries.py`).

## 21.2 The Telegram channel (`notify/telegram.py`)

| Rule | Behaviour |
|---|---|
| One chat | Messages go to the configured chat ID only. Severity prefix (`[P1 URGENT]`, `[WARNING]`, `[info]`), IST time, then the message; truncated to 4,096 characters |
| Never crash the loop | A failed send (network, HTTP 429/5xx, API error) is recorded, never raised. After 3 consecutive failures `healthy()` is False: the alert channel is down |
| No floods | Identical messages within 60 s are sent once |
| Commands reduce risk only | From the configured chat only: `/deny <ref>` (refuse today's token: the system stays flat), `/kill` (latch the manual master kill), `/status`. Messages from any other chat are ignored. **There is no command that resumes trading, resets a kill or adds risk**; those stay on the dashboard and in the runbook, where they need a deliberate action |

What a down channel means: the host keeps doing everything that reduces risk (stops, flatten, Exit-All). The go-live checklist requires a test alert to arrive before the first session, and the paper run checks the channel each morning.

## 21.3 The daily token gate (`ops/daily_gate.py`)

Upstox (verified from its documentation on 3-Oct-2026): `POST /v3/login/auth/token/request/{client_id}` with the API secret notifies the owner in the Upstox app and on WhatsApp. On approval Upstox POSTs the token to the app's **Notifier Webhook**. The request and the token both lapse at 03:30 IST the next morning. So the approval itself happens in Upstox; Telegram carries the prompt, the outcome and the refusal.

```
IDLE --start--> REQUESTED --token (validated)--> ACTIVE
                    |--"/deny <ref>"-----------> DENIED   (a later token is discarded)
                    |--09:05 IST, no token-----> LAPSED   (no trading today)
IDLE --request fails--> FAILED                             (no trading today)
ACTIVE --"/deny <ref>"--> DENIED                           (host flattens and stops entries)
```

- **Fail closed.** Only ACTIVE, on the same IST day and before the token's own expiry, allows trading.
- **Reference code.** Each morning's request gets a short random reference (for example `AB12CD`). `/deny` must quote it, so a stale message cannot refuse another day's request.
- **Alerts.** Every transition sends one: the request (URGENT, with the reference and the deadline), a reminder 15 minutes before the deadline, the token's arrival (INFO, with its expiry), a denial, a lapse or a failure (URGENT).
- **The gate never sees the token.** The Upstox adapter validates the webhook payload (`session_from_webhook`: message type, app ID, Bearer type, validity window, 03:30 rule) and hands the gate only the expiry.

## 21.4 The webhook receiver (`broker/upstox/webhook.py`, built 3-Oct-2026)

`NotifierReceiver.handle()` holds the whole policy and has no HTTP in it; `serve()` wraps it in a standard-library
threaded HTTP server that never logs requests. It fails closed:

- Only `POST` to the configured path, which must carry an unguessable segment of at least 16 characters (Upstox does
  not sign the payload, so the path is defence in depth). Another path is 404 and is not rate-counted, so a scanner
  cannot block the real callback; another method is 405.
- `application/json` (ASSUMED: Upstox's documentation does not state the header), at most 4 KB, at most 20 requests
  a minute on the real path (429 beyond).
- The payload must pass `session_from_webhook`; then the gate must accept the expiry (409 on a denied or expired
  day). Only then does the in-memory session reach `on_session`.
- Responses say only `accepted` or `refused`. The token is never written to disk, the journal, a log or a response.
- TLS is terminated in `serve()` from an `ssl.SSLContext` or by a reverse proxy on the same host; plain HTTP is
  allowed only on loopback.

Still to do: run it on the Mumbai host with a real certificate and register the URL on the Upstox app (go-live
checklist part 1).

## 21.5 Not built yet

- The host's composition root (`ops/host_main`): it builds the gate, the notifier's command polling, the webhook receiver, the feed and the kernel runtime, and runs them in `ops/host.HostService` (built 3-Oct-2026, K-12).
