from datetime import UTC, date, timedelta
from datetime import datetime as dt
from decimal import Decimal

import duckdb
import pytest

from invest import db, signals, swing

NOW = dt(2026, 8, 26, tzinfo=UTC)

CONFIG = {
    "universe_index": "NIFTY 100",
    "benchmark": "NIFTY 50",
    "window_days": 252,
    "min_observations": 3,
    "max_price": 3000.0,
    "top_n": 5,
    "max_price_age_days": 30,
    "constituent_min_count": 1,
    "capital": 100_000,
    "risk_fraction": 0.02,
    "stop_mode": "ema21",
    "initial_lookback_days": 5,
}


def weekdays(start, count):
    days = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def connection():
    conn = duckdb.connect()
    db.init_schema(conn)
    return conn


def store_prices(conn, symbol, pairs):
    conn.executemany(
        "INSERT INTO stock_price VALUES (?, ?, NULL, NULL, NULL, ?, NULL, NULL, 'fx', ?)",
        [(symbol, day, close, NOW) for day, close in pairs],
    )


def store_index(conn, pairs):
    conn.executemany(
        "INSERT INTO index_close VALUES ('NIFTY 50', ?, ?, 'fx', ?)",
        [(day, close, NOW) for day, close in pairs],
    )


def single_member_db(closes):
    """DB whose watchlist has exactly one pick covering ``closes``."""
    conn = connection()
    conn.execute(
        "INSERT INTO index_constituent VALUES ('NIFTY 100', 'SWING', 'S', NULL, "
        "'I1', 'EQ', 'fx', ?)",
        [NOW],
    )
    days = weekdays(date(2026, 1, 5), len(closes))
    store_index(conn, [(day, 100.0 + i * 0.5) for i, day in enumerate(days)])
    store_prices(conn, "SWING", list(zip(days, closes, strict=True)))
    return conn, days


def oracle_signals(closes):
    """Crossover dates straight from the independently-tested primitives."""
    points = swing.ema_crossover([float(close) for close in closes])
    return [
        (index, point.signal.value)
        for index, point in enumerate(points)
        if point.signal is not swing.CrossoverSignal.NONE
    ]


def test_enter_signal_surfaced_once_with_exact_sizing():
    conn, days = single_member_db([3, 2, 1, 2, 3, 2, 1])
    found = signals.scan_symbol(
        conn,
        "SWING",
        since=days[0] - timedelta(days=1),
        config={**CONFIG},
        fast_period=2,
        slow_period=3,
    )
    assert [(item["date"], item["action"]) for item in found] == [
        (days[4], "enter"),
        (days[5], "exit"),
    ]
    entry = found[0]
    assert entry["close"] == 3.0
    assert entry["sizing"] == {
        "quantity": 4000,
        "stop": 2.5,
        "capital_to_deploy": 12000.0,
        "maximum_loss_at_stop": 2000.0,
    }
    assert "sizing" not in found[1]
    conn.close()


def test_since_boundary_excludes_already_reported_day():
    conn, days = single_member_db([3, 2, 1, 2, 3, 2, 1])
    found = signals.scan_symbol(
        conn,
        "SWING",
        since=days[4],
        config=CONFIG,
        fast_period=2,
        slow_period=3,
    )
    assert [item["date"] for item in found] == [days[5]]
    conn.close()


def test_run_scan_matches_oracle_and_watermark_deduplicates():
    # Twenty-two exact-flat closes make both EMAs exactly 100 (armed neutral);
    # the first +5 close must produce exactly one ENTER (hand-checkable:
    # fast gains 0.91 vs slow 0.45).
    closes = [100.0] * 22 + [105.0, 110.0]
    conn, days = single_member_db(closes)
    expected = [(days[index], action) for index, action in oracle_signals(closes)]
    report = signals.run_scan(conn, CONFIG)
    assert report["scanned"] == 1
    actual = [(item["date"], item["action"]) for item in report["signals"]]
    assert actual == expected
    assert len(expected) >= 1
    signals.advance_watermark(conn, report, updated_at=NOW)
    assert signals.run_scan(conn, CONFIG)["signals"] == []
    conn.close()


def test_first_run_lookback_limits_history():
    closes = [100.0] * 22 + [105.0, 110.0]
    conn, days = single_member_db(closes)
    expected = [(days[index], action) for index, action in oracle_signals(closes)]
    stale_config = {**CONFIG, "initial_lookback_days": 1}
    report = signals.run_scan(conn, stale_config)
    reference = signals.latest_session(conn)
    assert report["since"] == reference - timedelta(days=1)
    assert report["first_run"] is True
    surfaced = {(item["date"], item["action"]) for item in report["signals"]}
    assert surfaced <= set(expected)
    assert all(day >= report["since"] for day, _action in surfaced)
    conn.close()


def test_run_scan_fails_closed_without_constituents():
    conn = connection()
    with pytest.raises(ValueError, match="constituents"):
        signals.run_scan(conn, CONFIG)
    conn.close()


def test_run_scan_does_not_advance_on_empty_eligible_watchlist():
    conn = connection()
    conn.execute(
        "INSERT INTO index_constituent VALUES "
        "('NIFTY 100', 'THIN', 'T', NULL, 'I1', 'EQ', 'fx', ?)",
        [NOW],
    )
    day = date(2026, 1, 5)
    store_index(conn, [(day, 100.0)])
    store_prices(conn, "THIN", [(day, 50.0)])
    with pytest.raises(ValueError, match="no eligible"):
        signals.run_scan(conn, CONFIG)
    assert db.get_watermark(conn, signals.WATERMARK_KIND) is None
    conn.close()


def test_reference_ignores_forward_dated_non_watchlist_symbol():
    closes = [100.0] * 22 + [105.0, 110.0]
    conn, days = single_member_db(closes)
    future = days[-1] + timedelta(days=30)
    store_prices(conn, "STRAY", [(future, 1.0)])
    report = signals.run_scan(conn, CONFIG)
    assert report["as_of"] == days[-1]
    assert all(item["date"] <= days[-1] for item in report["signals"])
    conn.close()


def test_canonical_cutoff_bounds_signal_watchlist_and_report_date():
    closes = [100.0] * 22 + [105.0, 110.0]
    conn, days = single_member_db(closes)
    future = days[-1] + timedelta(days=30)
    store_index(conn, [(future, 500.0)])
    store_prices(conn, "SWING", [(future, 999.0)])
    report = signals.run_scan(conn, CONFIG, canonical_cutoff=days[-1])
    assert report["as_of"] == days[-1]
    assert all(item["date"] <= days[-1] for item in report["signals"])
    conn.close()


def test_size_entry_reports_honest_gaps():
    good_config = dict(CONFIG)
    assert signals._size_entry(good_config, 100.0, 95.0)["quantity"] > 0
    unavailable = signals._size_entry(good_config, 100.0, None)
    assert unavailable == {"quantity": 0, "reason": "ema_unavailable"}
    invalid = signals._size_entry(good_config, 90.0, 95.0)
    assert invalid["quantity"] == 0 and invalid["reason"].startswith("invalid_stop:")
    broke = signals._size_entry({**good_config, "capital": 50}, 100.0, 99.0)
    assert broke == {"quantity": 0, "reason": "zero_shares_within_risk"}
    assert (
        signals._size_entry({**good_config, "risk_fraction": Decimal("0.02")}, 100.0, 95.0)[
            "quantity"
        ]
        > 0
    )


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"capital": 0}, "capital"),
        ({"capital": "100000"}, "capital"),
        ({"stop_mode": "swing_low"}, "stop_mode"),
        ({"risk_fraction": 1.5}, "risk_fraction"),
        ({"risk_fraction": 0}, "risk_fraction"),
        ({"initial_lookback_days": 0}, "initial_lookback_days"),
    ],
)
def test_validate_signal_config_rejects_bad_values(override, message):
    bad = {**CONFIG, **override}
    with pytest.raises(ValueError, match=message):
        signals.validate_signal_config(bad)


def test_render_reports_counts_and_sizing_gaps():
    text = signals.render(
        {
            "as_of": date(2026, 8, 25),
            "since": date(2026, 8, 20),
            "first_run": False,
            "scanned": 7,
            "signals": [
                {
                    "symbol": "SWING",
                    "action": "enter",
                    "date": date(2026, 8, 25),
                    "close": 3.0,
                    "ema_fast": 3.1,
                    "ema_slow": 2.5,
                    "sizing": {"quantity": 0, "reason": "invalid_stop:x"},
                }
            ],
            "sizing_gaps": 1,
        }
    )
    assert "SWING SIGNALS as_of=2026-08-25 scanned=7 new=1" in text
    assert "ENTER SWING" in text and "sizing=UNAVAILABLE(invalid_stop:x)" in text
    assert "sizing_gaps=1" in text


def test_main_report_only_leaves_no_state(tmp_path, capsys):
    import json

    database = tmp_path / "signals.duckdb"
    config_path = tmp_path / "swing.json"
    config_path.write_text(json.dumps(CONFIG))
    closes = [100.0] * 22 + [105.0, 110.0]
    conn = duckdb.connect(str(database))
    db.init_schema(conn)
    conn.execute(
        "INSERT INTO index_constituent VALUES ('NIFTY 100', 'SWING', 'S', NULL, "
        "'I1', 'EQ', 'fx', ?)",
        [NOW],
    )
    days = weekdays(date(2026, 1, 5), len(closes))
    store_index(conn, [(day, 100.0 + i * 0.5) for i, day in enumerate(days)])
    store_prices(conn, "SWING", list(zip(days, closes, strict=True)))
    conn.close()

    out_path = tmp_path / "swing-latest.txt"
    rc = signals.main(
        [
            "--db",
            str(database),
            "--config",
            str(config_path),
            "--out",
            str(out_path),
            "--report-only",
        ]
    )
    assert rc == 0
    assert not out_path.exists()  # report-only never writes state or artifact
    check = duckdb.connect(str(database))
    assert (
        check.execute(
            "SELECT COUNT(*) FROM ingest_watermark WHERE kind = ?",
            [signals.WATERMARK_KIND],
        ).fetchone()[0]
        == 0
    )
    check.close()
    captured = capsys.readouterr().out
    assert "ENTER SWING" in captured

    rc = signals.main(["--db", str(database), "--config", str(config_path), "--out", str(out_path)])
    assert rc == 0
    assert out_path.exists()
    check = duckdb.connect(str(database))
    row = check.execute(
        "SELECT last_date FROM ingest_watermark WHERE kind = ?",
        [signals.WATERMARK_KIND],
    ).fetchone()
    check.close()
    assert row is not None and row[0] >= days[-1]


def test_main_rejects_empty_out(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        signals.main(["--db", str(tmp_path / "x.duckdb"), "--out", ""])
    assert excinfo.value.code == 2
