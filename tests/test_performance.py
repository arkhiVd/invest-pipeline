"""T11.4 hand-worked XIRR and TWR oracle fixtures."""

from datetime import UTC, date, datetime
from decimal import Decimal

import duckdb
import pytest

from invest import accounting, db, performance


def test_xirr_one_year_fixture_is_ten_percent():
    flows = [
        performance.CashFlow(date(2025, 1, 1), Decimal("-100")),
        performance.CashFlow(date(2026, 1, 1), Decimal("110")),
    ]
    assert performance.xirr(flows) == pytest.approx(0.10, abs=1e-9)
    assert performance.xnpv(performance.xirr(flows), flows) == pytest.approx(0, abs=1e-8)


def test_xirr_irregular_flow_solves_to_zero_npv():
    flows = [
        performance.CashFlow(date(2025, 1, 1), Decimal("-1000")),
        performance.CashFlow(date(2025, 4, 1), Decimal("-250")),
        performance.CashFlow(date(2025, 10, 1), Decimal("100")),
        performance.CashFlow(date(2026, 2, 1), Decimal("1400")),
    ]
    rate = performance.xirr(flows)
    assert rate == pytest.approx(0.1971166540, abs=1e-9)
    assert performance.xnpv(rate, flows) == pytest.approx(0, abs=1e-7)


def test_xirr_rejects_one_sided_flows():
    with pytest.raises(performance.PerformanceError, match="positive and one negative"):
        performance.xirr(
            [
                performance.CashFlow(date(2025, 1, 1), Decimal("-1")),
                performance.CashFlow(date(2025, 2, 1), Decimal("-2")),
            ]
        )


def test_twr_hand_worked_external_flow_fixture():
    periods = [
        performance.TwrPeriod(date(2025, 1, 1), date(2025, 2, 1), Decimal("100"), Decimal("110")),
        performance.TwrPeriod(
            date(2025, 2, 1),
            date(2025, 3, 1),
            Decimal("110"),
            Decimal("132"),
            Decimal("11"),
        ),
    ]
    assert performance.twr(periods) == pytest.approx(0.21)


def test_twr_rejects_missing_flow_boundary_period():
    with pytest.raises(performance.PerformanceError, match="not contiguous"):
        performance.twr(
            [
                performance.TwrPeriod(
                    date(2025, 1, 1), date(2025, 2, 1), Decimal("100"), Decimal("110")
                ),
                performance.TwrPeriod(
                    date(2025, 2, 2), date(2025, 3, 1), Decimal("110"), Decimal("120")
                ),
            ]
        )


def test_account_inputs_use_investor_flow_signs_and_terminal_value():
    conn = duckdb.connect()
    db.init_schema(conn)
    accounting.install_schema(conn)
    conn.execute(
        "INSERT INTO portfolio_account VALUES (?,?,?,?,?,?)",
        ["acct", "VESTED", "US", "USD", "UNPROVEN", datetime.now(UTC)],
    )
    conn.execute(
        "INSERT INTO accounting_import_run VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            "imp",
            "acct",
            "VESTED_TRANSACTIONS",
            "a" * 64,
            date(2025, 1, 1),
            date(2025, 12, 31),
            2,
            "b" * 64,
            datetime.now(UTC),
            None,
            None,
        ],
    )
    conn.executemany(
        "INSERT INTO portfolio_cash_flow VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                "a",
                "imp",
                "acct",
                datetime(2025, 1, 1),
                "DEPOSIT",
                100,
                "USD",
                "SOURCE_PAYMENT_DATE",
                "a",
            ),
            (
                "b",
                "imp",
                "acct",
                datetime(2025, 6, 1),
                "WITHDRAWAL",
                10,
                "USD",
                "SOURCE_PAYMENT_DATE",
                "b",
            ),
        ],
    )
    flows = performance.account_xirr_inputs(conn, "acct", date(2025, 12, 31), Decimal("120"))
    assert [flow.amount for flow in flows] == [Decimal("-100"), Decimal("10"), Decimal("120")]
    conn.close()


def test_native_summary_and_allocation_use_latest_source_values():
    conn = duckdb.connect()
    db.init_schema(conn)
    accounting.install_schema(conn)
    now = datetime.now(UTC)
    conn.execute(
        "INSERT INTO portfolio_account VALUES (?,?,?,?,?,?)",
        ["acct", "VESTED", "US", "USD", "UNPROVEN", now],
    )
    conn.execute(
        "INSERT INTO accounting_import_run VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            "imp",
            "acct",
            "VESTED_HOLDINGS",
            "a" * 64,
            date(2025, 1, 1),
            date(2025, 12, 31),
            1,
            "b" * 64,
            now,
            None,
            None,
        ],
    )
    conn.execute(
        "INSERT INTO portfolio_instrument VALUES (?,?,?,?,?,?)",
        ["stock", "US", "ACME", "EQUITY", "USD", "source"],
    )
    conn.executemany(
        "INSERT INTO portfolio_valuation VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "old",
                "imp",
                "acct",
                "stock",
                datetime(2025, 6, 1),
                100,
                90,
                "USD",
                "SOURCE_SNAPSHOT",
                "old",
            ),
            (
                "new",
                "imp",
                "acct",
                "stock",
                datetime(2025, 12, 31),
                120,
                90,
                "USD",
                "SOURCE_SNAPSHOT",
                "new",
            ),
            (
                "cash",
                "imp",
                "acct",
                None,
                datetime(2025, 12, 30),
                30,
                30,
                "USD",
                "SOURCE_SNAPSHOT",
                "cash",
            ),
        ],
    )
    summary = performance.native_account_summary(conn, "acct", date(2025, 12, 31))
    assert summary["current_value"] == Decimal("150.0000000000")
    assert summary["unrealized_return"] == Decimal("30.0000000000")
    allocation = performance.native_allocation(conn, "acct", date(2025, 12, 31))
    assert sum(row["weight"] for row in allocation) == Decimal("1")
    assert {row["asset_class"] for row in allocation} == {"EQUITY", "CASH"}
    converted = performance.convert_allocation(allocation, Decimal("80"))
    assert sum(row["base_value"] for row in converted) == Decimal("12000.0000000000")
    conn.close()


def test_managed_product_allocation_requires_dated_source_membership():
    conn = duckdb.connect()
    db.init_schema(conn)
    accounting.install_schema(conn)
    now = datetime.now(UTC)
    conn.execute(
        "INSERT INTO portfolio_account VALUES (?,?,?,?,?,?)",
        ["acct", "VESTED", "US", "USD", "UNPROVEN", now],
    )
    conn.execute(
        "INSERT INTO accounting_import_run VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            "imp",
            "acct",
            "MANAGED_PRODUCT_MEMBERSHIP",
            "a" * 64,
            date(2025, 1, 1),
            date(2025, 12, 31),
            2,
            "b" * 64,
            now,
            None,
            None,
        ],
    )
    conn.executemany(
        "INSERT INTO portfolio_instrument VALUES (?,?,?,?,?,?)",
        [
            ("one", "US", "ONE", "EQUITY", "USD", "one-source"),
            ("two", "US", "TWO", "EQUITY", "USD", "two-source"),
        ],
    )
    conn.execute(
        "INSERT INTO managed_product VALUES (?,?,?,?)", ["vest", "acct", "Vest A", "vest-a"]
    )
    conn.executemany(
        "INSERT INTO portfolio_valuation VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "v1",
                "imp",
                "acct",
                "one",
                datetime(2025, 12, 31),
                60,
                50,
                "USD",
                "SOURCE_SNAPSHOT",
                "v1",
            ),
            (
                "v2",
                "imp",
                "acct",
                "two",
                datetime(2025, 12, 31),
                30,
                20,
                "USD",
                "SOURCE_SNAPSHOT",
                "v2",
            ),
            (
                "cash",
                "imp",
                "acct",
                None,
                datetime(2025, 12, 31),
                10,
                10,
                "USD",
                "SOURCE_SNAPSHOT",
                "cash",
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO managed_product_membership VALUES (?,?,?,?,?,?,?)",
        [
            ("m1", "imp", "vest", "one", date(2025, 1, 1), None, "SOURCE"),
            ("m2", "imp", "vest", "two", date(2025, 7, 1), None, "SOURCE"),
        ],
    )
    result = performance.managed_product_allocation(conn, "acct", date(2025, 12, 31))
    assert result["status"] == "EXACT"
    assert [
        (row["product"], row["native_value"], row["weight"]) for row in result["allocation"]
    ] == [
        ("CASH", Decimal("10.0000000000"), Decimal("0.1")),
        ("Vest A", Decimal("90.0000000000"), Decimal("0.9")),
    ]
    assert len(result["input_fingerprint"]) == 64
    conn.close()


def test_managed_product_allocation_fails_closed_on_missing_or_overlap():
    conn = duckdb.connect()
    db.init_schema(conn)
    accounting.install_schema(conn)
    now = datetime.now(UTC)
    conn.execute(
        "INSERT INTO portfolio_account VALUES (?,?,?,?,?,?)",
        ["acct", "VESTED", "US", "USD", "UNPROVEN", now],
    )
    conn.execute(
        "INSERT INTO accounting_import_run VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ["imp", "acct", "MEMBERSHIP", "a" * 64, None, None, 0, "b" * 64, now, None, None],
    )
    conn.execute(
        "INSERT INTO portfolio_instrument VALUES (?,?,?,?,?,?)",
        ["one", "US", "ONE", "EQUITY", "USD", "one-source"],
    )
    conn.execute(
        "INSERT INTO portfolio_valuation VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            "v1",
            "imp",
            "acct",
            "one",
            datetime(2025, 12, 31),
            60,
            50,
            "USD",
            "SOURCE_SNAPSHOT",
            "v1",
        ],
    )
    missing = performance.managed_product_allocation(conn, "acct", date(2025, 12, 31))
    assert missing["status"] == "UNAVAILABLE"
    assert "missing structured product membership" in missing["exclusions"][0]
    conn.executemany(
        "INSERT INTO managed_product VALUES (?,?,?,?)",
        [("a", "acct", "Vest A", "a"), ("b", "acct", "Vest B", "b")],
    )
    conn.executemany(
        "INSERT INTO managed_product_membership VALUES (?,?,?,?,?,?,?)",
        [
            ("m1", "imp", "a", "one", date(2025, 1, 1), None, "SOURCE"),
            ("m2", "imp", "b", "one", date(2025, 1, 1), None, "SOURCE"),
        ],
    )
    overlap = performance.managed_product_allocation(conn, "acct", date(2025, 12, 31))
    assert overlap["status"] == "UNAVAILABLE"
    assert "overlapping structured product membership" in overlap["exclusions"][0]
    conn.close()


def test_estimated_and_unavailable_results_persist_assumptions():
    conn = duckdb.connect()
    db.init_schema(conn)
    accounting.install_schema(conn)
    conn.execute(
        "INSERT INTO portfolio_account VALUES (?,?,?,?,?,?)",
        ["acct", "ZERODHA", "EQUITY", "INR", "UNPROVEN", datetime.now(UTC)],
    )
    payload = performance.result_payload(
        account_id="acct",
        metric="XIRR",
        status="ESTIMATED",
        value=0.1,
        currency="INR",
        coverage_start=date(2025, 1, 1),
        coverage_end=date(2026, 1, 1),
        assumptions=["ex-date substitutes for dividend payment date"],
        exclusions=["Coin"],
        residuals=[],
        inputs={"flows": 2},
    )
    now = datetime.now(UTC)
    assert performance.store_result(conn, payload, now) == "stored"
    assert performance.store_result(conn, payload, now) == "duplicate"
    row = conn.execute(
        "SELECT status,assumptions_json,exclusions_json FROM portfolio_performance_result"
    ).fetchone()
    assert row == (
        "ESTIMATED",
        '["ex-date substitutes for dividend payment date"]',
        '["Coin"]',
    )
    unavailable = performance.result_payload(
        account_id="acct",
        metric="TWR",
        status="UNAVAILABLE",
        value=None,
        currency="INR",
        coverage_start=None,
        coverage_end=None,
        assumptions=[],
        exclusions=["flow-boundary valuations absent"],
        residuals=[],
        inputs={"reason": "missing valuations"},
    )
    assert performance.store_result(conn, unavailable, now) == "stored"
    conn.close()
