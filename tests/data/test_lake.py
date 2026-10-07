"""Data-lake writer (D-09 groundwork): raw immutability, atomic deterministic parts, lineage, corruption."""

from __future__ import annotations

import gzip
from datetime import date
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pytest

from project100c.data.lake import Lake, Lineage, Zone, canonical_json
from project100c.errors import LakeError


def _lineage(**kw: object) -> Lineage:
    base: dict[str, object] = dict(
        source="dhan",
        dataset="t",
        data_version="t@1",
        parser_version="p1",
        config_version="c1",
        dq_thresholds_version="dq1",
        dq_status="PASS",
        raw_path="raw/x",
        raw_sha256="ab",
        fetched_at_utc="2026-09-30T20:00:00+00:00",
        request={"a": 1},
    )
    base.update(kw)
    return Lineage(**base)  # type: ignore[arg-type]


def _table() -> pa.Table:
    return pa.table({"x": pa.array([Decimal("1.2500")], pa.decimal128(18, 4)), "n": [1]})


def test_raw_is_write_once_and_idempotent(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    r1 = lake.write_raw("dhan", "d", date(2026, 9, 1), "k1", b'{"a":1}', sidecar={"fetched_at_utc": "T1"})
    r2 = lake.write_raw("dhan", "d", date(2026, 9, 1), "k1", b'{"a":1}', sidecar={"fetched_at_utc": "T2"})
    assert r1 == r2 and r2.fetched_at_utc == "T1"  # first sidecar wins -> deterministic lineage
    r3 = lake.write_raw("dhan", "d", date(2026, 9, 1), "k1", b'{"a":2}', sidecar={"fetched_at_utc": "T3"})
    assert r3.path != r1.path  # different bytes -> a new immutable file, the old one untouched
    assert lake.read_raw(r1.path) == b'{"a":1}'


def test_raw_corruption_detected(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    r = lake.write_raw("dhan", "d", date(2026, 9, 1), "k", b"hello", sidecar={"fetched_at_utc": "T"})
    (tmp_path / r.path).write_bytes(gzip.compress(b"tampered"))
    with pytest.raises(LakeError, match="corrupt"):
        lake.read_raw(r.path)
    (tmp_path / r.path).write_bytes(b"not gzip")
    with pytest.raises(LakeError, match="corrupt"):
        lake.read_raw(r.path)


def test_raw_requires_fetched_at(tmp_path: Path) -> None:
    with pytest.raises(LakeError):
        Lake(tmp_path).write_raw("dhan", "d", date(2026, 9, 1), "k", b"x", sidecar={})


def test_parts_deterministic_with_lineage(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    p1 = lake.write_part(Zone.CLEAN, "t", [("a", "1")], "k", _table(), _lineage())
    p2 = lake.write_part(Zone.CLEAN, "t", [("a", "1")], "k", _table(), _lineage())
    assert p1 == p2  # same input -> byte-identical file (D-09 AT: re-ingest gives identical hashes)
    assert lake.lineage_of(p1.path)["raw_sha256"] == "ab"
    assert lake.read_part(p1.path, expected_sha256=p1.sha256).num_rows == 1
    assert lake.parts(Zone.CLEAN, "t") == [p1.path]
    (tmp_path / p1.path).write_bytes(b"garbage")
    with pytest.raises(LakeError, match="sha256"):
        lake.read_part(p1.path, expected_sha256=p1.sha256)
    with pytest.raises(LakeError, match="unreadable"):
        lake.read_part(p1.path)


def test_unsafe_segments_and_escape_rejected(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    with pytest.raises(LakeError):
        lake.write_part(Zone.CLEAN, "../x", [], "k", _table(), _lineage())
    with pytest.raises(LakeError):
        lake.write_part(Zone.CLEAN, "t", [("a", "b/c")], "k", _table(), _lineage())
    with pytest.raises(LakeError):
        lake.abspath("../../etc/passwd")


def test_canonical_json_refuses_non_json() -> None:
    with pytest.raises(LakeError):
        canonical_json({"d": Decimal("1.5")})
    with pytest.raises(LakeError):
        canonical_json({"f": float("nan")})
    assert canonical_json({"b": 1, "a": [2]}) == b'{"a":[2],"b":1}'


def test_manifest_roundtrip_and_errors(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    rel = lake.write_manifest("t", "m1", {"rows": 3})
    assert lake.read_manifest(rel) == {"rows": 3}
    (tmp_path / rel).write_text("{bad")
    with pytest.raises(LakeError):
        lake.read_manifest(rel)
    with pytest.raises(LakeError):
        lake.read_manifest("manifests/t/none.json")
