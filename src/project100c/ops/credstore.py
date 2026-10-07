"""The encrypted credential store (K-S1, docs/engineering/security.md): secrets come from the environment or from one
encrypted file.

* **File:** a Fernet token (AES-128-CBC with an HMAC-SHA256 tag, from the ``cryptography`` package) over a small
  JSON object of named values. A wrong key or any change to the file is refused (fail closed). The file lives
  outside the repository, for example ``/etc/project100c/credentials.enc``, mode 0600, owned by the service user
  (``check_private_file``).
* **Key:** from ``P100C_CREDSTORE_KEY``, or from the file named by ``P100C_CREDSTORE_KEY_FILE`` (a Docker or
  systemd secret, also 0600). The key is never stored next to the file in the image or the repository.
* **Precedence:** a variable set in the environment wins over the file, so an operator can override one value
  without re-encrypting. Only the names in ``ALLOWED_NAMES`` are ever returned.
* Every value returned is meant to be registered with the ``Redactor`` (``register_all``), so it cannot reach a log
  line or an alert.

The daily broker access token is **not** stored here: it arrives on the webhook and stays in memory
(docs/engineering/alerts-and-daily-token.md).

CLI (values are read from the terminal without echo, never from the command line)::

    python -m project100c.ops.credstore new-key --out /etc/project100c/credstore.k   # writes the key, mode 0600
    python -m project100c.ops.credstore set UPSTOX_API_SECRET --store /etc/project100c/credentials.enc
    python -m project100c.ops.credstore list --store /etc/project100c/credentials.enc   # names only
    python -m project100c.ops.credstore rotate --store ... --new-key-file ...
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from project100c.ops.redact import CREDENTIAL_ENV, Redactor, check_private_file

ENV_STORE = "P100C_CREDSTORE"
ENV_STORE_KEY = "P100C_CREDSTORE_KEY"
ENV_STORE_KEY_FILE = "P100C_CREDSTORE_KEY_FILE"
ALLOWED_NAMES = frozenset(
    (
        *CREDENTIAL_ENV,
        "UPSTOX_REDIRECT_URI",
        "TELEGRAM_CHAT_ID",
        "P100C_PROXY_SHARED",  # the reverse proxy's shared value for the private status routes
        "P100C_WEBHOOK_PATH",  # the unguessable notifier-webhook path
    )
)
FORMAT_VERSION = 1


class CredentialStoreError(Exception):
    """The store cannot be read or written: wrong key, tampered file, bad permissions, unknown name."""


def _fernet(key: str | bytes) -> Any:
    try:
        from cryptography.fernet import Fernet
    except ImportError as e:  # pragma: no cover - the host image installs it
        raise CredentialStoreError("the encrypted store needs the 'cryptography' package (pip install .[host])") from e
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as e:
        raise CredentialStoreError("the store key is not a valid Fernet key (44 url-safe base64 characters)") from e


def generate_store_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


class CredentialStore:
    def __init__(self, path: Path, key: str | bytes) -> None:
        self.path = path
        self._f = _fernet(key)

    def __repr__(self) -> str:
        return f"CredentialStore({self.path}, <key hidden>)"

    def read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            check_private_file(self.path)
        except PermissionError as e:
            raise CredentialStoreError(str(e)) from e
        from cryptography.fernet import InvalidToken

        try:
            raw = self._f.decrypt(self.path.read_bytes())
        except InvalidToken as e:
            raise CredentialStoreError(f"{self.path}: wrong key or the file was changed (refused)") from e
        obj = json.loads(raw)
        if not isinstance(obj, dict) or obj.get("v") != FORMAT_VERSION or not isinstance(obj.get("values"), dict):
            raise CredentialStoreError(f"{self.path}: not a credential store of version {FORMAT_VERSION}")
        values = obj["values"]
        bad = sorted(k for k in values if k not in ALLOWED_NAMES)
        if bad:
            raise CredentialStoreError(f"{self.path}: unknown names {bad}")
        return {str(k): str(v) for k, v in values.items()}

    def write(self, values: Mapping[str, str]) -> None:
        bad = sorted(k for k in values if k not in ALLOWED_NAMES)
        if bad:
            raise CredentialStoreError(f"unknown names {bad}; allowed: {sorted(ALLOWED_NAMES)}")
        blob = json.dumps({"v": FORMAT_VERSION, "values": dict(sorted(values.items()))}).encode()
        _write_private(self.path, self._f.encrypt(blob))

    def set(self, name: str, value: str) -> None:
        if not value or any(c in value for c in "\r\n"):
            raise CredentialStoreError(f"{name}: empty or multi-line value refused")
        v = self.read()
        v[name] = value
        self.write(v)

    def unset(self, name: str) -> bool:
        v = self.read()
        if v.pop(name, None) is None:
            return False
        self.write(v)
        return True

    def names(self) -> list[str]:
        return sorted(self.read())

    def rotate(self, new_key: str | bytes) -> CredentialStore:
        values = self.read()
        new = CredentialStore(self.path, new_key)
        new.write(values)
        return new


def store_key_from_env(env: Mapping[str, str]) -> str | None:
    if env.get(ENV_STORE_KEY):
        return env[ENV_STORE_KEY].strip()
    kf = env.get(ENV_STORE_KEY_FILE)
    if kf:
        p = Path(kf)
        try:
            check_private_file(p)
        except (PermissionError, FileNotFoundError) as e:
            raise CredentialStoreError(f"store key file: {e}") from e
        return p.read_text().strip()
    return None


def resolve_credentials(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The credentials for this process: the encrypted file (if ``P100C_CREDSTORE`` names one), overridden by any
    allowed name set in the environment. Only ``ALLOWED_NAMES`` are returned."""
    e = os.environ if env is None else env
    out: dict[str, str] = {}
    store = e.get(ENV_STORE)
    if store:
        key = store_key_from_env(e)
        if key is None:
            raise CredentialStoreError(f"{ENV_STORE} is set but neither {ENV_STORE_KEY} nor {ENV_STORE_KEY_FILE} is")
        out.update(CredentialStore(Path(store), key).read())
    for name in ALLOWED_NAMES:
        if e.get(name):
            out[name] = e[name]
    return out


def register_all(redactor: Redactor, creds: Mapping[str, str], env: Mapping[str, str] | None = None) -> int:
    """Mask every credential value, and the store key itself."""
    e = os.environ if env is None else env
    values: Iterable[str | None] = (*creds.values(), e.get(ENV_STORE_KEY))
    n = 0
    for v in values:
        if v:
            redactor.register(v)
            n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m project100c.ops.credstore", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    nk = sub.add_parser("new-key", help="write a new store key to a 0600 file (never printed)")
    nk.add_argument("--out", type=Path, required=True)
    for name in ("set", "unset", "list", "rotate"):
        p = sub.add_parser(name)
        p.add_argument("--store", type=Path, required=True)
        if name in ("set", "unset"):
            p.add_argument("name", choices=sorted(ALLOWED_NAMES))
        if name == "rotate":
            p.add_argument("--new-key-file", type=Path, required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "new-key":
            if a.out.exists():
                raise CredentialStoreError(f"{a.out} exists: refusing to overwrite a key")
            _write_private(a.out, (generate_store_key() + "\n").encode())
            print(f"wrote a new store key to {a.out} (mode 0600)")
            return 0
        key = store_key_from_env(os.environ)
        if key is None:
            raise CredentialStoreError(f"set {ENV_STORE_KEY_FILE} (or {ENV_STORE_KEY}) first")
        cs = CredentialStore(a.store, key)
        if a.cmd == "set":
            value = getpass.getpass(f"{a.name} (not echoed): ") if sys.stdin.isatty() else sys.stdin.readline()
            cs.set(a.name, value.strip())
            print(f"stored {a.name} in {a.store}")
        elif a.cmd == "unset":
            print(f"removed {a.name}" if cs.unset(a.name) else f"{a.name} was not stored")
        elif a.cmd == "list":
            for n in cs.names():
                print(n)
        else:
            check_private_file(a.new_key_file)
            cs.rotate(a.new_key_file.read_text().strip())
            print(f"re-encrypted {a.store} with the new key; retire the old key now")
    except (CredentialStoreError, PermissionError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
