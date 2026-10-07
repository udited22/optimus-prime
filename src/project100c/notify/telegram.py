"""Telegram alert channel (OD-017): P1 alerts and the daily token prompt go to the owner's Telegram chat.

Configuration comes only from the environment (``TELEGRAM_BOT_TOKEN``, ``TELEGRAM_CHAT_ID``), supplied through the
secure form at go-live; neither is ever in git, a config file or a log line (the bot token is part of the API URL,
so URLs are never logged either).

Design rules:

* **Sending never raises into the trading loop.** A failed send is recorded and counted; `healthy()` turns False
  after `max_consecutive_failures`, which the host treats as "alert channel down" (it may keep reducing risk, but
  the go-live checklist requires a working channel before any entry).
* **Commands can only reduce risk.** From the configured chat only: ``/deny <ref>`` (refuse today's broker token,
  so the system stays flat), ``/kill`` (latch the manual master kill) and ``/status``. There is deliberately no
  command that resumes, approves risk or resets a kill: those stay on the dashboard and the runbook
  (docs/engineering/alerts-and-daily-token.md).
* Identical messages within `dedupe_s` are sent once; text is truncated to Telegram's 4096-character limit.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from project100c.errors import MissingCredentialError, Project100CError

ENV_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHAT_ID = "TELEGRAM_CHAT_ID"
API = "https://api.telegram.org"
MAX_TEXT = 4096
_SEVERITY_PREFIX = {"URGENT": "[P1 URGENT]", "WARNING": "[WARNING]", "INFO": "[info]"}


class NotifyError(Project100CError):
    """The alert channel failed (network, HTTP or API error)."""


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    bot_token: str = field(repr=False)
    chat_id: str

    def __post_init__(self) -> None:
        if not self.bot_token or ":" not in self.bot_token or any(c.isspace() for c in self.bot_token):
            raise MissingCredentialError("TELEGRAM_BOT_TOKEN is empty or malformed")
        if not self.chat_id.lstrip("-").isdigit():
            raise MissingCredentialError("TELEGRAM_CHAT_ID must be a numeric chat id")

    def __repr__(self) -> str:
        return f"TelegramConfig(bot_token=<{len(self.bot_token)} chars>, chat_id={self.chat_id})"


def load_telegram_config(env: Mapping[str, str] | None = None) -> TelegramConfig:
    e = os.environ if env is None else env
    missing = [k for k in (ENV_BOT_TOKEN, ENV_CHAT_ID) if not e.get(k)]
    if missing:
        raise MissingCredentialError(f"Telegram settings missing from the environment: {missing}")
    return TelegramConfig(e[ENV_BOT_TOKEN], e[ENV_CHAT_ID])


class JsonPoster(Protocol):
    def post(self, url: str, body: Mapping[str, Any], timeout_s: float) -> tuple[int, dict[str, Any]]: ...


class UrllibPoster:
    def post(self, url: str, body: Mapping[str, Any], timeout_s: float) -> tuple[int, dict[str, Any]]:
        if not url.startswith(API + "/"):
            raise NotifyError("refusing a non-Telegram URL")
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                return r.status, _decode(r.read())
        except urllib.error.HTTPError as e:
            return e.code, _decode(e.read())
        except (urllib.error.URLError, OSError) as e:
            raise NotifyError(f"telegram unreachable: {type(e).__name__}") from None


def _decode(raw: bytes) -> dict[str, Any]:
    try:
        v = json.loads(raw.decode() or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return v if isinstance(v, dict) else {}


class Command(StrEnum):
    DENY = "/deny"
    KILL = "/kill"
    STATUS = "/status"


@dataclass(frozen=True, slots=True)
class OwnerCommand:
    command: Command
    arg: str
    update_id: int


class TelegramNotifier:
    """An `AlertSink` (``send(severity, message)``) that posts to one Telegram chat."""

    def __init__(
        self,
        config: TelegramConfig,
        *,
        clock: Callable[[], datetime],
        poster: JsonPoster | None = None,
        timeout_s: float = 5.0,
        dedupe_s: float = 60.0,
        max_consecutive_failures: int = 3,
        label: str = "Project 100C",
    ) -> None:
        self._cfg = config
        self._clock = clock
        self._p: JsonPoster = poster or UrllibPoster()
        self._timeout = timeout_s
        self._dedupe = dedupe_s
        self._max_fail = max_consecutive_failures
        self._label = label
        self._last_sent: dict[str, datetime] = {}
        self._offset = 0
        self.consecutive_failures = 0
        self.sent = 0
        self.failures: list[str] = []

    def _url(self, method: str) -> str:
        return f"{API}/bot{self._cfg.bot_token}/{method}"

    def healthy(self) -> bool:
        return self.consecutive_failures < self._max_fail

    def send(self, severity: str, message: str) -> None:
        now = self._clock()
        prefix = _SEVERITY_PREFIX.get(severity.upper(), f"[{severity}]")
        text = f"{prefix} {self._label} {now:%H:%M:%S} IST\n{message}"
        if len(text) > MAX_TEXT:
            text = text[: MAX_TEXT - 3] + "..."
        key = f"{severity}|{message}"
        last = self._last_sent.get(key)
        if last is not None and (now - last).total_seconds() < self._dedupe:
            return
        try:
            status, body = self._p.post(
                self._url("sendMessage"),
                {"chat_id": self._cfg.chat_id, "text": text, "disable_web_page_preview": True},
                self._timeout,
            )
        except NotifyError as e:
            self._fail(str(e))
            return
        if status != 200 or body.get("ok") is not True:
            retry = (body.get("parameters") or {}).get("retry_after") if isinstance(body, dict) else None
            self._fail(f"HTTP {status} {body.get('description', '')}" + (f" retry_after={retry}" if retry else ""))
            return
        self._last_sent[key] = now
        self.consecutive_failures = 0
        self.sent += 1

    def _fail(self, why: str) -> None:
        self.consecutive_failures += 1
        self.failures.append(why)

    def poll_commands(self) -> list[OwnerCommand]:
        """Fetch new messages and return the risk-reducing commands sent from the configured chat only."""
        try:
            status, body = self._p.post(
                self._url("getUpdates"),
                {"offset": self._offset, "timeout": 0, "allowed_updates": ["message"]},
                self._timeout,
            )
        except NotifyError as e:
            self._fail(str(e))
            return []
        if status != 200 or body.get("ok") is not True or not isinstance(body.get("result"), list):
            self._fail(f"getUpdates HTTP {status}")
            return []
        out: list[OwnerCommand] = []
        for u in body["result"]:
            if not isinstance(u, dict) or not isinstance(u.get("update_id"), int):
                continue
            self._offset = max(self._offset, u["update_id"] + 1)
            msg = u.get("message") or {}
            chat = (msg.get("chat") or {}).get("id") if isinstance(msg, dict) else None
            if str(chat) != self._cfg.chat_id:
                continue  # anyone else talking to the bot is ignored
            parts = str(msg.get("text") or "").strip().split()
            if not parts:
                continue
            word = parts[0].split("@")[0].lower()
            for c in Command:
                if word == c.value:
                    out.append(OwnerCommand(c, parts[1] if len(parts) > 1 else "", u["update_id"]))
        return out
