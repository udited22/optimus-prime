"""Dhan client: no-token path, redaction, error classification, retries/backoff, response parsing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from project100c.core_types import OptionRight
from project100c.data.dhan import DataEndpoint, DhanCredentials
from project100c.data.dhan.client import loads_decimal
from project100c.data.dhan.credentials import (
    ENV_ACCESS_TOKEN,
    ENV_CLIENT_ID,
    NO_TOKEN_HELP,
    merged_env,
    read_local_env_file,
)
from project100c.data.dhan.parse import ROLLING_FIELDS, parse_candles, parse_rolling_option
from project100c.data.http import HttpResponse, ScriptedTransport
from project100c.data.ratelimit import SimClock
from project100c.errors import (
    EndpointNotAllowedError,
    MissingCredentialError,
    TransportError,
    VendorAuthError,
    VendorRateLimitError,
    VendorRequestError,
    VendorResponseError,
    VendorServerError,
    VendorSubscriptionError,
)
from tests.data.dhan_fakes import FAKE_TOKEN, FakeDhan, make_rig

PAYLOAD = {"exchangeSegment": "NSE_FNO", "strike": "ATM", "drvOptionType": "CALL"}


def _err(status: int, code: str, msg: str = "m") -> HttpResponse:
    return HttpResponse(status, json.dumps({"errorType": "x", "errorCode": code, "errorMessage": msg}).encode())


# ------------------------------------------------------------------ credentials
@pytest.mark.parametrize("env", [{}, {ENV_ACCESS_TOKEN: ""}, {ENV_ACCESS_TOKEN: "   "}])
def test_no_token_raises_clear_typed_error(env: dict[str, str]) -> None:
    with pytest.raises(MissingCredentialError) as ei:
        DhanCredentials.from_env(env)
    msg = str(ei.value)
    assert ENV_ACCESS_TOKEN in msg and "nothing was downloaded" in msg and msg == NO_TOKEN_HELP


def test_no_token_means_no_request_at_all(tmp_path: Path) -> None:
    with pytest.raises(MissingCredentialError):
        make_rig(tmp_path, env={})
    # construction failed before any transport existed: nothing can have been sent


@pytest.mark.parametrize("bad", ["abc def", "abc\ndef", "tok\r\nX-Evil: 1"])
def test_token_with_whitespace_rejected(bad: str) -> None:
    with pytest.raises(MissingCredentialError, match="whitespace"):
        DhanCredentials.from_env({ENV_ACCESS_TOKEN: bad})


def test_client_id_optional_and_numeric() -> None:
    c = DhanCredentials.from_env({ENV_ACCESS_TOKEN: FAKE_TOKEN})
    assert "client-id" not in c.auth_headers()
    c2 = DhanCredentials.from_env({ENV_ACCESS_TOKEN: FAKE_TOKEN, ENV_CLIENT_ID: "1000000001"})
    assert c2.auth_headers()["client-id"] == "1000000001"
    with pytest.raises(MissingCredentialError):
        DhanCredentials.from_env({ENV_ACCESS_TOKEN: FAKE_TOKEN, ENV_CLIENT_ID: "abc"})


def test_credentials_repr_never_shows_token() -> None:
    c = DhanCredentials.from_env({ENV_ACCESS_TOKEN: FAKE_TOKEN, ENV_CLIENT_ID: "1000000001"})
    for text in (repr(c), str(c), f"{c}"):
        assert FAKE_TOKEN not in text and "1000000001" not in text
    assert c.redact(f"x {FAKE_TOKEN} y") == "x <redacted> y"


def test_local_env_file_only_supplies_client_id(tmp_path: Path) -> None:
    f = tmp_path / "dhan.env"
    f.write_text("# local\nexport DHAN_CLIENT_ID='1000000009'\nDHAN_ACCESS_TOKEN=should-be-ignored\nOTHER=1\n")
    res = read_local_env_file(f)
    assert res.values == {ENV_CLIENT_ID: "1000000009"}
    assert set(res.ignored_keys) == {ENV_ACCESS_TOKEN, "OTHER"}
    assert "1000000009" not in repr(res) and "should-be-ignored" not in repr(res)
    env, _ = merged_env({}, f)
    assert env == {ENV_CLIENT_ID: "1000000009"}  # the token is NOT taken from the file (OD-011)
    env2, _ = merged_env({ENV_CLIENT_ID: "1000000001"}, f)
    assert env2[ENV_CLIENT_ID] == "1000000001"  # process env wins
    assert read_local_env_file(tmp_path / "missing.env").values == {}
    (tmp_path / "bad.env").write_text("NOEQUALS\n")
    with pytest.raises(MissingCredentialError):
        read_local_env_file(tmp_path / "bad.env")


# ------------------------------------------------------------------ allowlist
def test_non_allowlisted_endpoint_refused(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    with pytest.raises(EndpointNotAllowedError):
        rig.client.call("orders", {})  # type: ignore[arg-type]
    with pytest.raises(EndpointNotAllowedError):
        rig.client.call(DataEndpoint.PROFILE, {"x": 1})
    assert rig.transport.requests == []


def test_profile_preflight(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    p = rig.client.profile()
    assert p["dataPlan"] == "Active" and "dhanClientId" not in p
    assert rig.transport.requests[0].method == "GET"


# ------------------------------------------------------------------ classification and retries
@pytest.mark.parametrize(
    ("resp", "exc"),
    [
        (_err(401, "DH-901"), VendorAuthError),
        (_err(400, "807"), VendorAuthError),
        (HttpResponse(401, b""), VendorAuthError),
        (_err(403, "DH-902"), VendorSubscriptionError),
        (_err(400, "806"), VendorSubscriptionError),
        (_err(400, "DH-905"), VendorRequestError),
        (_err(400, "812"), VendorRequestError),
        (HttpResponse(404, b"not json"), VendorRequestError),
        (
            HttpResponse(200, json.dumps({"status": "failure", "remarks": {"error_code": "DH-905"}}).encode()),
            VendorRequestError,
        ),
    ],
)
def test_non_retryable_errors_are_typed(tmp_path: Path, resp: HttpResponse, exc: type[Exception]) -> None:
    clock = SimClock()
    rig = make_rig(tmp_path, fake=FakeDhan(clock, faults={1: resp}))
    with pytest.raises(exc):
        rig.client.call(DataEndpoint.ROLLING_OPTION, PAYLOAD)
    assert rig.fake.calls == 1


def test_auth_error_hint_and_redaction(tmp_path: Path) -> None:
    clock = SimClock()
    rig = make_rig(tmp_path, fake=FakeDhan(clock, faults={1: _err(401, "DH-901", f"bad {FAKE_TOKEN}")}))
    with pytest.raises(VendorAuthError) as ei:
        rig.client.call(DataEndpoint.ROLLING_OPTION, PAYLOAD)
    assert "24 hours" in str(ei.value) and FAKE_TOKEN not in str(ei.value)


def test_429_backs_off_and_honours_retry_after(tmp_path: Path) -> None:
    clock = SimClock()
    faults = {1: HttpResponse(429, b"", {"retry-after": "7"}), 2: _err(429, "DH-904"), 3: _err(400, "805")}
    rig = make_rig(tmp_path, fake=FakeDhan(clock, faults=faults))
    r = rig.client.call(
        DataEndpoint.ROLLING_OPTION,
        {**PAYLOAD, "requiredData": ["open"], "fromDate": "2026-09-01", "toDate": "2026-09-02", "interval": "1"},
    )
    assert r.attempts == 4 and r.http_status == 200
    assert 7.0 in clock.sleeps and 2.0 in clock.sleeps and 4.0 in clock.sleeps  # max(backoff, Retry-After)


def test_persistent_429_raises_after_max_attempts(tmp_path: Path) -> None:
    clock = SimClock()
    rig = make_rig(tmp_path, fake=FakeDhan(clock, faults={i: HttpResponse(429, b"") for i in range(1, 20)}))
    with pytest.raises(VendorRateLimitError):
        rig.client.call(DataEndpoint.ROLLING_OPTION, PAYLOAD)
    assert rig.fake.calls == rig.cfg.retry_rate_limit_max_attempts
    assert max(clock.sleeps) <= float(rig.cfg.backoff_cap_seconds)


def test_server_errors_retry_then_raise(tmp_path: Path) -> None:
    clock = SimClock()
    rig = make_rig(
        tmp_path, fake=FakeDhan(clock, faults={1: HttpResponse(502, b""), 2: _err(500, "DH-908"), 3: _err(500, "800")})
    )
    with pytest.raises(VendorServerError):
        rig.client.call(DataEndpoint.ROLLING_OPTION, PAYLOAD)
    assert rig.fake.calls == 3


def test_transport_errors_retry_then_raise(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)

    def boom(_: object) -> HttpResponse:
        raise TransportError("connection reset")

    rig.client._transport = ScriptedTransport(boom)
    with pytest.raises(TransportError, match="gave up after 3"):
        rig.client.call(DataEndpoint.ROLLING_OPTION, PAYLOAD)


def test_dh907_is_explicit_no_data(tmp_path: Path) -> None:
    clock = SimClock()
    rig = make_rig(tmp_path, fake=FakeDhan(clock, faults={1: _err(400, "DH-907", "No data")}))
    r = rig.client.call(DataEndpoint.ROLLING_OPTION, PAYLOAD)
    assert r.no_data and r.vendor_code == "DH-907"


def test_200_with_invalid_json_is_an_error(tmp_path: Path) -> None:
    clock = SimClock()
    rig = make_rig(tmp_path, fake=FakeDhan(clock, faults={1: HttpResponse(200, b"<html>")}))
    with pytest.raises(VendorResponseError):
        rig.client.call(DataEndpoint.ROLLING_OPTION, PAYLOAD)


# ------------------------------------------------------------------ parsing (recorded doc examples + edge cases)
def test_docs_rolling_example_is_ragged_and_rejected(fixtures_dir: Path) -> None:
    parsed = loads_decimal((fixtures_dir / "dhan" / "docs_rollingoption_response_example.json").read_bytes())
    with pytest.raises(VendorResponseError, match="ragged"):
        parse_rolling_option(parsed, OptionRight.CE, ROLLING_FIELDS)
    ok = parse_rolling_option(parsed, OptionRight.CE, ("open",))  # only what was requested must be complete
    assert [str(v) for v in ok.cols["open"]] == ["354.0000", "360.3000"]
    assert ok.ts[0].isoformat() == "2025-09-01T03:45:00+00:00"  # 09:15 IST: timestamps are bar starts
    pe = parse_rolling_option(parsed, OptionRight.PE, ("open",))
    assert pe.rows == 0 and pe.no_data_reason == "data.pe is null"


def test_docs_error_envelope_with_empty_code(fixtures_dir: Path) -> None:
    from project100c.data.dhan.client import Outcome, classify

    body = (fixtures_dir / "dhan" / "docs_error_response_example.json").read_bytes()
    assert classify(HttpResponse(400, body))[0] is Outcome.REQUEST
    assert classify(HttpResponse(500, body))[0] is Outcome.SERVER


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (b'{"open":[1],"high":[1],"low":[1],"close":[1],"volume":[1]}', "timestamp"),
        (b'{"open":[1],"high":[1],"low":[1],"close":[1],"volume":[1.5],"timestamp":[1756698300]}', "integer"),
        (b'{"open":[1],"high":[1],"low":[1],"close":[1],"volume":[1],"timestamp":[1756698300000]}', "milliseconds"),
        (b'{"open":["1"],"high":[1],"low":[1],"close":[1],"volume":[1],"timestamp":[1756698300]}', "number"),
        (b'{"open":[true],"high":[1],"low":[1],"close":[1],"volume":[1],"timestamp":[1756698300]}', "boolean"),
        (b"[]", "object"),
    ],
)
def test_candle_structural_errors(body: bytes, match: str) -> None:
    with pytest.raises(VendorResponseError, match=match):
        parse_candles(loads_decimal(body), with_oi=False)


def test_extra_precision_is_rounded_and_counted() -> None:
    body = b'{"open":[24.349999999],"high":[25],"low":[24],"close":[24.5],"volume":[3],"timestamp":[1756698300]}'
    cols = parse_candles(loads_decimal(body), with_oi=False)
    assert str(cols.cols["open"][0]) == "24.3500" and cols.rounded == {"open": 1}


def test_nan_rejected() -> None:
    with pytest.raises(ValueError):
        loads_decimal(b'{"open":[NaN]}')
