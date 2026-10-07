"""OD-017 architecture rule: the core stays market- and broker-agnostic.

Only the integration adapters (brokers such as Upstox; market-data sources such as Dhan; alert channels such as
Telegram) may be venue-specific. The core -- order engine, Risk Governor, strategies, regime model, allocator --
may depend on the broker *interface* (``project100c.broker`` types and protocol) and on the instrument/venue layer
(``instruments``, ``sessions``, ``calendar``, ``costs``: lot size, tick, session hours, expiry, charges, all from
config), but never on an adapter module. Future venues: Bank Nifty, Sensex, crypto, crypto futures, US perps.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "project100c"
CORE = ("kernel", "execution", "portfolio", "regime", "strategies", "spec", "agents", "journal")
# adapter modules: anything venue- or vendor-specific
ADAPTERS = (
    "project100c.data",  # market-data sources (Dhan, the lake)
    "project100c.recorder",  # the live tick recorder's vendor clients
    "project100c.broker.upstox",  # broker adapters
    "project100c.broker.fake",  # a test/paper venue, injected by the caller, never imported by core
    "project100c.broker.paper",
    "project100c.notify",  # alert channels (Telegram)
    "project100c.netio",  # network clients (WebSocket)
)
VENDOR_WORDS = (
    "upstox",
    "dhan",
    "telegram",
    "zerodha",
    "kite",
    "breeze",
    "binance",
    "coinlore",
    "coingecko",
    "coindcx",
    "stooq",
    "yfinance",
    "alpaca",
    "polygon",
    "tiingo",
    "edgar",
    "cboe",
    "yahoo",
)
# Known, tracked exception: the OD-006 mandate names the one underlying the directive currently allows. It is to
# move into the instrument/venue config when a second venue is added.
NIFTY_ALLOWED = {"kernel/mandate.py"}


def _core_files() -> list[Path]:
    return sorted(p for pkg in CORE if (SRC / pkg).exists() for p in (SRC / pkg).rglob("*.py"))


def _imports(tree: ast.AST) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
            out += [f"{node.module}.{a.name}" for a in node.names]
    return out


def _non_doc_strings_and_names(tree: ast.AST) -> list[str]:
    docs: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docs.add(id(first.value))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            out.append(node.value)
        elif isinstance(node, ast.Name):
            out.append(node.id)
        elif isinstance(node, ast.Attribute):
            out.append(node.attr)
    return out


def test_core_packages_exist() -> None:
    assert len(_core_files()) > 20


@pytest.mark.parametrize("path", _core_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_core_never_imports_an_adapter(path: Path) -> None:
    tree = ast.parse(path.read_text(), str(path))
    bad = [m for m in _imports(tree) if any(m == a or m.startswith(a + ".") for a in ADAPTERS)]
    assert not bad, f"{path.relative_to(SRC)} imports adapter modules {bad} (OD-017: core stays venue-agnostic)"


@pytest.mark.parametrize("path", _core_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_core_code_names_no_vendor(path: Path) -> None:
    tree = ast.parse(path.read_text(), str(path))
    words = [w for w in _non_doc_strings_and_names(tree) if any(v in w.lower() for v in VENDOR_WORDS)]
    assert not words, f"{path.relative_to(SRC)} names a vendor in code: {words[:5]}"


def test_kernel_and_execution_name_no_market() -> None:
    hits = []
    for pkg in ("kernel", "execution", "portfolio", "regime"):
        for p in sorted((SRC / pkg).rglob("*.py")) if (SRC / pkg).exists() else []:
            rel = str(p.relative_to(SRC))
            if rel in NIFTY_ALLOWED:
                continue
            words = _non_doc_strings_and_names(ast.parse(p.read_text()))
            hits += [(rel, w) for w in words if any(m in w.upper() for m in ("NIFTY", "SENSEX", "BANKNIFTY"))]
    assert not hits, f"market names in core code (OD-017): {hits[:10]}"


def test_the_checker_catches_a_violation(tmp_path: Path) -> None:
    tree = ast.parse("from project100c.broker.upstox import UpstoxBroker\nx = 'UPSTOX_ACCESS_TOKEN'\n")
    assert any(m.startswith("project100c.broker.upstox") for m in _imports(tree))
    assert any("upstox" in w.lower() for w in _non_doc_strings_and_names(tree))


# ---------------------------------------------------------------- credentials (the LLM never holds them)
# Only these modules may read the process environment, and only adapters read credentials from it.
ENV_ALLOWED = {
    "data/dhan/credentials.py",  # Dhan data token (OD-011)
    "broker/upstox/credentials.py",  # Upstox access token (OD-004), loaded by the adapter only
    "notify/telegram.py",  # TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (OD-017)
    "ops/redact.py",  # reads the credential variables only to mask their values (K-S1); never logs them
    "ops/credstore.py",  # the encrypted credential store: the environment overrides the file (K-S1)
    "observability/dashboard/__main__.py",  # refuses to start when a broker token is present; never reads it
    "alpha.py",  # P100C_ALPHA_DIR: where the optional private alpha library lives (a path, never a credential)
}


def _all_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_only_adapters_read_the_environment() -> None:
    bad = []
    for p in _all_files():
        rel = str(p.relative_to(SRC))
        tree = ast.parse(p.read_text())
        uses = [
            n
            for n in ast.walk(tree)
            if (isinstance(n, ast.Attribute) and n.attr in ("environ", "getenv", "environb"))
            or (isinstance(n, ast.Name) and n.id in ("environ", "getenv"))
        ]
        if uses and rel not in ENV_ALLOWED:
            bad.append(rel)
    assert not bad, f"environment reads outside the credential adapters: {bad}"


def test_agents_cannot_reach_brokers_execution_or_credentials() -> None:
    """The research agents (the only place an LLM may ever run) cannot import a broker, the execution layer, the
    kernel runtime, an alert channel or a credential loader. Their output is a TradeIntent for the Governor."""
    forbidden = (
        "project100c.broker",
        "project100c.execution",
        "project100c.kernel.runtime",
        "project100c.notify",
        "project100c.data",
    )
    for p in sorted((SRC / "agents").rglob("*.py")):
        mods = _imports(ast.parse(p.read_text()))
        bad = [m for m in mods if any(m == f or m.startswith(f + ".") for f in forbidden)]
        assert not bad, f"{p.relative_to(SRC)} imports {bad}"
