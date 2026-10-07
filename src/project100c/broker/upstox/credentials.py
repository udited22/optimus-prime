"""Upstox credentials and the daily session, from the environment only (OD-004, OD-017).

Nothing here writes, logs or prints a secret: ``repr`` masks every value. The API key and secret are created by
the owner at go-live and supplied through the secure form into the host's environment; the daily access token arrives
through the token-approval flow (docs/engineering/alerts-and-daily-token.md). No credential is ever in git, in a config
file in the repository, or
reachable from the research agents (tests/test_architecture_boundaries.py).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from project100c.errors import MissingCredentialError
from project100c.sessions import IST

ENV_API_KEY = "UPSTOX_API_KEY"
ENV_API_SECRET = "UPSTOX_API_SECRET"
ENV_REDIRECT_URI = "UPSTOX_REDIRECT_URI"
ENV_ACCESS_TOKEN = "UPSTOX_ACCESS_TOKEN"
TOKEN_EXPIRY = time(3, 30)  # Upstox access tokens expire at 03:30 IST the next day


def _mask(v: str) -> str:
    return "<unset>" if not v else f"<{len(v)} chars>"


@dataclass(frozen=True, slots=True)
class UpstoxAppCredentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    redirect_uri: str

    def __repr__(self) -> str:
        return f"UpstoxAppCredentials(api_key={_mask(self.api_key)}, api_secret={_mask(self.api_secret)})"


@dataclass(frozen=True, slots=True)
class UpstoxSession:
    """One trading day's access token. Valid from issue until the next 03:30 IST."""

    access_token: str = field(repr=False)
    issued_at: datetime

    def __post_init__(self) -> None:
        if not self.access_token or any(c.isspace() for c in self.access_token):
            raise MissingCredentialError("Upstox access token is empty or malformed")
        if self.issued_at.tzinfo is None:
            raise ValueError("issued_at must be timezone-aware")

    @property
    def expires_at(self) -> datetime:
        t = self.issued_at.astimezone(IST)
        exp = datetime.combine(t.date(), TOKEN_EXPIRY, tzinfo=IST)
        return exp if t < exp else exp + timedelta(days=1)

    def valid_at(self, now: datetime) -> bool:
        return self.issued_at <= now < self.expires_at

    def __repr__(self) -> str:
        return f"UpstoxSession(access_token={_mask(self.access_token)}, expires_at={self.expires_at.isoformat()})"


def load_app_credentials(env: Mapping[str, str] | None = None) -> UpstoxAppCredentials:
    e = os.environ if env is None else env
    missing = [k for k in (ENV_API_KEY, ENV_API_SECRET, ENV_REDIRECT_URI) if not e.get(k)]
    if missing:
        raise MissingCredentialError(f"Upstox app credentials missing from the environment: {missing}")
    return UpstoxAppCredentials(e[ENV_API_KEY], e[ENV_API_SECRET], e[ENV_REDIRECT_URI])


def load_session(issued_at: datetime, env: Mapping[str, str] | None = None) -> UpstoxSession:
    """The day's token from ``UPSTOX_ACCESS_TOKEN`` (placed there by the token-approval service, never by hand
    into a file in the repository)."""
    e = os.environ if env is None else env
    tok = e.get(ENV_ACCESS_TOKEN, "")
    if not tok:
        raise MissingCredentialError(f"{ENV_ACCESS_TOKEN} is not set: complete today's token approval first")
    return UpstoxSession(tok, issued_at)
