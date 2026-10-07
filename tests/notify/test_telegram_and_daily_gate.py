"""OD-017 alert channel and daily token gate. No network: an injected poster records every call."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import pytest

from project100c.errors import MissingCredentialError
from project100c.kernel.runtime import MemoryAlerts
from project100c.notify import (
    Command,
    NotifyError,
    TelegramConfig,
    TelegramNotifier,
    load_telegram_config,
)
from project100c.ops import GateState, TokenGate
from project100c.sessions import IST

BOT = "123456:" + "A" * 30
CHAT = "987654321"


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 5, 8, 45, tzinfo=IST)

    def __call__(self) -> datetime:
        return self.t


class Poster:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.replies: list[tuple[int, dict[str, Any]] | Exception] = []

    def post(self, url: str, body: Mapping[str, Any], timeout_s: float) -> tuple[int, dict[str, Any]]:
        self.calls.append((url, dict(body)))
        if self.replies:
            r = self.replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return 200, {"ok": True, "result": {"message_id": len(self.calls)}}


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def poster() -> Poster:
    return Poster()


@pytest.fixture
def tg(clock: Clock, poster: Poster) -> TelegramNotifier:
    return TelegramNotifier(TelegramConfig(BOT, CHAT), clock=clock, poster=poster)


def test_config_from_env_only_and_masked() -> None:
    c = load_telegram_config({"TELEGRAM_BOT_TOKEN": BOT, "TELEGRAM_CHAT_ID": CHAT})
    assert BOT not in repr(c) and "AAAA" not in repr(c)
    with pytest.raises(MissingCredentialError):
        load_telegram_config({})
    with pytest.raises(MissingCredentialError):
        TelegramConfig("no-colon", CHAT)
    with pytest.raises(MissingCredentialError):
        TelegramConfig(BOT, "@channel")


def test_send_posts_to_the_one_chat_with_severity(tg: TelegramNotifier, poster: Poster) -> None:
    tg.send("URGENT", "BROKER_CONNECTIVITY latched")
    url, body = poster.calls[0]
    assert url.endswith("/sendMessage") and url.startswith("https://api.telegram.org/bot")
    assert body["chat_id"] == CHAT and body["text"].startswith("[P1 URGENT]") and "08:45:00 IST" in body["text"]
    assert "BROKER_CONNECTIVITY latched" in body["text"] and tg.sent == 1


def test_dedupe_and_truncation(tg: TelegramNotifier, poster: Poster, clock: Clock) -> None:
    tg.send("URGENT", "same")
    tg.send("URGENT", "same")
    assert len(poster.calls) == 1
    clock.t += timedelta(seconds=61)
    tg.send("URGENT", "same")
    assert len(poster.calls) == 2
    tg.send("INFO", "x" * 5000)
    assert len(poster.calls[-1][1]["text"]) == 4096


def test_failures_never_raise_and_mark_the_channel_unhealthy(tg: TelegramNotifier, poster: Poster) -> None:
    poster.replies = [
        NotifyError("down"),
        (429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 5}}),
        (500, {}),
    ]
    for i in range(3):
        tg.send("URGENT", f"m{i}")
    assert not tg.healthy() and tg.consecutive_failures == 3 and "retry_after=5" in tg.failures[1]
    tg.send("URGENT", "back")
    assert tg.healthy() and tg.consecutive_failures == 0


def _update(uid: int, text: str, chat: int | str = int(CHAT)) -> dict[str, Any]:
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def test_commands_only_from_the_owner_chat_and_only_risk_reducing(tg: TelegramNotifier, poster: Poster) -> None:
    poster.replies = [
        (
            200,
            {
                "ok": True,
                "result": [
                    _update(10, "/deny AB12CD"),
                    _update(11, "/kill", chat=111),  # a stranger: ignored
                    _update(12, "/resume"),  # no such command exists
                    _update(13, "/status@p100c_bot"),
                    _update(14, "/KILL now"),
                ],
            },
        )
    ]
    cmds = tg.poll_commands()
    assert [(c.command, c.arg) for c in cmds] == [(Command.DENY, "AB12CD"), (Command.STATUS, ""), (Command.KILL, "now")]
    tg.poll_commands()
    assert poster.calls[-1][1]["offset"] == 15  # each update is consumed once
    assert {c.value for c in Command} == {"/deny", "/kill", "/status"}


def test_poll_failure_returns_nothing(tg: TelegramNotifier, poster: Poster) -> None:
    poster.replies = [NotifyError("down")]
    assert tg.poll_commands() == [] and tg.consecutive_failures == 1


# -- token gate ----------------------------------------------------------------------------------------------


def _st(g: TokenGate) -> GateState:
    return g.state  # read through a call so mypy does not narrow across transitions


def _gate(clock: Clock, alerts: MemoryAlerts, fail: bool = False) -> TokenGate:
    def req() -> datetime:
        if fail:
            raise ConnectionError("x")
        return clock.t.replace(hour=3, minute=30) + timedelta(days=1)

    return TokenGate(req, alerts, broker_name="Upstox", ref_factory=lambda: "AB12CD")


def test_gate_happy_path(clock: Clock) -> None:
    a = MemoryAlerts()
    g = _gate(clock, a)
    assert not g.trading_allowed(clock.t)
    g.start(clock.t)
    assert _st(g) is GateState.REQUESTED and "Ref AB12CD" in a.urgent()[0] and "/deny AB12CD" in a.urgent()[0]
    g.start(clock.t)  # same day: no second request
    assert len(a.sent) == 1
    exp = datetime(2026, 10, 6, 3, 30, tzinfo=IST)
    assert g.on_token(exp, clock.t)
    assert g.trading_allowed(clock.t) and _st(g) is GateState.ACTIVE
    assert not g.trading_allowed(datetime(2026, 10, 6, 9, 0, tzinfo=IST))  # next day needs a new token


def test_gate_reminds_then_lapses_fail_closed(clock: Clock) -> None:
    a = MemoryAlerts()
    g = _gate(clock, a)
    g.start(clock.t)
    clock.t = clock.t.replace(hour=8, minute=50)
    g.tick(clock.t)
    g.tick(clock.t)
    assert sum("Reminder" in m for m in a.urgent()) == 1
    clock.t = clock.t.replace(hour=9, minute=5)
    g.tick(clock.t)
    assert _st(g) is GateState.LAPSED and "no trading today" in a.urgent()[-1]
    assert not g.on_token(clock.t + timedelta(hours=10), clock.t)  # a late token is discarded
    assert not g.trading_allowed(clock.t)


def test_gate_deny_wins_over_a_later_token(clock: Clock) -> None:
    a = MemoryAlerts()
    g = _gate(clock, a)
    g.start(clock.t)
    assert not g.deny("ZZZZZZ", clock.t) and _st(g) is GateState.REQUESTED
    assert g.deny("ab12cd", clock.t) and _st(g) is GateState.DENIED
    assert not g.on_token(clock.t + timedelta(hours=10), clock.t)
    assert not g.trading_allowed(clock.t)


def test_gate_deny_after_activation_stops_trading(clock: Clock) -> None:
    a = MemoryAlerts()
    g = _gate(clock, a)
    g.start(clock.t)
    g.on_token(clock.t + timedelta(hours=10), clock.t)
    assert g.deny("AB12CD", clock.t) and not g.trading_allowed(clock.t)


def test_gate_request_failure_fails_closed(clock: Clock) -> None:
    a = MemoryAlerts()
    g = _gate(clock, a, fail=True)
    g.start(clock.t)
    assert _st(g) is GateState.FAILED and "FAILED" in a.urgent()[0] and not g.trading_allowed(clock.t)


def test_gate_expired_token_is_refused(clock: Clock) -> None:
    g = _gate(clock, MemoryAlerts())
    g.start(clock.t)
    assert not g.on_token(clock.t - timedelta(seconds=1), clock.t) and _st(g) is GateState.REQUESTED


def test_gate_resets_on_a_new_day(clock: Clock) -> None:
    a = MemoryAlerts()
    g = _gate(clock, a)
    g.start(clock.t)
    g.deny("AB12CD", clock.t)
    clock.t += timedelta(days=1)
    g.start(clock.t)
    assert _st(g) is GateState.REQUESTED and g.day == clock.t.date()
