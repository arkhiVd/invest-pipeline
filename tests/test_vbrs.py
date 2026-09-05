"""Synthetic VBRS boundaries and PE-store idempotence."""

import json

import pytest

from invest import db, pe, vbrs


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


def test_schema_v3_migration_recorded(conn):
    versions = [
        r[0]
        for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    ]
    assert 3 in versions
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nifty_pe)").fetchall()}
    assert {"nav_date", "pe", "pb", "dy", "close", "source", "fetched_at"} <= cols


def test_pe_store_idempotent_and_overwrites(conn):
    from datetime import date

    day = date(2025, 3, 14)
    pe.store(conn, {"pe": 23.5, "pb": 3.0, "dy": 1.2, "close": 25000.0}, day=day)
    pe.store(conn, {"pe": 24.0, "pb": 3.1, "dy": 1.1, "close": 25100.0}, day=day)
    rows = conn.execute("SELECT count(*) FROM nifty_pe").fetchone()[0]
    assert rows == 1  # same-day refresh upserts, never duplicates
    nav_date, p, pb, dy, close = pe.latest(conn)
    assert (p, close) == (24.0, 25100.0)


def test_pe_parse_normalizes_strings():
    payload = {
        "data": [
            {"index": "OTHER", "last": 1.0},
            {"index": "NIFTY 50", "last": "25000.00", "pe": "24.00", "pb": "3.00", "dy": "1.20"},
        ]
    }
    canned = lambda: json.dumps(payload)  # noqa: E731
    vals = pe.fetch_nifty_valuations(fetcher=canned)
    assert vals == {"close": 25000.0, "pe": 24.0, "pb": 3.0, "dy": 1.2}


def test_pe_parse_missing_row_raises():
    with pytest.raises(ValueError, match="NIFTY 50"):
        pe.fetch_nifty_valuations(fetcher=lambda: json.dumps({"data": []}))


def test_hand_worked_formula_case_exact():
    # Synthetic hand calculation: 5% + ((22.5 / 23.4) - 1) * 30%.
    cfg = vbrs.DEFAULT_CONFIG
    got = vbrs.cash_position(22.5, 23.4, cfg)
    assert got == pytest.approx(0.0384615384615385, rel=1e-12)
    # Pin the equation independently of configuration loading.
    assert got == 0.05 + ((22.5 / 23.4) - 1) * 0.30


def test_allocation_matches_public_example_buckets():
    rows = vbrs.allocate(20000, vbrs.cash_position(22.5, 23.4))
    by_name = dict((n, (w, a)) for n, w, a in rows)
    assert by_name["Core Holdings"][0] == 0.50
    assert by_name["Tactical Allocation"][0] == 0.30
    assert by_name["Cyclical - Counter"][0] == 0.16
    assert by_name["Core Holdings"][1] == pytest.approx(10000)
    assert by_name["Tactical Allocation"][1] == pytest.approx(6000)
    # The engine retains the unrounded formula result.
    assert by_name["Cash"][1] == pytest.approx(20000 * 0.0384615384615385)
    total = sum(a for _, _, a in rows)
    assert total == pytest.approx(sum(w for _, w, _a in rows) * 20000)


def test_zone_bands_informational():
    cfg = dict(vbrs.DEFAULT_CONFIG)
    assert vbrs.zone(19.9, cfg) == "Cheap"
    assert vbrs.zone(24.0, cfg) == "Base"
    assert vbrs.zone(28.1, cfg) == "Expensive"


def test_cli_runs_with_override(tmp_path, capsys):
    dbpath = tmp_path / "t.duckdb"
    rc = vbrs.main(["--db", str(dbpath), "--pe", "24.0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "5.7692%" in out and "Base zone" in out
