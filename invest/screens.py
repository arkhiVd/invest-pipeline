"""Prefilter screen engines over stored fundamentals/prices (T3.3).

Thresholds live in config/screens.json: original July oracles plus the user's
later exact predicates. Explicit `>`/`<` wording always wins over shorthand
range labels.

Semantics:
- Inputs per symbol: the full active NSE EQ universe, latest official-XBRL
  fundamentals, normalized BharatStock market snapshots, and official
  bhavcopy prices.
- Derived fields use MAX(high), a complete 50-session DMA, direct EPS
  comparison, and positive-earnings PEG.
- `gt`/`lt` are strict and may coexist in one condition; `between` is
  inclusive only where the user wrote <=/>=; percentage ops receive decimal
  fractions.
- Missing inputs FAIL the condition and count toward the screen's gap
  report — never a silent pass.

Frozen-oracle honesty: exact July result sets are unreproducible post hoc
(prices and fundamentals have moved six weeks); validation is semantic —
golden fixtures pin every threshold boundary (tests), and the live run
reports counts against oracle magnitude.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from invest import db
from invest.nse_filings import DEFAULT_DB

log = logging.getLogger("invest.screens")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config/screens.json"

COMPUTED_SOURCE = "nse_xbrl_computed"
MARKET_SOURCE = "bharatstock_v1"


def load_config(path: str | Path = DEFAULT_CONFIG) -> dict:
    cfg = json.loads(Path(path).read_text())
    known_ops = {"gt_pct", "lt_pct", "lt", "gt", "between", "eq"}
    for screen_id, screen in cfg.get("screens", {}).items():
        for field, condition in screen.get("conditions", {}).items():
            ops = {key for key in condition if not key.startswith("_")}
            if not ops or not ops <= known_ops or ("between" in ops and len(ops) != 1):
                raise ValueError(f"invalid condition {screen_id}.{field}: {condition}")
    return cfg


def _latest_rows(conn, source: str, *, cutoff: date | None = None) -> dict[str, dict]:
    cols = [
        "symbol",
        "as_of",
        "market_cap_cr",
        "pe_ratio",
        "pb_ratio",
        "roe",
        "roce",
        "operating_margin",
        "revenue_growth_yoy",
        "profit_growth_yoy",
        "eps_growth_yoy",
        "debt_to_equity",
        "promoter_pledged",
        "avg_roe_3y",
        "avg_roe_5y",
        "avg_roce_3y",
        "avg_roce_5y",
        "revenue_cagr_3y",
        "profit_cagr_3y",
        "eps_cagr_3y",
        "current_ratio",
        "free_cash_flow",
        "free_cash_flow_3y",
        "eps",
        "eps_previous",
        "piotroski_score",
        "interest_coverage",
        "dividend_yield",
        "promoter_holding",
        "fii_holding",
    ]
    rows = conn.execute(
        f"""
        SELECT {", ".join(cols)} FROM stock_fundamentals f
        WHERE source = ?
          AND (?::DATE IS NULL OR as_of <= ?::DATE)
          AND as_of = (SELECT MAX(as_of) FROM stock_fundamentals g
                       WHERE g.symbol = f.symbol AND g.source = f.source
                         AND (?::DATE IS NULL OR g.as_of <= ?::DATE))
        """,
        [source, cutoff, cutoff, cutoff, cutoff],
    ).fetchall()
    return {r[0]: dict(zip(cols, r, strict=True)) for r in rows}


def price_stats(conn, *, cutoff: date | None = None) -> dict[str, dict]:
    """Latest close, daily-high maxima, and complete 50-session DMA."""
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT symbol, close, high, trade_date,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rk,
                   MAX(trade_date) OVER (PARTITION BY symbol) AS maxd
            FROM stock_price
            WHERE ?::DATE IS NULL OR trade_date <= ?::DATE
        )
        SELECT symbol,
             MAX(CASE WHEN rk = 1 THEN close END),
             MAX(high) FILTER (WHERE trade_date >= maxd - INTERVAL 365 DAY),
             MAX(high),
             CASE WHEN COUNT(close) FILTER (WHERE rk <= 50) = 50
                  THEN AVG(close) FILTER (WHERE rk <= 50) END
        FROM ranked
        GROUP BY symbol
        """,
        [cutoff, cutoff],
    ).fetchall()
    return {
        sym: {
            "close": close,
            "high_52w": high_52w,
            "max_loaded_high": max_loaded_high,
            "dma_50": dma_50,
        }
        for sym, close, high_52w, max_loaded_high, dma_50 in rows
        if close is not None
    }


def build_universe(
    conn, *, cutoff: date | None = None, universe_source: str | None = None
) -> dict[str, dict]:
    """Left-join every active NSE EQ symbol; absent or stale facts become gaps."""
    computed = _latest_rows(conn, COMPUTED_SOURCE, cutoff=cutoff)
    periods = Counter(row["as_of"] for row in computed.values() if row.get("as_of") is not None)
    # The modal latest period is robust to one malformed future-dated filing.
    # Ties prefer the newer period.
    current_period = max(periods, key=lambda period: (periods[period], period), default=None)
    market = _latest_rows(conn, MARKET_SOURCE, cutoff=cutoff)
    prices = price_stats(conn, cutoff=cutoff)
    symbols = [
        row[0]
        for row in conn.execute(
            """
            SELECT symbol FROM stock_universe
            WHERE series = 'EQ' AND COALESCE(is_active, TRUE)
              AND (? IS NULL OR (source = ?
                  AND CAST(fetched_at AS DATE) <= ?::DATE))
            ORDER BY symbol
            """,
            [universe_source, universe_source, cutoff],
        ).fetchall()
    ]
    market_primary = ("market_cap_cr", "pe_ratio", "pb_ratio", "dividend_yield")
    market_fallback = (
        "current_ratio",
        "interest_coverage",
        "promoter_holding",
        "fii_holding",
    )
    universe: dict[str, dict] = {}
    for symbol in symbols:
        official = computed.get(symbol)
        # A prior fiscal period must not masquerade as current fundamentals
        # merely because it is the newest row available for this symbol.
        # Keep its date as evidence, but null the stale computed metrics so
        # every dependent predicate fails closed and appears in gap counts.
        if official is not None and official.get("as_of") == current_period:
            row = dict(official)
            row["fundamentals_stale"] = False
        else:
            row = {
                "symbol": symbol,
                "as_of": official.get("as_of") if official else None,
                "fundamentals_stale": official is not None,
            }
        mk = market.get(symbol, {})
        for field in market_primary:
            row[field] = mk.get(field)
        for field in market_fallback:
            if row.get(field) is None:
                row[field] = mk.get(field)
        pr = prices.get(symbol, {})
        close, hi, dma = pr.get("close"), pr.get("high_52w"), pr.get("dma_50")
        loaded_high = pr.get("max_loaded_high")
        row.update(close=close, high_52w=hi, dma_50=dma)
        row["price_to_52w_high"] = (
            close / hi if close is not None and hi is not None and hi > 0 else None
        )
        row["price_to_loaded_high"] = (
            close / loaded_high
            if close is not None and loaded_high is not None and loaded_high > 0
            else None
        )
        # Do not mislabel the Jan-2025+ loaded maximum as all-time high.
        row["price_to_all_time_high"] = None
        row["price_above_50dma"] = close > dma if close is not None and dma is not None else None
        eps, previous_eps = row.get("eps"), row.get("eps_previous")
        row["eps_increased"] = (
            eps > previous_eps if eps is not None and previous_eps is not None else None
        )
        pe, eps_cagr = row.get("pe_ratio"), row.get("eps_cagr_3y")
        row["peg"] = (
            pe / (eps_cagr * 100.0)
            if pe is not None and pe > 0 and eps_cagr is not None and eps_cagr > 0
            else None
        )
        universe[symbol] = row
    return universe


def _check(condition: dict, value) -> bool | None:
    """True/False verdict; None when the input needed is absent.

    Ops mirror the oracle wording: 'lt'/'gt' are STRICT (<x / >x);
    'between' is inclusive ("P/E 7-25"); '*_pct' variants compare a
    decimal-fraction field against a percentage threshold.
    """
    if value is None:
        return None
    verdicts: list[bool] = []
    if "gt_pct" in condition:
        verdicts.append(value * 100.0 > condition["gt_pct"])
    if "lt_pct" in condition:
        verdicts.append(value * 100.0 < condition["lt_pct"])
    if "lt" in condition:
        verdicts.append(value < condition["lt"])
    if "gt" in condition:
        verdicts.append(value > condition["gt"])
    if "between" in condition:
        low, high = condition["between"]
        verdicts.append(low <= value <= high)
    if "eq" in condition:
        expected = condition["eq"]
        verdicts.append(
            bool(value) == expected if isinstance(expected, bool) else value == expected
        )
    if not verdicts:
        raise ValueError(f"condition without known op: {condition}")
    return all(verdicts)


def evaluate_screen(universe: dict[str, dict], conditions: dict) -> dict:
    survivors: list[dict] = []
    gaps: dict[str, int] = {}
    for symbol, row in universe.items():
        failed, missing = [], []
        for field, condition in conditions.items():
            verdict = _check(condition, row.get(field))
            if verdict is None:
                missing.append(field)
            elif not verdict:
                failed.append(field)
        for field in missing:
            gaps[field] = gaps.get(field, 0) + 1
        if not failed and not missing:
            survivors.append(
                {
                    "symbol": symbol,
                    **{k: row.get(k) for k in ("market_cap_cr", "pe_ratio", "roe", "roce")},
                }
            )
    return {
        "survivors": sorted(survivors, key=lambda s: s["symbol"]),
        "gaps": gaps,
        "evaluated": len(universe),
    }


def run_screen(conn, screen_id: str, cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    screens = cfg["screens"]
    if screen_id not in screens:
        raise ValueError(f"unknown screen {screen_id!r}; known: {sorted(screens)}")
    result = evaluate_screen(build_universe(conn), screens[screen_id]["conditions"])
    declared_gaps = {
        field for field, cond in screens[screen_id]["conditions"].items() if cond.get("_gap")
    }
    result["declared_gaps"] = sorted(declared_gaps)
    return result


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="invest-screens")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--screen", help="run one screen (default: all)")
    args = parser.parse_args(argv)

    cfg = load_config()
    ids = [args.screen] if args.screen else sorted(cfg["screens"])
    conn = db.connect(args.db)
    try:
        db.init_schema(conn)
        universe = build_universe(conn)
        for screen_id in ids:
            if screen_id not in cfg["screens"]:
                raise ValueError(f"unknown screen {screen_id!r}")
            conditions = cfg["screens"][screen_id]["conditions"]
            result = evaluate_screen(universe, conditions)
            print(
                f"{screen_id}: evaluated={result['evaluated']} "
                f"survivors={len(result['survivors'])} "
                f"data-gap-failed={sum(result['gaps'].values())}"
            )
            for field, count in sorted(result["gaps"].items()):
                print(f"    gap {field}: {count} symbols lacked input")
            for s in result["survivors"]:
                print(
                    f"    {s['symbol']:<12} mcap={s['market_cap_cr']} "
                    f"pe={s['pe_ratio']} roe={s['roe']} roce={s['roce']}"
                )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
