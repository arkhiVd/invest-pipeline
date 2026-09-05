"""Rolling return, volatility, beta, Sharpe, and capture-ratio engine.

Methodology `m2026.1` uses the last NAV of each calendar month. Rolling return
uses a declared-year exponent. Volatility is monthly sample deviation scaled by
sqrt(12). Beta and capture ratios use aligned benchmark months. Sharpe subtracts
the configured annual risk-free rate.

Insufficient history creates no metric row. Benchmark overlap gaps preserve
return and volatility while leaving beta and capture values null with an
explicit note. Category statistics require the configured minimum peer count.
"""

from __future__ import annotations

import calendar
import json
import logging
import math
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC
from datetime import date as date_cls
from datetime import datetime as dt
from pathlib import Path

from invest import db

log = logging.getLogger("invest.metrics")

DEFAULT_CONFIG = {
    "methodology_version": "m2026.1",
    "frequency": "monthly",
    "risk_free_rate": 0.07,
    "lookbacks": ["3Y", "1Y"],
    "min_category_peers": 2,
    "benchmark": {
        "scheme_code": 900002,
        "label": "Synthetic Index Fund proxy",
    },
}

RESULT_OUT = "Outperformer"
RESULT_UNDER = "Underperformer"
RESULT_PAR = "At Category"


def load_config(path: str | None = None) -> dict:
    """Defaults merged with config/metrics.json when present."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if path and Path(path).exists():
        cfg.update(json.loads(Path(path).read_text()))
    return cfg


def parse_lookback(label: str) -> int:
    m = re.fullmatch(r"(\d+)Y", label)
    if not m:
        msg = f"unsupported lookback label: {label!r} (expected e.g. '3Y')"
        raise ValueError(msg)
    return int(m.group(1))


def shift_years(d: date_cls, years: int) -> date_cls:
    try:
        return d.replace(year=d.year - years)
    except ValueError:  # Feb-29 -> Feb-28
        return d.replace(year=d.year - years, day=28)


def month_end_date(key: tuple[int, int]) -> date_cls:
    y, m = key
    return date_cls(y, m, calendar.monthrange(y, m)[1])


def category_key(raw: str | None) -> str | None:
    """Normalize AMFI category_sub: 'Equity Scheme - Mid Cap Fund' -> 'MID CAP'."""
    if not raw:
        return None
    k = re.sub(r"\s+", " ", raw.strip().upper())
    k = k.replace("SCHEMES", "SCHEME")
    k = re.sub(r"( FUNDS?)*$", "", k)
    return k or None


def risk_profile(beta: float | None) -> str | None:
    if beta is None:
        return None
    if beta < 0.85:
        return "Conservative"
    if beta <= 1.10:
        return "Moderate"
    return "Aggressive"


# --- month grid ------------------------------------------------------------


def month_end_levels(pairs) -> dict[tuple[int, int], float]:
    """Last NAV per calendar month from [(date, nav), ...]."""
    levels: dict[tuple[int, int], float] = {}
    for d, nav in sorted(pairs):
        levels[(d.year, d.month)] = float(nav)
    return levels


def levels_in_window(levels, start: date_cls, end: date_cls) -> list[tuple]:
    return [(k, v) for k, v in sorted(levels.items()) if start <= month_end_date(k) <= end]


def monthly_returns(window_levels) -> list[tuple]:
    """[(key, simple_return)] between consecutive month-end levels."""
    out = []
    for (_, prev), (key, cur) in zip(window_levels, window_levels[1:], strict=False):
        if prev == 0:
            continue
        out.append((key, cur / prev - 1))
    return out


# --- statistics (hand-computed fixtures in tests/test_metrics.py pin these) -


def _mean(xs):
    return sum(xs) / len(xs)


def sample_sd(xs):
    n = len(xs)
    if n < 2:
        return None
    mu = _mean(xs)
    return math.sqrt(sum((x - mu) ** 2 for x in xs) / (n - 1))


def annualised_sd(monthly_rets):
    sd = sample_sd(monthly_rets)
    return None if sd is None else sd * math.sqrt(12)


def beta_of(fund_rets, bench_rets):
    n = len(fund_rets)
    if n < 2 or n != len(bench_rets):
        return None
    mf, mb = _mean(fund_rets), _mean(bench_rets)
    var = sum((b - mb) ** 2 for b in bench_rets) / (n - 1)
    if var == 0:
        return None
    cov = sum((f - mf) * (b - mb) for f, b in zip(fund_rets, bench_rets, strict=False)) / (n - 1)
    return cov / var


def capture_ratios(fund_rets, bench_rets):
    ups_f = [f for f, b in zip(fund_rets, bench_rets, strict=False) if b > 0]
    ups_b = [b for b in bench_rets if b > 0]
    downs_f = [f for f, b in zip(fund_rets, bench_rets, strict=False) if b < 0]
    downs_b = [b for b in bench_rets if b < 0]
    up = _mean(ups_f) / _mean(ups_b) if ups_f and ups_b else None
    down = _mean(downs_f) / _mean(downs_b) if downs_f and downs_b else None
    return up, down


def sharpe(cagr, sd, rf):
    if cagr is None or not sd:
        return None
    return (cagr - rf) / sd


# --- computation -----------------------------------------------------------


@dataclass
class FundMetrics:
    scheme_code: int
    lookback: str
    display_name: str | None = None
    category_key: str | None = None
    cagr: float | None = None
    sd: float | None = None
    beta: float | None = None
    sharpe_ratio: float | None = None
    upside_capture: float | None = None
    downside_capture: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def note(self) -> str | None:
        return "; ".join(self.notes) or None

    @property
    def computable(self) -> bool:
        return self.cagr is not None


def compute_fund(
    scheme_code: int,
    pairs,
    bench_ret_by_key: dict,
    *,
    lookback: str,
    start: date_cls,
    end: date_cls,
    rf: float,
    category: str | None = None,
    display_name: str | None = None,
) -> FundMetrics:
    fm = FundMetrics(
        scheme_code=scheme_code,
        lookback=lookback,
        display_name=display_name,
        category_key=category_key(category),
    )
    years = parse_lookback(lookback)
    expected = years * 12
    min_required = expected - 1  # tolerate one missing month

    levels = month_end_levels(pairs)
    window = levels_in_window(levels, start, end)
    rets = monthly_returns(window)
    if len(rets) < min_required:
        fm.notes.append(f"insufficient_history:{len(rets)}/{expected}")
        return fm
    keys = [k for k, _ in rets]
    vals = [v for _, v in rets]
    endpoint_start, endpoint_end = window[0][1], window[-1][1]

    # CAGR uses LEVEL endpoints, never return values (regression-pinned in tests)
    fm.cagr = (endpoint_end / endpoint_start) ** (1 / years) - 1
    fm.sd = annualised_sd(vals)
    fm.sharpe_ratio = sharpe(fm.cagr, fm.sd, rf)
    if fm.sharpe_ratio is None and fm.sd == 0:
        fm.notes.append("zero_sd")

    common = [
        (v, bench_ret_by_key[k]) for k, v in zip(keys, vals, strict=False) if k in bench_ret_by_key
    ]
    if len(common) < min_required:
        fm.notes.append(f"benchmark_overlap:{len(common)}/{expected}")
        return fm
    fund_r = [c[0] for c in common]
    bench_r = [c[1] for c in common]
    fm.beta = beta_of(fund_r, bench_r)
    fm.upside_capture, fm.downside_capture = capture_ratios(fund_r, bench_r)
    if fm.upside_capture is None:
        fm.notes.append("no_bench_up_months")
    if fm.downside_capture is None:
        fm.notes.append("no_bench_down_months")
    return fm


def _category_means(group: list[FundMetrics], min_peers: int) -> dict[int, dict[str, float | None]]:
    by_cat: dict[str, list[FundMetrics]] = defaultdict(list)
    for fm in group:
        if fm.category_key:
            by_cat[fm.category_key].append(fm)

    attrs = ("cagr", "sd", "beta", "upside_capture", "downside_capture")
    out: dict[int, dict[str, float | None]] = {}
    for fm in group:
        # only computable funds are real category peers
        peers = [
            p for p in (by_cat.get(fm.category_key, []) if fm.category_key else []) if p.computable
        ]
        means: dict[str, float | None] = {}
        for attr in attrs:
            vals = [getattr(p, attr) for p in peers if getattr(p, attr) is not None]
            enough = len(peers) >= min_peers and vals
            means[f"category_{attr}"] = _mean(vals) if enough else None
        if fm.computable and peers and len(peers) < min_peers:
            fm.notes.append(f"category_peers:{len(peers)}")
        out[fm.scheme_code] = means
    return out


def _ou(value: float | None, ref: float | None) -> str | None:
    if value is None or ref is None:
        return None
    if math.isclose(value, ref, rel_tol=1e-9):
        return RESULT_PAR
    return RESULT_OUT if value > ref else RESULT_UNDER


def _rows_for(
    fm: FundMetrics, cat: dict[str, float | None], cfg: dict, calc_at
) -> tuple[dict, dict]:
    methodology = dict(
        benchmark=cfg["benchmark"]["label"],
        frequency=cfg["frequency"],
        methodology_version=cfg["methodology_version"],
        sources="tigzig",
        calculated_at=calc_at,
    )
    cat_cagr = cat.get("category_cagr")
    ret_row = dict(
        scheme_code=fm.scheme_code,
        lookback=fm.lookback,
        fund_return=fm.cagr,
        category_avg_return=cat_cagr,
        result=_ou(fm.cagr, cat_cagr),
        note=fm.note,
        **methodology,
    )
    cat_up, cat_down = cat.get("category_upside_capture"), cat.get("category_downside_capture")
    risk_row = dict(
        scheme_code=fm.scheme_code,
        lookback=fm.lookback,
        sd=fm.sd,
        category_sd=cat.get("category_sd"),
        volatility_class=None
        if fm.sd is None or cat.get("category_sd") is None
        else (
            RESULT_PAR
            if math.isclose(fm.sd, cat["category_sd"], rel_tol=1e-9)
            else ("Lower Volatile" if fm.sd < cat["category_sd"] else "Higher Volatile")
        ),
        beta=fm.beta,
        category_beta=cat.get("category_beta"),
        risk_profile=risk_profile(fm.beta),
        sharpe=fm.sharpe_ratio,
        upside_cr=fm.upside_capture,
        category_upside_cr=cat_up,
        upside_result=_ou(fm.upside_capture, cat_up),
        downside_cr=fm.downside_capture,
        category_downside_cr=cat_down,
        downside_result=_ou(fm.downside_capture, cat_down),
        note=fm.note,
        **methodology,
    )
    return ret_row, risk_row


def run(
    conn, cfg: dict | None = None, *, as_of: date_cls | None = None, calculated_at=None
) -> dict:
    """Compute metrics for all tracked funds and upsert rows. Returns summary."""
    cfg = cfg or load_config()
    calc_at = calculated_at or dt.now(UTC)
    (data_end,) = conn.execute("SELECT max(nav_date) FROM mf_nav").fetchone()
    end = as_of or data_end
    if end is None:
        msg = "no NAV data in DB; run ingest first"
        raise RuntimeError(msg)

    bench_code = int(cfg["benchmark"]["scheme_code"])
    bench_pairs = conn.execute(
        "SELECT nav_date, nav FROM mf_nav WHERE scheme_code = ? ORDER BY nav_date", [bench_code]
    ).fetchall()
    if not bench_pairs:
        msg = f"benchmark series {bench_code} missing from mf_nav"
        raise RuntimeError(msg)

    tracked = conn.execute(
        "SELECT scheme_code, display_name FROM mf_scheme "
        "WHERE display_name IS NOT NULL ORDER BY scheme_code"
    ).fetchall()

    summary: dict = {
        "as_of": end,
        "lookbacks": list(cfg["lookbacks"]),
        "rows_written": 0,
        "computed": [],
        "skipped": [],
    }
    failures: dict[int, tuple[str | None, str | None]] = {}
    for lookback in cfg["lookbacks"]:
        years = parse_lookback(lookback)
        start = shift_years(end, years)
        min_required = years * 12 - 1

        bench_rets = dict(
            monthly_returns(levels_in_window(month_end_levels(bench_pairs), start, end))
        )
        if len(bench_rets) < min_required:
            msg = f"benchmark {bench_code} incomplete for {lookback}: {len(bench_rets)} months"
            raise RuntimeError(msg)

        group: list[FundMetrics] = []
        no_nav = 0
        for code, name in tracked:
            pairs = conn.execute(
                "SELECT nav_date, nav FROM mf_nav WHERE scheme_code = ? ORDER BY nav_date", [code]
            ).fetchall()
            if not pairs:
                no_nav += 1
                continue
            (raw_cat,) = conn.execute(
                "SELECT category FROM mf_scheme WHERE scheme_code = ?", [code]
            ).fetchone()
            fm = compute_fund(
                code,
                pairs,
                bench_rets,
                lookback=lookback,
                start=start,
                end=end,
                rf=float(cfg["risk_free_rate"]),
                category=raw_cat,
                display_name=name,
            )
            group.append(fm)
        if no_nav:
            log.warning("%d tracked schemes have no NAV rows; skipped", no_nav)

        cats = _category_means(group, int(cfg["min_category_peers"]))
        computed_here = 0
        for fm in group:
            if not fm.computable:
                # a lookback failure only skips the fund if NO lookback succeeds
                failures.setdefault(fm.scheme_code, (fm.display_name, fm.note))
                continue
            failures.pop(fm.scheme_code, None)
            ret_row, risk_row = _rows_for(fm, cats[fm.scheme_code], cfg, calc_at)
            db.upsert_return_metric(conn, **ret_row)
            db.upsert_risk_metric(conn, **risk_row)
            summary["rows_written"] += 2
            summary["computed"].append(fm)
            computed_here += 1
        log.info("lookback %s: %d computed", lookback, computed_here)
    summary["skipped"] = sorted((code, name, note) for code, (name, note) in failures.items())
    return summary


def main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(prog="invest-metrics")
    parser.add_argument("--db", default="data/invest.duckdb")
    parser.add_argument("--config", default="config/metrics.json")
    parser.add_argument("--as-of", default=None, help="override window end (YYYY-MM-DD)")
    args = parser.parse_args(argv)

    conn = db.connect(args.db)
    db.init_schema(conn)
    as_of = date_cls.fromisoformat(args.as_of) if args.as_of else None
    summary = run(conn, load_config(args.config), as_of=as_of)

    print(f"as-of {summary['as_of']} lookbacks={summary['lookbacks']}")
    print(
        f"{'code':>7}  {'3Y ret':>7} {'SD':>6} {'beta':>5} {'sharpe':>6} {'upCR':>5} "
        f"{'dnCR':>5}  fund"
    )

    def fmt(v, p=2):
        return f"{v * 100:.{p}f}" if v is not None else "-"

    for fm in summary["computed"]:
        beta = f"{fm.beta:.2f}" if fm.beta is not None else "-"
        shp = f"{fm.sharpe_ratio:.2f}" if fm.sharpe_ratio is not None else "-"
        print(
            f"{fm.scheme_code:>7}  {fmt(fm.cagr):>7} {fmt(fm.sd):>6} {beta:>5} {shp:>6}"
            f" {fmt(fm.upside_capture):>5} {fmt(fm.downside_capture):>5}  {fm.display_name}"
            + (f"  [{fm.note}]" if fm.note else "")
        )
    for code, name, why in summary["skipped"]:
        print(f"{code:>7}  {'SKIPPED':>7}  {name}  [{why}]")
    print(f"rows written: {summary['rows_written']}, skipped funds: {len(summary['skipped'])}")
    violations = db.metric_violation_count(conn)
    print(f"methodology violations: {violations}")
    return 0 if summary["rows_written"] and not violations else 1


if __name__ == "__main__":
    sys.exit(main())
