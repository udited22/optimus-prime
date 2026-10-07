"""Cost-drag study (docs/risk/system-economics.md, OD-012, OD-014): a zero-edge baseline of what costs take out of a
month by NAV and
entries a day, run through the real allocator, Risk Governor, kernel runtime, fake broker and cost model.

    .venv/bin/python scripts/cost_drag_study.py --workers 8 --out docs/research/cost-drag-results.md
    .venv/bin/python scripts/cost_drag_study.py --quick          # a tiny grid (smoke run)

SIMULATED / SYNTHETIC / ASSUMED throughout. The P&L is a zero-edge baseline, not a forecast.
"""

from __future__ import annotations

import argparse
import time
from decimal import Decimal
from pathlib import Path

from project100c.studies.cost_drag import StudyConfig, load_economics, render_markdown, run_study

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> str:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--seeds", type=int, default=8)
    ap.add_argument("--sessions", type=int, default=22)
    ap.add_argument("--quick", action="store_true", help="1 seed, 2 sessions, NAV 1L/2L, 1 and 10 entries a day")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    cfg = StudyConfig(ROOT / "configs", ROOT / "specs", seeds=tuple(range(1, a.seeds + 1)), sessions=a.sessions,
                      workers=a.workers)  # fmt: skip
    if a.quick:
        cfg = StudyConfig(cfg.configs, cfg.specs, navs=(Decimal(100_000), Decimal(200_000)), ks=(1, 10), seeds=(1,),
                          sessions=2, workers=1)  # fmt: skip
    t0 = time.monotonic()
    cells = run_study(cfg)
    md = render_markdown(cells, cfg, load_economics(cfg.configs))
    md += f"\n_Run time {time.monotonic() - t0:.0f}s._\n"
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(md, encoding="utf-8")
    print(md)
    return md


if __name__ == "__main__":
    main()
