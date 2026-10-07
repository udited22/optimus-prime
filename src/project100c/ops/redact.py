"""Secrets handling on the host and redaction of logs and alerts (K-S1).

Rules (docs/engineering/security.md, OD-004):

* Credentials reach the process only through the environment, set by the OS supervisor from a file outside the
  repository that only the service user can read (``check_private_file`` refuses anything wider than 0600 or a
  symlink). The daily Upstox access token is never in a file at all: it arrives on the notifier webhook and lives
  in memory.
* Nothing the process writes (log lines, exception text, alerts, the journal) may contain a credential.
  ``Redactor`` masks (1) the exact values of every known credential variable present in the environment and any
  value registered at run time (the daily token, when the webhook delivers it), and (2) credential-shaped text:
  ``Authorization: Bearer ...``, ``bot<id>:<secret>`` in a Telegram URL, JWTs, and ``code=``, ``access_token=``,
  ``client_secret=`` or ``api_key=`` query or form fields.
* ``install_log_redaction()`` puts a filter on the root logger's handlers that rewrites each record's message,
  arguments and exception text before any handler formats it.

The redactor never stores the values in a printable form: ``repr`` hides them, and only values of at least 8
characters are registered (shorter strings would mask ordinary text).
"""

from __future__ import annotations

import logging
import os
import re
import stat
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Protocol

from project100c.sessions import IST as _IST

MASK = "[REDACTED]"
CREDENTIAL_ENV = (
    "UPSTOX_API_KEY",
    "UPSTOX_API_SECRET",
    "UPSTOX_ACCESS_TOKEN",
    "UPSTOX_SANDBOX_ACCESS_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "DHAN_ACCESS_TOKEN",
    "P100C_CREDSTORE_KEY",  # the encrypted store's key (ops/credstore.py)
    "P100C_PROXY_SHARED",  # the reverse proxy's shared value for the private status routes
)
MIN_LEN = 8
_PATTERNS = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + MASK),
    (re.compile(r"/bot\d{5,}:[A-Za-z0-9_-]{20,}"), "/bot" + MASK),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), MASK),
    (re.compile(r"\bgAAAAA[A-Za-z0-9_=-]{40,}"), MASK),  # a Fernet token (an encrypted store blob)
    (re.compile(r"(?i)\b(authorization|x-p100c-proxy-auth)\s*[:=]\s*\S{8,}"), r"\1: " + MASK),
    (
        re.compile(r"(?i)\b(code|access_token|client_secret|api_secret|api_key|token)=[^&\s\"']{4,}"),
        r"\1=" + MASK,
    ),
)


class Redactor:
    def __init__(self, values: Iterable[str] = ()) -> None:
        self._values: set[str] = set()
        self._lock = threading.Lock()
        for v in values:
            self.register(v)

    def __repr__(self) -> str:
        return f"Redactor(<{len(self._values)} values hidden>)"

    def register(self, value: str | None) -> None:
        """Mask this exact value from now on (e.g. the daily token when it arrives)."""
        if value and len(value.strip()) >= MIN_LEN:
            with self._lock:
                self._values.add(value.strip())

    def register_env(self, env: Mapping[str, str] | None = None, names: Iterable[str] = CREDENTIAL_ENV) -> int:
        """Register the values of the known credential variables that are set; returns how many."""
        src = os.environ if env is None else env
        n = 0
        for name in names:
            v = src.get(name)
            if v and len(v.strip()) >= MIN_LEN:
                self.register(v)
                n += 1
        return n

    def __call__(self, text: str) -> str:
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for v in values:
            if v in text:
                text = text.replace(v, MASK)
        for rx, sub in _PATTERNS:
            text = rx.sub(sub, text)
        return text


class RedactingFilter(logging.Filter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._r = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = f"{record.msg!r} (unformattable arguments)"
        record.msg, record.args = self._r(msg), None
        if record.exc_info:
            text = logging.Formatter().formatException(record.exc_info)
            record.exc_info, record.exc_text = None, self._r(text)
        elif record.exc_text:
            record.exc_text = self._r(record.exc_text)
        if record.stack_info:
            record.stack_info = self._r(record.stack_info)
        return True


def install_log_redaction(redactor: Redactor, logger: logging.Logger | None = None) -> RedactingFilter:
    """Add the filter to the logger and to each of its handlers (filters on a logger do not see records that
    propagate from child loggers; handler filters do). Call again after adding handlers."""
    lg = logger or logging.getLogger()
    f = next((x for x in lg.filters if isinstance(x, RedactingFilter)), None) or RedactingFilter(redactor)
    if f not in lg.filters:
        lg.addFilter(f)
    for h in lg.handlers:
        if not any(isinstance(x, RedactingFilter) for x in h.filters):
            h.addFilter(f)
    return f


class _Sink(Protocol):
    def send(self, severity: str, message: str) -> None: ...


class RedactingAlerts:
    """Wraps any alert sink so no alert text carries a credential."""

    def __init__(self, inner: _Sink, redactor: Redactor) -> None:
        self._inner, self._r = inner, redactor

    def send(self, severity: str, message: str) -> None:
        self._inner.send(severity, self._r(message))


def check_private_file(path: Path) -> None:
    """Refuse a credential file that is a symlink, not a regular file, or readable by group or others."""
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise PermissionError(f"{path}: credential file must be a regular file, not a link")
    if st.st_mode & 0o077:
        raise PermissionError(f"{path}: credential file mode {oct(st.st_mode & 0o777)} is wider than 0600")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise PermissionError(f"{path}: credential file is not owned by the service user")


class JsonFormatter(logging.Formatter):
    """One JSON object per line (structured logs for the container runtime). Every text field goes through the
    redactor, so this formatter is safe even on a handler that has no ``RedactingFilter``."""

    def __init__(self, redactor: Redactor, *, service: str = "project100c") -> None:
        super().__init__()
        self._r = redactor
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        import json
        from datetime import UTC, datetime

        try:
            msg = record.getMessage()
        except Exception:
            msg = f"{record.msg!r} (unformattable arguments)"
        out: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).astimezone(_IST).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "service": self._service,
            "msg": self._r(msg),
        }
        for k in ("event", "mode", "phase"):
            v = record.__dict__.get(k)
            if isinstance(v, str | int | float | bool):
                out[k] = self._r(v) if isinstance(v, str) else v
        if record.exc_info:
            out["exc"] = self._r(self.formatException(record.exc_info))
        elif record.exc_text:
            out["exc"] = self._r(record.exc_text)
        return json.dumps(out, ensure_ascii=False)
