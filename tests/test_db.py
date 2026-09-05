"""T1.3 acceptance: schema integrity, upsert idempotence, methodology gates."""

import hashlib
import json
from datetime import date
from datetime import datetime as dt

import duckdb
import pytest

from invest import db, kite

SCHEME = dict(
    scheme_code=900001,
    display_name="ORBIT Flexi Cap (Direct)",
    name="ORBIT Flexi Cap Fund - Growth Option - Direct Plan",
    amc="ORBIT Mutual Fund",
    isin="INF179K01UT0",
    category="Equity Scheme - Flexi Cap Fund",
)

NAV_ROWS = [(date(2026, 8, 20), 100.0), (date(2026, 8, 21), 101.5)]

RETURN_ROW = dict(
    scheme_code=SCHEME["scheme_code"],
    lookback="3Y",
    fund_return=0.224,
    category_avg_return=0.21,
    result="+1.4% (O)",
    benchmark="Nifty 500 TRI",
    frequency="daily",
    methodology_version="v1",
    calculated_at=dt(2026, 8, 25, 3, 0, 0),
)

RISK_ROW = dict(
    scheme_code=SCHEME["scheme_code"],
    lookback="3Y",
    sd=0.1171,
    category_sd=0.1205,
    volatility_class="Lower Volatile",
    beta=0.92,
    category_beta=0.95,
    risk_profile="Conservative/Moderate",
    sharpe=1.02,
    benchmark="Nifty 500 TRI",
    frequency="daily",
    methodology_version="v1",
    calculated_at=dt(2026, 8, 25, 3, 0, 0),
)


@pytest.fixture()
def conn():
    c = duckdb.connect()  # in-memory
    db.init_schema(c)
    db.upsert_scheme(c, **SCHEME)
    yield c
    c.close()


def test_v3_database_gains_v4_stock_tables():
    c = duckdb.connect()
    try:
        c.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMP)"
        )
        c.executemany(
            "INSERT INTO schema_migrations VALUES (?, current_timestamp)", [(1,), (2,), (3,)]
        )
        db.init_schema(c)
        tables = {r[0] for r in c.execute("SHOW TABLES").fetchall()}
        assert {"stock_fundamentals", "stock_filing"} <= tables
        assert (
            c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == db.SCHEMA_VERSION
        )
    finally:
        c.close()


def test_schema_init_is_idempotent():
    c = duckdb.connect()
    try:
        db.init_schema(c)
        db.init_schema(c)
        rows = c.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        assert [r[0] for r in rows] == list(db._MIGRATIONS)  # every migration recorded once
    finally:
        c.close()


def test_scheme_upsert_idempotent(conn):
    before = db.fingerprint(conn, "mf_scheme")
    db.upsert_scheme(conn, **SCHEME)  # exact replay
    assert db.fingerprint(conn, "mf_scheme") == before


def test_scheme_upsert_updates_changed_field(conn):
    changed = {**SCHEME, "aaum_cr_quarterly_avg": 123.45}
    db.upsert_scheme(conn, **changed)
    (aum,) = conn.execute(
        "SELECT aaum_cr_quarterly_avg FROM mf_scheme WHERE scheme_code = ?",
        [SCHEME["scheme_code"]],
    ).fetchone()
    assert aum == pytest.approx(123.45)
    (count,) = conn.execute("SELECT COUNT(*) FROM mf_scheme").fetchone()
    assert count == 1


def test_nav_reload_twice_leaves_state_unchanged(conn):
    db.upsert_navs(conn, SCHEME["scheme_code"], NAV_ROWS)
    before = db.fingerprint(conn, "mf_nav")
    db.upsert_navs(conn, SCHEME["scheme_code"], NAV_ROWS)  # full reload
    assert db.fingerprint(conn, "mf_nav") == before

    # same day, corrected value: count stays, value updates
    db.upsert_navs(conn, SCHEME["scheme_code"], [(date(2026, 8, 21), 101.6)])
    (count,) = conn.execute("SELECT COUNT(*) FROM mf_nav").fetchone()
    assert count == len(NAV_ROWS)
    (_, nav) = conn.execute(
        "SELECT nav_date, nav FROM mf_nav WHERE nav_date = ?",
        [date(2026, 8, 21)],
    ).fetchone()
    assert nav == pytest.approx(101.6)


def test_nav_requires_parent_scheme(conn):
    with pytest.raises(duckdb.ConstraintException):
        db.upsert_navs(conn, 999999999, NAV_ROWS)


def test_return_metric_without_benchmark_rejected(conn):
    bad = {k: v for k, v in RETURN_ROW.items() if k != "benchmark"}
    with pytest.raises((duckdb.ConstraintException, ValueError)):
        db.upsert_return_metric(conn, **bad)


def test_risk_metric_missing_calculated_at_rejected(conn):
    bad = {k: v for k, v in RISK_ROW.items() if k != "calculated_at"}
    with pytest.raises(ValueError):  # caught by helper before SQL
        db.upsert_risk_metric(conn, **bad)


def test_v4_database_gains_v5_tables():
    c = duckdb.connect()
    try:
        c.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMP)"
        )
        c.executemany(
            "INSERT INTO schema_migrations VALUES (?, current_timestamp)", [(1,), (2,), (3,), (4,)]
        )
        db.init_schema(c)
        tables = {r[0] for r in c.execute("SHOW TABLES").fetchall()}
        assert {"stock_universe", "stock_price", "ingest_watermark"} <= tables
        assert (
            c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == db.SCHEMA_VERSION
        )
    finally:
        c.close()


def test_v6_database_gains_v7_tombstone_table():
    c = duckdb.connect()
    try:
        c.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMP)"
        )
        c.executemany(
            "INSERT INTO schema_migrations VALUES (?, current_timestamp)",
            [(1,), (2,), (3,), (4,), (5,), (6,)],
        )
        db.init_schema(c)
        tables = {r[0] for r in c.execute("SHOW TABLES").fetchall()}
        assert "stock_crawl_skip" in tables
        assert (
            c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == db.SCHEMA_VERSION
        )
    finally:
        c.close()


def test_stock_upsert_rejects_unknown_dynamic_column(conn):
    with pytest.raises(ValueError, match="unknown stock_fundamentals columns"):
        db.upsert_stock_fundamental(
            conn,
            symbol="TEST",
            as_of=date(2026, 8, 25),
            source="fixture",
            methodology_version="fixture-v1",
            calculated_at=dt(2026, 8, 25),
            fetched_at=dt(2026, 8, 25),
        )


def test_watermark_set_is_idempotent_but_advances(conn):
    day = date(2026, 8, 21)
    db.set_watermark(conn, "bhavcopy_daily", day, detail="bars=1", updated_at=dt(2026, 8, 25, 12))
    before = db.fingerprint(conn, "ingest_watermark")
    db.set_watermark(conn, "bhavcopy_daily", day, detail="bars=1", updated_at=dt(2026, 8, 25, 13))
    assert db.fingerprint(conn, "ingest_watermark") == before
    db.set_watermark(conn, "bhavcopy_daily", day, detail="bars=2", updated_at=dt(2026, 8, 25, 14))
    assert db.fingerprint(conn, "ingest_watermark") != before


def test_metric_violation_query_returns_zero(conn):
    db.upsert_return_metric(conn, **RETURN_ROW)
    db.upsert_risk_metric(conn, **RISK_ROW)
    assert db.metric_violation_count(conn) == 0


def test_current_schema_has_reconciliation_evidence_tables():
    c = duckdb.connect()
    try:
        db.init_schema(c)
        tables = {r[0] for r in c.execute("SHOW TABLES").fetchall()}
        assert {"stock_crawl_status", "stock_crawl_ref"} <= tables
        assert (
            c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == db.SCHEMA_VERSION
        )
    finally:
        c.close()


def test_v9_database_gains_v10_reconciliation_tables():
    c = duckdb.connect()
    try:
        c.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMP)"
        )
        c.executemany(
            "INSERT INTO schema_migrations VALUES (?, current_timestamp)",
            [(i,) for i in range(1, 10)],
        )
        db.init_schema(c)
        tables = {r[0] for r in c.execute("SHOW TABLES").fetchall()}
        assert {"stock_crawl_status", "stock_crawl_ref"} <= tables
        assert (
            c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == db.SCHEMA_VERSION
        )
    finally:
        c.close()


def test_v10_database_gains_later_index_research_and_broker_tables():
    c = duckdb.connect()
    try:
        c.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMP)"
        )
        c.executemany(
            "INSERT INTO schema_migrations VALUES (?, current_timestamp)",
            [(i,) for i in range(1, 11)],
        )
        db.init_schema(c)
        tables = {r[0] for r in c.execute("SHOW TABLES").fetchall()}
        assert {
            "index_close",
            "index_constituent",
            "stock_research_score",
            "stock_research_run",
            "stock_research_attempt",
            "stock_research_delivery",
            "broker_snapshot_run",
            "broker_holding",
            "broker_mf_holding",
            "broker_position",
        } <= tables
        assert {
            "news_article",
            "news_article_entity",
            "news_classification",
            "news_classification_attempt",
            "news_run",
        } <= tables
        assert c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 17
    finally:
        c.close()


def test_v14_broker_parent_migrates_with_appended_column_and_keeps_integrity():
    c = duckdb.connect()
    try:
        c.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMP)"
        )
        c.executemany(
            "INSERT INTO schema_migrations VALUES (?, current_timestamp)",
            [(version,) for version in range(1, 15)],
        )
        c.execute(
            "CREATE TABLE broker_snapshot_run ("
            "run_id TEXT PRIMARY KEY, broker TEXT, account_sha256 TEXT, "
            "snapshot_date DATE, content_sha256 TEXT, holding_count INTEGER, "
            "position_count INTEGER, fetched_at TIMESTAMP)"
        )
        legacy = json.dumps(
            {"holdings": [], "positions": []}, sort_keys=True, separators=(",", ":")
        )
        c.execute(
            "INSERT INTO broker_snapshot_run VALUES "
            "('old', 'zerodha', 'account', '2026-08-27', ?, 0, 0, current_timestamp)",
            [hashlib.sha256(legacy.encode()).hexdigest()],
        )
        db.init_schema(c)
        columns = [row[0] for row in c.execute("DESCRIBE broker_snapshot_run").fetchall()]
        assert columns[-1] == "mf_holding_count"
        assert kite.snapshot_integrity(c, "old")
    finally:
        c.close()


def test_v15_database_gains_news_tables_without_changing_broker_rows():
    c = duckdb.connect()
    try:
        c.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMP)"
        )
        c.executemany(
            "INSERT INTO schema_migrations VALUES (?, current_timestamp)",
            [(version,) for version in range(1, 16)],
        )
        db.init_schema(c)
        tables = {row[0] for row in c.execute("SHOW TABLES").fetchall()}
        assert {
            "news_article",
            "news_article_entity",
            "news_classification",
            "news_classification_attempt",
            "news_run",
        } <= tables
        assert c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 17
    finally:
        c.close()


def test_broker_snapshot_children_require_a_content_addressed_parent_run():
    c = duckdb.connect()
    try:
        db.init_schema(c)
        run = ["run1", "zerodha", "account-hash", "2026-08-27", "content-hash", 0, 0, 0]
        c.execute(
            "INSERT INTO broker_snapshot_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)",
            run,
        )
        with pytest.raises(duckdb.ConstraintException):
            c.execute(
                "INSERT INTO broker_snapshot_run VALUES "
                "('run2', 'zerodha', 'account-hash', '2026-08-27', "
                "'content-hash', 0, 0, 0, current_timestamp)"
            )
        with pytest.raises(duckdb.ConstraintException):
            c.execute(
                "INSERT INTO broker_holding VALUES "
                "('missing', 'NSE', 'ABC', 'CNC', NULL, NULL, "
                "1, 0, 0, 10, 11, 10, 1, 1, 10)"
            )
    finally:
        c.close()
