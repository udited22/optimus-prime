"""Event-day certification (OD-014 default, 2-Oct-2026): may a strategy trade on an EVENT_REGIME day?

A strategy may enter on an event day (``MarketSnapshot.event_day`` or an ``EVENT_REGIME`` regime tag) only if its
spec has ``event_certified: true``. That flag can only be set with an ``event_certification`` record whose
``gate_result_id`` names a saved gate result in which the strategy passed the validation toolkit
(docs/research/validation.md V1-V18)
on the subset of its historical trades taken on event days:

* the subset is the trades with ``event_day=True`` (in-sample, out-of-sample, delayed and holdout alike);
* the verdict must be VALIDATED, which ``validate`` gives only on REAL data, so SYNTHETIC runs can never certify;
* V15 (event contamination) asks whether the edge survives *without* event days. On an event-only subset it has
  no meaning, so it is evaluated as certified for this run; every other gate applies unchanged.

The gate result is saved as ``configs/validation/event_certifications/<gate_result_id>.json``. The id is a hash
of the saved document, so ``verify_event_certification`` can tell when the file is missing, edited or belongs to
another spec version. No strategy is certified today (no real data yet).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

from project100c.errors import ConfigError
from project100c.spec.models import EVENT_CERT_SUBSET, StrategySpec
from project100c.validation.gates import GateConfig, ValidationInputs, ValidationReport, Verdict, validate
from project100c.validation.trades import Trade

CERT_DIR = Path("configs/validation/event_certifications")


def _event_only(trades: Sequence[Trade] | None) -> list[Trade] | None:
    return None if trades is None else [t for t in trades if t.event_day]


def _document(report: ValidationReport, event_days: int) -> dict[str, Any]:
    return {"subset": EVENT_CERT_SUBSET, "event_days": event_days, "report": report.to_dict()}


def gate_result_id(doc: dict[str, Any]) -> str:
    """``VR-`` + the first 12 hex characters of the SHA-256 of the canonical JSON document."""
    blob = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return "VR-" + hashlib.sha256(blob).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class EventCertificationResult:
    report: ValidationReport
    event_days: int  # distinct event days in the subset
    certified: bool
    reason: str

    def document(self) -> dict[str, Any]:
        return _document(self.report, self.event_days)

    @property
    def gate_result_id(self) -> str:
        return gate_result_id(self.document())

    def record(self, certified_on: date) -> dict[str, Any]:
        """The spec's ``event_certification`` block. Refuses unless the run certified."""
        if not self.certified:
            raise ConfigError(f"{self.report.spec_id}: not event-certified ({self.reason})")
        return {"gate_result_id": self.gate_result_id, "gate_config_version": self.report.config_version,
                "subset": EVENT_CERT_SUBSET, "data_label": "REAL", "verdict": Verdict.VALIDATED.value,
                "event_days": self.event_days, "certified_on": certified_on.isoformat()}  # fmt: skip

    def save(self, directory: Path = CERT_DIR) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.gate_result_id}.json"
        path.write_text(json.dumps(self.document(), sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return path


def certify_event_days(inp: ValidationInputs, cfg: GateConfig) -> EventCertificationResult:
    """Run the V1-V18 gates on the historical event-day subset of ``inp``. Certified only on VALIDATED (REAL data)."""
    trades = _event_only(inp.trades) or []
    sub = replace(inp, trades=trades, delayed_trades=_event_only(inp.delayed_trades),
                  holdout_trades=_event_only(inp.holdout_trades), event_certified=True)  # fmt: skip
    report = validate(sub, cfg)
    days = len({t.day for t in trades})
    if not trades:
        return EventCertificationResult(report, 0, False, "no event-day trades")
    if inp.data_label.upper() != "REAL":
        return EventCertificationResult(report, days, False, f"{inp.data_label} data can never certify")
    if report.verdict is not Verdict.VALIDATED:
        return EventCertificationResult(report, days, False, f"verdict {report.verdict.value}")
    return EventCertificationResult(report, days, True, "VALIDATED on the event-day subset")


def verify_event_certification(spec: StrategySpec, directory: Path = CERT_DIR) -> None:
    """Raise ConfigError unless a certified spec's gate result is on file, unedited, and matches the spec."""
    if not spec.event_certified:
        return
    cert = spec.event_certification
    assert cert is not None  # the spec model requires it
    path = directory / f"{cert.gate_result_id}.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"{spec.id}: event certification {cert.gate_result_id} not readable: {e}") from e
    problems = []
    if gate_result_id(doc) != cert.gate_result_id:
        problems.append("the file does not hash to its id (edited?)")
    rep = doc.get("report", {})
    if doc.get("subset") != EVENT_CERT_SUBSET:
        problems.append(f"subset {doc.get('subset')!r}")
    if rep.get("verdict") != Verdict.VALIDATED.value or str(rep.get("data_label", "")).upper() != "REAL":
        problems.append(f"verdict {rep.get('verdict')!r} on {rep.get('data_label')!r} data")
    if (rep.get("spec_id"), rep.get("spec_version")) != (spec.id, spec.version):
        problems.append(f"result is for {rep.get('spec_id')} {rep.get('spec_version')}, spec is {spec.version}")
    if rep.get("config_version") != cert.gate_config_version:
        problems.append(f"gate config {rep.get('config_version')} != {cert.gate_config_version}")
    if problems:
        raise ConfigError(f"{spec.id}: event certification {cert.gate_result_id} invalid: {'; '.join(problems)}")


def event_certified_strategies(specs: Iterable[StrategySpec], directory: Path = CERT_DIR) -> frozenset[str]:
    """The ids allowed to trade on event days, after verifying each certification against its saved gate result."""
    out = set()
    for s in specs:
        if s.event_certified:
            verify_event_certification(s, directory)
            out.add(s.id)
    return frozenset(out)
