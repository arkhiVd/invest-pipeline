import json
from datetime import UTC
from datetime import datetime as dt
from decimal import Decimal

import duckdb
import pytest

from invest import db, market_history


@pytest.fixture()
def conn():
    value = duckdb.connect()
    db.init_schema(value)
    market_history.install_schema(value)
    yield value
    value.close()


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size):
        return self.body[:size]


class Opener:
    def __init__(self, body):
        self.body = body
        self.url = None

    def open(self, req, timeout):
        self.url = req.full_url
        assert timeout == 30
        return Response(self.body)


def test_official_fetch_is_bounded_and_archive_allowlisted():
    body = b"[]"
    opener = Opener(body)
    parsed = market_history.fetch_corporate_actions(
        dt(2026, 8, 1).date(), dt(2026, 8, 31).date(), opener=opener
    )
    assert parsed["rows"] == []
    assert "from_date=01-08-2026" in opener.url
    with pytest.raises(market_history.MarketHistoryError, match="allowlisted"):
        market_history.fetch_archive("other.csv", opener=opener)
    oversized = Opener(b"x" * (market_history.MAX_SOURCE_BYTES + 1))
    with pytest.raises(market_history.MarketHistoryError, match="size limit"):
        market_history.fetch_url("https://example.invalid", opener=oversized)


@pytest.mark.parametrize(
    ("subject", "kind", "status", "factor", "cash"),
    [
        ("Bonus 1:1", "BONUS", "SUPPORTED", Decimal("0.5"), None),
        (
            "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share",
            "SPLIT",
            "SUPPORTED",
            Decimal("0.1"),
            None,
        ),
        (
            "Consolidation Of Equity Shares From Re 1 Per Share To Rs 10 Per Share",
            "CONSOLIDATION",
            "SUPPORTED",
            Decimal("10"),
            None,
        ),
        (
            "Annual General Meeting /Dividend - Rs 4 Per Share & Special Dividend - Rs 3 Per Share",
            "CASH_DISTRIBUTION",
            "SUPPORTED",
            None,
            Decimal("7"),
        ),
        (
            "Interim Dividend - Rs 10 Per Sh",
            "CASH_DISTRIBUTION",
            "SUPPORTED",
            None,
            Decimal("10"),
        ),
        (
            "Dividend - Rs 5 Per Share and Rights 1:2",
            "UNKNOWN",
            "UNSUPPORTED",
            None,
            None,
        ),
        (
            "Dividend - Rs 5 Per Share and Amalgamation",
            "UNKNOWN",
            "UNSUPPORTED",
            None,
            None,
        ),
        (
            "Dividend - Rs 5 Per Share and Consolidation of Equity Shares",
            "UNKNOWN",
            "UNSUPPORTED",
            None,
            None,
        ),
        (
            "Dividend - Rs 5 Per Share and Buy Back",
            "UNKNOWN",
            "UNSUPPORTED",
            None,
            None,
        ),
        ("Rights 1:2 @ Premium Rs 10/-", "RIGHTS", "UNSUPPORTED", None, None),
        ("Buy Back", "UNKNOWN", "UNSUPPORTED", None, None),
        ("Demerger", "DEMERGER", "UNSUPPORTED", None, None),
    ],
)
def test_action_classifier_is_strict(subject, kind, status, factor, cash):
    result = market_history.classify_action_subject(subject)
    assert result["action_kind"] == kind
    assert result["parse_status"] == status
    assert result["structural_factor"] == factor
    assert result["cash_amount"] == cash


def test_corporate_actions_parse_source_dates_and_replay(conn):
    raw = json.dumps(
        [
            {
                "symbol": "ACME",
                "isin": "INE000A01001",
                "series": "EQ",
                "subject": "Bonus 1:1",
                "exDate": "14-Aug-2026",
                "recDate": "14-Aug-2026",
                "caBroadcastDate": "01-Aug-2026 18:30:00",
                "faceVal": "10",
            }
        ]
    ).encode()
    parsed = market_history.parse_corporate_actions(raw)
    assert parsed["coverage_start"].isoformat() == "2026-08-14"
    assert parsed["rows"][0]["broadcast_at"] == dt(2026, 8, 1, 13, 0, tzinfo=UTC)
    assert market_history.store(conn, parsed)["status"] == "stored"
    assert market_history.store(conn, parsed)["status"] == "duplicate"
    assert conn.execute("SELECT subject,face_value FROM market_corporate_action").fetchone() == (
        "Bonus 1:1",
        Decimal("10.000000"),
    )


def test_security_lineage_sources_preserve_effective_dates(conn):
    delisted = market_history.parse_delistings(
        b"Symbol,Company,Delisted Date,Type of Delisting,,,,,\n"
        b"OLD,Old Ltd,15-Aug-2025,Voluntary,,,,,\n"
    )
    symbols = market_history.parse_symbol_changes(b"Acme Ltd,OLD,NEW,01-JAN-2025\n")
    names = market_history.parse_name_changes(
        b"NCH_SYMBOL,NCH_PREV_NAME,NCH_NEW_NAME,NCH_DT\n"
        b"NEW,Old Name,New Name,02-JAN-2025\n"
        b"NEW,Old Name,New Name,02-JAN-2025\n"
    )
    for parsed in (delisted, symbols, names):
        market_history.store(conn, parsed)
    assert conn.execute(
        "SELECT source_row_count,row_count,duplicate_row_count FROM market_history_import "
        "WHERE source_type='NSE_NAME_CHANGE'"
    ).fetchone() == (2, 1, 1)
    assert conn.execute(
        "SELECT event_type,effective_date,coalesce(old_symbol,symbol),"
        "coalesce(new_symbol,symbol) FROM security_lineage_event ORDER BY event_type"
    ).fetchall() == [
        ("DELISTING", dt(2025, 8, 15).date(), "OLD", "OLD"),
        ("NAME_CHANGE", dt(2025, 1, 2).date(), "NEW", "NEW"),
        ("SYMBOL_CHANGE", dt(2025, 1, 1).date(), "OLD", "NEW"),
    ]


def test_filing_availability_uses_exchange_broadcast_not_period_end(conn):
    url = "https://nsearchives.nseindia.com/corporate/ACME_01012025120000.xml"
    raw = json.dumps(
        [
            {
                "symbol": "ACME",
                "toDate": "31-Mar-2024",
                "broadCastDate": "22-Apr-2024 19:47:12",
                "xbrl": url,
            },
            {
                "symbol": "ACME",
                "toDate": "31-Mar-2023",
                "broadCastDate": "22-Apr-2023 19:47:12",
                "xbrl": "-",
            },
        ]
    ).encode()
    parsed = market_history.parse_filing_availability(raw, "legacy", "fixture")
    market_history.store(conn, parsed)
    assert conn.execute(
        "SELECT period_end,available_at,timestamp_field FROM filing_availability"
    ).fetchone() == (
        dt(2024, 3, 31).date(),
        dt(2024, 4, 22, 14, 17, 12, tzinfo=UTC),
        "broadCastDate",
    )
    assert conn.execute(
        "SELECT source_row_count,row_count,excluded_row_count,exclusions_json "
        "FROM market_history_import"
    ).fetchone() == (2, 1, 1, '[{"position":1,"reason":"missing_or_invalid_xbrl_url"}]')


def _price(conn, day, close):
    conn.execute(
        "INSERT INTO stock_price VALUES (?,?,?,?,?,?,?,?,?,?)",
        ["ACME", day, close, close, close, close, close, 100, "fixture", dt.now(UTC)],
    )


def _action(subject, ex_date):
    return {
        "symbol": "ACME",
        "isin": "INE000A01001",
        "series": "EQ",
        "subject": subject,
        "exDate": ex_date,
        "recDate": ex_date,
        "caBroadcastDate": "01-Jan-2025 18:30:00",
        "faceVal": "10",
    }


def test_adjusted_prices_apply_structural_and_cash_factors_without_overwriting_raw(conn):
    for day, close in [
        ("2025-01-01", 100),
        ("2025-01-02", 110),
        ("2025-01-03", 60),
        ("2025-01-04", 60),
        ("2025-01-05", 55),
    ]:
        _price(conn, day, close)
    raw = json.dumps(
        [_action("Bonus 1:1", "03-Jan-2025"), _action("Dividend - Rs 5 Per Share", "05-Jan-2025")]
    ).encode()
    market_history.store(conn, market_history.parse_corporate_actions(raw))
    result = market_history.derive_adjusted_prices(
        conn, "ACME", dt(2025, 1, 1).date(), dt(2025, 1, 5).date()
    )
    assert result["status"] == "READY"
    adjusted = {row["trade_date"].isoformat(): row["adjusted_close"] for row in result["rows"]}
    assert adjusted["2025-01-01"] == Decimal("45.83333333333333333333333334")
    assert adjusted["2025-01-02"] == Decimal("50.41666666666666666666666667")
    assert adjusted["2025-01-03"] == Decimal("55.00000000000000000000000000")
    assert adjusted["2025-01-05"] == Decimal("55")
    assert (
        conn.execute(
            "SELECT close FROM stock_price WHERE symbol='ACME' AND trade_date='2025-01-01'"
        ).fetchone()[0]
        == 100
    )
    reconciled = {row["ex_session"].isoformat(): row for row in result["reconciliations"]}
    assert reconciled["2025-01-03"]["raw_overnight_return"] == Decimal(
        "-0.4545454545454545454545454545"
    )
    assert reconciled["2025-01-05"]["adjusted_overnight_return"] == Decimal("0")
    assert market_history.store_adjusted_prices(conn, result)["status"] == "stored"
    assert conn.execute("SELECT count(*) FROM market_action_reconciliation").fetchone()[0] == 2
    assert market_history.store_adjusted_prices(conn, result)["status"] == "duplicate"


def test_lineage_event_excludes_entire_symbol_interval(conn):
    _price(conn, "2025-01-01", 100)
    _price(conn, "2025-01-02", 101)
    lineage = market_history.parse_symbol_changes(b"Acme Ltd,ACME,ACMENEW,02-JAN-2025\n")
    market_history.store(conn, lineage)
    result = market_history.derive_adjusted_prices(
        conn, "ACME", dt(2025, 1, 1).date(), dt(2025, 1, 2).date()
    )
    assert result["status"] == "EXCLUDED"
    assert "lineage event" in result["reason"]


def test_invalid_raw_ohlc_fails_closed(conn):
    conn.execute(
        "INSERT INTO stock_price VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            "ACME",
            "2025-01-01",
            100,
            90,
            80,
            100,
            100,
            1,
            "fixture",
            dt.now(UTC),
        ],
    )
    with pytest.raises(market_history.MarketHistoryError, match="invalid raw OHLC"):
        market_history.derive_adjusted_prices(
            conn, "ACME", dt(2025, 1, 1).date(), dt(2025, 1, 1).date()
        )


def test_overlapping_action_imports_dedupe_and_partial_replay_fails(conn):
    for day, close in [("2025-01-01", 100), ("2025-01-02", 50)]:
        _price(conn, day, close)
    action = _action("Bonus 1:1", "02-Jan-2025")
    market_history.store(
        conn, market_history.parse_corporate_actions(json.dumps([action]).encode())
    )
    other = _action("Annual General Meeting", "02-Jan-2025")
    other["symbol"] = "OTHER"
    market_history.store(
        conn, market_history.parse_corporate_actions(json.dumps([action, other]).encode())
    )
    result = market_history.derive_adjusted_prices(
        conn, "ACME", dt(2025, 1, 1).date(), dt(2025, 1, 2).date()
    )
    assert result["status"] == "READY"
    market_history.store_adjusted_prices(conn, result)
    conn.execute("DELETE FROM adjusted_stock_price WHERE symbol='ACME' AND trade_date='2025-01-01'")
    with pytest.raises(market_history.MarketHistoryError, match="count conflict"):
        market_history.store_adjusted_prices(conn, result)


def test_unsupported_action_excludes_entire_symbol_interval(conn):
    _price(conn, "2025-01-01", 100)
    _price(conn, "2025-01-02", 95)
    raw = json.dumps([_action("Rights 1:2 @ Premium Rs 10/-", "02-Jan-2025")]).encode()
    market_history.store(conn, market_history.parse_corporate_actions(raw))
    result = market_history.derive_adjusted_prices(
        conn, "ACME", dt(2025, 1, 1).date(), dt(2025, 1, 2).date()
    )
    assert result["status"] == "EXCLUDED"
    assert "unsupported corporate action" in result["reason"]
    conn.execute(
        "INSERT INTO market_action_reconciliation VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            "orphan",
            "orphan",
            "ACME",
            dt(2025, 1, 1).date(),
            dt(2025, 1, 2).date(),
            dt(2025, 1, 1).date(),
            dt(2025, 1, 2).date(),
            0,
            0,
            market_history.ADJUSTMENT_METHODOLOGY,
            "f" * 64,
        ],
    )
    with pytest.raises(market_history.MarketHistoryError, match="state conflicts"):
        market_history.store_adjusted_prices(conn, result)
    conn.execute("DELETE FROM market_action_reconciliation")
    assert market_history.store_adjusted_prices(conn, result)["status"] == "excluded"
    assert market_history.store_adjusted_prices(conn, result)["status"] == "duplicate"
    assert conn.execute("SELECT count(*) FROM adjusted_stock_price").fetchone()[0] == 0
    conn.execute(
        "UPDATE stock_price SET open=90,high=90,low=90,close=90 "
        "WHERE symbol='ACME' AND trade_date='2025-01-01'"
    )
    changed = market_history.derive_adjusted_prices(
        conn, "ACME", dt(2025, 1, 1).date(), dt(2025, 1, 2).date()
    )
    with pytest.raises(market_history.MarketHistoryError, match="identity conflict"):
        market_history.store_adjusted_prices(conn, changed)


def test_source_contract_drift_fails_closed(conn):
    with pytest.raises(market_history.MarketHistoryError, match="contract"):
        market_history.parse_corporate_actions(b'[{"symbol":"ACME"}]')
    with pytest.raises(market_history.MarketHistoryError, match="header"):
        market_history.parse_delistings(b"changed,header\n")
    with pytest.raises(market_history.MarketHistoryError, match="empty"):
        market_history.parse_symbol_changes(b"")
    url = "https://nsearchives.nseindia.com/corporate/ACME_01012025120000.xml"
    raw = json.dumps([{"symbol": "ACME", "toDate": "31-Mar-2024", "xbrl": url}]).encode()
    with pytest.raises(market_history.MarketHistoryError, match="timestamp missing"):
        market_history.parse_filing_availability(raw, "legacy", "fixture")
    parsed = market_history.parse_name_changes(
        b"NCH_SYMBOL,NCH_PREV_NAME,NCH_NEW_NAME,NCH_DT\n"
        b"NEW,Old Name,New Name,02-JAN-2025\n"
        b"NEW,Old Name,New Name,02-JAN-2025\n"
    )
    approved_type = parsed["source_type"]
    parsed["source_type"] = "UNAPPROVED"
    with pytest.raises(market_history.MarketHistoryError, match="allowlisted"):
        market_history.store(conn, parsed)
    parsed["source_type"] = approved_type
    import_id = market_history.store(conn, parsed)["import_id"]
    with pytest.raises(duckdb.ConstraintException):
        conn.execute(
            "INSERT INTO security_lineage_event VALUES "
            "(?,'row','SYMBOL_CHANGE','2025-01-01',NULL,NULL,NULL,NULL,NULL,NULL,'[]')",
            [import_id],
        )


def test_v22_install_is_atomic_and_disposable(conn):
    assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 22
    assert {row[0] for row in conn.execute("SHOW TABLES").fetchall()} >= {
        "market_history_import",
        "market_corporate_action",
        "security_lineage_event",
        "filing_availability",
        "adjusted_stock_price",
        "adjusted_price_exclusion",
        "market_action_reconciliation",
    }


def test_pre_release_v21_shape_is_refused():
    conn = duckdb.connect()
    db.init_schema(conn)
    conn.execute("CREATE TABLE market_corporate_action(subject TEXT)")
    try:
        with pytest.raises(RuntimeError, match="must be recreated"):
            market_history.install_schema(conn)
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 17
    finally:
        conn.close()
