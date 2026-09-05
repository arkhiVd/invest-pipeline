"""Read-only Phase 10 coverage and cohort diagnostics. No score is produced."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import duckdb
import pandas as pd

from invest import screens

DEFAULT_DB = Path("data/invest.duckdb")


@dataclass(frozen=True)
class Metric:
    component: str
    field: str
    unit: str
    direction: str


METRICS = (
    Metric("valuation", "pe_ratio", "multiple", "lower"),
    Metric("valuation", "pb_ratio", "multiple", "lower"),
    Metric("valuation", "peg", "multiple", "lower"),
    Metric("valuation", "dividend_yield", "fraction", "higher"),
    Metric("quality", "roe", "fraction", "higher"),
    Metric("quality", "roce", "fraction", "higher"),
    Metric("quality", "operating_margin", "fraction", "higher"),
    Metric("quality", "free_cash_flow_3y", "INR absolute", "higher"),
    Metric("financial_strength", "debt_to_equity", "ratio", "lower"),
    Metric("financial_strength", "interest_coverage", "ratio", "higher"),
    Metric("financial_strength", "current_ratio", "ratio", "higher"),
    Metric("financial_strength", "piotroski_score", "integer 0-9", "higher"),
    Metric("momentum", "price_to_52w_high", "ratio", "higher"),
    Metric("momentum", "price_above_50dma", "boolean", "higher"),
    Metric("momentum", "revenue_growth_yoy", "fraction", "higher"),
    Metric("momentum", "profit_growth_yoy", "fraction", "higher"),
    Metric("momentum", "eps_growth_yoy", "fraction", "higher"),
)


def _frame(conn) -> tuple[pd.DataFrame, dict[str, set[str]], dict[str, set[str]]]:
    universe = screens.build_universe(conn)
    frame = pd.DataFrame(universe.values())
    sectors = conn.execute(
        """
        SELECT symbol, sector FROM stock_fundamentals f
        WHERE source=? AND as_of=(SELECT max(as_of) FROM stock_fundamentals g
          WHERE g.symbol=f.symbol AND g.source=f.source)
        """,
        [screens.MARKET_SOURCE],
    ).fetchall()
    sector_map = dict(sectors)
    frame["sector"] = frame["symbol"].map(sector_map).fillna("UNKNOWN")
    config = screens.load_config()
    survivors = {
        screen_id: {
            item["symbol"]
            for item in screens.evaluate_screen(universe, definition["conditions"])["survivors"]
        }
        for screen_id, definition in config["screens"].items()
    }
    eligible = {
        screen_id: {
            symbol
            for symbol, row in universe.items()
            if not row.get("fundamentals_stale")
            and all(row.get(field) is not None for field in definition["conditions"])
        }
        for screen_id, definition in config["screens"].items()
    }
    return frame, eligible, survivors


def percentile_order(values: dict[str, float], *, higher: bool) -> list[str]:
    return [
        symbol
        for symbol, _value in sorted(
            values.items(), key=lambda item: ((-item[1]) if higher else item[1], item[0])
        )
    ]


def one_member_instability(values: dict[str, float], *, higher: bool) -> dict[str, float | int]:
    """Measure maximum rank movement after removing one cohort member."""
    baseline = percentile_order(values, higher=higher)
    if len(baseline) < 3:
        return {"cohort_size": len(baseline), "max_rank_move": 0, "max_percentile_move": 0.0}
    base_rank = {symbol: rank for rank, symbol in enumerate(baseline)}
    max_rank = 0
    max_percentile = 0.0
    for removed in baseline:
        reduced = percentile_order(
            {symbol: value for symbol, value in values.items() if symbol != removed}, higher=higher
        )
        for rank, symbol in enumerate(reduced):
            max_rank = max(max_rank, abs(rank - base_rank[symbol]))
            before = base_rank[symbol] / (len(baseline) - 1)
            after = rank / (len(reduced) - 1)
            max_percentile = max(max_percentile, abs(after - before))
    return {
        "cohort_size": len(baseline),
        "max_rank_move": max_rank,
        "max_percentile_move": round(max_percentile, 6),
    }


def analyze(conn) -> dict:
    frame, eligible, survivors = _frame(conn)
    cohorts: dict[str, set[str]] = {"active_eq": set(frame["symbol"])}
    cohorts.update({f"screen_eligible:{name}": symbols for name, symbols in eligible.items()})
    cohorts.update({f"screen_survivor:{name}": symbols for name, symbols in survivors.items()})
    cohorts["survivors:union"] = set().union(*survivors.values()) if survivors else set()
    for sector, group in frame.groupby("sector"):
        if sector != "UNKNOWN":
            cohorts[f"sector:{sector}"] = set(group["symbol"])

    coverage = []
    for cohort, symbols in sorted(cohorts.items()):
        selected = frame[frame["symbol"].isin(symbols)]
        for metric in METRICS:
            present = int(selected[metric.field].notna().sum()) if metric.field in selected else 0
            coverage.append(
                {
                    "cohort": cohort,
                    "size": len(selected),
                    **asdict(metric),
                    "present": present,
                    "coverage": round(present / len(selected), 6) if len(selected) else 0.0,
                }
            )

    numeric = [m.field for m in METRICS if m.field in frame and m.unit != "boolean"]
    correlation = frame[numeric].corr(method="spearman", min_periods=5)
    pairs = []
    for left_index, left in enumerate(numeric):
        for right in numeric[left_index + 1 :]:
            value = correlation.loc[left, right]
            if pd.notna(value) and abs(value) >= 0.5:
                pairs.append({"left": left, "right": right, "spearman": round(float(value), 6)})

    union = cohorts["survivors:union"]
    stability = []
    for metric in METRICS:
        if metric.unit == "boolean":
            continue
        values = frame[frame["symbol"].isin(union)].set_index("symbol")[metric.field].dropna()
        diagnostic = one_member_instability(
            {str(symbol): float(value) for symbol, value in values.items()},
            higher=metric.direction == "higher",
        )
        stability.append({"field": metric.field, **diagnostic})

    sector_sizes = {
        name.removeprefix("sector:"): len(symbols)
        for name, symbols in cohorts.items()
        if name.startswith("sector:")
    }
    return {
        "metrics": [asdict(metric) for metric in METRICS],
        "cohort_sizes": {name: len(symbols) for name, symbols in cohorts.items()},
        "coverage": coverage,
        "high_correlation_pairs": pairs,
        "survivor_stability": stability,
        "sector_sizes": sector_sizes,
        "unknown_sector_count": int((frame["sector"] == "UNKNOWN").sum()),
    }


def main() -> int:
    conn = duckdb.connect(str(DEFAULT_DB), read_only=True)
    try:
        print(json.dumps(analyze(conn), sort_keys=True))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
