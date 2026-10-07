"""Dhan credentials from the environment only (OD-011). Nothing here writes, logs or prints the token.

The token is created by the owner on web.dhan.co (valid 24 hours, S60) and exported as ``DHAN_ACCESS_TOKEN``.
If it is absent, ``DhanCredentials.from_env`` raises ``MissingCredentialError`` and nothing is sent anywhere.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from project100c.errors import MissingCredentialError

ENV_ACCESS_TOKEN = "DHAN_ACCESS_TOKEN"
ENV_CLIENT_ID = "DHAN_CLIENT_ID"
_HEADER_SAFE = re.compile(r"^[\x21-\x7e]+$")  # visible ASCII, no whitespace/control chars (header injection)

LOCAL_ENV_FILE = Path("configs/local/dhan.env")  # gitignored; holds DHAN_CLIENT_ID only (never the token)
_ENV_FILE_KEYS = frozenset({ENV_CLIENT_ID})

NO_TOKEN_HELP = (
    f"{ENV_ACCESS_TOKEN} is not set, so no Dhan request was sent and nothing was downloaded. "
    "To run a real download: (1) subscribe to the Dhan Data API, (2) generate an access token on web.dhan.co "
    "(My Profile -> Access DhanHQ APIs; it is valid for 24 hours), (3) export it as "
    f"{ENV_ACCESS_TOKEN} (and {ENV_CLIENT_ID} if needed) in the shell that runs the job. "
    "Planning ('plan') works without a token."
)


@dataclass(frozen=True, slots=True)
class DhanCredentials:
    _token: str = field(repr=False)
    client_id: str | None = None

    def __repr__(self) -> str:
        return (
            f"DhanCredentials(token=<redacted {len(self._token)} chars>, client_id={'set' if self.client_id else None})"
        )

    __str__ = __repr__

    def auth_headers(self) -> dict[str, str]:
        h = {"access-token": self._token}
        if self.client_id:
            h["client-id"] = self.client_id
        return h

    def redact(self, text: str) -> str:
        """Remove the token from any text before it is stored or raised."""
        return text.replace(self._token, "<redacted>") if self._token else text

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> DhanCredentials:
        token = env.get(ENV_ACCESS_TOKEN)
        if token is None or token.strip() == "":
            raise MissingCredentialError(NO_TOKEN_HELP)
        if not _HEADER_SAFE.match(token):
            raise MissingCredentialError(
                f"{ENV_ACCESS_TOKEN} contains whitespace or control characters; re-export it without quotes/newlines"
            )
        cid = env.get(ENV_CLIENT_ID)
        if cid is not None:
            cid = cid.strip() or None
            if cid is not None and not cid.isdigit():
                raise MissingCredentialError(f"{ENV_CLIENT_ID} must be the numeric Dhan client id")
        return cls(token, cid)


@dataclass(frozen=True, slots=True)
class EnvFileResult:
    values: dict[str, str] = field(repr=False)
    ignored_keys: tuple[str, ...] = ()  # names only, never values

    def __repr__(self) -> str:
        return f"EnvFileResult(keys={sorted(self.values)}, ignored_keys={list(self.ignored_keys)})"


def read_local_env_file(path: Path) -> EnvFileResult:
    """Read ``KEY=VALUE`` lines from the gitignored local file. Only DHAN_CLIENT_ID is taken from it.

    Any other key (including DHAN_ACCESS_TOKEN, which must arrive as a real environment variable, OD-011) is
    ignored and reported by NAME in ``ignored_keys``. A missing file is not an error (empty result).
    Values are never logged or included in exception messages.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return EnvFileResult({})
    except (OSError, UnicodeDecodeError) as e:
        raise MissingCredentialError(f"cannot read {path}: {type(e).__name__}") from None
    values: dict[str, str] = {}
    ignored: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise MissingCredentialError(f"{path}:{lineno}: expected KEY=VALUE")
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'\"")
        if key in _ENV_FILE_KEYS:
            values[key] = val
        else:
            ignored.append(key)
    return EnvFileResult(values, tuple(ignored))


def merged_env(env: Mapping[str, str], local_file: Path | None) -> tuple[dict[str, str], EnvFileResult]:
    """Process environment wins; the local file only fills DHAN_CLIENT_ID when the variable is unset."""
    res = read_local_env_file(local_file) if local_file is not None else EnvFileResult({})
    out = dict(env)
    for k, v in res.values.items():
        if not out.get(k):
            out[k] = v
    return out, res
