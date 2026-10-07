#!/usr/bin/env python3
"""Per-regime results, the walk-forward regime -> strategy map and the portfolio book (configs/portfolio/regime_map).

Reads every strategy run (scripts/regime_strategy_runs.py, scripts/orbml_overlay.py) and the classifier stream of the
map's classifier (tags at each trade's signal bar). Research data only unless --holdout-look:

1. per-regime tables per (strategy, cell, NAV): in-sample (before first_test_from) and walk-forward test period;
2. books on the walk-forward test windows (research days; holdout days excluded), each through the OD-014 caps:
   REGIME (the map: K-11 vol cells), AGNOSTIC (the same walk-forward rule per strategy, no regime) and UNGATED (every
   eligible trade); event days, abnormal markets, warm-up and spec-prohibited regimes are never traded;
3. the alternative regime models H25/H26 (configs/regime/alt_models.toml; labels from scripts/regime_alt_models.py)
   in place of the K-11 cell, judged by the pre-registered test;
4. V1-V18 for every (strategy, cell, NAV) pair and every (strategy, ALL, NAV) (regime-agnostic) pair.
Every evaluation is registered as a trial (V9).

--holdout-look: the ONE holdout evaluation (HD-2026-10-03.1) of the final map, the agnostic fallback and the ungated
book, plus V16 for any shortlisted pair. Refuses to run twice (marker file and registry).

  scripts/regime_portfolio.py [--holdout-look]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
import tomllib
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from project100c.portfolio.regime_book import (  # noqa: E402
    BookTrade,
    LabelIndex,
    RegimeMapConfig,
    book_metrics,
    book_trades_from_run,
    cell_table,
    final_selection,
    load_regime_map_config,
    mean_diff_ci,
    simulate_book,
    walk_forward_select,
    walk_forward_windows,
)
from project100c.registry import RunPurpose  # noqa: E402
from project100c.registry.trials import DEFAULT_REGISTRY, code_sha, digest, open_registry, record_trial  # noqa: E402
from project100c.spec.io import load_spec_file  # noqa: E402
from project100c.spec.models import StrategySpec  # noqa: E402
from project100c.validation.gates import ValidationInputs, load_gate_config, validate  # noqa: E402
from project100c.validation.holdout import load_holdout  # noqa: E402
from project100c.validation.trades import Trade  # noqa: E402

CONFIGS = REPO / "configs"
EXPECTED_SEGMENTS = 26
REG_BY = "scripts/regime_portfolio.py"


def load_specs() -> dict[str, StrategySpec]:
    out = {}
    for p in sorted((REPO / "specs").glob("*.yaml")) + sorted((REPO / "specs" / "drafts").glob("*.yaml")):
        s = load_spec_file(p)
        out.setdefault(s.id, s)
    return out


def load_runs(runs: Path) -> dict[tuple[str, str], list[dict[str, Any]]]:
    docs: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for f in sorted(runs.glob("S-*.json")):
        d = json.loads(f.read_text())
        docs[(str(d["strategy"]), str(d["nav"]))].append(d)
    return docs


def alt_labels(path: Path) -> dict[date, tuple[str, time]]:
    out = {}
    with path.open() as fh:
        for r in csv.DictReader(fh):
            out[date.fromisoformat(r["day"])] = (r["label"], time.fromisoformat(r["from_time"]))
    return out


def alt_cell(t: BookTrade, lab: dict[date, tuple[str, time]], prev_day: dict[date, date]) -> str | None:
    got = lab.get(t.day)
    if got is not None and t.signal_at.time() >= got[1]:
        return got[0]
    p = prev_day.get(t.day)
    if got is not None and got[1] == time(9, 15):
        return got[0]
    return lab[p][0] if p is not None and p in lab else None


def run_book(
    trades: list[BookTrade],
    cfg: RegimeMapConfig,
    days: list[date],
    nav0: float,
    caps: dict[str, int],
    windows: list[tuple[date, date]] | None = None,
    mode: str = "MAP",
) -> tuple[dict[str, Any], Any, list[Any]]:
    """MAP: walk-forward selection on the trades' cells; UNGATED: every trade, equal priority."""
    wins = windows or walk_forward_windows(cfg)
    lo, hi = wins[0][0], wins[-1][1]
    d_in = [d for d in days if lo <= d <= hi]
    cands: list[tuple[BookTrade, float]] = []
    sels = []
    if mode == "UNGATED":
        cands = [(t, 0.0) for t in trades if lo <= t.day <= hi]
    else:
        sels = [w for w in walk_forward_select(trades, cfg) if lo <= w.start and w.end <= hi]
        for w in sels:
            sel = w.selected
            cands += [
                (t, sel[(t.strategy, t.cell)].mean_r)
                for t in trades
                if w.start <= t.day <= w.end and (t.strategy, t.cell) in sel
            ]
    res = simulate_book(cands, nav0=nav0, days=d_in, caps=caps, cfg=cfg)
    m = book_metrics(res)
    m.update(candidates=len(cands), period=[str(lo), str(hi)])
    return m, res, sels


def val_trade(t: BookTrade, qty: int, oos: bool) -> Trade:
    return Trade(
        day=t.day,
        entry_ts=t.entry_ts,
        exit_ts=t.exit_ts,
        net_pnl=Decimal(str(t.net_pnl)),
        charges=Decimal(str(t.charges)),
        slippage=Decimal(str(t.slippage)),
        risk_at_stop=Decimal(str(t.risk_at_stop)),
        qty=qty,
        regimes=frozenset(t.tags),
        event_day="EVENT_REGIME" in t.tags,
        exit_reason=t.exit_reason,
        oos=oos,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lake", type=Path, default=Path("lake"))
    ap.add_argument("--map-version", default=None)
    ap.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    ap.add_argument("--no-register", action="store_true")
    ap.add_argument("--holdout-look", action="store_true")
    ap.add_argument("--standalone", action="store_true", help="only the per-strategy standalone books (diagnostic)")
    a = ap.parse_args()
    cfg = load_regime_map_config(CONFIGS / "portfolio" / "regime_map.toml", version=a.map_version)
    hd = load_holdout(CONFIGS / "validation" / "holdout.toml")
    if hd.version != cfg.holdout:
        raise SystemExit(f"holdout {hd.version} != map's {cfg.holdout}")
    gcfg = load_gate_config(CONFIGS / "validation" / "gates.toml")
    alt_cfg = {m["id"]: m for m in tomllib.loads((CONFIGS / "regime" / "alt_models.toml").read_text())["alt_model"]}
    specs = load_specs()
    caps = {sid: s.entry.max_entries_per_day for sid, s in specs.items()}
    out_dir = a.lake / "runs" / "regime" / "portfolio"
    out_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(a.lake / "runs" / "regime" / "labels" / f"{cfg.classifier}.csv.gz", "rt", encoding="utf-8") as fh:
        li = LabelIndex(csv.DictReader(fh))
    all_days = li.days
    prev_day = {d: p for p, d in pairwise(all_days)}
    research_days = [d for d in all_days if cfg.research_from <= d <= cfg.research_to and not hd.contains(d)]
    holdout_days = [d for d in all_days if hd.contains(d)]
    docs = load_runs(a.lake / "runs" / "regime" / "strategies")
    reg = None if a.no_register else open_registry(a.registry)
    code = code_sha(REPO)

    trades: dict[str, list[BookTrade]] = defaultdict(list)  # nav -> research trades
    hold: dict[str, list[BookTrade]] = defaultdict(list)
    qty: dict[tuple[str, str], int] = {}
    meta: dict[tuple[str, str], dict[str, Any]] = {}
    excluded: dict[str, dict[str, dict[str, int]]] = defaultdict(dict)
    coverage: dict[str, int] = {}
    for (sid, nav), ds in sorted(docs.items()):
        spec = specs[sid]
        coverage[f"{sid}@{nav}"] = len(ds)
        stamps, fills, fm, ledgers, n_raw = [], True, None, [], 0
        for d in ds:
            bt, sk = book_trades_from_run(d, dims=cfg.cell_dimensions, prohibited=spec.prohibited_regimes, labels=li)
            for k, v in sk.items():
                excluded[nav].setdefault(sid, {})[k] = excluded[nav].get(sid, {}).get(k, 0) + v
            for raw in d["trades"]:
                qty[(sid, raw["signal_at"])] = int(raw.get("qty", 0) or 0)
                n_raw += 1
            for t in bt:
                if hd.contains(t.day):
                    hold[nav].append(t)
                elif cfg.research_from <= t.day <= cfg.research_to:
                    trades[nav].append(t)
            stamps += [(datetime.fromisoformat(x), datetime.fromisoformat(y)) for x, y in d.get("signal_stamps", [])]
            fills = fills and bool(d.get("fills_dated_costs"))
            fm = fm or d.get("fill_model")
            ledgers.append(d.get("ledger_hash"))
        meta[(sid, nav)] = {
            "stamps": stamps,
            "fills": fills,
            "fill_model": fm,
            "ledgers": ledgers,
            "raw": n_raw,
            "spec_version": ds[0].get("spec_version", spec.version),
        }
    data_hash = digest(sorted((k[0], k[1], v["ledgers"]) for k, v in meta.items()))

    def register(
        sid: str,
        version: str,
        purpose: RunPurpose,
        params: dict[str, Any],
        n: int,
        metrics: dict[str, Any],
        notes: str = "",
    ) -> None:
        if reg is not None:
            record_trial(
                reg,
                strategy_id=sid,
                strategy_version=version,
                spec_hash=digest(params),
                code=code,
                data_hash=data_hash,
                cost_version="dated",
                purpose=purpose,
                params=params,
                ledger_hash=digest(metrics),
                n_trades=n,
                metrics=metrics,
                registered_by=REG_BY,
                notes=notes,
            )

    if a.standalone:
        return standalone(a, cfg, trades, research_days, caps, register, out_dir)
    if not a.holdout_look:  # the library runs themselves, one trial per (strategy, NAV) (V9)
        for (sid, nav), m in sorted(meta.items()):
            register(
                sid,
                m["spec_version"],
                RunPurpose.EXPLORATION,
                {"run": "library", "nav": nav, "classifier": cfg.classifier},
                m["raw"],
                {"segments": coverage[f"{sid}@{nav}"]},
            )

    report: dict[str, Any] = {
        "map": cfg.version,
        "classifier": cfg.classifier,
        "holdout": hd.version,
        "holdout_status": hd.status,
        "coverage": coverage,
        "excluded": excluded,
        "research_days": len(research_days),
        "holdout_days": len(holdout_days),
    }
    marker = out_dir / f"HOLDOUT-LOOK-{cfg.version}.json"
    if a.holdout_look:
        if marker.exists():
            raise SystemExit(f"the holdout was already read for {cfg.version}: {marker}")
        alt_labs = {
            mid: alt_labels(a.lake / "runs" / "regime" / "labels" / f"alt-{mc['version']}.csv")
            for mid, mc in alt_cfg.items()
        }
        return holdout_look(
            a, cfg, hd, trades, hold, holdout_days, caps, specs, meta, qty, register, marker, alt_labs, prev_day
        )

    # 1. per-regime tables
    tables: dict[str, Any] = {}
    for nav, ts in sorted(trades.items()):
        tables[nav] = {
            "in_sample": cell_table([t for t in ts if t.day < cfg.first_test_from]),
            "walk_forward": cell_table([t for t in ts if t.day >= cfg.first_test_from]),
        }
    report["tables"] = tables

    # 2. books
    books: dict[str, Any] = {}
    selections: dict[str, Any] = {}
    for nav, ts in sorted(trades.items()):
        n0 = float(nav)
        agn = [replace(t, cell="ALL") for t in ts]
        b_map, r_map, s_map = run_book(ts, cfg, research_days, n0, caps)
        b_agn, r_agn, s_agn = run_book(agn, cfg, research_days, n0, caps)
        b_ung, r_ung, _ = run_book(ts, cfg, research_days, n0, caps, mode="UNGATED")
        books[nav] = {"REGIME": b_map, "AGNOSTIC": b_agn, "UNGATED": b_ung}
        selections[nav] = {
            "REGIME": [
                {"window": [str(w.start), str(w.end)], "selected": sorted(f"{s}|{c}" for s, c in w.selected)}
                for w in s_map
            ],
            "AGNOSTIC": [
                {"window": [str(w.start), str(w.end)], "selected": sorted(s for s, _ in w.selected)} for w in s_agn
            ],
            "final_REGIME": {f"{k[0]}|{k[1]}": vars_(v) for k, v in final_selection(ts, cfg, cfg.research_to).items()},
        }
        for name, b in books[nav].items():
            register(
                f"BOOK-{name}",
                cfg.version,
                RunPurpose.WALK_FORWARD,
                {"nav": nav, "book": name},
                int(b["trades"]),
                {k: b[k] for k in ("net_cagr", "sharpe", "max_drawdown", "expectancy_r")},
            )
        books[nav]["_taken"] = {
            "REGIME": [taken_row(t) for t in r_map.taken],
            "AGNOSTIC": [taken_row(t) for t in r_agn.taken],
        }
        _ = r_ung
    report["books"] = books
    report["selections"] = selections

    # 3. alternative regime models
    alt: dict[str, Any] = {}
    daily_rv = {}
    with (a.lake / "runs" / "regime" / "labels" / "alt-daily.csv").open() as fh:
        for r in csv.DictReader(fh):
            if r["rv_oc"]:
                daily_rv[date.fromisoformat(r["day"])] = float(r["rv_oc"])
    for mid, mc in alt_cfg.items():
        lab = alt_labels(a.lake / "runs" / "regime" / "labels" / f"alt-{mc['version']}.csv")
        fits_path = a.lake / "runs" / "regime" / "labels" / "alt-models-fits.json"
        valid_from = cfg.first_test_from
        if mc["kind"] == "gaussian_hmm":
            fits = json.loads(fits_path.read_text())["fits"]
            valid_from = date.fromisoformat(fits[0]["month"]) if fits else cfg.research_to
        wins = [w for w in walk_forward_windows(cfg) if w[0] >= valid_from]
        res_m: dict[str, Any] = {"valid_from": str(valid_from), "windows": [str(wins[0][0]), str(wins[-1][1])]}
        # (b) information: next session's RV by state, research days in the test period
        by: dict[str, list[tuple[date, float]]] = defaultdict(list)
        next_day = {p: d for d, p in prev_day.items()}
        for d in research_days:
            # the first full session after the label is known: the same session for a pre-open label (H26), the
            # next one for a 09:30 label (H25)
            nxt = d if d in lab and lab[d][1] <= time(9, 15) else next_day.get(d)
            if d >= wins[0][0] and d in lab and nxt is not None and nxt in daily_rv:
                by[lab[d][0]].append((d, daily_rv[nxt]))
        lo_l, hi_l = mc["labels"][0], mc["labels"][-1]
        info = mean_diff_ci(by[hi_l], by[lo_l], level=0.9, iterations=2000, seed=cfg.seed)
        res_m["forward_rv_by_state"] = {
            k: {"n": len(v), "mean": round(sum(x for _, x in v) / len(v), 4)} for k, v in sorted(by.items()) if v
        }
        res_m["info_diff_hi_minus_lo"] = None if info is None else [round(x, 4) for x in info]
        info_pass = info is not None and (info[1] > 0 or info[2] < 0)
        for nav, ts in sorted(trades.items()):
            n0 = float(nav)
            mt = []
            for t in ts:
                c = alt_cell(t, lab, prev_day)
                if c is not None:
                    mt.append(replace(t, cell=c))
            b_alt, r_alt, s_alt = run_book(mt, cfg, research_days, n0, caps, windows=wins)
            b_k11, r_k11, _ = run_book(ts, cfg, research_days, n0, caps, windows=wins)
            b_ung, r_ung, _ = run_book(ts, cfg, research_days, n0, caps, windows=wins, mode="UNGATED")

            def rr(res: Any) -> list[tuple[date, float]]:
                return [(t.day, t.r) for t in res.taken]

            vs_k11 = mean_diff_ci(rr(r_alt), rr(r_k11), level=0.9, iterations=2000, seed=cfg.seed)
            vs_ung = mean_diff_ci(rr(r_alt), rr(r_ung), level=0.9, iterations=2000, seed=cfg.seed)
            econ = vs_k11 is not None and vs_ung is not None and vs_k11[1] > 0 and vs_ung[1] > 0
            res_m[nav] = {
                "book": b_alt,
                "k11_same_windows": b_k11,
                "ungated_same_windows": b_ung,
                "diff_r_vs_k11": None if vs_k11 is None else [round(x, 4) for x in vs_k11],
                "diff_r_vs_ungated": None if vs_ung is None else [round(x, 4) for x in vs_ung],
                "economic_pass": econ,
                "labelled_trades": len(mt),
                "table_wf": cell_table([t for t in mt if t.day >= wins[0][0]]),
                "selected": [
                    {"window": [str(w.start), str(w.end)], "selected": sorted(f"{s}|{c}" for s, c in w.selected)}
                    for w in s_alt
                ],
            }
        res_m["information_pass"] = info_pass
        res_m["verdict"] = "PASS" if info_pass and all(res_m[n]["economic_pass"] for n in trades) else "FAIL"
        alt[mid] = res_m
        register(
            mid,
            mc["version"],
            RunPurpose.OUT_OF_SAMPLE,
            {"map": cfg.version, "kind": mc["kind"]},
            sum(int(res_m[n]["book"]["trades"]) for n in trades),
            {
                "verdict": res_m["verdict"],
                "info": res_m["info_diff_hi_minus_lo"],
                **{f"vs_k11_{n}": res_m[n]["diff_r_vs_k11"] for n in trades},
            },
        )
    report["alt_models"] = alt

    # 4. gates per pair
    pairs: list[tuple[str, str, str, list[BookTrade]]] = []
    for nav, ts in sorted(trades.items()):
        g: dict[tuple[str, str], list[BookTrade]] = defaultdict(list)
        for t in ts:
            g[(t.strategy, t.cell)].append(t)
            g[(t.strategy, "ALL")].append(t)
        pairs += [(nav, s, c, v) for (s, c), v in sorted(g.items())]
    base_trials = reg.total_trials() if reg is not None else 0
    n_trials = base_trials + len(pairs)
    gate_rows = []
    for nav, sid, cell, ts in pairs:
        m = meta[(sid, nav)]
        vt = [val_trade(t, qty.get((sid, t.signal_at.isoformat()), 0), t.day >= cfg.first_test_from) for t in ts]
        inp = ValidationInputs(
            spec=specs[sid],
            trades=vt,
            nav=Decimal(nav),
            data_label="REAL",
            n_trials=n_trials,
            lookahead_stamps=m["stamps"],
            universe_point_in_time=True,
            fills_dated_costs=m["fills"],
            fill_model=m["fill_model"],
            holdout_prior_looks=hd.prior_looks.get(sid, 0),
            event_certified=False,
        )
        rep = validate(inp, gcfg)
        st = {gr.code: gr.status.value for gr in rep.gates}
        fails = [c for c, s in st.items() if s == "FAIL"]
        oos = [t for t in vt if t.oos]
        row = {
            "nav": nav,
            "strategy": sid,
            "cell": cell,
            "verdict": rep.verdict.value,
            "gates": st,
            "fails": fails,
            "oos_trades": len(oos),
            "oos_net": round(sum(float(t.net_pnl) for t in oos), 2),
            "oos_exp_r": round(sum(float(t.r_multiple) for t in oos) / len(oos), 4) if oos else None,
            "survivor": not [c for c in fails if c != "V16"] and len(oos) > 0,
            "metrics": {gr.code: gr.metrics for gr in rep.gates if gr.metrics},
        }
        gate_rows.append(row)
        register(
            sid,
            m["spec_version"],
            RunPurpose.WALK_FORWARD,
            {"map": cfg.version, "nav": nav, "cell": cell},
            len(oos),
            {"verdict": rep.verdict.value, "fails": ",".join(fails), "oos_net": row["oos_net"]},
        )
    report["gates"] = {"n_trials": n_trials, "pairs": gate_rows}
    report["shortlist"] = [r for r in gate_rows if r["survivor"]]
    (out_dir / f"{cfg.version}.json").write_text(json.dumps(report, indent=1, default=str))
    print(
        json.dumps(
            {
                "books": {n: {k: v for k, v in b.items() if k != "_taken"} for n, b in books.items()},
                "alt": {k: {"verdict": v["verdict"], "info": v["info_diff_hi_minus_lo"]} for k, v in alt.items()},
                "shortlist": [(r["nav"], r["strategy"], r["cell"]) for r in report["shortlist"]],
                "n_trials": n_trials,
            },
            indent=1,
            default=str,
        )
    )
    return 0


def standalone(
    a: Any,
    cfg: RegimeMapConfig,
    trades: dict[str, list[BookTrade]],
    days: list[date],
    caps: dict[str, int],
    register: Any,
    out_dir: Path,
) -> int:
    """Diagnostic: each strategy alone through the same book (caps, limits, latch) on the walk-forward windows, all
    eligible trades (no selection), so its stand-alone CAGR/Sharpe/Sortino/DD/Calmar can be read. Registered."""
    out: dict[str, Any] = {}
    for nav, ts in sorted(trades.items()):
        for sid in sorted({t.strategy for t in ts}):
            m, _, _ = run_book([t for t in ts if t.strategy == sid], cfg, days, float(nav), caps, mode="UNGATED")
            out[f"{sid}@{nav}"] = m
            register(
                f"BOOK-STANDALONE-{sid}",
                cfg.version,
                RunPurpose.WALK_FORWARD,
                {"nav": nav},
                int(m["trades"]),
                {k: m[k] for k in ("net_cagr", "sharpe", "max_drawdown", "expectancy_r")},
            )
    (out_dir / f"{cfg.version}-standalone.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))
    return 0


def vars_(v: Any) -> dict[str, Any]:
    return {
        "n": v.n,
        "mean": round(v.mean, 2),
        "ci_lower": round(v.ci_lower, 2),
        "mean_r": round(v.mean_r, 4),
        "selected": v.selected,
    }


def taken_row(t: BookTrade) -> list[Any]:
    return [t.strategy, t.cell, str(t.day), t.entry_ts.isoformat(), round(t.net_pnl, 2), round(t.r, 3)]


def holdout_look(
    a: Any,
    cfg: RegimeMapConfig,
    hd: Any,
    trades: dict[str, list[BookTrade]],
    hold: dict[str, list[BookTrade]],
    holdout_days: list[date],
    caps: dict[str, int],
    specs: dict[str, StrategySpec],
    meta: dict[tuple[str, str], dict[str, Any]],
    qty: dict[tuple[str, str], int],
    register: Any,
    marker: Path,
    alt_labs: dict[str, dict[date, tuple[str, time]]],
    prev_day: dict[date, date],
) -> int:
    """The single holdout evaluation: the maps refitted on all research data, traded on every holdout day."""
    prior = json.loads((a.lake / "runs" / "regime" / "portfolio" / f"{cfg.version}.json").read_text())
    short = {(r["nav"], r["strategy"], r["cell"]) for r in prior["shortlist"]}
    out: dict[str, Any] = {
        "map": cfg.version,
        "holdout": hd.version,
        "holdout_status": hd.status,
        "read_at": datetime.now().isoformat(timespec="seconds"),
        "shortlist": sorted(short),
    }
    recent = [d for d in holdout_days if hd.recent_from <= d <= hd.recent_to]
    for nav in sorted(hold):
        n0 = float(nav)
        ht = sorted(hold[nav], key=lambda t: t.entry_ts)
        sel_map = {k for k, v in final_selection(trades[nav], cfg, cfg.research_to).items() if v.selected}
        agn_r = [replace(t, cell="ALL") for t in trades[nav]]
        fin_agn = final_selection(agn_r, cfg, cfg.research_to)
        sel_agn = {k[0] for k, v in fin_agn.items() if v.selected}
        pri_map = {k: v.mean_r for k, v in final_selection(trades[nav], cfg, cfg.research_to).items()}
        pri_agn = {k[0]: v.mean_r for k, v in fin_agn.items()}
        res: dict[str, Any] = {
            "final_regime_map": sorted(f"{s}|{c}" for s, c in sel_map),
            "final_agnostic": sorted(sel_agn),
        }
        alt_sel: dict[str, tuple[dict[tuple[str, str], float], dict[str, dict[date, tuple[str, time]]]]] = {}
        for mid, lab in alt_labs.items():
            at = []
            for t in trades[nav]:
                c = alt_cell(t, lab, prev_day)
                if c is not None:
                    at.append(replace(t, cell=c))
            fin = final_selection(at, cfg, cfg.research_to)
            alt_sel[mid] = ({k: v.mean_r for k, v in fin.items() if v.selected}, {mid: lab})
            res[f"final_{mid}"] = sorted(f"{s}|{c}" for s, c in alt_sel[mid][0])
        for scope, days in (("all_holdout", holdout_days), ("recent_block", recent)):
            dset = set(days)
            hs = [t for t in ht if t.day in dset]
            for name, cands in (
                ("REGIME", [(t, pri_map[(t.strategy, t.cell)]) for t in hs if (t.strategy, t.cell) in sel_map]),
                ("AGNOSTIC", [(t, pri_agn[t.strategy]) for t in hs if t.strategy in sel_agn]),
                ("UNGATED", [(t, 0.0) for t in hs]),
                *[
                    (
                        f"ALT-{mid}",
                        [
                            (replace(t, cell=c), sel[(t.strategy, c)])
                            for t in hs
                            if (c := alt_cell(t, labs[mid], prev_day)) is not None and (t.strategy, c) in sel
                        ],
                    )
                    for mid, (sel, labs) in alt_sel.items()
                ],
            ):
                m = book_metrics(simulate_book(cands, nav0=n0, days=days, caps=caps, cfg=cfg))
                res[f"{scope}:{name}"] = m
        # the registry allows ONE holdout evaluation per (id, version): one registration per book and NAV, with the
        # recent block (a slice of the same look) in its metrics
        for name in sorted({k.split(":", 1)[1] for k in res if k.startswith("all_holdout:")}):
            mets = {f"{sc}:{k}": res[f"{sc}:{name}"][k] for sc in ("all_holdout", "recent_block")
                    for k in ("trades", "net_cagr", "sharpe", "max_drawdown", "expectancy_r")}  # fmt: skip
            register(f"BOOK-{name}", f"{cfg.version}@{nav}", RunPurpose.HOLDOUT, {"nav": nav},
                     int(res[f"all_holdout:{name}"]["trades"]), mets)  # fmt: skip
        res["shortlist_v16"] = []
        for n2, sid, cell in sorted(short):
            if n2 != nav:
                continue
            hs = [t for t in ht if t.strategy == sid and (cell == "ALL" or t.cell == cell)]
            mean = sum(t.net_pnl for t in hs) / len(hs) if hs else None
            res["shortlist_v16"].append(
                {"strategy": sid, "cell": cell, "holdout_trades": len(hs), "holdout_mean": mean}
            )
            register(
                sid,
                meta[(sid, nav)]["spec_version"],
                RunPurpose.HOLDOUT,
                {"nav": nav, "cell": cell},
                len(hs),
                {"holdout_mean": mean},
            )
        out[nav] = res
    marker.write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
