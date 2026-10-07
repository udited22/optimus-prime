"""K-S1: credential values and credential-shaped text never reach logs or alerts; credential files must be
private. All values here are fake and built at run time."""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path

import pytest

from project100c.kernel.runtime import MemoryAlerts
from project100c.ops.redact import (
    MASK,
    RedactingAlerts,
    Redactor,
    check_private_file,
    install_log_redaction,
)

FAKE = "fake" + "Access" + "Value" + "9f8e7d6c5b4a"  # stands in for a real credential
FAKE_BOT = "/bot" + "1234567890" + ":" + "AAH" + "x" * 32
FAKE_JWT = "eyJ" + "hbGciOiJIUzI1" + "." + "eyJzdWIiOiIxMjM0NTY3ODkw" + "." + "SflKxwRJSMeKKF2QT4fwpM"


def test_known_values_and_shapes_are_masked() -> None:
    r = Redactor()
    assert r.register_env({"UPSTOX_ACCESS_TOKEN": FAKE, "TELEGRAM_CHAT_ID": "12345678", "UPSTOX_API_KEY": "short"}) == 1
    out = r(f"call failed with {FAKE} in it")
    assert FAKE not in out and MASK in out
    cases = [
        ("Authorization: Bearer abcdefgh12345678", "abcdefgh12345678"),
        (f"POST https://api.telegram.org{FAKE_BOT}/sendMessage", FAKE_BOT[5:]),
        (f"jwt {FAKE_JWT} end", FAKE_JWT),
        ("GET /login?code=Xy12Zt99&state=1", "Xy12Zt99"),
        ("body access_token=abc123def456&x=1", "abc123def456"),
        ("client_secret=s3cr3tvalue", "s3cr3tvalue"),
    ]
    for text, leaked in cases:
        assert leaked not in r(text), text
    assert r("ordinary order I1.CE.1 placed at 3.00") == "ordinary order I1.CE.1 placed at 3.00"
    assert FAKE not in repr(r) and "hidden" in repr(r)


def test_run_time_registration_masks_the_daily_value() -> None:
    r = Redactor()
    assert r(f"x {FAKE}") == f"x {FAKE}"
    r.register(FAKE)
    r.register("abc")  # too short to mask safely: ignored
    assert r(f"x {FAKE}") == f"x {MASK}" and r("abc") == "abc"


def test_log_records_messages_args_and_tracebacks_are_redacted() -> None:
    r = Redactor([FAKE])
    lg = logging.getLogger("p100c.test.redact")
    lg.propagate = False
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    lg.addHandler(h)
    try:
        install_log_redaction(r, lg)
        install_log_redaction(r, lg)  # idempotent
        assert len(lg.filters) == 1 and len(h.filters) == 1
        lg.warning("value %s", FAKE)
        lg.warning("bad format %d", FAKE)
        try:
            raise RuntimeError(f"broker said Bearer {FAKE}")
        except RuntimeError:
            lg.exception("failed")
        child = logging.getLogger("p100c.test.redact.child")
        child.error("child %s", FAKE)
    finally:
        lg.removeHandler(h)
    text = buf.getvalue()
    assert FAKE not in text and text.count(MASK) >= 3 and "RuntimeError" in text and "child" in text


def test_alerts_are_redacted() -> None:
    inner = MemoryAlerts()
    a = RedactingAlerts(inner, Redactor([FAKE]))
    a.send("URGENT", f"401 with {FAKE}")
    assert inner.sent == [("URGENT", f"401 with {MASK}")]


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX permissions")
def test_credential_files_must_be_private(tmp_path: Path) -> None:
    f = tmp_path / "upstox.env"
    f.write_text("X=1\n")
    f.chmod(0o600)
    check_private_file(f)
    f.chmod(0o640)
    with pytest.raises(PermissionError, match="wider than 0600"):
        check_private_file(f)
    link = tmp_path / "link.env"
    link.symlink_to(f)
    with pytest.raises(PermissionError, match="link"):
        check_private_file(link)
