"""T1.5 acceptance: metric fixtures with hand-computed expected values."""

import math
from datetime import date
from datetime import datetime as dt

import duckdb
import pytest

from invest import db, metrics

# naive on purpose: DuckDB TIMESTAMP columns return naive datetimes
calc_at = dt(2026, 8, 25, 12, 0, 0)


def levels_from_returns(start_ym: tuple[int, int], base: float, rets):
    """Monthly closes; rets[i] compounds level i -> i+1. Returns len(rets)+1 closes."""
    y, m = start_ym
    out = {}
    level = base
    for r in rets:
        out[(y, m)] = level
        level *= 1 + r
        m += 1
        if m > 12:
            y, m = y + 1, 1
    out[(y, m)] = level
    return out


def pairs_from_levels(levels):
    return [(metrics.month_end_date(k), v) for k, v in sorted(levels.items())]


def month_ret_pattern(pattern, repeats):
    return [x for _ in range(repeats) for x in pattern]


# --- unit fixtures ----------------------------------------------------------


def test_parse_lookback_and_shift_years():
    assert metrics.parse_lookback("3Y") == 3
    with pytest.raises(ValueError, match="lookback"):
        metrics.parse_lookback("18M")
    # Feb-29 clamp
    assert metrics.shift_years(date(2024, 2, 29), 3) == date(2021, 2, 28)
    assert metrics.shift_years(date(2025, 8, 31), 3) == date(2022, 8, 31)


def test_category_key_normalization():
    f = metrics.category_key
    assert f("Equity Scheme - Mid Cap Fund") == f("Equity Schemes - Mid Cap Funds")
    assert f("Mid Cap Fund") == "MID CAP"
    assert f(None) is None


def test_risk_profile_bands():
    f = metrics.risk_profile
    assert f(0.30) == "Conservative"
    assert f(0.84) == "Conservative"
    assert f(0.85) == "Moderate"
    assert f(1.10) == "Moderate"
    assert f(1.11) == "Aggressive"
    assert f(None) is None


def test_cagr_hand_computed():
    # 100 -> 133.1 over exactly 36 monthly returns = (133.1)^(1/3) = 10% p.a.
    growth = 1.331 ** (1 / 36) - 1
    levels = levels_from_returns((2022, 8), 100.0, [growth] * 36)
    win = metrics.levels_in_window(levels, date(2022, 8, 31), date(2025, 8, 31))
    rets = metrics.monthly_returns(win)
    assert len(rets) == 36
    cagr = (win[-1][1] / win[0][1]) ** (1 / 3) - 1
    assert cagr == pytest.approx(0.10, abs=1e-9)


def test_sd_hand_computed():
    # alternating +-2%: sample sd = 0.02*sqrt(36/35), annualised * sqrt(12)
    rets = month_ret_pattern([0.02, -0.02], 18)
    expected = 0.02 * math.sqrt(12 * 36 / 35)
    got = metrics.annualised_sd(rets)
    assert got == pytest.approx(expected, rel=1e-12)


def test_beta_exact_slope():
    bench = month_ret_pattern([0.03, -0.01, 0.02, 0.04, -0.02, 0.01], 6)
    fund = [1.5 * b + 0.002 for b in bench]
    assert metrics.beta_of(fund, bench) == pytest.approx(1.5, abs=1e-12)


def test_capture_ratios_hand_computed():
    bench = month_ret_pattern([0.04, -0.02, 0.02, -0.01], 9)
    fund = [0.75 * b for b in bench]
    up, down = metrics.capture_ratios(fund, bench)
    # up months: mean(fund)=mean(.03,.015)=.0225 vs mean(bench)=mean(.04,.02)=.03 -> .75
    assert up == pytest.approx(0.75, abs=1e-12)
    # down months: mean(-.015,-.0075)=-.01125 vs mean(-.02,-.01)=-.015 -> .75
    assert down == pytest.approx(0.75, abs=1e-12)


def test_sharpe_hand_computed():
    assert metrics.sharpe(0.10, 0.15, rf=0.07) == pytest.approx(0.20)
    assert metrics.sharpe(0.10, 0.0, rf=0.07) is None


def test_insufficient_history_note():
    levels = levels_from_returns((2024, 1), 100.0, month_ret_pattern([0.01], 19))  # 20 closes
    fm = metrics.compute_fund(
        1,
        pairs_from_levels(levels),
        {},
        lookback="3Y",
        start=date(2022, 8, 31),
        end=date(2025, 8, 31),
        rf=0.07,
    )
    assert not fm.computable
    assert fm.note and fm.note.startswith("insufficient_history:")
    assert "19/36" in fm.note  # 20 closes -> 19 monthly returns


# --- end-to-end on synthetic DB ---------------------------------------------

BENCH_RETS = month_ret_pattern([0.03, -0.01, 0.02, 0.04, -0.02, 0.01], 6)
START, END = date(2022, 8, 31), date(2025, 8, 31)


@pytest.fixture()
def synthetic_db():
    conn = duckdb.connect(":memory:")
    db.init_schema(conn)
    bench_levels = levels_from_returns((2022, 8), 100.0, BENCH_RETS)
    db.upsert_scheme(conn, scheme_code=900002, name="Synthetic Index Fund proxy")
    db.upsert_navs(conn, 900002, pairs_from_levels(bench_levels))

    fund_a = [1.2 * b for b in BENCH_RETS]  # beta exactly 1.2 -> Aggressive
    db.upsert_scheme(
        conn,
        scheme_code=101,
        name="Fund A",
        display_name="Fund A",
        category="Equity Scheme - Mid Cap Fund",
    )
    db.upsert_navs(conn, 101, pairs_from_levels(levels_from_returns((2022, 8), 50.0, fund_a)))

    fund_b_rets = month_ret_pattern([0.01], 19)  # short history -> skipped
    db.upsert_scheme(
        conn,
        scheme_code=102,
        name="Fund B",
        display_name="Fund B",
        category="Equity Scheme - Mid Cap Fund",
    )
    db.upsert_navs(conn, 102, pairs_from_levels(levels_from_returns((2024, 1), 10.0, fund_b_rets)))
    return conn


def test_run_end_to_end(synthetic_db):
    summary = metrics.run(synthetic_db, calculated_at=calc_at)
    codes_written = {fm.scheme_code for fm in summary["computed"]}
    assert codes_written == {101, 102}  # B is too short for 3Y but fits 1Y
    pairs = {(fm.scheme_code, fm.lookback) for fm in summary["computed"]}
    assert pairs == {(101, "3Y"), (101, "1Y"), (102, "1Y")}
    assert summary["skipped"] == []

    rows = synthetic_db.execute(
        "SELECT fund_return, category_avg_return, result, benchmark, frequency,"
        " methodology_version, sources, calculated_at, note FROM mf_return_metrics"
        " WHERE scheme_code = 101 AND lookback = '3Y'"
    ).fetchall()
    assert len(rows) == 1
    r = rows[0]
    # Fund A moves at 1.2x the benchmark every month; CAGR must come from LEVEL
    # endpoints (regression: it once used return-series endpoints).
    expect_cagr = math.prod(1 + 1.2 * b for b in BENCH_RETS) ** (1 / 3) - 1
    assert r[0] == pytest.approx(expect_cagr, rel=1e-9)
    # single-fund category: mean over peers incl self -> equals own value...
    # but min_category_peers=3 blocks it -> category fields null + note
    assert r[1] is None
    assert r[2] is None
    assert r[3] == metrics.DEFAULT_CONFIG["benchmark"]["label"]
    assert r[4] == "monthly" and r[5] == "m2026.1" and r[6] == "tigzig"
    assert r[7] == calc_at
    assert "category_peers:1" in r[8]

    sd, beta, rp, shp, up, dn, note = synthetic_db.execute(
        "SELECT sd, beta, risk_profile, sharpe, upside_cr, downside_cr, note"
        " FROM mf_risk_metrics WHERE scheme_code = 101"
    ).fetchone()
    monthly_a = [1.2 * b for b in BENCH_RETS]
    mu = sum(monthly_a) / len(monthly_a)
    exp_sd = math.sqrt(sum((x - mu) ** 2 for x in monthly_a) / 35) * math.sqrt(12)
    assert sd == pytest.approx(exp_sd, rel=1e-12)
    assert beta == pytest.approx(1.2, abs=1e-12)
    assert rp == "Aggressive"
    assert shp == pytest.approx((expect_cagr - 0.07) / exp_sd, rel=1e-9)
    # fund moves 1.2x bench every month -> both captures exactly 1.2
    assert up == pytest.approx(1.2, abs=1e-12)
    assert dn == pytest.approx(1.2, abs=1e-12)
    assert "category_peers:1" in note

    assert db.metric_violation_count(synthetic_db) == 0


def test_category_aggregation_min_peers(synthetic_db):
    # add two more funds in the same category so peers >= 3
    for code, mult in ((103, 1.0), (104, 1.5)):
        db.upsert_scheme(
            conn=synthetic_db,
            scheme_code=code,
            name=f"Fund {code}",
            display_name=f"Fund {code}",
            category="Equity Scheme - Mid Cap Fund",
        )
        db.upsert_navs(
            synthetic_db,
            code,
            pairs_from_levels(levels_from_returns((2022, 8), 80.0, [mult * b for b in BENCH_RETS])),
        )
    metrics.run(synthetic_db, calculated_at=calc_at)
    cat_vals = synthetic_db.execute(
        "SELECT scheme_code, category_avg_return, category_sd FROM mf_return_metrics"
        " JOIN mf_risk_metrics USING (scheme_code, lookback)"
    ).fetchall()
    by_code = dict((c, (cr, csd)) for c, cr, csd in cat_vals)
    # all four funds share one category now -> means present everywhere
    assert all(cr is not None and csd is not None for cr, csd in by_code.values())
    # fund 103 tracks the benchmark exactly -> its beta-capture is 1.0 and it is
    # the lowest-vol member -> Lower Volatile; 1.5x fund must be Higher Volatile
    vol = dict(
        synthetic_db.execute("SELECT scheme_code, volatility_class FROM mf_risk_metrics").fetchall()
    )
    assert vol[103] == "Lower Volatile" or vol[101] == "Higher Volatile"


def test_rerun_idempotent(synthetic_db):
    metrics.run(synthetic_db, calculated_at=calc_at)
    fp1 = (
        db.fingerprint(synthetic_db, "mf_return_metrics"),
        db.fingerprint(synthetic_db, "mf_risk_metrics"),
    )
    metrics.run(synthetic_db, calculated_at=calc_at)
    fp2 = (
        db.fingerprint(synthetic_db, "mf_return_metrics"),
        db.fingerprint(synthetic_db, "mf_risk_metrics"),
    )
    assert fp1 == fp2


def test_benchmark_missing_is_fatal(synthetic_db):
    empty = duckdb.connect(":memory:")
    db.init_schema(empty)
    with pytest.raises(RuntimeError, match="no NAV data"):
        metrics.run(empty, calculated_at=calc_at)


def test_benchmark_overlap_gap_notes_partial_nulls(synthetic_db):
    # fund whose history covers only half the window: enough months? no —
    # use a fund with full history but a benchmark missing half its months:
    # instead simulate via config with a different (shorter) benchmark series.
    partial_bench = levels_from_returns((2024, 6), 100.0, BENCH_RETS[:14])
    conn = duckdb.connect(":memory:")
    db.init_schema(conn)
    db.upsert_scheme(conn, scheme_code=999001, name="partial bench")
    db.upsert_navs(conn, 999001, pairs_from_levels(partial_bench))
    cfg = dict(metrics.DEFAULT_CONFIG)
    cfg["benchmark"] = {"scheme_code": 999001, "label": "partial"}
    with pytest.raises(RuntimeError, match="benchmark .* incomplete"):
        metrics.run(conn, cfg, as_of=date(2025, 8, 31), calculated_at=calc_at)
