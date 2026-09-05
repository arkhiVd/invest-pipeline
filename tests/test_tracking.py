import json
from datetime import UTC, date, timedelta
from datetime import datetime as dt

import pytest

from invest import db, kite, tracking

NOW = dt(2026, 8, 29, 12, tzinfo=UTC)
CONFIG = {
    "index": "NIFTY 100",
    "top_n": 20,
    "price_cap": 5000,
    "beta_window_days": 252,
    "beta_min_observations": 120,
    "ema_fast": 10,
    "ema_slow": 21,
    "stop_mode": "ema21",
    "risk_fraction": 0.02,
    "max_price_age_days": 3,
    "constituent_source": "official-fixture",
    "constituent_min_count": 1,
}


def migrated():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    tracking.install_schema(conn)
    tracking.register_methodology(conn, tracking.METHODOLOGY, CONFIG, registered_at=NOW)
    return conn


def constituent_snapshot(conn, cutoff, symbols=("SAFE",)):
    return tracking.persist_constituent_snapshot(
        conn,
        index_name="NIFTY 100",
        source_as_of=cutoff,
        fetched_at=NOW,
        source="official-fixture",
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        members=[
            {
                "symbol": symbol,
                "company_name": f"{symbol} Ltd",
                "industry": "Fixture",
                "isin": f"IN-{symbol}",
                "series": "EQ",
            }
            for symbol in symbols
        ],
    )


def watchlist(conn, cutoff=date(2026, 8, 28), fingerprint="watch-input"):
    snapshot_id = constituent_snapshot(conn, cutoff)
    run_id = tracking.record_watchlist_run_header(
        conn,
        index_name="NIFTY 100",
        source_as_of=cutoff,
        canonical_cutoff=cutoff,
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        input_fingerprint=fingerprint,
        constituent_snapshot_id=snapshot_id,
        universe_count=1,
        selected_count=1,
        recorded_at=NOW,
    )
    return run_id


def report(day=date(2026, 8, 28), close=100.0):
    return {
        "as_of": day,
        "since": date(2026, 8, 27),
        "first_run": False,
        "scanned": 20,
        "sizing_gaps": 0,
        "signals": [
            {
                "symbol": "SAFE",
                "action": "enter",
                "date": day,
                "close": close,
                "ema_fast": 101.0,
                "ema_slow": 99.0,
                "sizing": {
                    "quantity": 10,
                    "stop": 99.0,
                    "capital_to_deploy": 1000.0,
                    "maximum_loss_at_stop": 10.0,
                },
            }
        ],
    }


def test_v18_schema_is_explicit_not_init_schema():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 17
    assert "signal_event" not in {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
    tracking.install_schema(conn)
    assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 18
    tracking.install_schema(conn)
    assert (
        conn.execute("SELECT count(*) FROM schema_migrations WHERE version=18").fetchone()[0] == 1
    )
    conn.close()


def test_methodology_config_is_immutable():
    conn = migrated()
    assert (
        tracking.register_methodology(conn, tracking.METHODOLOGY, CONFIG, registered_at=NOW)
        == "replay"
    )
    changed = {**CONFIG, "top_n": 21}
    with pytest.raises(tracking.TrackingConflict, match="semantic config"):
        tracking.register_methodology(conn, tracking.METHODOLOGY, changed, registered_at=NOW)
    conn.close()


def test_signal_run_persists_generated_fact_and_exact_replay_is_noop():
    conn = migrated()
    watchlist_run_id = watchlist(conn)
    first = tracking.persist_signal_run(
        conn,
        report(),
        watchlist_run_id=watchlist_run_id,
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        recorded_at=NOW,
    )
    second = tracking.persist_signal_run(
        conn,
        report(),
        watchlist_run_id=watchlist_run_id,
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        recorded_at=NOW,
    )
    assert first["status"] == "ACCEPTED"
    assert second == {**first, "status": "REPLAY"}
    assert conn.execute("SELECT count(*) FROM signal_run").fetchone()[0] == 1
    row = conn.execute(
        "SELECT action,quantity,sizing_stop,methodology_version FROM signal_event"
    ).fetchone()
    assert row == ("ENTER", 10, 99.0, tracking.METHODOLOGY)
    position = conn.execute(
        "SELECT current_state,entry_sizing_stop,methodology_version FROM research_position"
    ).fetchone()
    assert position == ("SIGNALLED", 99.0, tracking.METHODOLOGY)
    assert conn.execute("SELECT count(*) FROM position_state_event").fetchone()[0] == 1
    conn.close()


def test_same_accepted_key_changed_content_records_conflict_without_events():
    conn = migrated()
    watchlist_run_id = watchlist(conn)
    tracking.persist_signal_run(
        conn,
        report(),
        watchlist_run_id=watchlist_run_id,
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        recorded_at=NOW,
    )
    result = tracking.persist_signal_run(
        conn,
        report(close=101.0),
        watchlist_run_id=watchlist_run_id,
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        recorded_at=NOW,
    )
    assert result["status"] == "CONFLICT"
    replayed_conflict = tracking.persist_signal_run(
        conn,
        report(close=101.0),
        watchlist_run_id=watchlist_run_id,
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        recorded_at=NOW,
    )
    assert replayed_conflict == result
    assert (
        conn.execute("SELECT count(*) FROM signal_run WHERE status='CONFLICT'").fetchone()[0] == 1
    )
    assert conn.execute("SELECT count(*) FROM signal_event").fetchone()[0] == 1
    conn.close()


def test_out_of_order_run_is_retained_but_cannot_emit_event():
    conn = migrated()
    older_day = date(2026, 8, 27)
    older_watchlist = watchlist(conn, older_day, "older-input")
    newer_watchlist = watchlist(conn)
    tracking.persist_signal_run(
        conn,
        report(),
        watchlist_run_id=newer_watchlist,
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        recorded_at=NOW,
    )
    result = tracking.persist_signal_run(
        conn,
        report(older_day),
        watchlist_run_id=older_watchlist,
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        recorded_at=NOW,
    )
    assert result["status"] == "REJECTED_OUT_OF_ORDER"
    assert conn.execute("SELECT count(*) FROM signal_event").fetchone()[0] == 1
    conn.close()


def test_older_watchlist_header_is_rejected_and_cannot_link_signal():
    conn = migrated()
    watchlist(conn)
    older = watchlist(conn, date(2026, 8, 27), "older-input")
    assert (
        conn.execute("SELECT status FROM watchlist_run WHERE run_id=?", [older]).fetchone()[0]
        == "REJECTED_OUT_OF_ORDER"
    )
    assert watchlist(conn, date(2026, 8, 27), "older-input") == older
    assert (
        conn.execute("SELECT count(*) FROM watchlist_run WHERE run_id=?", [older]).fetchone()[0]
        == 1
    )
    with pytest.raises(ValueError, match="accepted watchlist"):
        tracking.persist_signal_run(
            conn,
            report(date(2026, 8, 27)),
            watchlist_run_id=older,
            methodology_version=tracking.METHODOLOGY,
            semantic_config=CONFIG,
            recorded_at=NOW,
        )
    conn.close()


def test_watchlist_index_must_match_registered_semantics():
    conn = migrated()
    with pytest.raises(tracking.TrackingConflict, match="index"):
        tracking.record_watchlist_run_header(
            conn,
            index_name="NIFTY 50",
            source_as_of=date(2026, 8, 28),
            canonical_cutoff=date(2026, 8, 28),
            methodology_version=tracking.METHODOLOGY,
            semantic_config=CONFIG,
            input_fingerprint="other-index",
            constituent_snapshot_id="constituents-2",
            universe_count=50,
            selected_count=20,
            recorded_at=NOW,
        )
    conn.close()


def test_cutoff_mismatch_or_future_signal_fails_atomically():
    conn = migrated()
    watchlist_run_id = watchlist(conn)
    bad = report()
    bad["as_of"] = date(2026, 8, 27)
    with pytest.raises(ValueError, match="cutoff"):
        tracking.persist_signal_run(
            conn,
            bad,
            watchlist_run_id=watchlist_run_id,
            methodology_version=tracking.METHODOLOGY,
            semantic_config=CONFIG,
            recorded_at=NOW,
        )
    future = report()
    future["signals"][0]["date"] = date(2026, 8, 29)
    with pytest.raises(ValueError, match="after canonical cutoff"):
        tracking.persist_signal_run(
            conn,
            future,
            watchlist_run_id=watchlist_run_id,
            methodology_version=tracking.METHODOLOGY,
            semantic_config=CONFIG,
            recorded_at=NOW,
        )
    assert conn.execute("SELECT count(*) FROM signal_run").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM signal_event").fetchone()[0] == 0
    conn.close()


def test_detail_change_is_conflict_and_incomplete_or_duplicate_signal_is_rejected():
    conn = migrated()
    watchlist_run_id = watchlist(conn)
    tracking.persist_signal_run(
        conn,
        report(),
        watchlist_run_id=watchlist_run_id,
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        recorded_at=NOW,
    )
    changed = report()
    changed["sizing_gaps"] = 1
    assert (
        tracking.persist_signal_run(
            conn,
            changed,
            watchlist_run_id=watchlist_run_id,
            methodology_version=tracking.METHODOLOGY,
            semantic_config=CONFIG,
            recorded_at=NOW,
        )["status"]
        == "CONFLICT"
    )
    duplicate = report()
    duplicate["signals"].append(dict(duplicate["signals"][0]))
    with pytest.raises(ValueError, match="duplicate natural key"):
        tracking.persist_signal_run(
            conn,
            duplicate,
            watchlist_run_id=watchlist_run_id,
            methodology_version=tracking.METHODOLOGY,
            semantic_config=CONFIG,
            recorded_at=NOW,
        )
    incomplete = report()
    incomplete["signals"][0]["ema_fast"] = None
    with pytest.raises(ValueError, match="EMA values"):
        tracking.persist_signal_run(
            conn,
            incomplete,
            watchlist_run_id=watchlist_run_id,
            methodology_version=tracking.METHODOLOGY,
            semantic_config=CONFIG,
            recorded_at=NOW,
        )
    conn.close()


def test_operator_controls_legal_position_lifecycle_and_exact_replay():
    conn = migrated()
    tracking.persist_signal_run(
        conn,
        report(),
        watchlist_run_id=watchlist(conn),
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        recorded_at=NOW,
    )
    position_id = conn.execute("SELECT position_id FROM research_position").fetchone()[0]
    watching_at = dt(2026, 8, 29, 13, tzinfo=UTC)
    watching = tracking.transition_position(
        conn,
        position_id=position_id,
        to_state="WATCHING",
        source_at=watching_at,
        recorded_at=watching_at,
        methodology_version=tracking.METHODOLOGY,
    )
    replay = tracking.transition_position(
        conn,
        position_id=position_id,
        to_state="WATCHING",
        source_at=watching_at,
        recorded_at=dt(2026, 8, 29, 14, tzinfo=UTC),
        methodology_version=tracking.METHODOLOGY,
    )
    assert replay == {**watching, "status": "REPLAY"}
    opened_at = dt(2026, 8, 29, 15, tzinfo=UTC)
    tracking.transition_position(
        conn,
        position_id=position_id,
        to_state="OPEN_CONFIRMED",
        source_at=opened_at,
        recorded_at=opened_at,
        methodology_version=tracking.METHODOLOGY,
        operator_note="Confirmed from operator records",
    )
    closed_at = dt(2026, 8, 30, 12, tzinfo=UTC)
    tracking.transition_position(
        conn,
        position_id=position_id,
        to_state="CLOSED_CONFIRMED",
        source_at=closed_at,
        recorded_at=closed_at,
        methodology_version=tracking.METHODOLOGY,
        operator_note="Operator confirmed closure",
    )
    assert (
        conn.execute(
            "SELECT current_state FROM research_position WHERE position_id=?", [position_id]
        ).fetchone()[0]
        == "CLOSED_CONFIRMED"
    )
    assert conn.execute(
        "SELECT actor,to_state FROM position_state_event ORDER BY source_at"
    ).fetchall() == [
        ("SYSTEM_SIGNAL", "SIGNALLED"),
        ("OPERATOR", "WATCHING"),
        ("OPERATOR", "OPEN_CONFIRMED"),
        ("OPERATOR", "CLOSED_CONFIRMED"),
    ]
    conn.close()


def test_position_transition_rejects_illegal_stale_and_methodology_mismatch():
    conn = migrated()
    tracking.persist_signal_run(
        conn,
        report(),
        watchlist_run_id=watchlist(conn),
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        recorded_at=NOW,
    )
    position_id = conn.execute("SELECT position_id FROM research_position").fetchone()[0]
    source_at = dt(2026, 8, 29, 13, tzinfo=UTC)
    with pytest.raises(ValueError, match="illegal"):
        tracking.transition_position(
            conn,
            position_id=position_id,
            to_state="CLOSED_CONFIRMED",
            source_at=source_at,
            recorded_at=source_at,
            methodology_version=tracking.METHODOLOGY,
            operator_note="Not opened",
        )
    with pytest.raises(ValueError, match="non-empty"):
        tracking.transition_position(
            conn,
            position_id=position_id,
            to_state="OPEN_CONFIRMED",
            source_at=source_at,
            recorded_at=source_at,
            methodology_version=tracking.METHODOLOGY,
        )
    with pytest.raises(ValueError, match="newer"):
        tracking.transition_position(
            conn,
            position_id=position_id,
            to_state="WATCHING",
            source_at=dt(2026, 8, 28, tzinfo=UTC),
            recorded_at=source_at,
            methodology_version=tracking.METHODOLOGY,
        )
    with pytest.raises(tracking.TrackingConflict, match="methodology"):
        tracking.transition_position(
            conn,
            position_id=position_id,
            to_state="WATCHING",
            source_at=source_at,
            recorded_at=source_at,
            methodology_version="tracking-other",
        )
    assert conn.execute("SELECT count(*) FROM position_state_event").fetchone()[0] == 1
    conn.close()


def test_later_enter_does_not_duplicate_an_active_lifecycle():
    conn = migrated()
    tracking.persist_signal_run(
        conn,
        report(),
        watchlist_run_id=watchlist(conn),
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        recorded_at=NOW,
    )
    second_day = date(2026, 8, 30)
    tracking.persist_signal_run(
        conn,
        report(second_day),
        watchlist_run_id=watchlist(conn, second_day, "watch-input-2"),
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        recorded_at=dt(2026, 8, 30, 13, tzinfo=UTC),
    )
    assert conn.execute("SELECT count(*) FROM signal_event").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM research_position").fetchone()[0] == 1
    conn.close()


def test_terminal_lifecycle_allows_later_enter():
    conn = migrated()
    first_watchlist = watchlist(conn)
    tracking.persist_signal_run(
        conn,
        report(),
        watchlist_run_id=first_watchlist,
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        recorded_at=NOW,
    )
    first_position = conn.execute("SELECT position_id FROM research_position").fetchone()[0]
    ignored_at = dt(2026, 8, 29, 13, tzinfo=UTC)
    tracking.transition_position(
        conn,
        position_id=first_position,
        to_state="IGNORED",
        source_at=ignored_at,
        recorded_at=ignored_at,
        methodology_version=tracking.METHODOLOGY,
    )
    second_day = date(2026, 8, 30)
    tracking.persist_signal_run(
        conn,
        report(second_day),
        watchlist_run_id=watchlist(conn, second_day, "watch-input-2"),
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        recorded_at=dt(2026, 8, 30, 13, tzinfo=UTC),
    )
    assert conn.execute("SELECT count(*) FROM research_position").fetchone()[0] == 2
    assert (
        conn.execute(
            "SELECT count(*) FROM research_position WHERE current_state='SIGNALLED'"
        ).fetchone()[0]
        == 1
    )
    positions = conn.execute(
        "SELECT position_id,state_event_id FROM research_position ORDER BY created_at"
    ).fetchall()
    with pytest.raises(Exception, match="foreign key"):
        conn.execute(
            "INSERT INTO position_state_event_link VALUES (?,?)",
            [positions[1][0], positions[0][1]],
        )
    conn.close()


def test_delayed_enter_cannot_backdate_a_lifecycle_after_terminal_state():
    conn = migrated()
    tracking.persist_signal_run(
        conn,
        report(),
        watchlist_run_id=watchlist(conn),
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        recorded_at=NOW,
    )
    position_id = conn.execute("SELECT position_id FROM research_position").fetchone()[0]
    expired_at = dt(2026, 8, 29, 13, tzinfo=UTC)
    tracking.transition_position(
        conn,
        position_id=position_id,
        to_state="EXPIRED",
        source_at=expired_at,
        recorded_at=expired_at,
        methodology_version=tracking.METHODOLOGY,
    )
    delayed = report(date(2026, 8, 30))
    delayed["signals"][0]["date"] = date(2026, 8, 29)
    tracking.persist_signal_run(
        conn,
        delayed,
        watchlist_run_id=watchlist(conn, date(2026, 8, 30), "watch-input-2"),
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        recorded_at=dt(2026, 8, 30, 13, tzinfo=UTC),
    )
    assert conn.execute("SELECT count(*) FROM signal_event").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM research_position").fetchone()[0] == 1
    conn.close()


def test_schema_enforces_active_slot_and_state_event_actor_boundaries():
    conn = migrated()
    tracking.persist_signal_run(
        conn,
        report(),
        watchlist_run_id=watchlist(conn),
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        recorded_at=NOW,
    )
    with pytest.raises(Exception, match="CHECK constraint"):
        conn.execute("UPDATE research_position SET active_slot=NULL")
    position_id = conn.execute("SELECT position_id FROM research_position").fetchone()[0]
    with pytest.raises(Exception, match="foreign key"):
        conn.execute("UPDATE research_position SET state_event_id='missing-event'")
    with pytest.raises(Exception, match="CHECK constraint"):
        conn.execute(
            "INSERT INTO position_state_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                "invalid-actor-event",
                position_id,
                None,
                "OPEN_CONFIRMED",
                NOW,
                NOW,
                tracking.METHODOLOGY,
                "OPERATOR",
                None,
                "invalid-command",
                "OPERATOR_COMMAND",
                "invalid-command",
                None,
            ],
        )
    assert conn.execute("SELECT count(*) FROM position_state_event").fetchone()[0] == 1
    conn.close()


def test_signal_state_recorded_at_requires_explicit_utc():
    conn = migrated()
    with pytest.raises(ValueError, match="explicit UTC"):
        tracking.persist_signal_run(
            conn,
            report(),
            watchlist_run_id=watchlist(conn),
            methodology_version=tracking.METHODOLOGY,
            semantic_config=CONFIG,
            recorded_at=dt(2026, 8, 29, 12),
        )
    assert conn.execute("SELECT count(*) FROM signal_run").fetchone()[0] == 0
    conn.close()


def watch_candidate(
    symbol,
    *,
    rank=1,
    close=100.0,
    beta=1.0,
    observations=150,
    day=None,
    rank_day=None,
    beta_day=None,
):
    return {
        "symbol": symbol,
        "rank": rank,
        "close": close,
        "beta": beta,
        "observations": observations,
        "close_as_of": day,
        "rank_as_of": rank_day if rank_day is not None else day,
        "beta_as_of": beta_day if beta_day is not None else day,
    }


def tracked_watchlist(conn, day, symbols, candidates):
    snapshot_id = constituent_snapshot(conn, day, tuple(symbols))
    return tracking.persist_watchlist_run(
        conn,
        index_name="NIFTY 100",
        source_as_of=day,
        canonical_cutoff=day,
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        constituent_snapshot_id=snapshot_id,
        candidates=candidates,
        recorded_at=NOW,
    )


def test_constituent_snapshot_is_validated_immutable_and_replay_safe():
    conn = migrated()
    day = date(2026, 8, 28)
    first = constituent_snapshot(conn, day, ("AAA", "BBB"))
    assert constituent_snapshot(conn, day, ("BBB", "AAA")) == first
    assert conn.execute("SELECT count(*) FROM index_constituent_snapshot_member").fetchone()[0] == 2
    with pytest.raises(ValueError, match="approved source"):
        tracking.persist_constituent_snapshot(
            conn,
            index_name="NIFTY 100",
            source_as_of=day,
            fetched_at=NOW,
            source="unapproved",
            methodology_version=tracking.METHODOLOGY,
            semantic_config=CONFIG,
            members=[
                {
                    "symbol": "AAA",
                    "company_name": "AAA Ltd",
                    "isin": "IN1",
                    "series": "EQ",
                }
            ],
        )
    with pytest.raises(ValueError, match="partial or contains duplicate"):
        constituent_snapshot(conn, day, ("AAA", "AAA"))
    other_method = "tracking-other-source"
    other_config = {**CONFIG, "constituent_source": "different-official-source"}
    tracking.register_methodology(conn, other_method, other_config, registered_at=NOW)
    with pytest.raises(ValueError, match="policy-matching"):
        tracking.record_watchlist_run_header(
            conn,
            index_name="NIFTY 100",
            source_as_of=day,
            canonical_cutoff=day,
            methodology_version=other_method,
            semantic_config=other_config,
            input_fingerprint="cross-policy",
            constituent_snapshot_id=first,
            universe_count=2,
            selected_count=1,
            recorded_at=NOW,
        )
    conn.close()


def test_watchlist_results_persist_every_reason_and_constituent_removal():
    conn = migrated()
    first_day = date(2026, 8, 28)
    first_candidates = [
        watch_candidate("CONT", rank=1, day=first_day),
        watch_candidate("REMOVED", rank=2, day=first_day),
    ]
    first = tracked_watchlist(conn, first_day, ["CONT", "REMOVED"], first_candidates)
    assert first["status"] == "ACCEPTED"

    day = date(2026, 8, 29)
    candidates = [
        watch_candidate("CONT", rank=1, day=day),
        watch_candidate("ADMIT", rank=2, day=day),
        watch_candidate("DROP", rank=21, day=day),
        watch_candidate("PRICE", rank=3, close=6000.0, day=day),
        watch_candidate("NOCLOSE", rank=None, close=None, beta=None, observations=None, day=None),
        watch_candidate("STALE", rank=4, day=date(2026, 8, 20)),
        watch_candidate("THIN", rank=5, beta=None, observations=10, day=day),
    ]
    result = tracked_watchlist(
        conn,
        day,
        ["CONT", "ADMIT", "DROP", "PRICE", "NOCLOSE", "STALE", "THIN"],
        candidates,
    )
    run_id = result["run_id"]
    assert result == {"run_id": run_id, "status": "ACCEPTED", "result_count": 8}
    assert dict(
        conn.execute(
            "SELECT symbol,result FROM watchlist_symbol_result WHERE run_id=?", [run_id]
        ).fetchall()
    ) == {
        "CONT": "CONTINUED",
        "ADMIT": "ADMITTED",
        "DROP": "DROPPED_BELOW_TOP_N",
        "PRICE": "PRICE_ABOVE_CAP",
        "NOCLOSE": "NO_CLOSE",
        "STALE": "STALE_CLOSE",
        "THIN": "INSUFFICIENT_BETA",
        "REMOVED": "CONSTITUENT_REMOVED",
    }
    assert (
        tracked_watchlist(
            conn,
            day,
            ["CONT", "ADMIT", "DROP", "PRICE", "NOCLOSE", "STALE", "THIN"],
            candidates,
        )["status"]
        == "REPLAY"
    )
    result_ids = conn.execute(
        "SELECT result_id FROM watchlist_symbol_result WHERE run_id=?", [run_id]
    ).fetchall()
    assert len(result_ids) == len(set(result_ids)) == 8
    assert tracking.enqueue_change_alerts(conn, destination="fixture-chat", recorded_at=NOW) == 4
    queued_subjects = {
        row[0]
        for row in conn.execute(
            "SELECT subject_id FROM research_alert_delivery WHERE subject_type='WATCHLIST_SYMBOL'"
        ).fetchall()
    }
    assert queued_subjects == {
        "NIFTY 100:CONT",
        "NIFTY 100:REMOVED",
        "NIFTY 100:ADMIT",
    }
    conn.close()


def test_watchlist_results_reject_incomplete_snapshot_or_changed_replay():
    conn = migrated()
    day = date(2026, 8, 28)
    candidates = [watch_candidate("AAA", day=day), watch_candidate("BBB", rank=2, day=day)]
    snapshot_id = constituent_snapshot(conn, day, ("AAA", "BBB"))
    run_id = tracking.record_watchlist_run_header(
        conn,
        index_name="NIFTY 100",
        source_as_of=day,
        canonical_cutoff=day,
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        input_fingerprint=tracking.watchlist_input_fingerprint(candidates),
        constituent_snapshot_id=snapshot_id,
        universe_count=2,
        selected_count=2,
        recorded_at=NOW,
    )
    with pytest.raises(tracking.TrackingConflict, match="fingerprint"):
        tracking.persist_watchlist_results(conn, run_id=run_id, candidates=candidates[:1])
    tracking.persist_watchlist_results(conn, run_id=run_id, candidates=candidates)
    changed = [dict(candidates[0]), {**candidates[1], "close": 101.0}]
    with pytest.raises(tracking.TrackingConflict, match="fingerprint"):
        tracking.persist_watchlist_results(conn, run_id=run_id, candidates=changed)
    assert (
        conn.execute(
            "SELECT count(*) FROM watchlist_symbol_result WHERE run_id=?", [run_id]
        ).fetchone()[0]
        == 2
    )
    conn.close()


def test_atomic_watchlist_run_rolls_back_header_and_results(monkeypatch):
    conn = migrated()
    day = date(2026, 8, 28)
    candidates = [watch_candidate("AAA", day=day), watch_candidate("BBB", rank=2, day=day)]
    snapshot_id = constituent_snapshot(conn, day, ("AAA", "BBB"))
    monkeypatch.setattr(tracking, "_event_id", lambda *_parts: "duplicate-result-id")
    with pytest.raises(Exception, match="(duplicate|Duplicate|unique|Unique)"):
        tracking.persist_watchlist_run(
            conn,
            index_name="NIFTY 100",
            source_as_of=day,
            canonical_cutoff=day,
            methodology_version=tracking.METHODOLOGY,
            semantic_config=CONFIG,
            constituent_snapshot_id=snapshot_id,
            candidates=candidates,
            recorded_at=NOW,
        )
    assert conn.execute("SELECT count(*) FROM watchlist_run").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM watchlist_symbol_result").fetchone()[0] == 0
    conn.close()


def test_incomplete_prior_watchlist_header_blocks_new_classification():
    conn = migrated()
    first_day = date(2026, 8, 28)
    first = [watch_candidate("AAA", day=first_day)]
    snapshot_id = constituent_snapshot(conn, first_day, ("AAA",))
    tracking.record_watchlist_run_header(
        conn,
        index_name="NIFTY 100",
        source_as_of=first_day,
        canonical_cutoff=first_day,
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        input_fingerprint=tracking.watchlist_input_fingerprint(first),
        constituent_snapshot_id=snapshot_id,
        universe_count=1,
        selected_count=1,
        recorded_at=NOW,
    )
    second_day = date(2026, 8, 29)
    with pytest.raises(tracking.TrackingConflict, match="incomplete results"):
        tracked_watchlist(
            conn,
            second_day,
            ["AAA"],
            [watch_candidate("AAA", day=second_day)],
        )
    assert conn.execute("SELECT count(*) FROM watchlist_run").fetchone()[0] == 1
    conn.close()


def screen_result(symbol="SAFE", outcome="PASS", **overrides):
    fields = {
        "symbol": symbol,
        "outcome": outcome,
        "failed_predicates": [],
        "missing_fields": [],
        "stale_fields": [],
        "evidence_as_of": date(2026, 8, 28),
        "source": "fixture",
        "metrics": {"pe": 10.0},
    }
    fields.update(overrides)
    return fields


def test_screen_membership_distinguishes_predicate_missing_and_stale_exits():
    conn = migrated()
    tracking.persist_screen_evaluation(
        conn,
        screen_id="garp",
        source_as_of=date(2026, 8, 28),
        canonical_cutoff=date(2026, 8, 28),
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        expected_symbols={"PRED", "MISS", "STALE", "KEEP"},
        results=[
            screen_result("PRED"),
            screen_result("MISS"),
            screen_result("STALE"),
            screen_result("KEEP"),
        ],
        recorded_at=NOW,
    )
    second_day = date(2026, 8, 29)
    second = [
        screen_result(
            "PRED",
            "PREDICATE_FAIL",
            failed_predicates=["pe"],
            evidence_as_of=second_day,
        ),
        screen_result("MISS", "MISSING_DATA", missing_fields=["roe"], evidence_as_of=second_day),
        screen_result(
            "STALE", "STALE_DATA", stale_fields=["fundamentals"], evidence_as_of=second_day
        ),
        screen_result("KEEP", evidence_as_of=second_day),
    ]
    tracking.persist_screen_evaluation(
        conn,
        screen_id="garp",
        source_as_of=second_day,
        canonical_cutoff=second_day,
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        expected_symbols={"PRED", "MISS", "STALE", "KEEP"},
        results=second,
        recorded_at=dt(2026, 8, 29, 14, tzinfo=UTC),
    )
    events = dict(
        conn.execute(
            "SELECT symbol,event_type FROM screen_membership_event WHERE source_as_of=?",
            [second_day],
        ).fetchall()
    )
    assert events == {
        "PRED": "EXITED_PREDICATE",
        "MISS": "EXITED_MISSING_DATA",
        "STALE": "EXITED_STALE_DATA",
        "KEEP": "CONTINUED",
    }
    conn.close()


def test_screen_evidence_precedence_rejects_mislabelled_missing_or_stale():
    conn = migrated()
    for result in (
        screen_result("BAD", "PREDICATE_FAIL", missing_fields=["roe"]),
        screen_result("BAD", "MISSING_DATA", stale_fields=["price"]),
    ):
        with pytest.raises(ValueError, match="precedence"):
            tracking.persist_screen_evaluation(
                conn,
                screen_id="garp",
                source_as_of=date(2026, 8, 28),
                canonical_cutoff=date(2026, 8, 28),
                methodology_version=tracking.METHODOLOGY,
                semantic_config=CONFIG,
                expected_symbols={"BAD"},
                results=[result],
                recorded_at=NOW,
            )
    assert conn.execute("SELECT count(*) FROM screen_evaluation_run").fetchone()[0] == 0
    conn.close()


def test_screen_run_rejects_an_incomplete_evaluated_universe():
    conn = migrated()
    with pytest.raises(ValueError, match="incomplete"):
        tracking.persist_screen_evaluation(
            conn,
            screen_id="garp",
            source_as_of=date(2026, 8, 28),
            canonical_cutoff=date(2026, 8, 28),
            methodology_version=tracking.METHODOLOGY,
            semantic_config=CONFIG,
            expected_symbols={"SAFE", "OMITTED"},
            results=[screen_result("SAFE")],
            recorded_at=NOW,
        )
    assert conn.execute("SELECT count(*) FROM screen_evaluation_run").fetchone()[0] == 0
    conn.close()


def test_screen_methodology_change_emits_reset_not_organic_events():
    conn = migrated()
    tracking.persist_screen_evaluation(
        conn,
        screen_id="garp",
        source_as_of=date(2026, 8, 28),
        canonical_cutoff=date(2026, 8, 28),
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        expected_symbols={"OLD", "BOTH"},
        results=[screen_result("OLD"), screen_result("BOTH")],
        recorded_at=NOW,
    )
    new_method = "tracking-2026.2"
    tracking.register_methodology(conn, new_method, CONFIG, registered_at=NOW)
    day = date(2026, 8, 27)  # methodology backfill must still reset, never emit ENTERED
    result = tracking.persist_screen_evaluation(
        conn,
        screen_id="garp",
        source_as_of=day,
        canonical_cutoff=day,
        methodology_version=new_method,
        semantic_config=CONFIG,
        expected_symbols={"BOTH", "NEW"},
        results=[
            screen_result("BOTH", evidence_as_of=day),
            screen_result("NEW", evidence_as_of=day),
        ],
        recorded_at=dt(2026, 8, 29, 14, tzinfo=UTC),
    )
    assert result["status"] == "ACCEPTED"
    assert conn.execute(
        "SELECT symbol,event_type FROM screen_membership_event WHERE run_id=? ORDER BY symbol",
        [result["run_id"]],
    ).fetchall() == [
        ("BOTH", "METHODOLOGY_RESET"),
        ("NEW", "METHODOLOGY_RESET"),
        ("OLD", "METHODOLOGY_RESET"),
    ]
    conn.close()


def test_screen_replay_conflict_and_out_of_order_emit_no_duplicate_events():
    conn = migrated()
    day = date(2026, 8, 28)
    args = dict(
        screen_id="garp",
        source_as_of=day,
        canonical_cutoff=day,
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        expected_symbols={"SAFE"},
        results=[screen_result()],
        recorded_at=NOW,
    )
    first = tracking.persist_screen_evaluation(conn, **args)
    assert tracking.persist_screen_evaluation(conn, **args)["status"] == "REPLAY"
    changed = {**args, "results": [screen_result(metrics={"pe": 11.0})]}
    with pytest.raises(tracking.TrackingConflict, match="natural key"):
        tracking.persist_screen_evaluation(conn, **changed)
    older_day = date(2026, 8, 27)
    older = {
        **args,
        "source_as_of": older_day,
        "canonical_cutoff": older_day,
        "results": [screen_result(evidence_as_of=older_day)],
    }
    assert tracking.persist_screen_evaluation(conn, **older)["status"] == "REJECTED_OUT_OF_ORDER"
    assert conn.execute("SELECT count(*) FROM screen_symbol_result").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM screen_membership_event").fetchone()[0] == 1
    assert first["status"] == "ACCEPTED"
    conn.close()


def test_production_path_is_refused_without_t9_7_gate(tmp_path, monkeypatch):
    path = tmp_path / "production.duckdb"
    conn = db.connect(str(path))
    db.init_schema(conn)
    monkeypatch.setattr(tracking, "PRODUCTION_DB", path)
    with pytest.raises(PermissionError, match="T9.7"):
        tracking.install_schema(conn)
    assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 17
    conn.close()


def test_signal_fingerprint_is_stable_under_mapping_order():
    reordered = json.loads(json.dumps(report(), default=str, sort_keys=True))
    reordered["as_of"] = "2026-08-28"
    reordered["since"] = "2026-08-27"
    assert tracking.fingerprint(report()) == tracking.fingerprint(reordered)


def open_position(conn):
    tracking.persist_signal_run(
        conn,
        report(),
        watchlist_run_id=watchlist(conn),
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        recorded_at=NOW,
    )
    position_id = conn.execute("SELECT position_id FROM research_position").fetchone()[0]
    opened_at = dt(2026, 8, 29, 15, tzinfo=UTC)
    tracking.transition_position(
        conn,
        position_id=position_id,
        to_state="OPEN_CONFIRMED",
        source_at=opened_at,
        recorded_at=opened_at,
        methodology_version=tracking.METHODOLOGY,
        operator_note="Operator confirmed open",
    )
    return position_id, opened_at


def broker_snapshot(conn, holdings, fetched_at=dt(2026, 8, 30, 3, tzinfo=UTC)):
    return kite.store_snapshot(
        conn,
        {"user_id": "fixture-account"},
        holdings,
        {"net": [], "day": []},
        [],
        fetched_at=fetched_at,
    )["run_id"]


def broker_holding(symbol, exchange="NSE", quantity=1):
    return {
        "exchange": exchange,
        "tradingsymbol": symbol,
        "product": "CNC",
        "instrument_token": 1,
        "isin": "INE000A00001",
        "quantity": quantity,
        "t1_quantity": 0,
        "used_quantity": 0,
        "average_price": 100,
        "last_price": 101,
        "close_price": 100,
        "pnl": 1,
        "day_change": 1,
        "day_change_percentage": 1,
    }


def alert_fixture(conn):
    first_day = date(2026, 8, 28)
    second_day = date(2026, 8, 29)
    tracking.persist_screen_evaluation(
        conn,
        screen_id="garp",
        source_as_of=first_day,
        canonical_cutoff=first_day,
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        expected_symbols={"SAFE"},
        results=[screen_result("SAFE")],
        recorded_at=NOW,
    )
    tracking.persist_screen_evaluation(
        conn,
        screen_id="garp",
        source_as_of=second_day,
        canonical_cutoff=second_day,
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        expected_symbols={"SAFE"},
        results=[
            screen_result(
                "SAFE",
                "PREDICATE_FAIL",
                failed_predicates=["pe"],
                evidence_as_of=second_day,
            )
        ],
        recorded_at=NOW,
    )
    assert tracking.enqueue_change_alerts(conn, destination="fixture-chat", recorded_at=NOW) == 1
    assert tracking.enqueue_change_alerts(conn, destination="fixture-chat", recorded_at=NOW) == 0
    return conn.execute("SELECT fingerprint FROM research_alert_delivery").fetchone()[0]


def test_change_alert_replay_claim_and_success_are_fenced():
    conn = migrated()
    alert_fp = alert_fixture(conn)
    claim = tracking.claim_alert(conn, fingerprint_value=alert_fp, claimed_at=NOW, lease_seconds=30)
    assert claim is not None
    assert tracking.claim_alert(conn, fingerprint_value=alert_fp, claimed_at=NOW) is None
    sent = []
    outcome = tracking.deliver_claimed_alert(
        conn,
        fingerprint_value=alert_fp,
        generation=claim["generation"],
        token=claim["token"],
        sender=lambda destination, message: sent.append((destination, message)),
        attempted_at=NOW,
    )
    assert outcome == "SENT"
    assert len(sent) == 1
    assert conn.execute(
        "SELECT status,attempts,sent_at IS NOT NULL FROM research_alert_delivery"
    ).fetchone() == ("SENT", 1, True)
    assert tracking.claim_alert(conn, fingerprint_value=alert_fp, claimed_at=NOW) is None
    conn.close()


def test_before_send_failure_retries_but_ambiguous_failure_does_not():
    conn = migrated()
    alert_fp = alert_fixture(conn)
    first = tracking.claim_alert(conn, fingerprint_value=alert_fp, claimed_at=NOW)

    def before_send(_destination, _message):
        raise tracking.DeliveryBeforeSend("socket not opened")

    assert (
        tracking.deliver_claimed_alert(
            conn,
            fingerprint_value=alert_fp,
            generation=first["generation"],
            token=first["token"],
            sender=before_send,
            attempted_at=NOW,
        )
        == "FAILED_BEFORE_SEND"
    )
    second = tracking.claim_alert(conn, fingerprint_value=alert_fp, claimed_at=NOW)
    assert second["generation"] > first["generation"]

    def ambiguous(_destination, _message):
        raise TimeoutError("response timed out")

    assert (
        tracking.deliver_claimed_alert(
            conn,
            fingerprint_value=alert_fp,
            generation=second["generation"],
            token=second["token"],
            sender=ambiguous,
            attempted_at=NOW,
        )
        == "UNKNOWN_AFTER_SEND"
    )
    assert tracking.claim_alert(conn, fingerprint_value=alert_fp, claimed_at=NOW) is None
    assert tracking.resolve_unknown_alert(
        conn,
        fingerprint_value=alert_fp,
        resolution="REQUEUE",
        resolved_at=NOW,
    )
    stale_sender_calls = []
    assert (
        tracking.deliver_claimed_alert(
            conn,
            fingerprint_value=alert_fp,
            generation=second["generation"],
            token=second["token"],
            sender=lambda destination, message: stale_sender_calls.append(message),
            attempted_at=NOW,
        )
        == "LOST_FENCE"
    )
    assert stale_sender_calls == []
    third = tracking.claim_alert(conn, fingerprint_value=alert_fp, claimed_at=NOW)
    assert third["generation"] > second["generation"]
    conn.close()


def test_telegram_dispatch_uses_destination_and_marks_only_after_response():
    conn = migrated()
    alert_fp = alert_fixture(conn)
    requests = []
    assert (
        tracking.dispatch_telegram_alert(
            conn,
            fingerprint_value=alert_fp,
            bot_token="fixture-token",
            attempted_at=NOW,
            poster=lambda request: requests.append(request),
        )
        == "SENT"
    )
    assert len(requests) == 1
    payload = json.loads(requests[0].data)
    assert payload["chat_id"] == "fixture-chat"
    assert "EXITED_PREDICATE" in payload["text"]
    assert conn.execute("SELECT status FROM research_alert_delivery").fetchone() == ("SENT",)
    conn.close()


def test_expired_claim_becomes_unknown_and_old_worker_loses_fence():
    conn = migrated()
    alert_fp = alert_fixture(conn)
    claim = tracking.claim_alert(conn, fingerprint_value=alert_fp, claimed_at=NOW, lease_seconds=1)
    assert tracking.expire_alert_claims(conn, now=NOW + timedelta(seconds=2)) == 1
    sent = []
    assert (
        tracking.deliver_claimed_alert(
            conn,
            fingerprint_value=alert_fp,
            generation=claim["generation"],
            token=claim["token"],
            sender=lambda destination, message: sent.append(message),
            attempted_at=NOW + timedelta(seconds=2),
        )
        == "LOST_FENCE"
    )
    assert sent == []
    assert conn.execute("SELECT status FROM research_alert_delivery").fetchone()[0] == (
        "UNKNOWN_AFTER_SEND"
    )
    conn.close()


def test_open_position_observations_are_immutable_replay_safe_and_do_not_close():
    conn = migrated()
    position_id, _opened_at = open_position(conn)
    exit_day = date(2026, 8, 30)
    exit_report = report(exit_day, close=98.0)
    exit_report["signals"][0].update(action="exit", ema_fast=98.0, ema_slow=99.0)
    exit_report["signals"][0].pop("sizing")
    tracking.persist_signal_run(
        conn,
        exit_report,
        watchlist_run_id=watchlist(conn, exit_day, "exit-watch"),
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        recorded_at=dt(2026, 8, 30, 12, tzinfo=UTC),
    )
    signal_id = conn.execute("SELECT event_id FROM signal_event WHERE action='EXIT'").fetchone()[0]
    source_at = dt(2026, 8, 30, tzinfo=UTC)
    first = tracking.persist_position_observation(
        conn,
        position_id=position_id,
        observation_type="EMA_EXIT",
        source_at=source_at,
        observed_close=98.0,
        observed_ema10=98.0,
        observed_ema21=99.0,
        signal_event_id=signal_id,
        methodology_version=tracking.METHODOLOGY,
        recorded_at=source_at,
    )
    replay = tracking.persist_position_observation(
        conn,
        position_id=position_id,
        observation_type="EMA_EXIT",
        source_at=source_at,
        observed_close=98.0,
        observed_ema10=98.0,
        observed_ema21=99.0,
        signal_event_id=signal_id,
        methodology_version=tracking.METHODOLOGY,
        recorded_at=source_at,
    )
    assert replay == {**first, "status": "REPLAY"}
    conn.execute(
        "INSERT INTO stock_price (symbol,trade_date,close,source,fetched_at) VALUES (?,?,?,?,?)",
        ["SAFE", exit_day, 98.0, "fixture-price", source_at - timedelta(hours=6)],
    )
    price_id = tracking.persist_market_price_evidence(
        conn,
        symbol="SAFE",
        trade_date=exit_day,
        cutoff_at=source_at + timedelta(hours=1),
        recorded_at=source_at + timedelta(hours=1),
    )
    stop = tracking.persist_position_observation(
        conn,
        position_id=position_id,
        observation_type="BELOW_ENTRY_SIZING_STOP",
        source_at=source_at + timedelta(hours=1),
        observed_close=98.0,
        price_evidence_id=price_id,
        methodology_version=tracking.METHODOLOGY,
        recorded_at=source_at + timedelta(hours=1),
    )
    assert stop["status"] == "ACCEPTED"
    assert conn.execute(
        "SELECT current_state FROM research_position WHERE position_id=?", [position_id]
    ).fetchone() == ("OPEN_CONFIRMED",)
    assert (
        tracking.enqueue_change_alerts(conn, destination="fixture-chat", recorded_at=source_at) == 2
    )
    conn.close()


def test_observations_require_open_matching_lifecycle_and_valid_stop():
    conn = migrated()
    tracking.persist_signal_run(
        conn,
        report(),
        watchlist_run_id=watchlist(conn),
        methodology_version=tracking.METHODOLOGY,
        semantic_config=CONFIG,
        recorded_at=NOW,
    )
    position_id = conn.execute("SELECT position_id FROM research_position").fetchone()[0]
    with pytest.raises(ValueError, match="OPEN_CONFIRMED"):
        tracking.persist_position_observation(
            conn,
            position_id=position_id,
            observation_type="BELOW_ENTRY_SIZING_STOP",
            source_at=NOW + timedelta(days=1),
            observed_close=98,
            price_evidence_id="price",
            methodology_version=tracking.METHODOLOGY,
            recorded_at=NOW + timedelta(days=1),
        )
    conn.close()


def test_broker_reconciliation_is_exact_integrity_gated_and_never_transitions():
    conn = migrated()
    position_id, _ = open_position(conn)
    run_id = broker_snapshot(
        conn,
        [
            broker_holding("OTHER"),
            broker_holding("BSEONLY", exchange="BSE"),
            broker_holding("ZERO", quantity=0),
        ],
    )
    source_at = dt(2026, 8, 30, 12, tzinfo=UTC)
    result = tracking.persist_broker_reconciliation(
        conn,
        broker_run_id=run_id,
        source_at=source_at,
        methodology_version=tracking.METHODOLOGY,
        mapping_policy_version="exact-nse-2026.1",
        max_age_days=1,
        recorded_at=source_at,
    )
    assert result == {"status": "ACCEPTED", "event_count": 2}
    assert set(
        conn.execute(
            "SELECT symbol,reconciliation_type FROM broker_reconciliation_event"
        ).fetchall()
    ) == {("SAFE", "CONFIRMED_MISSING"), ("OTHER", "UNTRACKED_PRESENT")}
    assert tracking.persist_broker_reconciliation(
        conn,
        broker_run_id=run_id,
        source_at=source_at,
        methodology_version=tracking.METHODOLOGY,
        mapping_policy_version="exact-nse-2026.1",
        max_age_days=1,
        recorded_at=source_at,
    ) == {"status": "REPLAY", "event_count": 0}
    with pytest.raises(tracking.TrackingConflict, match="different content"):
        tracking.persist_broker_reconciliation(
            conn,
            broker_run_id=run_id,
            source_at=source_at,
            methodology_version=tracking.METHODOLOGY,
            mapping_policy_version="changed-policy",
            max_age_days=1,
            recorded_at=source_at,
        )
    assert conn.execute(
        "SELECT current_state FROM research_position WHERE position_id=?", [position_id]
    ).fetchone() == ("OPEN_CONFIRMED",)
    assert (
        tracking.enqueue_change_alerts(conn, destination="fixture-chat", recorded_at=source_at) == 2
    )
    conn.close()


def test_broker_reconciliation_rejects_stale_nonlatest_and_corrupt_snapshots():
    conn = migrated()
    old = broker_snapshot(conn, [], fetched_at=dt(2026, 8, 28, 3, tzinfo=UTC))
    latest = broker_snapshot(conn, [], fetched_at=dt(2026, 8, 30, 3, tzinfo=UTC))
    kwargs = {
        "source_at": dt(2026, 8, 30, 4, tzinfo=UTC),
        "methodology_version": tracking.METHODOLOGY,
        "mapping_policy_version": "exact-nse-2026.1",
        "max_age_days": 1,
        "recorded_at": dt(2026, 8, 30, 4, tzinfo=UTC),
    }
    with pytest.raises(ValueError, match="latest"):
        tracking.persist_broker_reconciliation(conn, broker_run_id=old, **kwargs)
    conn.execute("UPDATE broker_snapshot_run SET holding_count=1 WHERE run_id=?", [latest])
    with pytest.raises(ValueError, match="integrity"):
        tracking.persist_broker_reconciliation(conn, broker_run_id=latest, **kwargs)
    assert conn.execute("SELECT count(*) FROM broker_reconciliation_event").fetchone() == (0,)
    conn.close()


def test_broker_active_lifecycle_namespace_spans_methodologies():
    conn = migrated()
    open_position(conn)
    other_method = "tracking-2026.2"
    tracking.register_methodology(conn, other_method, CONFIG, registered_at=NOW)
    run_id = broker_snapshot(conn, [broker_holding("SAFE")])
    source_at = dt(2026, 8, 30, 12, tzinfo=UTC)
    assert tracking.persist_broker_reconciliation(
        conn,
        broker_run_id=run_id,
        source_at=source_at,
        methodology_version=other_method,
        mapping_policy_version="exact-nse-2026.1",
        max_age_days=1,
        recorded_at=source_at,
    ) == {"status": "REPLAY", "event_count": 0}
    conn.close()


def test_new_event_schema_rejects_orphan_positions_and_future_broker_fetch():
    conn = migrated()
    with pytest.raises(Exception, match="foreign key"):
        conn.execute(
            "INSERT INTO position_observation_event VALUES "
            "('orphan','missing','BELOW_ENTRY_SIZING_STOP',NULL,'price',?,1,NULL,NULL,?,?,?)",
            [NOW, tracking.METHODOLOGY, NOW, "content"],
        )
    future_run = broker_snapshot(conn, [], fetched_at=dt(2026, 8, 30, 23, tzinfo=UTC))
    with pytest.raises(ValueError, match="future-dated"):
        tracking.persist_broker_reconciliation(
            conn,
            broker_run_id=future_run,
            source_at=dt(2026, 8, 30, 4, tzinfo=UTC),
            methodology_version=tracking.METHODOLOGY,
            mapping_policy_version="exact-nse-2026.1",
            max_age_days=1,
            recorded_at=dt(2026, 8, 30, 4, tzinfo=UTC),
        )
    conn.close()
