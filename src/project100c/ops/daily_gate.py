"""The daily broker-token gate (OD-017, docs/engineering/alerts-and-daily-token.md): no approved token, no trading.
Venue-agnostic.

Each trading morning the host asks the broker for a token (Upstox: a request that the owner approves in the Upstox app
or on WhatsApp; the token then arrives on the app's notifier webhook). This state machine tracks that one request
and fails closed:

    IDLE -> REQUESTED -> ACTIVE
                 |-> DENIED
                 |-> LAPSED   (no token by the deadline, 09:05 IST by default)
    IDLE -> FAILED            (the request itself failed)

Only ACTIVE, before the token's own expiry, allows trading. Every transition sends an alert; a reminder goes out
`remind_before` ahead of the deadline. The reference is a short random code so that a stray "/deny" cannot match
another day's request. The gate never sees the token itself, only its expiry.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Protocol

from project100c.sessions import IST


class Alerts(Protocol):
    def send(self, severity: str, message: str) -> None: ...


class GateState(StrEnum):
    IDLE = "IDLE"
    REQUESTED = "REQUESTED"
    ACTIVE = "ACTIVE"
    DENIED = "DENIED"
    LAPSED = "LAPSED"
    FAILED = "FAILED"


@dataclass
class TokenGate:
    request_token: Callable[[], datetime]  # sends the broker request; returns when the request itself lapses
    alerts: Alerts
    broker_name: str = "broker"
    deadline: time = time(9, 5)
    remind_before: timedelta = timedelta(minutes=15)
    ref_factory: Callable[[], str] = field(default=lambda: secrets.token_hex(3).upper())
    state: GateState = GateState.IDLE
    day: date | None = None
    ref: str = ""
    valid_until: datetime | None = None
    request_lapses_at: datetime | None = None
    history: list[tuple[datetime, GateState, str]] = field(default_factory=list)
    _reminded: bool = False

    def _go(self, now: datetime, st: GateState, why: str) -> None:
        self.state = st
        self.history.append((now, st, why))

    def _deadline_at(self) -> datetime:
        assert self.day is not None
        return datetime.combine(self.day, self.deadline, tzinfo=IST)

    def start(self, now: datetime) -> None:
        """Request today's token. A new IST day resets the gate; a second call on the same day does nothing."""
        today = now.astimezone(IST).date()
        if self.day == today and self.state is not GateState.IDLE:
            return
        self.day, self.valid_until, self.request_lapses_at, self._reminded = today, None, None, False
        self.ref = self.ref_factory()
        try:
            self.request_lapses_at = self.request_token()
        except Exception as e:
            self._go(now, GateState.FAILED, f"request failed: {type(e).__name__}")
            self.alerts.send(
                "URGENT", f"{self.broker_name} token request FAILED ({type(e).__name__}). No trading today until fixed."
            )
            return
        self._go(now, GateState.REQUESTED, "requested")
        self.alerts.send(
            "URGENT",
            f"Approve today's {self.broker_name} token in the {self.broker_name} app (or WhatsApp) by "
            f"{self.deadline:%H:%M} IST. Ref {self.ref}. Reply /deny {self.ref} to keep the system flat today.",
        )

    def on_token(self, valid_until: datetime, now: datetime) -> bool:
        """A token arrived (already validated by the adapter). Returns True if it was accepted."""
        if self.state is not GateState.REQUESTED:
            self.alerts.send("WARNING", f"{self.broker_name} token arrived in state {self.state}: discarded")
            return False
        if valid_until <= now:
            self.alerts.send("WARNING", f"{self.broker_name} token arrived already expired: discarded")
            return False
        self.valid_until = valid_until
        self._go(now, GateState.ACTIVE, "token received")
        until = valid_until.astimezone(IST)
        self.alerts.send("INFO", f"{self.broker_name} token received, valid until {until:%d-%b %H:%M} IST.")
        return True

    def deny(self, ref: str, now: datetime) -> bool:
        if not ref or ref.upper() != self.ref or self.state not in (GateState.REQUESTED, GateState.ACTIVE):
            self.alerts.send("WARNING", f"/deny {ref!r} ignored: no matching request (state {self.state})")
            return False
        self.valid_until = None
        self._go(now, GateState.DENIED, "denied by owner")
        self.alerts.send("URGENT", f"Token {self.ref} DENIED by you. No trading today; the system stays flat.")
        return True

    def tick(self, now: datetime) -> None:
        if self.state is not GateState.REQUESTED or self.day is None:
            return
        dl = self._deadline_at()
        if now >= dl:
            self._go(now, GateState.LAPSED, "deadline passed")
            self.alerts.send("URGENT", f"No {self.broker_name} token by {self.deadline:%H:%M} IST: no trading today.")
        elif not self._reminded and now >= dl - self.remind_before:
            self._reminded = True
            self.alerts.send(
                "URGENT",
                f"Reminder: approve the {self.broker_name} token (ref {self.ref}) by {self.deadline:%H:%M} IST.",
            )

    def trading_allowed(self, now: datetime) -> bool:
        return (
            self.state is GateState.ACTIVE
            and self.valid_until is not None
            and self.day == now.astimezone(IST).date()
            and now < self.valid_until
        )
