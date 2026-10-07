"""The status page: public view by allow-list (no money), private view only through the proxy, health, GET only."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from project100c.ops.status_server import PROXY_HEADER, PUBLIC_KEYS, StatusBoard, handle, serve_status

SECRET = "s3cr3t-shared-with-the-proxy-0123456789"


def board() -> StatusBoard:
    b = StatusBoard(max_step_age=30)
    b.publish(
        {"mode": "PAPER", "real_money": "OFF", "gate": "ACTIVE", "nav": "10000", "pnl": "-12.5", "positions": 2},
        {"positions": {"K": "75"}, "paper_realised_today": "-12.5", "kills": ["X: why"]},
    )
    return b


def test_public_view_drops_everything_not_allow_listed() -> None:
    b = board()
    pub = b.public()
    assert pub == {"mode": "PAPER", "real_money": "OFF", "gate": "ACTIVE"}
    assert not {"nav", "pnl", "positions", "kills"} & PUBLIC_KEYS
    code, ctype, body = handle(b, "GET", "/status.json", {}, SECRET)
    assert code == 200 and ctype == "application/json" and json.loads(body) == pub
    code, ctype, body = handle(b, "GET", "/", {}, SECRET)
    text = body.decode()
    assert code == 200 and "Real money is OFF" in text and "-12.5" not in text and "10000" not in text


def test_private_view_needs_the_proxy_secret() -> None:
    b = board()
    assert handle(b, "GET", "/private/status", {}, SECRET)[0] == 404
    assert handle(b, "GET", "/private/status", {PROXY_HEADER: "wrong"}, SECRET)[0] == 404
    assert handle(b, "GET", "/private/status", {PROXY_HEADER: SECRET}, None)[0] == 404  # no secret configured
    assert handle(b, "GET", "/private/status", {PROXY_HEADER: "short"}, "short")[0] == 404  # too short to trust
    code, _, body = handle(b, "GET", "/private/status", {PROXY_HEADER.lower(): SECRET}, SECRET)
    assert code == 200 and json.loads(body)["paper_realised_today"] == "-12.5"


def test_health_and_methods() -> None:
    b = board()
    assert handle(b, "GET", "/healthz", {}, None)[0] == 503  # never stepped
    b.heartbeat()
    assert handle(b, "GET", "/healthz", {}, None)[0] == 200
    assert handle(b, "POST", "/status.json", {}, None)[0] == 405
    assert handle(b, "DELETE", "/private/status", {PROXY_HEADER: SECRET}, SECRET)[0] == 405
    assert handle(b, "GET", "/etc/passwd", {}, None)[0] == 404


def test_the_server_is_loopback_only_and_sends_safe_headers() -> None:
    with pytest.raises(ValueError, match="loopback"):
        serve_status(board(), 0, proxy_secret=None, host="0.0.0.0")
    with pytest.raises(ValueError, match="at least"):
        serve_status(board(), 0, proxy_secret="short")
    b = board()
    srv = serve_status(b, 0, proxy_secret=SECRET)
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        with urllib.request.urlopen(base + "/status.json", timeout=5) as r:
            assert (
                r.headers["Cache-Control"] == "no-store"
                and "default-src 'none'" in r.headers["Content-Security-Policy"]
            )
            assert json.loads(r.read())["real_money"] == "OFF"
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/private/status", timeout=5)
        assert e.value.code == 404
        req = urllib.request.Request(base + "/private/status", headers={PROXY_HEADER: SECRET})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert json.loads(r.read())["positions"] == {"K": "75"}
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base + "/", data=b"x", method="POST"), timeout=5)
        assert e.value.code == 405
    finally:
        srv.shutdown()
        srv.server_close()
