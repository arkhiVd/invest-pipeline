"""T3.2b-ii controlled XBRL crawl contracts; fully offline fixtures."""

from datetime import UTC, date
from datetime import datetime as dt

import duckdb
import pytest

from invest import db, nse_filings, xbrl_crawl
from invest.nse_filings import FilingRef

FETCHED = dt(2026, 8, 25, 12, tzinfo=UTC)

FIN_XML = b"""<?xml version='1.0'?>
<x:xbrl xmlns:x='urn:xbrl' xmlns:i='urn:indas'>
  <x:context id='FY26'>
    <x:period><x:startDate>2025-04-01</x:startDate><x:endDate>2026-03-31</x:endDate></x:period>
  </x:context>
  <i:RevenueFromOperations contextRef='FY26'>1000000</i:RevenueFromOperations>
  <i:ProfitLossForPeriod contextRef='FY26'>120000</i:ProfitLossForPeriod>
</x:xbrl>"""

SHP_XML = b"""<?xml version='1.0'?>
<x:xbrl xmlns:x='urn:xbrl' xmlns:s='urn:shp'>
  <s:WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged>false</s:WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged>
</x:xbrl>"""


def ref(kind: str, day: str, url: str, consolidation="Consolidated") -> FilingRef:
    return FilingRef(
        symbol="TEST",
        filing_type=kind,
        period_end=date.fromisoformat(day),
        consolidation=consolidation,
        taxonomy="indas",
        xbrl_url=url,
    )


def test_select_policy_caps_and_prefers_consolidated():
    refs = [
        ref("financial_annual_legacy", f"{i}-03-31", f"https://a/{i}.xml", "Standalone")
        for i in range(2015, 2025)
    ]
    refs += [
        ref("financial_annual_legacy", "2024-03-31", "https://a/cons24.xml"),
        ref("financial_annual_legacy", "2023-03-31", "https://a/cons23.xml"),
        ref("financial_integrated", "2026-06-30", "https://b/q1.xml"),
        ref("financial_integrated", "2026-03-31", "https://b/q4.xml"),
        ref("financial_integrated", "2025-12-31", "https://b/q3stand.xml", "Standalone"),
        ref("shareholding", "2026-06-30", "https://c/s1.xml", None),
        ref("shareholding", "2026-03-31", "https://c/s2.xml", None),
        ref("shareholding", "2025-12-31", "https://c/s3.xml", None),
    ]
    chosen = xbrl_crawl.select_filings(refs)
    urls = {r.xbrl_url for r in chosen}
    assert urls == {
        # consolidated legacy pool -> newest 8 (both exist here); standalone dropped
        "https://a/cons23.xml",
        "https://a/cons24.xml",
        "https://b/q1.xml",
        "https://b/q4.xml",
        "https://c/s1.xml",
        "https://c/s2.xml",
    }


@pytest.fixture()
def conn():
    c = duckdb.connect()
    db.init_schema(c)
    yield c
    c.close()


def test_select_policy_undated_filings_never_displace_dated():
    dated = [ref("shareholding", f"2026-0{m}-15", f"https://c/d{m}.xml", None) for m in (1, 2, 3)]
    undated = [FilingRef("TEST", "shareholding", None, None, "shp", "https://c/u2.xml")]
    chosen = {r.xbrl_url for r in xbrl_crawl.select_filings(dated + undated)}
    assert "https://c/u2.xml" not in chosen
    assert chosen == {"https://c/d3.xml", "https://c/d2.xml"}


def test_ingest_filing_stores_contexts_facts_pledge_and_is_idempotent(conn):
    fin = ref("financial_integrated", "2026-03-31", "https://nsearchives.nseindia.com/x/f.xml")
    shp = ref("shareholding", "2026-06-30", "https://nsearchives.nseindia.com/x/s.xml", None)
    first = nse_filings.ingest_filing(conn, fin, FIN_XML, fetched_at=FETCHED)
    assert first["contexts"] == 1 and first["facts"] == 2
    nse_filings.ingest_filing(conn, shp, SHP_XML, fetched_at=FETCHED)
    before = db.fingerprint(conn, "stock_filing_fact")
    nse_filings.ingest_filing(conn, shp, SHP_XML, fetched_at=FETCHED)
    nse_filings.ingest_filing(conn, fin, FIN_XML, fetched_at=dt(2026, 8, 25, 13, tzinfo=UTC))
    assert db.fingerprint(conn, "stock_filing_fact") == before

    pledge = conn.execute(
        "SELECT value FROM stock_filing_fact WHERE fact_name LIKE 'WhetherAnyShares%'"
    ).fetchone()
    assert pledge == ("false",)
    ctx = conn.execute(
        "SELECT start_date, end_date FROM stock_filing_context WHERE context_id='FY26'"
    ).fetchone()
    assert ctx == (date(2025, 4, 1), date(2026, 3, 31))


def test_pending_symbols_filters_eq_and_already_done(conn):
    for sym, series in (("AAA", "EQ"), ("BBB", "EQ"), ("BONDY", "GB")):
        db.upsert_universe_row(
            conn, symbol=sym, series=series, source="fixture", fetched_at=FETCHED
        )
    done_ref = ref("shareholding", "2026-06-30", "https://nsearchives.nseindia.com/x/bbb.xml")
    db.upsert_stock_filing(
        conn,
        xbrl_url=done_ref.xbrl_url,
        symbol="BBB",
        source="fixture",
        filing_type="shareholding",
        fetched_at=FETCHED,
    )
    assert xbrl_crawl.pending_symbols(conn, 10) == ["AAA"]


class CrawlOpener:
    """Serves discovery JSON scoped by endpoint+symbol, plus XBRL file bodies."""

    def __init__(self, discovery: dict[tuple[str, str], object], files: dict[str, bytes]):
        self.discovery = discovery
        self.files = files

    def open(self, req, timeout=30):  # noqa: ARG002 - mirrors urllib signature
        from urllib.parse import parse_qs, urlparse

        url = getattr(req, "full_url", req)
        parsed = urlparse(url)

        class Resp:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, size):
                return self.body

        for marker, payload in self.files.items():
            if marker in url:
                return Resp(payload)
        symbol = (parse_qs(parsed.query).get("symbol") or [""])[0]
        for (marker, sym), payload in self.discovery.items():
            if marker in parsed.path and sym == symbol:
                return Resp(payload.encode() if isinstance(payload, str) else payload)
        raise AssertionError(f"unexpected URL {url}")


def _discovery(symbol: str) -> dict[tuple[str, str], str]:
    s = symbol
    return {
        ("corporates-financial-results", s): (
            f'[{{"toDate":"31-Mar-2024","consolidated":"Consolidated",'
            f'"xbrl":"https://nsearchives.nseindia.com/corporate/xbrl/{s}_legacy.xml"}}]'
        ),
        ("integrated-filing-results", s): (
            f'{{"data":[{{"type":"Integrated Filing- Financials","qe_Date":"31-Mar-2026",'
            f'"consolidated":"Consolidated",'
            f'"xbrl":"https://nsearchives.nseindia.com/corporate/xbrl/{s}_int.xml"}}]}}'
        ),
        ("corporate-share-holdings-master", s): (
            f'[{{"date":"30-JUN-2026",'
            f'"xbrl":"https://nsearchives.nseindia.com/corporate/xbrl/{s}_shp.xml"}}]'
        ),
    }


def test_crawl_end_to_end_mini_and_empty_resume(conn):
    for sym in ("AAAL", "BBBL"):
        db.upsert_universe_row(conn, symbol=sym, series="EQ", source="fixture", fetched_at=FETCHED)
    opener = CrawlOpener(
        discovery={**_discovery("AAAL"), **_discovery("BBBL")},
        files={
            "AAAL_legacy.xml": FIN_XML,
            "AAAL_int.xml": FIN_XML,
            "AAAL_shp.xml": SHP_XML,
            "BBBL_legacy.xml": FIN_XML,
            "BBBL_int.xml": FIN_XML,
            "BBBL_shp.xml": SHP_XML,
        },
    )
    stats = xbrl_crawl.crawl(
        conn, ["AAAL", "BBBL"], opener=opener, sleep=lambda _s: None, fetched_at=FETCHED
    )
    assert stats["symbols_ok"] == 2 and stats["failed"] == []
    assert stats["filings"] == 6
    (manifest,) = conn.execute("SELECT COUNT(*) FROM stock_filing").fetchone()
    (pledge_rows,) = conn.execute(
        "SELECT COUNT(*) FROM stock_filing_fact WHERE fact_name LIKE 'WhetherAnyShares%'"
    ).fetchone()
    assert manifest == 6 and pledge_rows == 2

    # Queue drains: both symbols are now done.
    assert xbrl_crawl.pending_symbols(conn, 10) == []


def test_crawl_continues_after_one_symbol_fails(conn):
    for sym in ("GOOD", "BADX"):
        db.upsert_universe_row(conn, symbol=sym, series="EQ", source="fixture", fetched_at=FETCHED)
    discovery = _discovery("GOOD")
    opener = CrawlOpener(
        discovery=discovery,
        files={
            "GOOD_legacy.xml": FIN_XML,
            "GOOD_int.xml": FIN_XML,
            "GOOD_shp.xml": SHP_XML,
        },
    )
    stats = xbrl_crawl.crawl(
        conn, ["BADX", "GOOD"], opener=opener, sleep=lambda _s: None, fetched_at=FETCHED
    )
    assert stats["symbols_ok"] == 1 and stats["failed"] == ["BADX"]


def test_permanent_zero_symbol_gets_tombstoned_and_leaves_queue(conn):
    db.upsert_universe_row(conn, symbol="ZEROX", series="EQ", source="fx", fetched_at=FETCHED)
    # Discovery returns only a placeholder XBRL ref -> selection keeps nothing.
    placeholder_only = {
        ("corporates-financial-results", "ZEROX"): (
            '[{"toDate":"31-Mar-2024","consolidated":"Consolidated",'
            '"xbrl":"https://nsearchives.nseindia.com/corporate/xbrl/-"}]'
        ),
        ("integrated-filing-results", "ZEROX"): '{"data": []}',
        ("corporate-share-holdings-master", "ZEROX"): "[]",
    }
    opener = CrawlOpener(discovery=placeholder_only, files={})
    stats = xbrl_crawl.crawl(
        conn, ["ZEROX"], opener=opener, sleep=lambda _s: None, fetched_at=FETCHED
    )
    assert stats["symbols_ok"] == 1 and stats["skipped"] == 1 and stats["filings"] == 0
    (row,) = conn.execute("SELECT symbol, reason FROM stock_crawl_skip").fetchall()
    assert row == ("ZEROX", "no_retained_refs")
    # Tombstoned: queue is now empty even though no filing rows exist.
    assert xbrl_crawl.pending_symbols(conn, 10) == []
    (tombstones,) = conn.execute("SELECT COUNT(*) FROM stock_crawl_skip").fetchone()
    assert tombstones == 1


def test_crawl_skip_upsert_is_idempotent(conn):
    db.upsert_crawl_skip(conn, symbol="ZZZ", reason="no_retained_refs", checked_at=FETCHED)
    (before,) = conn.execute("SELECT checked_at FROM stock_crawl_skip").fetchone()
    db.upsert_crawl_skip(
        conn, symbol="ZZZ", reason="no_retained_refs", checked_at=dt(2026, 8, 26, tzinfo=UTC)
    )
    (after,) = conn.execute("SELECT checked_at FROM stock_crawl_skip").fetchone()
    assert before == after  # identical replay must not churn checked_at


def test_crawl_circuit_breaks_consecutive_systemic_failures(conn, monkeypatch):
    calls = []

    def always_fail(_conn, symbol, **_kwargs):
        calls.append(symbol)
        raise xbrl_crawl.nse_filings.SourceError("blocked")

    monkeypatch.setattr(xbrl_crawl, "ingest_symbol", always_fail)
    symbols = [f"FAIL{i}" for i in range(20)]
    stats = xbrl_crawl.crawl(conn, symbols, sleep=lambda _seconds: None)
    assert stats["aborted"] is True
    assert stats["symbols_ok"] == 0
    assert stats["failed"] == symbols[: xbrl_crawl.MAX_CONSECUTIVE_FAILURES]
    assert len(calls) == xbrl_crawl.MAX_CONSECUTIVE_FAILURES


def test_ingest_symbol_skips_one_archived_404_and_keeps_newer_filing(conn, monkeypatch):
    from urllib import error

    old = ref("financial_annual_legacy", "2024-03-31", "https://x/OLD.xml")
    new = ref("financial_annual_legacy", "2025-03-31", "https://x/NEW.xml")
    monkeypatch.setattr(xbrl_crawl.nse_filings, "discover", lambda *_a, **_k: [old, new])

    def fetch(url, **_kwargs):
        if url.endswith("OLD.xml"):
            http = error.HTTPError(url, 404, "missing", {}, None)
            raise xbrl_crawl.nse_filings.SourceError("download failed") from http
        return b"<xbrl/>"

    monkeypatch.setattr(xbrl_crawl.nse_filings, "fetch_xbrl", fetch)
    monkeypatch.setattr(
        xbrl_crawl.nse_filings,
        "ingest_filing",
        lambda *_a, **_k: {"contexts": 1, "facts": 2},
    )
    stats = xbrl_crawl.ingest_symbol(conn, "TEST", opener=object(), sleep=lambda _x: None)
    assert stats["downloads"] == 1 and stats["filings"] == 1
    assert conn.execute("SELECT COUNT(*) FROM stock_crawl_skip").fetchone()[0] == 0


def test_ingest_symbol_tombstones_when_every_selected_ref_is_404(conn, monkeypatch):
    from urllib import error

    only = ref("financial_annual_legacy", "2025-03-31", "https://x/GONE.xml")
    monkeypatch.setattr(xbrl_crawl.nse_filings, "discover", lambda *_a, **_k: [only])

    def missing(url, **_kwargs):
        http = error.HTTPError(url, 404, "missing", {}, None)
        raise xbrl_crawl.nse_filings.SourceError("download failed") from http

    monkeypatch.setattr(xbrl_crawl.nse_filings, "fetch_xbrl", missing)
    stats = xbrl_crawl.ingest_symbol(conn, "TEST", opener=object(), sleep=lambda _x: None)
    assert stats["skipped"] is True and stats["downloads"] == 0
    assert (
        conn.execute("SELECT reason FROM stock_crawl_skip").fetchone()[0] == "all_selected_xbrl_404"
    )


def test_ingest_symbol_reraises_non_404_source_errors(conn, monkeypatch):
    from urllib import error

    only = ref("financial_annual_legacy", "2025-03-31", "https://x/BLOCKED.xml")
    monkeypatch.setattr(xbrl_crawl.nse_filings, "discover", lambda *_a, **_k: [only])

    def forbidden(url, **_kwargs):
        http = error.HTTPError(url, 403, "forbidden", {}, None)
        raise xbrl_crawl.nse_filings.SourceError("download failed") from http

    monkeypatch.setattr(xbrl_crawl.nse_filings, "fetch_xbrl", forbidden)
    with pytest.raises(xbrl_crawl.nse_filings.SourceError):
        xbrl_crawl.ingest_symbol(conn, "TEST", opener=object(), sleep=lambda _x: None)

    monkeypatch.setattr(
        xbrl_crawl.nse_filings,
        "fetch_xbrl",
        lambda *_a, **_k: (_ for _ in ()).throw(xbrl_crawl.nse_filings.SourceError("transport")),
    )
    with pytest.raises(xbrl_crawl.nse_filings.SourceError):
        xbrl_crawl.ingest_symbol(conn, "TEST", opener=object(), sleep=lambda _x: None)


def test_all_404_tombstone_retries_after_30_days(conn):
    from datetime import timedelta

    now = dt.now(UTC)
    for symbol in ("FRESH404", "OLD404", "PERMANENT"):
        db.upsert_universe_row(conn, symbol=symbol, series="EQ", source="fx", fetched_at=now)
    db.upsert_crawl_skip(conn, symbol="FRESH404", reason="all_selected_xbrl_404", checked_at=now)
    db.upsert_crawl_skip(
        conn,
        symbol="OLD404",
        reason="all_selected_xbrl_404",
        checked_at=now - timedelta(days=31),
    )
    db.upsert_crawl_skip(
        conn,
        symbol="PERMANENT",
        reason="no_retained_refs",
        checked_at=now - timedelta(days=31),
    )
    assert xbrl_crawl.pending_symbols(conn, 10) == ["OLD404"]
