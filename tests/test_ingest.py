"""T1.4 unit tests — offline; fetchers are stubbed, no network."""

import json
from datetime import date
from datetime import datetime as dt

import duckdb
import pytest

from invest import db, ingest

SNAPSHOT_ROW = {
    "scheme_code": "900001",
    "isin": "INF179K01UT0",
    "isin2": "",
    "scheme_name": "ORBIT Flexi Cap Fund - Growth Option - Direct Plan",
    "amc": "ORBIT Mutual Fund",
    "scheme_type": "Open Ended Schemes",
    "category": "Equity Scheme - Flexi Cap Fund",
    "category_sub": "Flexi Cap Fund",
    "category_group_clean": "Equity Scheme",
    "category_group": "Equity Scheme",
    "scheme_plan": "Direct",
    "scheme_option": "Growth",
    "first_date": "2013-01-01",
    "last_date": "2026-08-24",
    "is_active": "true",
    "is_stale": "false",
    "txic_code": "SCFLEX",
    "aaum_cr_quarterly_avg": "45000.2",
    "aaum_quarter": "June-2026",
    "aaum_quarter_end": "2026-06-30",
    "nav_date": "2026-08-24",
    "nav": "2293.072",
}

HISTORY_PAYLOAD = {
    "scheme_code": 900001,
    "scheme_name": "ORBIT Flexi Cap Fund - Growth Option - Direct Plan",
    "data": [
        {"date": "2026-08-21", "nav": 2288.1},
        {"date": "2026-08-22", "nav": 2289.5},
        {"date": "2026-08-23", "nav": 2290.0},
    ],
}


@pytest.fixture()
def conn():
    c = duckdb.connect()
    db.init_schema(c)
    yield c
    c.close()


def test_normalize_name_expands_abbreviations():
    assert ingest.normalize_name("Aditya Birla SL Nifty Midcap") == [
        "aditya",
        "birla",
        "sun",
        "life",
        "nifty",
        "mid",
        "cap",
    ]
    assert ingest.normalize_name("ICICI Pru Banking & PSU Debt") == [
        "icici",
        "prudential",
        "banking",
        "psu",
        "debt",
    ]


def test_resolve_fund_prefers_direct_active():
    regular = dict(SNAPSHOT_ROW)
    direct = dict(SNAPSHOT_ROW)
    rows = []
    for i, (code, plan, active) in enumerate(
        [
            ("910002", "Regular", "false"),
            ("900001", "Direct", "true"),
            ("910001", "Regular", "true"),
        ]
    ):
        row = (regular if i % 2 else direct).copy()
        row.update(
            scheme_code=code,
            scheme_plan=plan,
            is_active=active,
            scheme_name=row["scheme_name"].replace("Direct", plan),
        )
        rows.append(row)
    hit = ingest.resolve_fund("ORBIT Flexi Cap Fund", rows, prefer_direct=True)
    assert hit is not None and hit["scheme_code"] == "900001"


def test_resolve_fund_returns_none_when_no_match():
    assert ingest.resolve_fund("Zzz Nonexistent Fund", [SNAPSHOT_ROW]) is None


def test_snapshot_row_conversion_normalizes_types():
    row = ingest.snapshot_to_scheme_row(SNAPSHOT_ROW, display_name="ORBIT Flexi Cap")
    assert isinstance(row["first_date"], date)
    assert row["is_active"] is True
    assert row["aaum_cr_quarterly_avg"] == pytest.approx(45000.2)
    assert row["display_name"] == "ORBIT Flexi Cap"


def test_backfill_is_idempotent_and_paces(conn, monkeypatch):
    db.upsert_scheme(
        conn,
        scheme_code=900001,
        name="ORBIT Flexi Cap Fund - Growth Option - Direct Plan",
        display_name="ORBIT Flexi Cap",
    )
    calls = []

    def fake_fetch(code):
        calls.append(code)
        return HISTORY_PAYLOAD

    stats1 = ingest.backfill_history(conn, [900001], min_interval_s=0, fetcher=fake_fetch)
    before = db.fingerprint(conn, "mf_nav")
    stats2 = ingest.backfill_history(conn, [900001], min_interval_s=0, fetcher=fake_fetch)
    after = db.fingerprint(conn, "mf_nav")

    assert stats1["schemes"] == stats2["schemes"] == 1
    assert stats1["nav_rows"] == stats2["nav_rows"] == len(HISTORY_PAYLOAD["data"])
    assert before == after  # second run changed nothing
    assert calls == [900001, 900001]


def test_backfill_falls_back_to_mfapi(conn, monkeypatch):
    db.upsert_scheme(conn, scheme_code=900001, name="ORBIT Flexi Cap Fund")

    def tigzig_fail(code):
        msg = "boom"
        raise RuntimeError(msg)

    def mfapi_ok(code):
        # recorded MFAPI shape: DD-MM-YYYY string dates, string navs
        return [
            (dt.strptime("21-08-2026", "%d-%m-%Y").date(), 2293.072),
            (dt.strptime("20-08-2026", "%d-%m-%Y").date(), 2290.0),
        ]

    monkeypatch.setattr(ingest, "fetch_scheme_history", tigzig_fail)
    monkeypatch.setattr(ingest, "fetch_scheme_history_mfapi", lambda code: mfapi_ok(code))
    stats = ingest.backfill_history(conn, [900001], min_interval_s=0)

    assert stats["schemes"] == 1
    assert stats["fallback"] == [900001]
    (n,) = conn.execute("SELECT COUNT(*) FROM mf_nav").fetchone()
    assert n == 2


def test_backfill_both_sources_failing_skips_and_reports(conn):
    def fail(code):
        msg = "down"
        raise RuntimeError(msg)

    stats = ingest.backfill_history(conn, [42], min_interval_s=0, fetcher=fail)
    assert stats["schemes"] == 0


def test_manual_overrides_roundtrip(tmp_path):
    p = tmp_path / "scheme_map.json"
    p.write_text(json.dumps({"JM Flexicap Fund": 910003}))
    assert ingest.load_manual_overrides(p) == {"JM Flexicap Fund": 910003}
    assert ingest.load_manual_overrides(tmp_path / "missing.json") == {}


def test_db_load_failure_does_not_kill_run(conn):
    """One scheme violating FK must be skipped, not crash the batch."""

    def fetch_no_parent(code):  # valid payload, no mf_scheme row exists
        return HISTORY_PAYLOAD

    stats = ingest.backfill_history(conn, [900001], min_interval_s=0, fetcher=fetch_no_parent)
    assert stats["schemes"] == 0


def test_watchlist_and_ignore_config_roundtrip(tmp_path):
    wl = tmp_path / "watchlist.json"
    wl.write_text(
        json.dumps(
            {"funds": [{"code": 910007, "name": "JioBlackRock Flexi Cap", "group": "jioblackrock"}]}
        )
    )
    ig = tmp_path / "ignore.json"
    ig.write_text(json.dumps({"ignore": ["Synthetic Transport Fund"]}))

    entries = ingest.load_watchlist(wl)
    assert entries[0]["code"] == 910007
    assert ingest.load_watchlist(tmp_path / "missing.json") == []
    assert ingest.load_ignore_set(ig) == {"Synthetic Transport Fund"}
    assert ingest.load_ignore_set(tmp_path / "missing.json") == set()


def test_register_scheme_code_from_snapshot(conn):
    ingest.register_scheme_code(conn, 900001, "ORBIT Flexi Cap", [SNAPSHOT_ROW])
    (row,) = conn.execute(
        "SELECT scheme_code, display_name, amc, is_active FROM mf_scheme"
    ).fetchall()
    assert row[0] == 900001 and row[1] == "ORBIT Flexi Cap" and row[3] is True
    # re-register with same code is a no-op except display_name update
    ingest.register_scheme_code(conn, 900001, "ORBIT Flexi Cap (renamed)", [SNAPSHOT_ROW])
    (count,) = conn.execute("SELECT COUNT(*) FROM mf_scheme").fetchone()
    (dn,) = conn.execute("SELECT display_name FROM mf_scheme WHERE scheme_code=900001").fetchone()
    assert count == 1 and dn == "ORBIT Flexi Cap (renamed)"


def test_register_scheme_code_unknown_code_gets_minimal_row(conn):
    ingest.register_scheme_code(conn, 42, None, [])
    (name,) = conn.execute("SELECT name FROM mf_scheme WHERE scheme_code=42").fetchone()
    assert name == "42"


def _variant_rows(base_name: str, variants: list[tuple[str, str]]) -> list[dict]:
    """Build snapshot rows differing only by plan/option suffix."""
    return [
        dict(
            SNAPSHOT_ROW,
            scheme_code=code,
            scheme_name=f"{base_name} - {suffix}",
            is_active="true",
        )
        for code, suffix in variants
    ]


def test_resolve_fund_never_picks_idcw_when_growth_exists():
    """Regression 2026-08-25: synthetic fund resolved to IDCW variant via length tie-break."""
    rows = _variant_rows(
        "Synthetic Alpha Fund",
        [("910008", "Direct Plan - IDCW"), ("910006", "Direct Plan - Growth")],
    )
    hit = ingest.resolve_fund("Synthetic Alpha Fund", rows)
    assert hit is not None and hit["scheme_code"] == "910006"


def test_resolve_fund_union_flexi_case_regression():
    rows = _variant_rows(
        "Synthetic Beta Fund",
        [("910005", "Direct Plan - IDCW Option"), ("910004", "Direct Plan - Growth Option")],
    )
    hit = ingest.resolve_fund("Synthetic Beta Fund", rows)
    assert hit is not None and hit["scheme_code"] == "910004"


def test_resolve_fund_idcw_only_fund_still_resolves():
    """Funds that genuinely only exist as IDCW must not break."""
    rows = _variant_rows("Only IDCW Fund", [("999", "Direct Plan - IDCW")])
    hit = ingest.resolve_fund("Only IDCW Fund", rows)
    assert hit is not None and hit["scheme_code"] == "999"
