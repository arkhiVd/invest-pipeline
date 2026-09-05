from datetime import UTC, date
from datetime import datetime as dt
from urllib import error

import duckdb

from invest import db, nse_filings, reconcile

NOW = dt(2026, 8, 26, tzinfo=UTC)
REF = nse_filings.FilingRef(
    symbol="TEST",
    filing_type="financial_annual_legacy",
    period_end=date(2026, 3, 31),
    consolidation="Consolidated",
    taxonomy="indas",
    xbrl_url="https://nsearchives.nseindia.com/corporate/xbrl/TEST.xml",
)


def discovery(*, refs=(REF,), legacy_ok=True, integrated_ok=True, shareholding_ok=True):
    return nse_filings.DiscoveryResult(
        refs=tuple(refs),
        legacy_ok=legacy_ok,
        integrated_ok=integrated_ok,
        shareholding_ok=shareholding_ok,
        legacy_refs=sum(r.filing_type == "financial_annual_legacy" for r in refs),
        integrated_refs=sum(r.filing_type == "financial_integrated" for r in refs),
        shareholding_refs=sum(r.filing_type == "shareholding" for r in refs),
        errors=tuple(
            name
            for name, ok in (
                ("legacy", legacy_ok),
                ("integrated", integrated_ok),
                ("shareholding", shareholding_ok),
            )
            if not ok
        ),
    )


def connection():
    conn = duckdb.connect()
    db.init_schema(conn)
    db.upsert_universe_row(
        conn, symbol="TEST", series="EQ", is_active=True, source="fx", fetched_at=NOW
    )
    return conn


def fake_ingester(conn):
    def ingest(ref, _xml):
        db.upsert_stock_filing(
            conn,
            xbrl_url=ref.xbrl_url,
            symbol=ref.symbol,
            source="fixture",
            filing_type=ref.filing_type,
            period_end=ref.period_end,
            consolidation=ref.consolidation,
            taxonomy=ref.taxonomy,
            fetched_at=NOW,
        )
        return {"contexts": 1, "facts": 1}

    return ingest


def test_reconcile_downloads_missing_ref_and_replay_does_not_churn():
    conn = connection()
    calls = []
    kwargs = {
        "discoverer": lambda _symbol: discovery(),
        "fetcher": lambda ref: calls.append(ref.xbrl_url) or b"<xbrl/>",
        "ingester": fake_ingester(conn),
        "checked_at": NOW,
        "opener": object(),
    }
    result = reconcile.reconcile_symbol(conn, "TEST", **kwargs)
    assert result["complete"] is True and result["usable_financial"] is True
    assert calls == [REF.xbrl_url]
    before = (db.fingerprint(conn, "stock_crawl_status"), db.fingerprint(conn, "stock_crawl_ref"))
    kwargs["checked_at"] = dt(2026, 8, 27, tzinfo=UTC)
    reconcile.reconcile_symbol(conn, "TEST", **kwargs)
    after = (db.fingerprint(conn, "stock_crawl_status"), db.fingerprint(conn, "stock_crawl_ref"))
    assert calls == [REF.xbrl_url]  # current-policy evidence avoids re-download
    assert after == before
    conn.close()


def test_section_failure_is_persisted_incomplete_even_when_ref_stores():
    conn = connection()
    result = reconcile.reconcile_symbol(
        conn,
        "TEST",
        discoverer=lambda _symbol: discovery(legacy_ok=False),
        fetcher=lambda _ref: b"<xbrl/>",
        ingester=fake_ingester(conn),
        opener=object(),
        checked_at=NOW,
    )
    assert result["complete"] is False and result["usable_financial"] is True
    assert conn.execute("SELECT legacy_ok,complete FROM stock_crawl_status").fetchone() == (
        False,
        False,
    )
    conn.close()


def test_404_is_accounted_without_becoming_usable():
    conn = connection()

    def missing(ref):
        http = error.HTTPError(ref.xbrl_url, 404, "missing", {}, None)
        raise nse_filings.SourceError("missing") from http

    result = reconcile.reconcile_symbol(
        conn,
        "TEST",
        discoverer=lambda _symbol: discovery(),
        fetcher=missing,
        ingester=fake_ingester(conn),
        opener=object(),
        checked_at=NOW,
    )
    assert result["complete"] is True and result["usable_financial"] is False
    assert conn.execute("SELECT outcome FROM stock_crawl_ref").fetchone() == ("http_404",)
    conn.close()


def test_pending_selection_uses_evidence_not_any_filing():
    conn = connection()
    # A stray shareholding row must not make the symbol done.
    db.upsert_stock_filing(
        conn,
        xbrl_url="https://nsearchives.nseindia.com/corporate/xbrl/SHP.xml",
        symbol="TEST",
        source="fixture",
        filing_type="shareholding",
        period_end=date(2026, 6, 30),
        fetched_at=NOW,
    )
    assert reconcile.pending_symbols(conn, 10) == ["TEST"]
    conn.close()


def test_prepolicy_existing_filing_is_refreshed_once():
    conn = connection()
    fake_ingester(conn)(REF, b"<old/>")
    calls = []
    reconcile.reconcile_symbol(
        conn,
        "TEST",
        discoverer=lambda _symbol: discovery(),
        fetcher=lambda ref: calls.append(ref.xbrl_url) or b"<new/>",
        ingester=fake_ingester(conn),
        opener=object(),
        checked_at=NOW,
    )
    assert calls == [REF.xbrl_url]
    assert conn.execute("SELECT outcome FROM stock_crawl_ref").fetchone() == ("stored",)
    conn.close()


def test_reconcile_run_circuit_breaks_section_failures():
    conn = connection()
    symbols = [f"FAIL{i}" for i in range(20)]
    for symbol in symbols:
        db.upsert_universe_row(
            conn, symbol=symbol, series="EQ", is_active=True, source="fx", fetched_at=NOW
        )

    bad = nse_filings.DiscoveryResult(
        refs=(),
        legacy_ok=False,
        integrated_ok=False,
        shareholding_ok=False,
        legacy_refs=0,
        integrated_refs=0,
        shareholding_refs=0,
        errors=("legacy", "integrated", "shareholding"),
    )
    stats = reconcile.run(
        conn,
        symbols,
        discoverer=lambda _symbol: bad,
        opener=object(),
        checked_at=NOW,
    )
    assert stats["aborted"] is True
    assert stats["processed"] == reconcile.MAX_CONSECUTIVE_FAILURES
    conn.close()


def test_404_evidence_rechecks_after_30_days():
    conn = connection()

    def missing(ref):
        http = error.HTTPError(ref.xbrl_url, 404, "missing", {}, None)
        raise nse_filings.SourceError("missing") from http

    base = dict(
        discoverer=lambda _symbol: discovery(),
        ingester=fake_ingester(conn),
        opener=object(),
    )
    reconcile.reconcile_symbol(conn, "TEST", fetcher=missing, checked_at=NOW, **base)
    calls = []
    reconcile.reconcile_symbol(
        conn,
        "TEST",
        fetcher=lambda ref: calls.append(ref.xbrl_url) or b"<x/>",
        checked_at=dt(2026, 9, 24, tzinfo=UTC),
        **base,
    )
    assert calls == []
    result = reconcile.reconcile_symbol(
        conn,
        "TEST",
        fetcher=lambda ref: calls.append(ref.xbrl_url) or b"<x/>",
        checked_at=dt(2026, 9, 27, tzinfo=UTC),
        **base,
    )
    assert calls == [REF.xbrl_url] and result["usable_financial"] is True
    conn.close()


def test_zero_parsed_financial_facts_never_counts_stored():
    conn = connection()
    result = reconcile.reconcile_symbol(
        conn,
        "TEST",
        discoverer=lambda _symbol: discovery(),
        fetcher=lambda _ref: b"<xbrl/>",
        ingester=lambda _ref, _xml: {"contexts": 2, "facts": 0},
        opener=object(),
        checked_at=NOW,
    )
    assert result["complete"] is False and result["usable_financial"] is False
    assert conn.execute("SELECT outcome FROM stock_crawl_ref").fetchone() == ("error",)
    conn.close()
