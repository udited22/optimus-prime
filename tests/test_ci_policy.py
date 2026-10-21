"""CI dependency policy: fixed Python, every install through ci/constraints.txt."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
CONSTRAINTS = REPO / "ci" / "constraints.txt"
INSTALL = re.compile(r"\bpip install\b")


def _steps() -> list[dict[str, Any]]:
    wf = yaml.safe_load(WORKFLOW.read_text())
    return [step for job in wf["jobs"].values() for step in job["steps"]]


def test_python_version_is_pinned_to_a_patch_release() -> None:
    versions = [s["with"]["python-version"] for s in _steps() if "setup-python" in s.get("uses", "")]
    assert versions and all(re.fullmatch(r"\d+\.\d+\.\d+", str(v)) for v in versions), versions


def test_every_install_command_uses_the_constraints_file() -> None:
    lines = [ln for s in _steps() for ln in str(s.get("run", "")).splitlines() if INSTALL.search(ln)]
    assert lines, "no install step found"
    for ln in lines:
        assert "-c ci/constraints.txt" in ln, ln


def test_constraints_are_exact_pins_and_cover_the_installer() -> None:
    pins = [ln for ln in CONSTRAINTS.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    assert all(re.fullmatch(r"[A-Za-z0-9_.\-]+==[A-Za-z0-9_.+\-]+", p) for p in pins), pins
    names = {p.split("==")[0].lower() for p in pins}
    assert {"uv", "pydantic", "pydantic-core", "pytest", "mypy", "ruff"} <= names
