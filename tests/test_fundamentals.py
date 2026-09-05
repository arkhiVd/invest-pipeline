"""Offline tests for T3.2c deterministic fundamentals."""

from datetime import UTC, date
from datetime import datetime as dt

import duckdb
import pytest

from invest import db, fundamentals

FETCHED = dt(2026, 8, 25, 12, tzinfo=UTC)
FY25 = date(2025, 3, 31)
FY26 = date(2026, 3, 31)


@pytest.fixture()
def conn():
    c = duckdb.connect()
    db.init_schema(c)
    yield c
    c.close()


def add_filing(
    conn,
    *,
    url,
    symbol="TEST",
    period_end,
    consolidation="Consolidated",
    taxonomy="indas",
    facts,
):
    """facts: {fact_name: value}; contexts auto-built from naming convention:
    'D:<name>' duration FY span, 'I' instant at period_end."""
    db.upsert_stock_filing(
        conn,
        xbrl_url=url,
        symbol=symbol,
        source="fixture",
        filing_type="financial_annual_legacy",
        period_end=period_end,
        consolidation=consolidation,
        taxonomy=taxonomy,
        fetched_at=FETCHED,
    )
    rows = []
    for name, value in facts.items():
        if name.startswith("D:"):
            ctx_id = f"CD_{name[2:]}"
            conn.execute(
                "INSERT OR IGNORE INTO stock_filing_context VALUES (?,?,?,?,?,?)",
                [url, ctx_id, date(period_end.year - 1, 4, 1), period_end, None, None],
            )
            rows.append((url, name[2:], ctx_id, str(value)))
        else:
            conn.execute(
                "INSERT OR IGNORE INTO stock_filing_context VALUES (?,?,?,?,?,?)",
                [url, "CI", None, None, period_end, None],
            )
            rows.append((url, name, "CI", str(value)))
    conn.executemany(
        "INSERT INTO stock_filing_fact VALUES (?,?,?,NULL,NULL,NULL)",
        [(u, n, c) for u, n, c, _v in rows],
    )
    conn.executemany(
        "UPDATE stock_filing_fact SET value = ? WHERE xbrl_url = ? AND fact_name = ?",
        [(v, u, n) for u, n, _c, v in rows],
    )


def test_v7_database_gains_current_schema_columns():
    c = duckdb.connect()
    try:
        c.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMP)"
        )
        c.executemany(
            "INSERT INTO schema_migrations VALUES (?, current_timestamp)",
            [(1,), (2,), (3,), (4,), (5,), (6,), (7,)],
        )
        db.init_schema(c)
        cols = {r[0] for r in c.execute("DESCRIBE stock_fundamentals").fetchall()}
        assert {
            "avg_roe_3y",
            "avg_roce_5y",
            "revenue_cagr_3y",
            "free_cash_flow_3y",
            "eps_previous",
            "current_ratio",
        } <= cols
        assert (
            c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == db.SCHEMA_VERSION
        )
    finally:
        c.close()


def test_fiscal_year_series_and_derived_metrics(conn):
    for url, pe, rev, pft, fc in (
        ("u22", date(2022, 3, 31), 100.0, 10.0, 2.0),
        ("u23", date(2023, 3, 31), 120.0, 14.0, 2.0),
        ("u24", date(2024, 3, 31), 150.0, 18.0, 3.0),
        ("u25", FY25, 180.0, 22.0, 3.0),
        ("u26", FY26, 216.0, 30.0, 4.0),
    ):
        add_filing(
            conn,
            url=url,
            period_end=pe,
            facts={
                "D:RevenueFromOperations": rev,
                "D:ProfitLossForPeriod": pft,
                "D:ProfitBeforeTax": pft * 1.25,
                "D:TaxExpense": pft * 0.25,
                "D:FinanceCosts": fc,
                "EquityShareCapital": 50.0,
                "OtherEquity": 150.0,
                "BorrowingsCurrent": 20.0,
                "BorrowingsNoncurrent": 30.0,
                "Assets": 400.0,
                "CurrentLiabilities": 100.0,
                "CurrentAssets": 150.0,
                "D:BasicEarningsLossPerShareFromContinuingOperations": 5.0,
            },
        )
    m = fundamentals.compute_symbol(conn, "TEST")
    assert m["as_of"] == FY26
    # ROCE FY26 = EBIT/capital employed = (PBT 37.5 + interest 4)/(400-100).
    assert m["roce"] == pytest.approx(41.5 / 300)
    assert m["roe"] == pytest.approx(30 / 200)
    # OPM needs Expenses; absent -> None
    assert m["operating_margin"] is None
    assert m["revenue_growth_yoy"] == pytest.approx(216 / 180 - 1)
    assert m["profit_growth_yoy"] == pytest.approx(30 / 22 - 1)
    # revenue CAGR over 3 compounding years: FY23 -> FY26
    assert m["revenue_cagr_3y"] == pytest.approx((216 / 120) ** (1 / 3) - 1)
    assert m["profit_cagr_3y"] == pytest.approx((30 / 14) ** (1 / 3) - 1)
    assert m["eps_cagr_3y"] == pytest.approx(0.0)  # flat EPS 5.0
    # averages need the FULL window: only 5 filings -> 3y ok, 5y ok too
    roce_years = [(p * 1.25 + f) / 300 for p, f in [(10, 2), (14, 2), (18, 3), (22, 3), (30, 4)]]
    assert m["avg_roce_3y"] == pytest.approx(sum(roce_years[-3:]) / 3)
    assert m["avg_roe_5y"] == pytest.approx(
        sum([10, 14, 18, 22, 30]) / 5 / 200
    )  # profit/equity each year
    assert m["debt_to_equity"] == pytest.approx(50 / 200)
    assert m["current_ratio"] == pytest.approx(1.5)
    assert m["promoter_pledged"] is None


def test_consolidated_preferred_and_bank_normalization(conn):
    add_filing(
        conn,
        url="standalone",
        period_end=FY26,
        consolidation="Standalone",
        facts={"D:RevenueFromOperations": 999.0},
    )
    add_filing(
        conn,
        url="bankcons",
        period_end=FY26,
        consolidation="Consolidated",
        taxonomy="banking",
        facts={
            "D:InterestEarned": 500.0,
            "D:InterestExpended": 300.0,
            "D:ProfitLossForThePeriod": 60.0,
            "D:ProfitBeforeExtraordinaryItemsAndTax": 80.0,
            "Capital": 100.0,
            "ReservesAndSurplus": 400.0,
            "Borrowings": 250.0,
            "BasicEarningsPerShareBeforeExtraordinaryItems": 12.0,
        },
    )
    m = fundamentals.compute_symbol(conn, "TEST")
    assert m["as_of"] == FY26
    assert m["roce"] == pytest.approx((80 + 300) / (500 + 250))
    assert m["roe"] == pytest.approx(60 / 500)
    assert m["eps_cagr_3y"] is None  # single year of EPS
    assert m["operating_margin"] is None  # banks carry no Expenses element


def test_insufficient_window_stays_null(conn):
    add_filing(
        conn,
        url="only26",
        period_end=FY26,
        facts={
            "D:RevenueFromOperations": 100.0,
            "D:ProfitLossForPeriod": 10.0,
            "EquityShareCapital": 40.0,
            "OtherEquity": 60.0,
        },
    )
    m = fundamentals.compute_symbol(conn, "TEST")
    assert m["revenue_cagr_3y"] is None and m["avg_roe_3y"] is None
    assert m["debt_to_equity"] is None  # no debt facts at all


def test_run_writes_fundamentals_and_is_idempotent(conn):
    db.upsert_universe_row(conn, symbol="TEST", series="EQ", source="fx", fetched_at=FETCHED)
    add_filing(
        conn,
        url="u26",
        period_end=FY26,
        facts={
            "D:RevenueFromOperations": 216.0,
            "D:Expenses": 151.2,
            "D:ProfitLossForPeriod": 30.0,
            "D:FinanceCosts": 10.0,
            "D:DepreciationDepletionAndAmortisationExpense": 20.0,
            "EquityShareCapital": 50.0,
            "OtherEquity": 150.0,
        },
    )
    stats = fundamentals.run(conn)
    assert stats == {"written": 1, "skipped": 0, "errors": 0, "requested": 1}
    row = conn.execute(
        """
        SELECT operating_margin, source, raw_json FROM stock_fundamentals
        WHERE symbol='TEST'
        """
    ).fetchone()
    assert row[0] == pytest.approx((216 - 151.2 + 10 + 20) / 216)
    assert row[1] == fundamentals.SOURCE
    before = conn.execute(
        "SELECT md5(raw_json || fetched_at::TEXT) FROM stock_fundamentals WHERE symbol='TEST'"
    ).fetchone()[0]
    fundamentals.run(conn)  # replay must not churn
    after = conn.execute(
        "SELECT md5(raw_json || fetched_at::TEXT) FROM stock_fundamentals WHERE symbol='TEST'"
    ).fetchone()[0]
    assert before == after


def test_recycle_taxonomy_requeues_symbols(conn):
    db.upsert_universe_row(conn, symbol="ORBIT", series="EQ", source="fx", fetched_at=FETCHED)
    add_filing(
        conn,
        url="bank1",
        symbol="ORBIT",
        period_end=FY26,
        taxonomy="banking",
        facts={"D:InterestEarned": 1.0},
    )
    db.upsert_stock_filing(
        conn,
        xbrl_url="shp1",
        symbol="ORBIT",
        source="fixture",
        filing_type="shareholding",
        period_end=FY26,
        taxonomy="shp",
        fetched_at=FETCHED,
    )
    removed = __import__("invest.xbrl_crawl", fromlist=["x"]).recycle_taxonomy(conn, "banking")
    assert removed == 1
    left = {
        r[0]
        for r in conn.execute(
            "SELECT filing_type FROM stock_filing WHERE symbol='ORBIT'"
        ).fetchall()
    }
    assert left == {"shareholding"}  # SHP untouched
    assert conn.execute("SELECT COUNT(*) FROM stock_filing_fact").fetchone()[0] == 0


def test_legacy_quarter_span_ytd_recovery_and_revision_preference(conn):
    # Legacy filing A (original revision, fewer facts): FY26 revenue under a
    # mislabeled quarter span in a Four-prefixed context.
    conn.execute(
        "INSERT INTO stock_filing_context VALUES (?,?,?,?,?,?)",
        ["legA", "FourD", date(2026, 1, 1), FY26, None, None],
    )
    db.upsert_stock_filing(
        conn,
        xbrl_url="legA",
        symbol="TEST",
        source="fixture",
        filing_type="financial_annual_legacy",
        period_end=FY26,
        consolidation="Consolidated",
        fetched_at=FETCHED,
    )
    conn.executemany(
        "INSERT INTO stock_filing_fact (xbrl_url, fact_name, context_ref, value) VALUES (?,?,?,?)",
        [("legA", "RevenueFromOperations", "FourD", "216.0")],
    )
    # Revised filing B (richer): true Apr..Mar span plus instants -> must win.
    conn.execute(
        "INSERT INTO stock_filing_context VALUES (?,?,?,?,?,?)",
        ["legB", "FYctx", date(2025, 4, 1), FY26, None, None],
    )
    conn.execute(
        "INSERT INTO stock_filing_context VALUES (?,?,?,?,?,?)",
        ["legB", "Inst", None, None, FY26, None],
    )
    db.upsert_stock_filing(
        conn,
        xbrl_url="legB",
        symbol="TEST",
        source="fixture",
        filing_type="financial_integrated",
        period_end=FY26,
        consolidation="Consolidated",
        fetched_at=FETCHED,
    )
    conn.executemany(
        "INSERT INTO stock_filing_fact (xbrl_url, fact_name, context_ref, value) VALUES (?,?,?,?)",
        [
            ("legB", "RevenueFromOperations", "FYctx", "220.0"),
            ("legB", "ProfitLossForPeriod", "FYctx", "30.0"),
            ("legB", "EquityShareCapital", "Inst", "50.0"),
            ("legB", "OtherEquity", "Inst", "150.0"),
        ],
    )
    m = fundamentals.compute_symbol(conn, "TEST")
    assert m["as_of"] == FY26
    assert m["revenue_growth_yoy"] is None
    assert m["debt_to_equity"] is None
    assert m["roe"] == pytest.approx(30 / 200)


def test_legacy_four_prefix_recovers_ytd_when_no_exact_span(conn):
    # Only a mislabeled quarter-span FourD context exists -> YTD recovered.
    conn.execute(
        "INSERT INTO stock_filing_context VALUES (?,?,?,?,?,?)",
        ["legC", "oned", date(2026, 1, 1), FY26, None, None],
    )
    conn.execute(
        "INSERT INTO stock_filing_context VALUES (?,?,?,?,?,?)",
        ["legC", "fourd", date(2026, 1, 1), FY26, None, None],
    )
    db.upsert_stock_filing(
        conn,
        xbrl_url="legC",
        symbol="SOLO",
        source="fixture",
        filing_type="financial_annual_legacy",
        period_end=FY26,
        consolidation="Consolidated",
        fetched_at=FETCHED,
    )
    conn.executemany(
        "INSERT INTO stock_filing_fact (xbrl_url, fact_name, context_ref, value) VALUES (?,?,?,?)",
        [
            ("legC", "RevenueFromOperations", "oned", "60.0"),
            ("legC", "RevenueFromOperations", "fourd", "216.0"),
        ],
    )
    m = fundamentals.compute_symbol(conn, "SOLO")
    audit = __import__("json").loads(m["raw_json"])
    assert audit["2026-03-31"]["duration"]["RevenueFromOperations"] == 216.0


def test_ambiguous_legacy_candidates_are_dropped(conn):
    conn.execute(
        "INSERT INTO stock_filing_context VALUES (?,?,?,?,?,?)",
        ["amb1", "fourd_a", date(2026, 1, 1), FY26, None, None],
    )
    conn.execute(
        "INSERT INTO stock_filing_context VALUES (?,?,?,?,?,?)",
        ["amb1", "fourd_b", date(2026, 1, 1), FY26, None, None],
    )
    db.upsert_stock_filing(
        conn,
        xbrl_url="amb1",
        symbol="AMBIG",
        source="fixture",
        filing_type="financial_annual_legacy",
        period_end=FY26,
        consolidation="Consolidated",
        fetched_at=FETCHED,
    )
    conn.executemany(
        "INSERT INTO stock_filing_fact (xbrl_url, fact_name, context_ref, value) VALUES (?,?,?,?)",
        [
            ("amb1", "RevenueFromOperations", "fourd_a", "100.0"),
            ("amb1", "RevenueFromOperations", "fourd_b", "999.0"),
        ],
    )
    m = fundamentals.compute_symbol(conn, "AMBIG")
    import json as _json

    audit = _json.loads(m["raw_json"])
    assert "RevenueFromOperations" not in audit["2026-03-31"]["duration"]


def test_yoy_with_nonpositive_base_stays_null(conn):
    add_filing(
        conn,
        url="loss25",
        period_end=FY25,
        facts={"D:RevenueFromOperations": 0.0},
    )
    add_filing(
        conn,
        url="ok26",
        period_end=FY26,
        facts={"D:RevenueFromOperations": 100.0},
    )
    m = fundamentals.compute_symbol(conn, "TEST")
    assert m["revenue_growth_yoy"] is None  # zero base must not crash or invert


def test_run_isolates_poisoned_symbol(conn, monkeypatch):
    for sym in ("GOOD1", "BAD1"):
        db.upsert_universe_row(conn, symbol=sym, series="EQ", source="fx", fetched_at=FETCHED)
    add_filing(
        conn,
        url="g1",
        symbol="GOOD1",
        period_end=FY26,
        facts={"D:RevenueFromOperations": 100.0, "D:ProfitLossForPeriod": 10.0},
    )
    add_filing(
        conn,
        url="b1",
        symbol="BAD1",
        period_end=FY26,
        facts={"D:RevenueFromOperations": 100.0},
    )
    real_upsert = db.upsert_stock_fundamental

    def flaky(conn_, **row):
        if row["symbol"] == "BAD1":
            raise RuntimeError("simulated poisoned symbol")
        return real_upsert(conn_, **row)

    monkeypatch.setattr(fundamentals.db, "upsert_stock_fundamental", flaky)
    stats = fundamentals.run(conn, ["GOOD1", "BAD1"])
    assert stats["requested"] == 2 and stats["written"] == 1 and stats["errors"] == 1


def test_long_span_fallback_is_input_order_independent():
    # Regression: _pick_filings used the final row's leaked period_end rather
    # than each bucket's fy_end, making an unordered SQL result affect facts.
    rows = [
        (
            FY26,
            "Consolidated",
            "u26",
            "RevenueFromOperations",
            "100",
            "ctx",
            date(2025, 5, 1),
            FY26,
            None,
        ),
        (
            date(2024, 3, 31),
            "Consolidated",
            "u24",
            "RevenueFromOperations",
            "80",
            "ctx",
            date(2023, 5, 1),
            date(2024, 3, 31),
            None,
        ),
    ]
    assert fundamentals._pick_filings(rows) == fundamentals._pick_filings(list(reversed(rows)))
    assert fundamentals._pick_filings(rows)[FY26]["duration"]["RevenueFromOperations"] == 100


def test_fcf_eps_and_direct_comparables(conn):
    for year, cfo, capex, eps in ((2024, 30, 10, -2), (2025, 40, 15, 1), (2026, 50, 20, 3)):
        end = date(year, 3, 31)
        add_filing(
            conn,
            url=f"u{year}",
            period_end=end,
            facts={
                "D:RevenueFromOperations": 100,
                "D:CashFlowsFromUsedInOperatingActivities": cfo,
                "D:PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": capex,
                "D:BasicEarningsLossPerShareFromContinuingOperations": eps,
            },
        )
    m = fundamentals.compute_symbol(conn, "TEST")
    assert m["free_cash_flow"] == 30
    assert m["free_cash_flow_3y"] == (20 + 25 + 30)
    assert m["eps"] == 3 and m["eps_previous"] == 1


def test_official_shareholding_dimensions(conn):
    db.upsert_stock_filing(
        conn,
        xbrl_url="shp",
        symbol="TEST",
        source="fixture",
        filing_type="shareholding",
        period_end=FY26,
        taxonomy="shp",
        fetched_at=FETCHED,
    )
    for context, member, value in (
        ("p", "ShareholdingOfPromoterAndPromoterGroupMember", "0.552"),
        ("f", "InstitutionsForeignMember", "0.081"),
    ):
        conn.execute(
            "INSERT INTO stock_filing_context VALUES (?,?,?,?,?,?)",
            [
                "shp",
                context,
                None,
                None,
                FY26,
                f"in-bse-shp:CategoryOfShareholdersAxis=in-bse-shp:{member}",
            ],
        )
        conn.execute(
            "INSERT INTO stock_filing_fact VALUES (?,?,?,?,NULL,NULL)",
            ["shp", "ShareholdingAsAPercentageOfTotalNumberOfShares", context, value],
        )
    assert fundamentals.shareholding_percentages(conn, "TEST") == pytest.approx((0.552, 0.081))


def test_piotroski_requires_and_scores_all_nine_signals(conn):
    for end, revenue, profit, cfo, assets, debt, current_assets, material_cost in (
        (FY25, 100.0, 10.0, 12.0, 200.0, 60.0, 75.0, 60.0),
        (FY26, 120.0, 15.0, 20.0, 210.0, 50.0, 100.0, 60.0),
    ):
        add_filing(
            conn,
            url=f"p{end.year}",
            period_end=end,
            facts={
                "D:RevenueFromOperations": revenue,
                "D:ProfitLossForPeriod": profit,
                "D:CashFlowsFromUsedInOperatingActivities": cfo,
                "D:CostOfMaterialsConsumed": material_cost,
                "Assets": assets,
                "CurrentAssets": current_assets,
                "CurrentLiabilities": 50.0,
                "BorrowingsCurrent": debt,
                "BorrowingsNoncurrent": 0.0,
                "EquityShareCapital": 25.0,
            },
        )
    assert fundamentals.compute_symbol(conn, "TEST")["piotroski_score"] == 9


def test_piotroski_incomplete_inputs_stay_null(conn):
    add_filing(
        conn,
        url="partial",
        period_end=FY26,
        facts={"D:ProfitLossForPeriod": 10.0, "Assets": 100.0},
    )
    assert fundamentals.compute_symbol(conn, "TEST")["piotroski_score"] is None


def test_piotroski_does_not_bridge_a_missing_fiscal_year(conn):
    # Even complete-looking FY24/FY26 facts cannot stand in for adjacent YoY.
    for end in (date(2024, 3, 31), FY26):
        add_filing(
            conn,
            url=f"gap{end.year}",
            period_end=end,
            facts={
                "D:RevenueFromOperations": 100.0,
                "D:ProfitLossForPeriod": 10.0,
                "D:CashFlowsFromUsedInOperatingActivities": 12.0,
                "D:CostOfMaterialsConsumed": 50.0,
                "Assets": 200.0,
                "CurrentAssets": 100.0,
                "CurrentLiabilities": 50.0,
                "BorrowingsCurrent": 20.0,
                "BorrowingsNoncurrent": 10.0,
                "EquityShareCapital": 25.0,
            },
        )
    assert fundamentals.compute_symbol(conn, "TEST")["piotroski_score"] is None


def test_piotroski_requires_comparable_gross_cost_tag_sets(conn):
    for end, extra in ((FY25, {}), (FY26, {"D:PurchasesOfStockInTrade": 5.0})):
        add_filing(
            conn,
            url=f"tags{end.year}",
            period_end=end,
            facts={
                "D:RevenueFromOperations": 100.0,
                "D:ProfitLossForPeriod": 10.0,
                "D:CashFlowsFromUsedInOperatingActivities": 12.0,
                "D:CostOfMaterialsConsumed": 50.0,
                "Assets": 200.0,
                "CurrentAssets": 100.0,
                "CurrentLiabilities": 50.0,
                "BorrowingsCurrent": 20.0,
                "BorrowingsNoncurrent": 10.0,
                "EquityShareCapital": 25.0,
                **extra,
            },
        )
    assert fundamentals.compute_symbol(conn, "TEST")["piotroski_score"] is None


def test_legacy_four_equity_values_are_treated_as_fy_end_instant():
    rows = [
        (
            FY26,
            "Consolidated",
            "u",
            "ProfitLossForPeriod",
            "25",
            "FourD",
            date(2025, 4, 1),
            FY26,
            None,
        ),
        (
            FY26,
            "Consolidated",
            "u",
            "PaidUpValueOfEquityShareCapital",
            "100",
            "FourD",
            date(2025, 4, 1),
            FY26,
            None,
        ),
        (
            FY26,
            "Consolidated",
            "u",
            "ReserveExcludingRevaluationReserves",
            "400",
            "FourD",
            date(2025, 4, 1),
            FY26,
            None,
        ),
    ]
    metrics = fundamentals.fy_metrics(fundamentals._pick_filings(rows))
    assert metrics["roe"] == pytest.approx(25 / 500)


def test_real_instant_equity_wins_over_legacy_fourd_fallback_in_any_order():
    fallback = (
        FY26,
        "Consolidated",
        "u",
        "PaidUpValueOfEquityShareCapital",
        "100",
        "FourD",
        date(2025, 4, 1),
        FY26,
        None,
    )
    instant = (
        FY26,
        "Consolidated",
        "u",
        "PaidUpValueOfEquityShareCapital",
        "150",
        "Instant",
        None,
        None,
        FY26,
    )
    profit = (
        FY26,
        "Consolidated",
        "u",
        "ProfitLossForPeriod",
        "15",
        "FourD",
        date(2025, 4, 1),
        FY26,
        None,
    )
    reserve = (
        FY26,
        "Consolidated",
        "u",
        "ReserveExcludingRevaluationReserves",
        "150",
        "FourD",
        date(2025, 4, 1),
        FY26,
        None,
    )
    for rows in ([fallback, instant, profit, reserve], [instant, fallback, profit, reserve]):
        metrics = fundamentals.fy_metrics(fundamentals._pick_filings(rows))
        assert metrics["roe"] == pytest.approx(15 / 300)
