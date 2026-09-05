"""T3.2a BharatStock adapter contracts; all tests stay offline."""

from datetime import UTC, date
from datetime import datetime as dt

import duckdb
import pytest

from invest import bharatstock, db

ITEM = {
    "symbol": "ONGC",
    "company_name": "Oil and Natural Gas Corporation Limited",
    "sector": "Oil Gas & Consumable Fuels",
    "exchange": "NSE",
    "price": 262.5,
    "market_cap": 330120.45,
    "pe_ratio": 7.8,
    "pb_ratio": 1.2,
    "roe": 18.5,
    "roce": 22.1,
    "operating_margin": 31.0,
    "debt_to_equity": 0.45,
    "promoter_holding": 58.89,
    "fii_holding": 18.5,
    "computed_at": "2026-08-25",
}


def test_screen_passes_repeatable_filters_and_validates_contract():
    seen = {}

    def fetch(params):
        seen.update(params)
        return {"data": [ITEM], "pagination": {"page": 1, "total_items": 1}}

    rows, pagination = bharatstock.screen(
        ["market_cap.gt.1000", "pe_ratio.lt.25"], page_size=20, fetcher=fetch
    )
    assert seen["filter"] == ["market_cap.gt.1000", "pe_ratio.lt.25"]
    assert seen["page_size"] == 20
    assert rows == [ITEM]
    assert pagination["total_items"] == 1


def test_screen_all_fetches_every_page_and_checks_total():
    def fetch(params):
        assert params["exchange"] == "NSE"
        page = params["page"]
        item = {**ITEM, "symbol": f"TEST{page}"}
        return {
            "data": [item],
            "pagination": {"page": page, "total_items": 2, "total_pages": 2},
        }

    rows, _pagination = bharatstock.screen_all([], page_size=1, exchange="NSE", fetcher=fetch)
    assert [row["symbol"] for row in rows] == ["TEST1", "TEST2"]


def test_screen_all_requires_complete_pagination_contract():
    with pytest.raises(bharatstock.SourceError, match="total_pages"):
        bharatstock.screen_all(
            [],
            fetcher=lambda _params: {"data": [ITEM], "pagination": {"total_items": 1}},
        )


def test_screen_all_refuses_page_count_above_quota_guard():
    def fetch(params):
        return {
            "data": [ITEM],
            "pagination": {"page": params["page"], "total_items": 999, "total_pages": 6},
        }

    with pytest.raises(bharatstock.SourceError, match="quota guard"):
        bharatstock.screen_all([], max_pages=5, fetcher=fetch)


def test_redirect_handler_never_forwards_api_key():
    handler = bharatstock._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.test") is None


def test_screen_rejects_changed_envelope():
    with pytest.raises(bharatstock.SourceError, match="contract changed"):
        bharatstock.screen([], fetcher=lambda _params: {"results": []})


def test_snapshot_preserves_documented_screener_market_cap_crores_and_date():
    fetched = dt(2026, 8, 25, 12, tzinfo=UTC)
    row = bharatstock.snapshot_row(
        {**ITEM, "computed_at": "2026-08-25T21:00:00Z"}, fetched_at=fetched
    )
    assert row["symbol"] == "ONGC"
    assert row["as_of"] == date(2026, 8, 25)
    assert row["market_cap_cr"] == pytest.approx(330120.45)
    assert row["roe"] == pytest.approx(0.185)
    assert row["roce"] == pytest.approx(0.221)
    assert row["operating_margin"] == pytest.approx(0.31)
    assert row["promoter_holding"] == pytest.approx(0.5889)
    assert row["fii_holding"] == pytest.approx(0.185)
    assert row["fetched_at"] == fetched
    assert "Oil and Natural Gas" in row["raw_json"]


def test_store_screen_is_idempotent():
    conn = duckdb.connect()
    db.init_schema(conn)
    fetched = dt(2026, 8, 25, 12, tzinfo=UTC)
    assert bharatstock.store_screen(conn, [ITEM, ITEM], fetched_at=fetched) == 1
    before = db.fingerprint(conn, "stock_fundamentals")
    later = dt(2026, 8, 25, 13, tzinfo=UTC)
    bharatstock.store_screen(conn, [ITEM], fetched_at=later)
    assert db.fingerprint(conn, "stock_fundamentals") == before
    (symbol, market_cap) = conn.execute(
        "SELECT symbol, market_cap_cr FROM stock_fundamentals"
    ).fetchone()
    assert symbol == "ONGC"
    assert market_cap == pytest.approx(330120.45)
    conn.close()


def test_missing_key_fails_without_echoing_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("BHARATSTOCKAPI", raising=False)
    assert bharatstock.load_api_key(tmp_path / "absent.env") is None
