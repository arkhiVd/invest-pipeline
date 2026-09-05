"""Golden threshold tests for the T3.3 screen engines.

Every oracle threshold boundary is pinned with synthetic universes; no live
data involved. The July-12 result sets themselves are unreproducible post
hoc (prices/mcaps moved), so validation here is semantic, per the frozen-
oracle honesty note in the module docstring.
"""

import duckdb
import pytest

from invest import db, screens

BASE = {
    "market_cap_cr": 5000.0,
    "pe_ratio": 15.0,
    "pb_ratio": 2.0,
    "roe": 0.30,
    "roce": 0.25,
    "revenue_cagr_3y": 0.25,
    "profit_cagr_3y": 0.25,
    "eps_cagr_3y": 0.25,
    "revenue_growth_yoy": 0.10,
    "debt_to_equity": 0.3,
    "promoter_pledged": False,
    "close": 100.0,
    "high_52w": 125.0,
    "all_time_high": 125.0,
    "dma_50": 90.0,
    "free_cash_flow_3y": 500.0,
    "promoter_holding": 0.55,
    "piotroski_score": 7,
    "current_ratio": 2.0,
    "fii_holding": 0.08,
    "avg_roce_3y": 0.35,
    "avg_roce_5y": 0.20,
    "avg_roe_5y": 0.25,
    "operating_margin": 0.20,
    "interest_coverage": 8.0,
    "dividend_yield": 0.02,
    "eps": 12.0,
    "eps_previous": 10.0,
}


def uni(**overrides):
    row = {**BASE, **overrides}
    row["price_to_52w_high"] = row["close"] / row["high_52w"] if row["high_52w"] else None
    row["price_above_50dma"] = bool(row["close"] > row["dma_50"])
    row["price_to_all_time_high"] = (
        row["close"] / row["all_time_high"] if row["all_time_high"] else None
    )
    row["eps_increased"] = (
        row["eps"] > row["eps_previous"]
        if row.get("eps") is not None and row.get("eps_previous") is not None
        else None
    )
    ec = row.get("eps_cagr_3y")
    pe = row.get("pe_ratio")
    row["peg"] = (
        pe / (ec * 100) if pe is not None and pe > 0 and ec is not None and ec > 0 else None
    )
    return {"TEST": row}


@pytest.fixture()
def conn():
    c = duckdb.connect()
    db.init_schema(c)
    yield c
    c.close()


CFG = screens.load_config()


def survivors(universe, screen_id):
    return {
        s["symbol"]
        for s in screens.evaluate_screen(universe, CFG["screens"][screen_id]["conditions"])[
            "survivors"
        ]
    }


def test_garp_passes_clean_quality_name():
    assert survivors(uni(), "garp") == {"TEST"}


def test_garp_pe_band_is_strict():
    assert survivors(uni(pe_ratio=7.0), "garp") == set()
    assert survivors(uni(pe_ratio=25.0), "garp") == set()
    assert survivors(uni(pe_ratio=7.01), "garp") == {"TEST"}
    assert survivors(uni(pe_ratio=24.99), "garp") == {"TEST"}


def test_garp_strict_inequalities():
    assert survivors(uni(debt_to_equity=0.7), "garp") == set()  # "<0.7" strict
    assert survivors(uni(debt_to_equity=0.69), "garp") == {"TEST"}
    assert survivors(uni(roe=0.25), "garp") == set()  # ">25%" strict
    assert survivors(uni(roce=0.20), "garp") == set()  # ">20%" strict
    assert survivors(uni(revenue_cagr_3y=0.18), "garp") == set()


def test_garp_peg_derived_from_pe_and_eps_cagr():
    # peg = pe / (eps_cagr*100): 15/20 = 0.75 passes; at cap fails strictly.
    assert survivors(uni(), "garp") == {"TEST"}
    assert survivors(uni(eps_cagr_3y=0.10), "garp") == set()  # peg=1.5
    assert survivors(uni(eps_cagr_3y=None), "garp") == set()  # gap -> fail


def dv(**over):
    over.setdefault("pb_ratio", 0.9)
    over.setdefault("pe_ratio", 8.0)
    return uni(**over)


def test_deep_value_boundaries():
    assert survivors(dv(), "deep_value") == {"TEST"}
    assert survivors(dv(market_cap_cr=1000.0), "deep_value") == set()  # strict >
    assert survivors(dv(pb_ratio=1.2), "deep_value") == set()  # strict <
    assert survivors(dv(pe_ratio=0.0), "deep_value") == set()  # strict >
    assert survivors(dv(pe_ratio=12.0), "deep_value") == set()  # strict <
    assert survivors(dv(pe_ratio=-5.0), "deep_value") == set()  # losses out
    assert survivors(dv(revenue_growth_yoy=0.05), "deep_value") == set()


def qp(**over):
    return uni(market_cap_cr=20000.0, **over)


def test_quality_pullback_pullback_and_dma():
    # close 110 of high 125 = 12% off, above dma -> in.
    assert survivors(qp(close=110.0), "quality_pullback") == {"TEST"}
    # only 8% off -> not pulled back enough.
    assert survivors(qp(close=115.0), "quality_pullback") == set()
    # below 50-DMA -> out even when pulled back.
    assert survivors(qp(close=80.0), "quality_pullback") == set()
    # pledged or levered names excluded.
    assert survivors(qp(promoter_pledged=True), "quality_pullback") == set()
    assert survivors(qp(debt_to_equity=0.5), "quality_pullback") == set()


def test_missing_fields_fail_and_count_as_gaps():
    row = uni()
    row["TEST"]["pe_ratio"] = None
    result = screens.evaluate_screen(row, CFG["screens"]["garp"]["conditions"])
    assert result["survivors"] == []
    assert result["gaps"]["pe_ratio"] == 1


def test_run_screen_smoke_on_empty_db(conn):
    result = screens.run_screen(conn, "garp", CFG)
    assert result["evaluated"] == 0 and result["survivors"] == []
    with pytest.raises(ValueError):
        screens.run_screen(conn, "nope", CFG)


def test_price_stats_from_real_schema(conn):
    from datetime import UTC, date, timedelta
    from datetime import datetime as dt

    fetched = dt(2026, 8, 25, tzinfo=UTC)
    closes = [100 + i for i in range(60)]
    start = date(2026, 6, 1)
    rows = [
        {
            "symbol": "AAA",
            "trade_date": start + timedelta(days=i),
            "open": float(c),
            "high": float(c + 10),
            "low": float(c),
            "close": float(c),
            "prev_close": None,
            "volume": 1,
        }
        for i, c in enumerate(closes)
    ]
    db.upsert_prices(conn, rows, source="fx", fetched_at=fetched)
    stats = screens.price_stats(conn)["AAA"]
    assert stats["close"] == closes[-1]
    assert stats["dma_50"] == sum(closes[10:]) / 50  # last 50 sessions
    assert stats["high_52w"] == closes[-1] + 10


def test_price_stats_requires_full_50_sessions(conn):
    from datetime import UTC, date, timedelta
    from datetime import datetime as dt

    rows = [
        {
            "symbol": "SHORT",
            "trade_date": date(2026, 1, 1) + timedelta(days=i),
            "open": 100.0,
            "high": 110.0,
            "low": 90.0,
            "close": 100.0,
            "prev_close": 100.0,
            "volume": 1,
        }
        for i in range(49)
    ]
    db.upsert_prices(conn, rows, source="fx", fetched_at=dt(2026, 8, 25, tzinfo=UTC))
    assert screens.price_stats(conn)["SHORT"]["dma_50"] is None


def test_high_growth_boundaries():
    def hg(**over):
        over.setdefault("close", 80.0)
        over.setdefault("dma_50", 70.0)
        return uni(**over)

    assert survivors(hg(), "high_growth") == {"TEST"}
    assert survivors(hg(close=87.5), "high_growth") == set()  # exactly 30% off
    assert survivors(hg(pe_ratio=-5.0), "high_growth") == set()  # PEG unavailable
    assert survivors(hg(promoter_pledged=True), "high_growth") == set()


def test_quality_value_boundaries():
    def qv(**over):
        over.setdefault("market_cap_cr", 6000.0)
        over.setdefault("pe_ratio", 12.0)
        over.setdefault("pb_ratio", 1.2)
        return uni(**over)

    assert survivors(qv(), "quality_value") == {"TEST"}
    assert survivors(qv(market_cap_cr=5000.0), "quality_value") == set()
    assert survivors(qv(avg_roce_5y=0.15), "quality_value") == set()
    assert survivors(qv(interest_coverage=4.0), "quality_value") == set()
    assert survivors(qv(dividend_yield=0.01), "quality_value") == set()


def test_beaten_down_exact_ath_band_and_eps_comparison():
    def bd(**over):
        over.setdefault("market_cap_cr", 2000.0)
        over.setdefault("pe_ratio", 30.0)
        return uni(**over)

    assert survivors(bd(close=100.0), "beaten_down") == {"TEST"}  # 20% edge
    assert survivors(bd(close=75.0), "beaten_down") == {"TEST"}  # 40% edge
    assert survivors(bd(close=101.0), "beaten_down") == set()
    assert survivors(bd(close=74.0), "beaten_down") == set()
    assert survivors(bd(eps=-1.0, eps_previous=-2.0), "beaten_down") == {"TEST"}
    assert survivors(bd(eps=9.0, eps_previous=10.0), "beaten_down") == set()


def test_build_universe_keeps_symbols_missing_computed_rows(conn):
    from datetime import UTC, date
    from datetime import datetime as dt

    fetched = dt(2026, 8, 25, tzinfo=UTC)
    for symbol in ("HASFACTS", "MISSING"):
        db.upsert_universe_row(
            conn, symbol=symbol, series="EQ", is_active=True, source="fx", fetched_at=fetched
        )
    db.upsert_stock_fundamental(
        conn,
        symbol="HASFACTS",
        as_of=date(2026, 3, 31),
        source=screens.COMPUTED_SOURCE,
        roe=0.2,
        promoter_holding=0.55,
        methodology_version="fx",
        fetched_at=fetched,
    )
    db.upsert_stock_fundamental(
        conn,
        symbol="HASFACTS",
        as_of=date(2026, 8, 25),
        source=screens.MARKET_SOURCE,
        market_cap_cr=6000,
        dividend_yield=0.02,
        methodology_version="fx",
        fetched_at=fetched,
    )
    universe = screens.build_universe(conn)
    assert set(universe) == {"HASFACTS", "MISSING"}
    assert universe["HASFACTS"]["promoter_holding"] == pytest.approx(0.55)
    assert universe["HASFACTS"]["dividend_yield"] == pytest.approx(0.02)
    assert universe["MISSING"].get("roe") is None


def test_build_universe_nulls_prior_period_computed_metrics(conn):
    from datetime import UTC, date
    from datetime import datetime as dt

    fetched = dt(2026, 8, 26, tzinfo=UTC)
    for symbol in ("CURRENT", "CURRENT2", "STALE", "FUTURE"):
        db.upsert_universe_row(
            conn, symbol=symbol, series="EQ", is_active=True, source="fx", fetched_at=fetched
        )
    db.upsert_stock_fundamental(
        conn,
        symbol="CURRENT",
        as_of=date(2026, 3, 31),
        source=screens.COMPUTED_SOURCE,
        roe=0.30,
        methodology_version="fx",
        fetched_at=fetched,
    )
    db.upsert_stock_fundamental(
        conn,
        symbol="CURRENT2",
        as_of=date(2026, 3, 31),
        source=screens.COMPUTED_SOURCE,
        roe=0.25,
        methodology_version="fx",
        fetched_at=fetched,
    )
    db.upsert_stock_fundamental(
        conn,
        symbol="STALE",
        as_of=date(2024, 3, 31),
        source=screens.COMPUTED_SOURCE,
        roe=0.40,
        methodology_version="fx",
        fetched_at=fetched,
    )
    db.upsert_stock_fundamental(
        conn,
        symbol="FUTURE",
        as_of=date(2099, 3, 31),
        source=screens.COMPUTED_SOURCE,
        roe=0.99,
        methodology_version="fx",
        fetched_at=fetched,
    )
    universe = screens.build_universe(conn)
    assert universe["CURRENT"]["roe"] == pytest.approx(0.30)
    assert universe["CURRENT"]["fundamentals_stale"] is False
    assert universe["STALE"].get("roe") is None
    assert universe["STALE"]["as_of"] == date(2024, 3, 31)
    assert universe["STALE"]["fundamentals_stale"] is True
    assert universe["FUTURE"].get("roe") is None
    assert universe["FUTURE"]["fundamentals_stale"] is True


def test_build_universe_sql_bounds_fundamental_and_price_inputs(conn):
    from datetime import UTC, date
    from datetime import datetime as dt

    fetched = dt(2026, 8, 26, tzinfo=UTC)
    cutoff = date(2026, 3, 31)
    db.upsert_universe_row(
        conn, symbol="SAFE", series="EQ", is_active=True, source="fx", fetched_at=fetched
    )
    db.upsert_stock_fundamental(
        conn,
        symbol="SAFE",
        as_of=cutoff,
        source=screens.COMPUTED_SOURCE,
        roe=0.3,
        methodology_version="fx",
        fetched_at=fetched,
    )
    db.upsert_stock_fundamental(
        conn,
        symbol="SAFE",
        as_of=date(2099, 3, 31),
        source=screens.COMPUTED_SOURCE,
        roe=0.9,
        methodology_version="fx",
        fetched_at=fetched,
    )
    conn.execute(
        "INSERT INTO stock_price VALUES ('SAFE', ?, NULL, NULL, NULL, 100, NULL, NULL, 'fx', ?)",
        [cutoff, fetched],
    )
    conn.execute(
        "INSERT INTO stock_price VALUES "
        "('SAFE', '2099-03-31', NULL, NULL, NULL, 999, NULL, NULL, 'fx', ?)",
        [fetched],
    )
    universe = screens.build_universe(conn, cutoff=cutoff)
    assert universe["SAFE"]["as_of"] == cutoff
    assert universe["SAFE"]["roe"] == pytest.approx(0.3)
    assert universe["SAFE"]["close"] == pytest.approx(100.0)


def test_build_universe_bounds_membership_source_and_fetch_time(conn):
    from datetime import UTC, date
    from datetime import datetime as dt

    cutoff = date(2026, 8, 28)
    db.upsert_universe_row(
        conn,
        symbol="SAFE",
        series="EQ",
        is_active=True,
        source="nse_equity_master",
        fetched_at=dt(2026, 8, 28, tzinfo=UTC),
    )
    db.upsert_universe_row(
        conn,
        symbol="FUTURE",
        series="EQ",
        is_active=True,
        source="nse_equity_master",
        fetched_at=dt(2026, 8, 29, tzinfo=UTC),
    )
    db.upsert_universe_row(
        conn,
        symbol="WRONG",
        series="EQ",
        is_active=True,
        source="fixture",
        fetched_at=dt(2026, 8, 28, tzinfo=UTC),
    )

    universe = screens.build_universe(conn, cutoff=cutoff, universe_source="nse_equity_master")

    assert set(universe) == {"SAFE"}


def test_config_validation_rejects_unknown_operator(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"screens":{"x":{"conditions":{"pe":{"gte":7}}}}}')
    with pytest.raises(ValueError, match="invalid condition"):
        screens.load_config(path)
