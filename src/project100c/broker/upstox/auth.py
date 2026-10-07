"""Upstox daily access token: the request that triggers the owner's approval, and the notifier-webhook payload.

Verified against Upstox's documentation on 3-Oct-2026 ("Access Token Request for User"):
``POST https://api.upstox.com/v3/login/auth/token/request/{client_id}`` with ``{"client_secret": ...}`` notifies
the account holder in the Upstox app and on WhatsApp. On approval Upstox POSTs the token to the Notifier Webhook
configured on the app (``message_type == "access_token"``, millisecond epoch ``issued_at``/``expires_at``). The
request itself expires at 03:30 IST the next day. Rejection sends nothing. Error codes: UDAPI100069 bad key or
secret, UDAPI1123 no notifier URL configured, UDAPI1124 not an individual user, UDAPI1155 app under review,
UDAPI1157 app expired.

The approval itself therefore happens in Upstox; Telegram (OD-017) carries the prompt, the outcome and the owner's
``/deny`` (docs/engineering/alerts-and-daily-token.md). This module performs no I/O except through the injected
transport and never logs a token.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from project100c.broker.upstox.credentials import UpstoxAppCredentials, UpstoxSession
from project100c.broker.upstox.transport import Transport
from project100c.errors import BrokerError, BrokerRejectError

TOKEN_REQUEST_PATH = "/v3/login/auth/token/request/"


@dataclass(frozen=True, slots=True)
class TokenRequestAck:
    authorization_expiry: datetime  # the request lapses at this moment if not approved


def _ms(v: Any, what: str) -> datetime:
    try:
        return datetime.fromtimestamp(int(str(v)) / 1000, tz=UTC)
    except (TypeError, ValueError) as e:
        raise BrokerError(f"{what} is not a millisecond epoch") from e


def request_access_token(
    creds: UpstoxAppCredentials, transport: Transport, *, base_url: str = "https://api.upstox.com"
) -> TokenRequestAck:
    resp = transport.request(
        "POST",
        f"{base_url}{TOKEN_REQUEST_PATH}{creds.api_key}",
        headers={"Accept": "application/json"},
        json_body={"client_secret": creds.api_secret},
        timeout_s=5.0,
    )
    b = resp.body
    if resp.status != 200 or b.get("status") != "success" or not isinstance(b.get("data"), dict):
        raw_errs = b.get("errors")
        errs: list[Any] = raw_errs if isinstance(raw_errs, list) else []
        codes = [str(e.get("errorCode") or e.get("error_code")) for e in errs if isinstance(e, dict)]
        raise BrokerRejectError(f"token request refused: HTTP {resp.status} {codes}")
    return TokenRequestAck(_ms(b["data"].get("authorization_expiry"), "authorization_expiry"))


def session_from_webhook(payload: Mapping[str, Any], *, expected_client_id: str, now: datetime) -> UpstoxSession:
    """Validate a notifier-webhook payload and turn it into the day's session. Fails closed on anything odd."""
    if payload.get("message_type") != "access_token":
        raise BrokerRejectError("webhook: not an access_token message")
    if not hmac.compare_digest(str(payload.get("client_id", "")), expected_client_id):
        raise BrokerRejectError("webhook: client_id does not match this app")
    if str(payload.get("token_type", "")).lower() != "bearer":
        raise BrokerRejectError("webhook: token_type is not Bearer")
    tok = payload.get("access_token")
    if not isinstance(tok, str) or not tok:
        raise BrokerRejectError("webhook: no access_token")
    issued = _ms(payload.get("issued_at"), "issued_at")
    expires = _ms(payload.get("expires_at"), "expires_at")
    if not issued <= now < expires:
        raise BrokerRejectError("webhook: token is not valid now (stale or future-dated)")
    session = UpstoxSession(tok, issued)
    if expires > session.expires_at:
        raise BrokerRejectError("webhook: expires_at is later than the 03:30 IST rule allows")
    return session
