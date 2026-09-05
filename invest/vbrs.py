"""Valuation-band risk-scaling cash-position model.

The public example uses a versioned median PE, base cash fraction, and
sensitivity. Calculations retain full precision. Informational valuation zones
do not alter the formula.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from invest import db, pe

log = logging.getLogger("invest.vbrs")

DEFAULT_CONFIG = {
    "investment_amount": 20000,
    "base_cash_pct": 0.05,
    "sensitivity": 0.30,
    "median_pe": 23.4,
    "cheap_below": 20.0,
    "expensive_above": 28.0,
}

WEIGHTS = [  # public example allocation buckets
    ("Core Holdings", 0.50),
    ("Tactical Allocation", 0.30),
    ("Cyclical - Counter", 0.16),
]


def load_config(path: str | None = None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    p = Path(path) if path else Path("config/vbrs.json")
    if p.exists():
        cfg.update(json.loads(p.read_text()))
    return cfg


def cash_position(pe_today: float, pe_median: float, cfg: dict | None = None) -> float:
    """Compute the configured cash fraction."""
    cfg = cfg or DEFAULT_CONFIG
    return cfg["base_cash_pct"] + ((pe_today / pe_median) - 1) * cfg["sensitivity"]


def allocate(amount: float, cash_pct: float) -> list[tuple[str, float, float]]:
    """[(name, weight, amount)] — fixed weights + formula-driven cash bucket."""
    rows = [(name, w, amount * w) for name, w in WEIGHTS]
    rows.append(("Cash", cash_pct, amount * cash_pct))
    return rows


def zone(pe_today: float, cfg: dict) -> str:
    """Informational bands from the original research research note (not part of formula)."""
    if pe_today < cfg["cheap_below"]:
        return "Cheap"
    if pe_today > cfg["expensive_above"]:
        return "Expensive"
    return "Base"


def main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="invest-vbrs")
    parser.add_argument("--db", default="data/invest.duckdb")
    parser.add_argument("--config", default="config/vbrs.json")
    parser.add_argument(
        "--pe", type=float, default=None, help="override PE (else latest nifty_pe row)"
    )
    parser.add_argument("--amount", type=float, default=None)
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    conn = db.connect(args.db)
    db.init_schema(conn)

    if args.pe is not None:
        pe_today, src = args.pe, "cli-override"
    else:
        row = pe.latest(conn)
        if row is None or row[1] is None:
            print("no PE data yet; run `python -m invest.pe fetch` first", file=sys.stderr)
            return 2
        pe_today, src = row[1], f"{row[0]} ({row[4] and 'nse'})"
        src = f"nifty_pe {row[0]}"

    cash_pct = cash_position(pe_today, float(cfg["median_pe"]), cfg)
    amount = args.amount if args.amount is not None else float(cfg["investment_amount"])

    print(
        f"VBRS position — PE {pe_today} ({src}) vs median {cfg['median_pe']} "
        f"[{zone(pe_today, cfg)} zone]"
    )
    print(
        f"cash = {cfg['base_cash_pct']:.0%} + "
        f"({pe_today}/{cfg['median_pe']} - 1) x {cfg['sensitivity']:.0%} "
        f"= {cash_pct:.4%}"
    )
    print(f"\n{'Bucket':<24} {'Weight':>7} {'Amount':>10}")
    total_w = 0.0
    for name, weight, amt in allocate(amount, cash_pct):
        total_w += weight
        print(f"{name:<24} {weight:>7.2%} {amt:>10,.0f}")
    print(f"{'TOTAL':<24} {total_w:>7.2%} {amount * total_w:>10,.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
