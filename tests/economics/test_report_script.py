from __future__ import annotations

import runpy
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "economics_report.py"


def test_worked_example_script_prints_the_design_19_numbers(capsys: pytest.CaptureFixture[str]) -> None:
    runpy.run_path(str(SCRIPT), run_name="__main__")
    out = capsys.readouterr().out
    for s in ("₹1,163.81/month", "from ₹1,16,381", "₹2,863.01/month", "₹3,995.81/month", "| ₹10,000 | 11.64% |"):
        assert s in out, s
    assert "ASSUMED" in out and "Advisory only" in out
