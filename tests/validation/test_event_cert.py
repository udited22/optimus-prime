"""Event-day certification (OD-014 default, 2-Oct-2026). The trade data is SYNTHETIC; the "REAL" runs below only
exercise the certification path, as in test_gates.py, and claim no edge. No shipped strategy is certified."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from project100c.errors import ConfigError
from project100c.spec.models import StrategySpec
from project100c.validation import Trade, Verdict
from project100c.validation.event_cert import (
    EventCertificationResult,
    certify_event_days,
    event_certified_strategies,
    gate_result_id,
    verify_event_certification,
)
from tests.strategies.library_rig import SPECS, spec
from tests.validation.test_gates import SP, cfg, good_inputs, make_trades

ON = date(2026, 10, 2)


def _event_trades() -> list[Trade]:
    return make_trades(300, 300.0, 1000.0, seed=1, event_day=True)


def _real_pass() -> EventCertificationResult:
    hold = make_trades(80, 415.0, 100.0, seed=99, start=date(2026, 4, 1), event_day=True)
    inp = good_inputs(_event_trades(), SP, data_label="REAL", universe_point_in_time=True, holdout_trades=hold)
    return certify_event_days(inp, cfg())


def _certified_spec(record: dict[str, Any]) -> StrategySpec:
    return StrategySpec.model_validate({**SP.model_dump(mode="json"), "event_certified": True,
                                        "event_certification": record})  # fmt: skip


def test_no_shipped_strategy_is_event_certified() -> None:
    specs = [spec(p.stem) for p in sorted(SPECS.glob("S-*.yaml"))]
    assert specs and all(not s.event_certified and s.event_certification is None for s in specs)
    assert event_certified_strategies(specs) == frozenset()


def test_synthetic_data_can_never_certify() -> None:
    r = certify_event_days(good_inputs(_event_trades(), SP), cfg())
    assert not r.certified and "SYNTHETIC" in r.reason
    assert r.report.verdict is Verdict.PASSES_ON_SYNTHETIC
    with pytest.raises(ConfigError, match="not event-certified"):
        r.record(ON)


def test_only_event_day_trades_are_judged() -> None:
    plain = make_trades(300, 300.0, 1000.0, seed=1)  # strong, but none on an event day
    r = certify_event_days(good_inputs(plain, SP, data_label="REAL", universe_point_in_time=True), cfg())
    assert not r.certified and r.reason == "no event-day trades" and r.event_days == 0
    losing = make_trades(300, -200.0, 500.0, seed=3, event_day=True)
    mixed = plain + losing  # the edge is on normal days; the event days lose
    r = certify_event_days(good_inputs(mixed, SP, data_label="REAL", universe_point_in_time=True), cfg())
    assert not r.certified and r.report.verdict is Verdict.REJECTED


def test_a_validated_event_subset_certifies_and_the_record_round_trips(tmp_path: Path) -> None:
    r = _real_pass()
    assert r.certified, [(g.code, g.status, g.note) for g in r.report.gates if g.status != "PASS"]
    assert r.gate_result_id == gate_result_id(r.document()) and r.gate_result_id.startswith("VR-")
    assert r.gate_result_id == _real_pass().gate_result_id  # seeded: the same inputs give the same id
    path = r.save(tmp_path)
    assert path.name == f"{r.gate_result_id}.json"
    sp = _certified_spec(r.record(ON))
    verify_event_certification(sp, tmp_path)
    assert event_certified_strategies([sp], tmp_path) == {SP.id}


def test_a_missing_edited_or_mismatched_gate_result_is_refused(tmp_path: Path) -> None:
    r = _real_pass()
    sp = _certified_spec(r.record(ON))
    with pytest.raises(ConfigError, match="not readable"):
        verify_event_certification(sp, tmp_path)
    path = r.save(tmp_path)
    path.write_text(path.read_text().replace('"VALIDATED"', '"VALIDATED" '), encoding="utf-8")
    verify_event_certification(sp, tmp_path)  # whitespace only: the canonical hash is unchanged
    path.write_text(path.read_text().replace('"event_days": ', '"event_days": 1'), encoding="utf-8")
    with pytest.raises(ConfigError, match="does not hash"):
        verify_event_certification(sp, tmp_path)
    r.save(tmp_path)
    bumped = _certified_spec(r.record(ON)).model_copy(update={"version": "9.9.9"})
    with pytest.raises(ConfigError, match=r"spec is 9\.9\.9"):
        verify_event_certification(bumped, tmp_path)


def test_the_spec_flag_needs_a_valid_record_and_vice_versa() -> None:
    good = _real_pass().record(ON)
    base = SP.model_dump(mode="json")
    with pytest.raises(ValidationError, match="event_certification"):
        StrategySpec.model_validate({**base, "event_certified": True})
    with pytest.raises(ValidationError, match="event_certified"):
        StrategySpec.model_validate({**base, "event_certification": good})
    for bad in ({"gate_result_id": "VR-123"}, {"data_label": "SYNTHETIC"}, {"verdict": "PASSES_ON_SYNTHETIC"},
                {"subset": "ALL_DAYS"}, {"event_days": 0}):  # fmt: skip
        with pytest.raises(ValidationError):
            _certified_spec({**good, **bad})
    assert _certified_spec(good).event_certified


def test_v15_is_waived_only_inside_the_event_subset() -> None:
    r = _real_pass()
    assert r.report.gate("V15").status == "PASS"
    inp = good_inputs(_event_trades(), SP, data_label="REAL", universe_point_in_time=True)
    full = replace(inp, event_certified=False)
    from project100c.validation import validate

    assert validate(full, cfg()).gate("V15").status == "FAIL"  # the ordinary run still flags event-only edges
