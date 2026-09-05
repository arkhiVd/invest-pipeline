"""T3.2a official NSE discovery/XBRL parser contracts; offline fixtures."""

from datetime import UTC, date
from datetime import datetime as dt

import duckdb
import pytest

from invest import db, nse_filings

LEGACY_URL = "https://nsearchives.nseindia.com/corporate/xbrl/INDAS_OLD.xml"
INTEGRATED_URL = "https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_INDAS_NEW.xml"
SHARE_URL = "https://nsearchives.nseindia.com/corporate/xbrl/SHP_CURRENT.xml"


def fake_fetch(path, params):
    assert params["symbol"] == "ZEAL"
    if path.endswith("corporates-financial-results"):
        return [
            {
                "toDate": "31-Mar-2024",
                "consolidated": "Consolidated",
                "xbrl": LEGACY_URL,
            }
        ]
    if path.endswith("integrated-filing-results"):
        return {
            "data": [
                {
                    "type": "Integrated Filing- Governance",
                    "qe_Date": "30-JUN-2026",
                    "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/GOV.xml",
                },
                {
                    "type": "Integrated Filing- Financials",
                    "qe_Date": "30-JUN-2026",
                    "consolidated": "Consolidated",
                    "xbrl": INTEGRATED_URL,
                },
            ]
        }
    if path.endswith("corporate-share-holdings-master"):
        return [{"date": "30-JUN-2026", "xbrl": SHARE_URL}]
    raise AssertionError(path)


def test_discover_joins_both_financial_regimes_and_shareholding():
    refs = nse_filings.discover("zeal", fetcher=fake_fetch)
    assert [r.filing_type for r in refs] == [
        "financial_annual_legacy",
        "financial_integrated",
        "shareholding",
    ]
    assert refs[0].period_end == date(2024, 3, 31)
    assert refs[1].period_end == date(2026, 6, 30)
    assert refs[1].taxonomy == "indas"
    assert refs[2].taxonomy == "shp"


def test_discover_drops_placeholder_xbrl_rows():
    def fake(path, params):
        if path.endswith("corporates-financial-results"):
            return [
                {
                    "toDate": "31-Mar-2020",
                    "consolidated": "Consolidated",
                    "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/-",
                },
                {
                    "toDate": "31-Mar-2024",
                    "consolidated": "Consolidated",
                    "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/REAL.xml",
                },
            ]
        if path.endswith("integrated-filing-results"):
            return {"data": []}
        return []

    refs = nse_filings.discover("ZEAL", fetcher=fake)
    assert [r.xbrl_url for r in refs] == [
        "https://nsearchives.nseindia.com/corporate/xbrl/REAL.xml"
    ]


def test_pledge_flag_and_financial_fact_extraction_are_namespace_agnostic():
    pledge = nse_filings.PLEDGE_FACT
    xml = f"""<?xml version='1.0'?>
    <x:xbrl xmlns:x='urn:xbrl' xmlns:i='urn:indas' xmlns:s='urn:shp'>
      <x:context id='FY'>
        <x:period><x:startDate>2025-04-01</x:startDate><x:endDate>2026-03-31</x:endDate></x:period>
      </x:context>
      <i:RevenueFromOperations contextRef='FY' unitRef='INR' decimals='-3'>
        123450000
      </i:RevenueFromOperations>
      <i:ProfitLossForPeriod contextRef='FY'>12300000</i:ProfitLossForPeriod>
      <i:Equity contextRef='END'>50000000</i:Equity>
      <s:{pledge} contextRef='Q'>true</s:{pledge}>
    </x:xbrl>""".encode()
    assert nse_filings.promoter_pledged(xml) is True
    facts = nse_filings.financial_facts(xml)
    contexts = nse_filings.xbrl_contexts(xml)
    assert contexts["FY"].start_date == "2025-04-01"
    assert contexts["FY"].end_date == "2026-03-31"
    revenue = facts["RevenueFromOperations"][0]
    assert revenue.value == "123450000"
    assert revenue.context_ref == "FY"
    assert revenue.unit_ref == "INR"
    assert revenue.decimals == "-3"
    assert facts["ProfitLossForPeriod"][0].value == "12300000"
    assert facts["Equity"][0].value == "50000000"


def test_missing_pledge_fact_is_schema_drift_not_false():
    assert nse_filings.promoter_pledged(b"<xbrl />") is None


def test_contradictory_pledge_facts_fail_loudly():
    name = nse_filings.PLEDGE_FACT
    xml = f"<xbrl><{name}>true</{name}><{name}>false</{name}></xbrl>".encode()
    try:
        nse_filings.promoter_pledged(xml)
    except nse_filings.SourceError as exc:
        assert "contradictory" in str(exc)
    else:
        raise AssertionError("contradictory pledge facts were silently accepted")


def test_symbol_path_traversal_is_rejected_before_fetch():
    for symbol in ("../../etc", ".", ".."):
        try:
            nse_filings.discover(symbol, fetcher=fake_fetch)
        except ValueError as exc:
            assert "unsupported characters" in str(exc)
        else:
            raise AssertionError(f"path traversal symbol accepted: {symbol}")


def test_retain_is_content_addressed_and_idempotent(tmp_path):
    ref = nse_filings.discover("ZEAL", fetcher=fake_fetch)[2]
    xml = b"<xbrl><Fact>1</Fact></xbrl>"
    conn = duckdb.connect()
    db.init_schema(conn)
    first = nse_filings.retain(
        conn, ref, xml, root=tmp_path, fetched_at=dt(2026, 8, 25, 12, tzinfo=UTC)
    )
    before = db.fingerprint(conn, "stock_filing")
    second = nse_filings.retain(
        conn, ref, xml, root=tmp_path, fetched_at=dt(2026, 8, 25, 13, tzinfo=UTC)
    )
    assert first == second
    assert first.is_absolute()
    assert first.read_bytes() == xml
    assert db.fingerprint(conn, "stock_filing") == before
    assert before[0] == 1
    conn.close()


def test_discovery_status_preserves_partial_section_failure_without_typeerror():
    def fake(path, _params):
        if path.endswith("corporates-financial-results"):
            raise nse_filings.SourceError("legacy blocked")
        if path.endswith("integrated-filing-results"):
            return {
                "data": [
                    {
                        "type": "financial",
                        "qe_Date": "31-03-2026",
                        "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/I.xml",
                    }
                ]
            }
        return []

    result = nse_filings.discover_with_status("NOVA", fetcher=fake)
    assert result.legacy_ok is False and result.integrated_ok is True
    assert result.errors == ("legacy",)
    assert len(result.refs) == 1
    assert len(nse_filings.discover("NOVA", fetcher=fake)) == 1


def test_legacy_reporting_metadata_synthesizes_missing_four_context():
    xml = b"""<xbrl>
      <DateOfStartOfReportingPeriod contextRef='FourD'>2021-04-01</DateOfStartOfReportingPeriod>
      <DateOfEndOfReportingPeriod contextRef='FourD'>2022-03-31</DateOfEndOfReportingPeriod>
      <ProfitLossForPeriod contextRef='FourD'>10</ProfitLossForPeriod>
    </xbrl>"""
    contexts = nse_filings.xbrl_contexts(xml)
    assert contexts["FourD"].start_date == "2021-04-01"
    assert contexts["FourD"].end_date == "2022-03-31"


def test_total_discovery_failure_is_evidence_for_status_but_wrapper_raises():
    def fail(_path, _params):
        raise nse_filings.SourceError("blocked")

    result = nse_filings.discover_with_status("NOVA", fetcher=fail)
    assert result.refs == ()
    assert result.errors == ("integrated", "legacy", "shareholding")
    with pytest.raises(nse_filings.SourceError, match="discovery unusable"):
        nse_filings.discover("NOVA", fetcher=fail)


def test_conflicting_synthetic_context_metadata_fails_loudly():
    xml = b"""<xbrl>
      <DateOfStartOfReportingPeriod contextRef='FourD'>2021-04-01</DateOfStartOfReportingPeriod>
      <DateOfStartOfReportingPeriod contextRef='FourD'>2021-05-01</DateOfStartOfReportingPeriod>
      <DateOfEndOfReportingPeriod contextRef='FourD'>2022-03-31</DateOfEndOfReportingPeriod>
    </xbrl>"""
    with pytest.raises(nse_filings.SourceError, match="conflicting startDate"):
        nse_filings.xbrl_contexts(xml)
