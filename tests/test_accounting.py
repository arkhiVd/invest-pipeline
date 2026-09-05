"""T11.2 disposable accounting-schema invariants."""

from datetime import UTC, date
from datetime import datetime as dt

import duckdb
import pytest

from invest import accounting, db


@pytest.fixture()
def conn():
    c = duckdb.connect()
    db.init_schema(c)
    accounting.install_schema(c)
    c.execute(
        "INSERT INTO portfolio_account VALUES (?,?,?,?,?,?)",
        ["acct", "ZERODHA", "EQUITY", "INR", "UNPROVEN", dt.now(UTC)],
    )
    yield c
    c.close()


def add_import(conn, import_id="imp", digest="a" * 64, supersedes=None, reason=None):
    conn.execute(
        "INSERT INTO accounting_import_run VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            import_id,
            "acct",
            "TRADEBOOK",
            digest,
            date(2025, 1, 1),
            date(2025, 12, 31),
            1,
            "b" * 64,
            dt.now(UTC),
            supersedes,
            reason,
        ],
    )


def test_install_schema_is_atomic_and_records_v20():
    c = duckdb.connect()
    try:
        db.init_schema(c)
        accounting.install_schema(c)
        tables = {row[0] for row in c.execute("SHOW TABLES").fetchall()}
        assert {
            "portfolio_account",
            "portfolio_instrument",
            "accounting_import_run",
            "portfolio_transaction",
            "portfolio_cash_flow",
            "portfolio_income",
            "portfolio_fee",
            "portfolio_tax",
            "portfolio_corporate_action",
            "portfolio_valuation",
            "portfolio_fx_rate",
            "portfolio_tax_lot",
            "broker_reported_summary",
            "accounting_completeness",
            "portfolio_performance_result",
            "portfolio_allocation_result",
            "managed_product",
            "managed_product_membership",
        } <= tables
        assert c.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 20
    finally:
        c.close()


def test_content_addressed_import_replay_is_unique(conn):
    add_import(conn)
    with pytest.raises(duckdb.ConstraintException):
        add_import(conn, import_id="different-id")


def test_correction_requires_reason_and_existing_parent(conn):
    add_import(conn)
    with pytest.raises(duckdb.ConstraintException):
        add_import(conn, import_id="bad", digest="c" * 64, supersedes="imp")
    add_import(
        conn,
        import_id="corrected",
        digest="d" * 64,
        supersedes="imp",
        reason="broker correction",
    )
    assert conn.execute(
        "SELECT supersedes_import_id FROM accounting_import_run WHERE import_id='corrected'"
    ).fetchone() == ("imp",)


def test_estimated_date_evidence_cannot_look_exact(conn):
    add_import(conn)
    conn.execute(
        "INSERT INTO portfolio_income VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            "income",
            "imp",
            "acct",
            None,
            dt(2025, 8, 14, tzinfo=UTC),
            "DIVIDEND",
            10,
            "INR",
            "SUBSTITUTED_EX_DATE",
            "source-row",
        ],
    )
    assert conn.execute(
        "SELECT date_evidence FROM portfolio_income WHERE event_id='income'"
    ).fetchone() == ("SUBSTITUTED_EX_DATE",)
    with pytest.raises(duckdb.ConstraintException):
        conn.execute("UPDATE portfolio_income SET date_evidence='EXACT' WHERE event_id='income'")


def test_managed_membership_rejects_reconstructed_evidence(conn):
    add_import(conn)
    conn.execute(
        "INSERT INTO portfolio_instrument VALUES (?,?,?,?,?,?)",
        ["instrument", "US", "TEST", "EQUITY", "USD", "source-hash"],
    )
    conn.execute("INSERT INTO managed_product VALUES (?,?,?,?)", ["product", "acct", "Vest", "p"])
    with pytest.raises(duckdb.ConstraintException):
        conn.execute(
            "INSERT INTO managed_product_membership VALUES (?,?,?,?,?,?,?)",
            [
                "membership",
                "imp",
                "product",
                "instrument",
                date(2025, 1, 1),
                None,
                "RECONSTRUCTED",
            ],
        )


def test_completeness_assessment_is_explicit_and_replay_safe(conn):
    statuses = {
        "transactions": "ESTIMATED",
        "cash_flows": "COMPLETE",
        "income": "ESTIMATED",
        "valuations": "ESTIMATED",
        "corporate_actions": "MISSING",
        "fx": "NOT_APPLICABLE",
    }
    first = accounting.store_completeness(
        conn,
        account_id="acct",
        coverage_start=date(2025, 1, 1),
        coverage_end=date(2025, 12, 31),
        statuses=statuses,
        assumptions=["dividend ex-date substitutes for payment date"],
        exclusions=["Coin"],
        residuals=[],
        methodology_version="portfolio-completeness-2026.1",
    )
    second = accounting.store_completeness(
        conn,
        account_id="acct",
        coverage_start=date(2025, 1, 1),
        coverage_end=date(2025, 12, 31),
        statuses=statuses,
        assumptions=["dividend ex-date substitutes for payment date"],
        exclusions=["Coin"],
        residuals=[],
        methodology_version="portfolio-completeness-2026.1",
    )
    assert first == second
    assert conn.execute("SELECT count(*) FROM accounting_completeness").fetchone()[0] == 1
    with pytest.raises(ValueError, match="incomplete"):
        accounting.store_completeness(
            conn,
            account_id="acct",
            coverage_start=None,
            coverage_end=None,
            statuses={"transactions": "COMPLETE"},
            assumptions=[],
            exclusions=[],
            residuals=[],
            methodology_version="x",
        )


def test_invalid_coverage_and_negative_amounts_fail_closed(conn):
    with pytest.raises(duckdb.ConstraintException):
        conn.execute(
            "INSERT INTO accounting_import_run VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                "bad-range",
                "acct",
                "LEDGER",
                "e" * 64,
                date(2025, 2, 1),
                date(2025, 1, 1),
                0,
                "f" * 64,
                dt.now(UTC),
                None,
                None,
            ],
        )
    add_import(conn)
    with pytest.raises(duckdb.ConstraintException):
        conn.execute(
            "INSERT INTO portfolio_cash_flow VALUES (?,?,?,?,?,?,?,?,?)",
            [
                "flow",
                "imp",
                "acct",
                dt.now(UTC),
                "DEPOSIT",
                -1,
                "INR",
                "SOURCE_POSTING_DATE",
                "row",
            ],
        )
