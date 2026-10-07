"""K-S1: the encrypted credential store and structured, redacted logs. Every value here is fake and built at
run time; nothing touches a real secret."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from project100c.ops.credstore import (
    ENV_STORE,
    ENV_STORE_KEY,
    ENV_STORE_KEY_FILE,
    CredentialStore,
    CredentialStoreError,
    generate_store_key,
    main,
    register_all,
    resolve_credentials,
)
from project100c.ops.redact import MASK, JsonFormatter, Redactor

FAKE = "fake" + "Secret" + "Value" + "0a1b2c3d4e5f"


def test_round_trip_names_only_and_tamper_refused(tmp_path: Path) -> None:
    key = generate_store_key()
    p = tmp_path / "creds.enc"
    cs = CredentialStore(p, key)
    assert cs.read() == {}
    cs.set("UPSTOX_API_SECRET", FAKE)
    cs.set("TELEGRAM_BOT_TOKEN", FAKE + "2")
    assert (p.stat().st_mode & 0o777) == 0o600
    assert FAKE.encode() not in p.read_bytes() and "key hidden" in repr(cs)
    assert cs.names() == ["TELEGRAM_BOT_TOKEN", "UPSTOX_API_SECRET"]
    assert CredentialStore(p, key).read()["UPSTOX_API_SECRET"] == FAKE
    with pytest.raises(CredentialStoreError, match="wrong key"):
        CredentialStore(p, generate_store_key()).read()
    raw = bytearray(p.read_bytes())
    raw[20] ^= 1
    p.write_bytes(bytes(raw))
    with pytest.raises(CredentialStoreError, match="wrong key or the file was changed"):
        cs.read()


def test_unknown_names_bad_values_and_bad_keys_are_refused(tmp_path: Path) -> None:
    cs = CredentialStore(tmp_path / "c.enc", generate_store_key())
    with pytest.raises(CredentialStoreError, match="unknown names"):
        cs.write({"AWS_SECRET_ACCESS_KEY": FAKE})
    with pytest.raises(CredentialStoreError, match="multi-line"):
        cs.set("UPSTOX_API_SECRET", "a\nb")
    with pytest.raises(CredentialStoreError, match="Fernet key"):
        CredentialStore(tmp_path / "c.enc", "not-a-key")
    cs.set("UPSTOX_API_KEY", FAKE)
    (tmp_path / "c.enc").chmod(0o644)
    with pytest.raises(CredentialStoreError, match="wider than 0600"):
        cs.read()


def test_resolve_env_wins_over_the_file_and_rotation(tmp_path: Path) -> None:
    key = generate_store_key()
    kf = tmp_path / "store.k"
    kf.write_text(key + "\n")
    kf.chmod(0o600)
    p = tmp_path / "c.enc"
    CredentialStore(p, key).write({"UPSTOX_API_KEY": FAKE, "UPSTOX_API_SECRET": FAKE + "s"})
    env = {ENV_STORE: str(p), ENV_STORE_KEY_FILE: str(kf), "UPSTOX_API_KEY": "override-value-123", "PATH": "/bin"}
    got = resolve_credentials(env)
    assert got == {"UPSTOX_API_KEY": "override-value-123", "UPSTOX_API_SECRET": FAKE + "s"}
    assert "PATH" not in got
    assert resolve_credentials({"UPSTOX_API_KEY": "env-only-value"}) == {"UPSTOX_API_KEY": "env-only-value"}
    with pytest.raises(CredentialStoreError, match="neither"):
        resolve_credentials({ENV_STORE: str(p)})
    kf.chmod(0o644)
    with pytest.raises(CredentialStoreError, match="key file"):
        resolve_credentials(env)
    new = generate_store_key()
    CredentialStore(p, key).rotate(new)
    assert CredentialStore(p, new).read()["UPSTOX_API_KEY"] == FAKE
    with pytest.raises(CredentialStoreError):
        CredentialStore(p, key).read()
    r = Redactor()
    assert register_all(r, got, {ENV_STORE_KEY: key}) == 3
    assert FAKE not in r(f"x {FAKE}s y") and key not in r(key)


def test_cli_never_prints_a_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    kf = tmp_path / "store.k"
    assert main(["new-key", "--out", str(kf)]) == 0
    assert (kf.stat().st_mode & 0o777) == 0o600
    assert main(["new-key", "--out", str(kf)]) == 2  # never overwrites a key
    monkeypatch.setenv(ENV_STORE_KEY_FILE, str(kf))
    monkeypatch.delenv(ENV_STORE_KEY, raising=False)
    store = tmp_path / "c.enc"
    monkeypatch.setattr("sys.stdin", io.StringIO(FAKE + "\n"))
    assert main(["set", "TELEGRAM_BOT_TOKEN", "--store", str(store)]) == 0
    assert main(["list", "--store", str(store)]) == 0
    assert main(["unset", "TELEGRAM_BOT_TOKEN", "--store", str(store)]) == 0
    out = capsys.readouterr()
    assert FAKE not in out.out + out.err and "TELEGRAM_BOT_TOKEN" in out.out
    assert kf.read_text().strip() not in out.out + out.err


def test_json_logs_are_structured_and_redacted() -> None:
    r = Redactor([FAKE])
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(JsonFormatter(r, service="host"))
    lg = logging.getLogger("p100c.test.jsonlog")
    lg.propagate = False
    lg.addHandler(h)
    try:
        lg.warning("value %s", FAKE, extra={"event": "feed_gap", "mode": "replay"})
        blob = "gAAAAA" + "B" * 60
        lg.error("store blob %s and Authorization: Bearer abcdefgh12345678", blob)
        try:
            raise ValueError(FAKE)
        except ValueError:
            lg.exception("boom")
    finally:
        lg.removeHandler(h)
    lines = [json.loads(x) for x in buf.getvalue().splitlines()]
    assert [x["level"] for x in lines] == ["WARNING", "ERROR", "ERROR"]
    assert lines[0]["event"] == "feed_gap" and lines[0]["mode"] == "replay" and lines[0]["service"] == "host"
    assert FAKE not in buf.getvalue() and "B" * 60 not in buf.getvalue() and "abcdefgh12345678" not in buf.getvalue()
    assert MASK in lines[0]["msg"] and "ValueError" in lines[2]["exc"]
