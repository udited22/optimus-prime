"""Every source and test file must be tracked by git, not silently dropped by a .gitignore rule.

The secret guard ``*token*`` in .gitignore once swallowed ``ops/token_gate.py`` and its tests: the commit looked
complete locally but main could not import ``project100c.paper`` on a fresh clone. File names must not trip the
secret patterns; this test fails if any .py, .toml, .yaml or .md file under src/, tests/, configs/ (except
configs/local/), specs/, docs/ or design/ is ignored.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIRS = ("src", "tests", "configs", "specs", "docs", "examples", "tools", "scripts")
SUFFIXES = {".py", ".toml", ".yaml", ".yml", ".md"}


def _candidates() -> list[str]:
    out: list[str] = []
    for d in DIRS:
        for p in (ROOT / d).rglob("*"):
            rel = p.relative_to(ROOT).as_posix()
            if (
                p.is_file()
                and p.suffix in SUFFIXES
                and "__pycache__" not in rel
                and not rel.startswith("configs/local/")
            ):
                out.append(rel)
    return out


@pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="not a git checkout")
def test_no_source_file_is_gitignored() -> None:
    files = _candidates()
    r = subprocess.run(
        ["git", "check-ignore", "--stdin", "--no-index"],
        cwd=ROOT,
        input="\n".join(files),
        capture_output=True,
        text=True,
        check=False,
    )
    ignored = [line for line in r.stdout.splitlines() if line.strip()]
    assert ignored == [], f"these files are ignored by .gitignore and would never reach main: {ignored}"
