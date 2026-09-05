from datetime import UTC
from datetime import datetime as dt

import duckdb
import pytest

from invest import db, kite, portfolio, research

NOW = dt(2026, 8, 27, 10, tzinfo=UTC)
MF_HOLDING = {
    "folio": "12345",
    "fund": "Tracked Fund Direct Growth",
    "tradingsymbol": "INF123A01017",
    "average_price": 10.0,
    "last_price": 12.0,
    "last_price_date": "2026-08-26",
    "pledged_quantity": 0,
    "pnl": 20.0,
    "quantity": 10.0,
}
HOLDING = {
    "exchange": "NSE",
    "tradingsymbol": "ACME",
    "product": "CNC",
    "instrument_token": 408065,
    "isin": "INE000A01001",
    "quantity": 10,
    "t1_quantity": 0,
    "used_quantity": 0,
    "average_price": 1400.0,
    "last_price": 1500.0,
    "close_price": 1490.0,
    "pnl": 1000.0,
    "day_change": 10.0,
    "day_change_percentage": 0.67,
}


def connection():
    conn = duckdb.connect()
    db.init_schema(conn)
    conn.executemany(
        "INSERT INTO stock_universe "
        "(symbol, company_name, series, isin, is_active, source, fetched_at) "
        "VALUES (?, ?, 'EQ', ?, true, 'fixture', ?)",
        [
            ("ACME", "Acme Synthetic", "INE000A01001", NOW),
            ("NOVA", "NOVA", "INE000N01001", NOW),
            ("BSEONLY", "BSE only mapping", "INE123A01016", NOW),
        ],
    )
    conn.execute(
        "INSERT INTO mf_scheme (scheme_code, name, isin) VALUES (1, 'Tracked MF', 'INF123A01017')"
    )
    return conn


def candidate(symbol):
    return {
        "symbol": symbol,
        "snapshot_date": NOW.date(),
        "screens": ["quality_pullback"],
        "metrics": {},
    }


def test_reconcile_owned_unowned_outside_and_unmatched(monkeypatch):
    conn = connection()
    bse = {
        **HOLDING,
        "exchange": "BSE",
        "tradingsymbol": "500001",
        "isin": "INE123A01016",
    }
    unmatched = {
        **HOLDING,
        "exchange": "NSE",
        "tradingsymbol": "GOLDETF",
        "isin": None,
    }
    zero = {**HOLDING, "tradingsymbol": "NOVA", "isin": "INE000N01001", "quantity": 0}
    result = kite.store_snapshot(
        conn,
        {"user_id": "AB1234"},
        [HOLDING, bse, unmatched, zero],
        {"net": [], "day": []},
        [
            MF_HOLDING,
            {
                **MF_HOLDING,
                "folio": "999",
                "fund": "Untracked Fund",
                "tradingsymbol": "INF999A01019",
            },
        ],
        fetched_at=NOW,
    )
    duplicate = {**candidate("ACME"), "screens": ["garp"]}
    monkeypatch.setattr(
        research,
        "candidates",
        lambda _conn: [candidate("ACME"), candidate("NOVA"), duplicate],
    )
    report = portfolio.reconcile(conn)
    assert report["run_id"] == result["run_id"]
    assert report["owned_research"] == ["ACME"]
    assert report["unowned_research"] == ["NOVA"]
    assert report["candidate_screens"]["ACME"] == ["garp", "quality_pullback"]
    assert report["owned_not_research"] == ["BSEONLY"]
    assert [item["tradingsymbol"] for item in report["unmatched_holdings"]] == ["GOLDETF"]
    assert [item["tracked_name"] for item in report["tracked_mutual_funds"]] == ["Tracked MF"]
    assert [item["fund"] for item in report["untracked_mutual_funds"]] == ["Untracked Fund"]
    text = portfolio.render(report)
    assert "No trade instruction" in text
    assert "NSE:GOLDETF" in text
    assert "TRACKED MUTUAL FUNDS" in text
    conn.close()


def test_mutual_fund_ambiguous_isin_is_not_reported_untracked(monkeypatch):
    conn = connection()
    conn.execute(
        "INSERT INTO mf_scheme (scheme_code, name, isin) "
        "VALUES (2, 'Duplicate Mapping', 'INF123A01017')"
    )
    kite.store_snapshot(
        conn,
        {"user_id": "AB1234"},
        [],
        {"net": [], "day": []},
        [MF_HOLDING],
        fetched_at=NOW,
    )
    monkeypatch.setattr(research, "candidates", lambda _conn: [])
    report = portfolio.reconcile(conn)
    assert not report["tracked_mutual_funds"]
    assert not report["untracked_mutual_funds"]
    assert [item["fund"] for item in report["ambiguous_mutual_funds"]] == [
        "Tracked Fund Direct Growth"
    ]
    conn.close()


def test_symbol_resolution_fails_ambiguous_isin_but_prefers_exact_nse_symbol():
    symbols = {"ACME", "ONE", "TWO"}
    ambiguous = {"INE123A01016": None, "INE000A01001": "ACME"}
    assert portfolio._resolve_symbol("BSE", "500001", "INE123A01016", symbols, ambiguous) is None
    assert portfolio._resolve_symbol("NSE", "ACME", "INE123A01016", symbols, ambiguous) == "ACME"


def test_latest_snapshot_must_pass_content_integrity(monkeypatch):
    conn = connection()
    result = kite.store_snapshot(
        conn,
        {"user_id": "AB1234"},
        [HOLDING],
        {"net": [], "day": []},
        [],
        fetched_at=NOW,
    )
    conn.execute("UPDATE broker_holding SET quantity=999 WHERE run_id=?", [result["run_id"]])
    monkeypatch.setattr(research, "candidates", lambda _conn: [])
    with pytest.raises(ValueError, match="integrity"):
        portfolio.reconcile(conn)
    conn.close()


def test_no_snapshot_fails_closed():
    conn = connection()
    with pytest.raises(ValueError, match="no broker snapshot"):
        portfolio.latest_run_id(conn)
    conn.close()
