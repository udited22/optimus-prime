"""The browser bundle carries no secret.

The frontend is built the way a static host would build it, with canary values planted in the build environment under
the
server-side names (P100C_API_KEY, JARVIS_PASSWORD_SHA256, ...) and under VITE_ names (which Vite would inline if
any code read them). Then every output file is scanned for the canaries, for the server-only names, and for
credential-shaped text. Any server-side proxy or authentication layer is not in the bundle.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "dashboard"

SERVER_ONLY = ("P100C_API_KEY", "P100C_API_BASE", "JARVIS_PASSWORD", "JARVIS_USER", "X-P100C-Api-Key", "process.env")
SHAPES = {
    "Fernet token": re.compile(r"gAAAAA[A-Za-z0-9_-]{60,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "bearer token": re.compile(r"Bearer [A-Za-z0-9._~+/-]{24,}"),
    "Telegram bot token": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    "GitHub token": re.compile(r"\b(ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{20,}"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "bcrypt hash": re.compile(r"\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}"),
}


def scan_bundle(out: Path, canaries: dict[str, str]) -> list[str]:
    findings: list[str] = []
    files = [p for p in out.rglob("*") if p.is_file()]
    for p in files:
        text = p.read_bytes().decode("utf-8", errors="ignore")
        rel = p.relative_to(out)
        for name, value in canaries.items():
            if value in text:
                findings.append(f"{rel}: the value of {name} from the build environment")
        for name in SERVER_ONLY:
            if name.lower() in text.lower():
                findings.append(f"{rel}: server-only name {name}")
        for what, rx in SHAPES.items():
            if rx.search(text):
                findings.append(f"{rel}: {what}-shaped text")
    return findings


def test_the_scanner_finds_what_it_looks_for(tmp_path: Path) -> None:
    canary = secrets.token_urlsafe(24)
    (tmp_path / "a.js").write_text(f'const k = "{canary}"; fetch(u, {{headers: {{"X-P100C-Api-Key": k}}}})')
    (tmp_path / "b.js").write_text("x='gAAAAA" + "B" * 80 + "'")
    found = scan_bundle(tmp_path, {"P100C_API_KEY": canary})
    assert any("value of P100C_API_KEY" in f for f in found)
    assert any("X-P100C-Api-Key" in f for f in found) and any("Fernet" in f for f in found)


@pytest.mark.skipif(
    shutil.which("npm") is None or not (ROOT / "node_modules").is_dir(),
    reason="npm or dashboard/node_modules missing (run `cd dashboard && npm install`)",
)
def test_the_built_bundle_carries_no_secret(tmp_path: Path) -> None:
    canaries = {
        name: "canary-" + secrets.token_urlsafe(24)
        for name in (
            "P100C_API_KEY",
            "P100C_API_BASE",
            "JARVIS_USER",
            "JARVIS_PASSWORD_SHA256",
            "VITE_P100C_API_KEY",
            "VITE_JARVIS_PASSWORD",
            "VITE_API_BASE",
        )
    }
    out = tmp_path / "dist"
    r = subprocess.run(
        ["npm", "run", "build", "--", "--outDir", str(out), "--emptyOutDir"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        env={**os.environ, **canaries},
    )
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert (out / "index.html").is_file() and (out / "status.html").is_file() and (out / "status.js").is_file()
    assert not (out / "api").exists() and not list(out.rglob("middleware*"))  # server code is not shipped
    assert scan_bundle(out, canaries) == []
