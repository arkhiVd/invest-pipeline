from datetime import UTC, date
from datetime import datetime as dt

import duckdb
import pytest

from invest import (
    accounting,
    db,
    ranking,
    screens,
    tracking,
    tracking_run,
    ui_projection,
    universe,
    watchlist,
)


class _Cursor:
    def fetchone(self):
        return (18,)


class _Connection:
    def execute(self, _query, _params=None):
        return _Cursor()


def test_signal_report_is_persisted_and_written_before_watermark(monkeypatch, tmp_path):
    calls = []
    report = {"as_of": "2026-08-28", "signals": [], "scanned": 1, "sizing_gaps": 0}
    monkeypatch.setattr(tracking_run.watchlist, "load_config", lambda: {"swing": True})
    monkeypatch.setattr(tracking_run.screens, "load_config", lambda: {"screens": {}})
    monkeypatch.setattr(tracking_run, "semantic_config", lambda *_args: {"semantic": True})
    monkeypatch.setattr(
        tracking_run.tracking, "register_methodology", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(tracking_run, "_persist_watchlist", lambda *_args: "watchlist-run")
    monkeypatch.setattr(tracking_run, "_persist_screens", lambda *_args: None)
    monkeypatch.setattr(tracking_run.signals, "run_scan", lambda *_args, **_kwargs: report)
    monkeypatch.setattr(
        tracking_run.tracking,
        "persist_signal_run",
        lambda *_args, **_kwargs: calls.append("persist") or {"status": "ACCEPTED"},
    )
    monkeypatch.setattr(
        tracking_run.signals,
        "render",
        lambda value: "exact-report" if value is report else None,
    )
    monkeypatch.setattr(
        tracking_run.watchlist,
        "atomic_write",
        lambda path, text: calls.append(("write", path, text)),
    )
    monkeypatch.setattr(
        tracking_run.signals,
        "advance_watermark",
        lambda *_args, **_kwargs: calls.append("advance"),
    )
    monkeypatch.setattr(tracking_run.alerts, "load_credentials", lambda: (None, None))

    tracking_run.run(_Connection(), signal_out=tmp_path / "swing.txt")

    assert calls == ["persist", ("write", tmp_path / "swing.txt", "exact-report"), "advance"]


@pytest.mark.parametrize(
    ("source", "fetched_at"),
    [
        (watchlist.SOURCE_CONSTITUENTS, dt(2026, 8, 29, tzinfo=UTC)),
        ("unapproved", dt(2026, 8, 28, tzinfo=UTC)),
    ],
)
def test_watchlist_rejects_newer_or_unapproved_mutable_constituents(source, fetched_at):
    conn = duckdb.connect()
    db.init_schema(conn)
    cutoff = date(2026, 8, 28)
    config = {
        "universe_index": "NIFTY 100",
        "benchmark": "NIFTY 50",
        "window_days": 3,
        "min_observations": 2,
        "max_price": 3000.0,
        "top_n": 1,
        "max_price_age_days": 3,
        "constituent_min_count": 1,
    }
    conn.execute(
        "INSERT INTO index_constituent VALUES ('NIFTY 100', 'SAFE', 'Safe', NULL, "
        "'INE000A01001', 'EQ', ?, ?)",
        [source, fetched_at],
    )
    conn.execute(
        "INSERT INTO stock_price VALUES ('SAFE', ?, NULL, NULL, NULL, 100, NULL, NULL, 'fx', ?)",
        [cutoff, dt(2026, 8, 28, tzinfo=UTC)],
    )

    with pytest.raises(ValueError, match="constituent evidence"):
        tracking_run._watchlist_candidates(conn, config)
    conn.close()


def test_real_v18_two_run_replay_artifact_alert_and_projection(tmp_path, monkeypatch):
    source = tmp_path / "source.duckdb"
    output = tmp_path / "ui.duckdb"
    artifact = tmp_path / "swing.txt"
    conn = db.connect(str(source))
    db.init_schema(conn)
    tracking.install_schema(conn)
    ranking.install_schema(conn)
    accounting.install_schema(conn)
    cutoff = date(2026, 8, 28)
    fetched = dt(2026, 8, 28, 12, tzinfo=UTC)
    swing_config = {
        "universe_index": "NIFTY 100",
        "benchmark": "NIFTY 50",
        "window_days": 20,
        "min_observations": 2,
        "max_price": 3000.0,
        "top_n": 1,
        "max_price_age_days": 3,
        "constituent_min_count": 1,
        "capital": 100000,
        "risk_fraction": 0.02,
        "stop_mode": "ema21",
        "initial_lookback_days": 5,
    }
    screen_config = {"screens": {"fixture": {"conditions": {"roe": {"gt": 0}}}}}
    conn.execute(
        "INSERT INTO index_constituent VALUES "
        "('NIFTY 100','SAFE','Safe Ltd','Fixture','INE000A01001','EQ',?,?)",
        [watchlist.SOURCE_CONSTITUENTS, fetched],
    )
    db.upsert_universe_row(
        conn,
        symbol="SAFE",
        company_name="Safe Ltd",
        series="EQ",
        isin="INE000A01001",
        is_active=True,
        source=universe.SOURCE,
        fetched_at=fetched,
    )
    db.upsert_stock_fundamental(
        conn,
        symbol="SAFE",
        as_of=cutoff,
        source=screens.COMPUTED_SOURCE,
        roe=0.2,
        methodology_version="fixture",
        fetched_at=fetched,
    )
    days = [date(2026, 8, day) for day in range(3, 29) if date(2026, 8, day).weekday() < 5]
    for number, day in enumerate(days, 1):
        conn.execute(
            "INSERT INTO stock_price VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                "SAFE",
                day,
                99 + number,
                101 + number,
                98 + number,
                100 + number,
                99 + number,
                1000,
                "fixture",
                fetched,
            ],
        )
        conn.execute(
            "INSERT INTO index_close VALUES (?,?,?,?,?)",
            ["NIFTY 50", day, 1000 + number * 10, watchlist.SOURCE_INDEX, fetched],
        )
    monkeypatch.setattr(tracking_run.watchlist, "load_config", lambda: swing_config)
    monkeypatch.setattr(tracking_run.screens, "load_config", lambda: screen_config)
    monkeypatch.setattr(ui_projection.watchlist, "load_config", lambda: swing_config)
    monkeypatch.setattr(ui_projection.screens, "load_config", lambda: screen_config)
    monkeypatch.setattr(tracking_run.alerts, "load_credentials", lambda: (None, "fixture-chat"))

    first = tracking_run.run(conn, signal_out=artifact)
    first_bytes = artifact.read_bytes()
    first_counts = {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "watchlist_run",
            "watchlist_symbol_result",
            "screen_evaluation_run",
            "screen_symbol_result",
            "screen_membership_event",
            "signal_run",
            "signal_event",
            "research_position",
            "research_alert_delivery",
        )
    }
    first_watermark = db.get_watermark(conn, "swing_signals")
    second = tracking_run.run(conn, signal_out=artifact)
    second_counts = {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in first_counts
    }
    assert first["signal_status"] == "ACCEPTED"
    assert second["signal_status"] == "REPLAY"
    assert second["queued"] == 0
    assert second_counts == first_counts
    assert artifact.read_bytes() == first_bytes
    assert db.get_watermark(conn, "swing_signals") == first_watermark == cutoff
    conn.close()

    ui_projection._publish(source, output)
    projected = duckdb.connect(str(output), read_only=True)
    assert (
        projected.execute("SELECT count(*) FROM signal_event").fetchone()[0]
        == first_counts["signal_event"]
    )
    assert "research_alert_delivery" not in {
        row[0] for row in projected.execute("SHOW TABLES").fetchall()
    }
    projected.close()
