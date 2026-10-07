"""OD-011: the Dhan client is DATA ONLY. No order / portfolio / funds / account-changing endpoint exists anywhere
in project100c.data (static proof over the source), and a full job only ever reaches allowlisted URLs (dynamic)."""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

from project100c.data.dhan import ALLOWED_ENDPOINTS, CandleJobSpec, DataEndpoint, RollingOptionJobSpec
from tests.data.dhan_fakes import REPO, make_rig

DATA_PKG = REPO / "src" / "project100c" / "data"

# Dhan v2 trading / account paths and SDK method names that must never appear in code (docstrings excluded).
BANNED_PATH_FRAGMENTS = (
    "orders",
    "order/",
    "/order",
    "super",
    "forever",
    "killswitch",
    "kill-switch",
    "positions",
    "holdings",
    "fundlimit",
    "margincalculator",
    "trades",
    "tradebook",
    "tradehistory",
    "edis",
    "ip/",
    "setip",
    "modifyip",
    "renewtoken",
    "generateaccesstoken",
    "consent",
    "pnlexit",
    "alerts",
    "exitall",
    "exit-all",
    "ledger",
)
BANNED_IDENTIFIER_FRAGMENTS = (
    "order",
    "position",
    "holding",
    "fund",
    "margin",
    "trade",
    "killswitch",
    "exit_all",
    "exitall",
    "edis",
)
BANNED_IMPORTS = ("dhanhq", "project100c.broker", "project100c.kernel", "project100c.execution")


def _py_files() -> list[Path]:
    files = sorted(DATA_PKG.rglob("*.py"))
    assert len(files) >= 10
    scripts = sorted((REPO / "scripts").glob("dhan_*.py"))  # the CLIs that hold the token at run time
    assert {f.name for f in scripts} >= {"dhan_download.py", "dhan_backfill.py"}
    return files + scripts


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def test_no_order_or_account_endpoint_strings_or_identifiers() -> None:
    offences: list[str] = []
    for f in _py_files():
        tree = ast.parse(f.read_text())
        docs = _docstring_nodes(tree)
        for node in ast.walk(tree):
            line = getattr(node, "lineno", 0)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                low = node.value.lower().replace(" ", "")
                offences += [f"{f.name}:{line} string {node.value!r}" for b in BANNED_PATH_FRAGMENTS if b in low]
            names: list[str] = []
            if isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                names.append(node.name)
            elif isinstance(node, ast.arg):
                names.append(node.arg)
            for n in names:
                offences += [f"{f.name}:{line} identifier {n}" for b in BANNED_IDENTIFIER_FRAGMENTS if b in n.lower()]
            if isinstance(node, ast.Import | ast.ImportFrom):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                offences += [f"{f.name}:{line} import {m}" for m in mods for b in BANNED_IMPORTS if m.startswith(b)]
    assert offences == []


def test_the_scan_would_catch_an_order_endpoint() -> None:
    """Guard against a vacuous scanner: the same rules flag a planted order call."""
    planted = 'def place_order(c):\n    return c.post("https://api.dhan.co/v2/orders", {})\n'
    tree = ast.parse(planted)
    strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert any(b in s.lower() for s in strings for b in BANNED_PATH_FRAGMENTS)
    names = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    assert any(b in n for n in names for b in BANNED_IDENTIFIER_FRAGMENTS)


def test_allowlist_is_exactly_the_data_endpoints() -> None:
    assert dict(ALLOWED_ENDPOINTS) == {
        DataEndpoint.ROLLING_OPTION: "POST",
        DataEndpoint.INTRADAY: "POST",
        DataEndpoint.HISTORICAL: "POST",
        DataEndpoint.PROFILE: "GET",
    }
    assert set(DataEndpoint) == set(ALLOWED_ENDPOINTS)
    assert all(e.value.startswith("charts/") or e.value == "profile" for e in DataEndpoint)
    assert set(ALLOWED_ENDPOINTS.values()) <= {"GET", "POST"}  # no PUT / DELETE anywhere


def test_full_jobs_touch_only_allowlisted_urls(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.client.profile()
    rig.downloader.run(
        rig.downloader.create(
            RollingOptionJobSpec(from_date=date(2026, 9, 1), to_date=date(2026, 9, 3), strike_offsets=(0,))
        )
    )
    rig.downloader.run(
        rig.downloader.create(
            CandleJobSpec(
                label="NIFTY-INDEX",
                security_id="13",
                exchange_segment="IDX_I",
                instrument="INDEX",
                from_date=date(2026, 9, 1),
                to_date=date(2026, 9, 3),
            )
        )
    )
    allowed = {f"https://api.dhan.co/v2/{e.value}" for e in ALLOWED_ENDPOINTS}
    assert rig.transport.requests and all(r.url in allowed for r in rig.transport.requests)
    assert {r.method for r in rig.transport.requests} <= {"GET", "POST"}
