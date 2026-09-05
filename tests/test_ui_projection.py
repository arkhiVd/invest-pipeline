import hashlib
import json
import multiprocessing as mp
import os
import stat
from datetime import UTC, date
from datetime import datetime as dt

import duckdb
import pytest

from invest import accounting, db, kite, ranking, tracking, ui_projection


def source_db(path):
    conn = db.connect(str(path))
    db.init_schema(conn)
    tracking.install_schema(conn)
    ranking.install_schema(conn)
    accounting.install_schema(conn)
    return conn


def broker_fixture(conn):
    return kite.store_snapshot(
        conn,
        {"user_id": "DENIED-USER-ID"},
        [
            {
                "exchange": "NSE",
                "tradingsymbol": "SAFE",
                "product": "CNC",
                "instrument_token": 123456,
                "isin": "INE000A00001",
                "quantity": 2,
                "t1_quantity": 0,
                "used_quantity": 0,
                "average_price": 10,
                "last_price": 12,
                "close_price": 11,
                "pnl": 4,
                "day_change": 1,
                "day_change_percentage": 9.09,
            }
        ],
        {"net": [], "day": []},
        [
            {
                "tradingsymbol": "INF000000001",
                "fund": "Safe Fund",
                "quantity": 3,
                "pledged_quantity": 0,
                "average_price": 10,
                "last_price": 11,
                "pnl": 3,
                "last_price_date": "2026-08-29",
            }
        ],
        fetched_at=dt(2026, 8, 29, 3, tzinfo=UTC),
    )


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hold_writer(path, ready, release):
    conn = duckdb.connect(str(path), config={"lock_configuration": True})
    conn.execute("BEGIN TRANSACTION")
    conn.execute("UPDATE ingest_watermark SET detail='held'")
    ready.set()
    release.wait(10)
    conn.execute("ROLLBACK")
    conn.close()


def test_projection_is_allowlisted_private_and_read_only(tmp_path):
    source = tmp_path / "source.duckdb"
    output = tmp_path / "ui.duckdb"
    conn = source_db(source)
    broker_fixture(conn)
    conn.execute(
        "INSERT INTO stock_fundamentals "
        "(symbol,as_of,source,raw_json,methodology_version,fetched_at) "
        "VALUES ('SAFE','2026-08-29','fixture','DENIED-RAW-JSON','m1',current_timestamp)"
    )
    conn.close()

    result = ui_projection._publish(source, output)

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert result["row_counts"]["broker_holding"] == 1
    projected = duckdb.connect(str(output), read_only=True)
    tables = {row[0] for row in projected.execute("SHOW TABLES").fetchall()}
    assert tables == set(ui_projection.DATASETS) | set(ui_projection.DERIVED_COLUMNS) | {
        "projection_metadata"
    }
    schema = json.dumps(
        projected.execute(
            "SELECT table_name,column_name FROM information_schema.columns ORDER BY 1,2"
        ).fetchall()
    ).lower()
    values = json.dumps(
        {
            table: projected.execute(
                f'SELECT {", ".join(dataset.columns)} FROM "{table}"'
            ).fetchall()
            for table, dataset in ui_projection.DATASETS.items()
        },
        default=str,
    )
    projected.close()
    for denied in ("account_sha256", "instrument_token", "isin", "raw_json"):
        assert denied not in schema
    assert "article_id" in ui_projection.HASH_COLUMNS
    assert not (ui_projection.HASH_COLUMNS & ui_projection.DENIED_NAMES)
    for denied_value in (
        "DENIED-USER-ID",
        "INE000A00001",
        "DENIED-RAW-JSON",
        "123456",
        "INF000000001",
    ):
        assert denied_value not in values
    with pytest.raises(duckdb.InvalidInputException):
        readonly = duckdb.connect(str(output), read_only=True)
        try:
            readonly.execute("CREATE TABLE forbidden(value INT)")
        finally:
            readonly.close()


def test_failed_verification_preserves_last_good(tmp_path, monkeypatch):
    source = tmp_path / "source.duckdb"
    output = tmp_path / "ui.duckdb"
    source_db(source).close()
    ui_projection._publish(source, output)
    before = file_hash(output)

    def reject(*_args):
        raise RuntimeError("injected verification failure")

    monkeypatch.setattr(ui_projection, "_verify", reject)
    with pytest.raises(RuntimeError, match="injected"):
        ui_projection._publish(source, output)
    assert file_hash(output) == before
    assert not list(tmp_path.glob(".ui.duckdb.tmp.*"))


def test_protected_value_in_allowlisted_text_preserves_last_good(tmp_path):
    source = tmp_path / "source.duckdb"
    output = tmp_path / "ui.duckdb"
    conn = source_db(source)
    conn.close()
    ui_projection._publish(source, output)
    before = file_hash(output)
    conn = duckdb.connect(str(source))
    conn.execute(
        "INSERT INTO ingest_watermark VALUES "
        "('fixture','2026-08-29','leak@example.invalid',current_timestamp)"
    )
    conn.close()
    with pytest.raises(RuntimeError, match="protected value"):
        ui_projection._publish(source, output)
    assert file_hash(output) == before


@pytest.mark.parametrize("field", ["account_scope", "assumptions_json"])
def test_accounting_projection_rejects_protected_text(tmp_path, field):
    source = tmp_path / "source.duckdb"
    output = tmp_path / "ui.duckdb"
    conn = source_db(source)
    scope = "leak@example.invalid" if field == "account_scope" else "US"
    conn.execute(
        "INSERT INTO portfolio_account VALUES (?,?,?,?,?,current_timestamp)",
        ["account", "VESTED", scope, "USD", "UNPROVEN"],
    )
    assumptions = '["leak@example.invalid"]' if field == "assumptions_json" else "[]"
    conn.execute(
        "INSERT INTO accounting_completeness VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            "evidence",
            "account",
            dt.now(UTC),
            date(2026, 1, 1),
            date(2026, 8, 31),
            "MISSING",
            "MISSING",
            "MISSING",
            "MISSING",
            "MISSING",
            "MISSING",
            assumptions,
            "[]",
            "[]",
            "fixture",
        ],
    )
    conn.close()
    with pytest.raises(RuntimeError, match="protected value"):
        ui_projection._publish(source, output)
    assert not output.exists()


def test_invalid_article_hash_preserves_last_good(tmp_path):
    source = tmp_path / "source.duckdb"
    output = tmp_path / "ui.duckdb"
    conn = source_db(source)
    conn.close()
    ui_projection._publish(source, output)
    before = file_hash(output)
    conn = duckdb.connect(str(source))
    conn.execute(
        "INSERT INTO news_article VALUES "
        "(repeat('a',64),'safe title','https://example.invalid/safe','fixture',"
        "'fixture',current_timestamp,current_timestamp)"
    )
    conn.close()
    with pytest.raises(RuntimeError, match="invalid news article identifier"):
        ui_projection._publish(source, output)
    assert file_hash(output) == before


def test_corrupt_broker_snapshot_preserves_last_good(tmp_path):
    source = tmp_path / "source.duckdb"
    output = tmp_path / "ui.duckdb"
    conn = source_db(source)
    stored = broker_fixture(conn)
    conn.close()
    ui_projection._publish(source, output)
    before = file_hash(output)
    conn = duckdb.connect(str(source))
    conn.execute(
        "UPDATE broker_snapshot_run SET holding_count=99 WHERE run_id=?",
        [stored["run_id"]],
    )
    conn.close()
    with pytest.raises(RuntimeError, match="broker snapshot failed integrity"):
        ui_projection._publish(source, output)
    assert file_hash(output) == before


def test_writer_contention_fails_without_retry_or_replace(tmp_path):
    source = tmp_path / "source.duckdb"
    output = tmp_path / "ui.duckdb"
    conn = source_db(source)
    conn.execute(
        "INSERT INTO ingest_watermark VALUES ('fixture','2026-08-29','ready',current_timestamp)"
    )
    conn.close()
    ui_projection._publish(source, output)
    before = file_hash(output)
    ready, release = mp.Event(), mp.Event()
    process = mp.Process(target=hold_writer, args=(source, ready, release))
    process.start()
    assert ready.wait(5)
    try:
        with pytest.raises(duckdb.Error, match="lock"):
            ui_projection._publish(source, output)
    finally:
        release.set()
        process.join(5)
    assert process.exitcode == 0
    assert file_hash(output) == before


def test_phase9_projection_is_the_approved_minimal_privacy_allowlist():
    approved = {
        "signal_run": (
            "run_id",
            "source_as_of",
            "canonical_cutoff",
            "recorded_at",
            "methodology_version",
            "status",
            "scanned_count",
            "signal_count",
        ),
        "signal_event": (
            "event_id",
            "run_id",
            "symbol",
            "signal_date",
            "source_as_of",
            "action",
            "close",
            "ema10",
            "ema21",
            "quantity",
            "sizing_stop",
            "capital_to_deploy",
            "maximum_loss_at_stop",
            "sizing_gap_reason",
            "methodology_version",
            "recorded_at",
        ),
        "research_position": (
            "position_id",
            "market",
            "symbol",
            "origin_signal_event_id",
            "current_state",
            "state_source_at",
            "state_event_id",
            "methodology_version",
            "entry_sizing_stop",
            "created_at",
            "updated_at",
        ),
        "position_state_event": (
            "event_id",
            "position_id",
            "from_state",
            "to_state",
            "source_at",
            "recorded_at",
            "methodology_version",
            "actor",
            "evidence_type",
        ),
        "screen_membership_event": (
            "event_id",
            "run_id",
            "screen_id",
            "symbol",
            "source_as_of",
            "recorded_at",
            "methodology_version",
            "event_type",
            "previous_run_id",
        ),
        "watchlist_run": (
            "run_id",
            "index_name",
            "source_as_of",
            "canonical_cutoff",
            "recorded_at",
            "methodology_version",
            "status",
            "universe_count",
            "selected_count",
        ),
        "watchlist_symbol_result": (
            "result_id",
            "run_id",
            "symbol",
            "source_as_of",
            "methodology_version",
            "result",
            "rank",
            "close",
            "beta",
            "observations",
            "evidence_as_of",
        ),
    }
    assert {name: ui_projection.DATASETS[name].columns for name in approved} == approved
    excluded = {
        "operator_note",
        "reason_json",
        "detail_json",
        "destination",
        "message",
        "claim_token",
        "account_sha256",
        "isin",
    }
    assert not excluded & {column for dataset in approved.values() for column in dataset}
    assert (
        not {"research_alert_delivery", "broker_reconciliation_event", "position_observation_event"}
        & ui_projection.DATASETS.keys()
    )


def test_phase10_projection_is_bounded_and_excludes_internal_fingerprints():
    approved = {
        "ranking_methodology": (
            "methodology_version",
            "semantic_config_fingerprint",
            "registered_at",
        ),
        "ranking_run": (
            "run_id",
            "source_as_of",
            "recorded_at",
            "methodology_version",
            "survivor_count",
            "available_count",
        ),
        "ranking_symbol": (
            "run_id",
            "symbol",
            "score",
            "research_rank",
            "evidence_completeness",
            "status",
            "missing_components_json",
        ),
        "ranking_component": (
            "run_id",
            "symbol",
            "component",
            "normalized_value",
            "component_weight",
            "weighted_contribution",
            "missing_status",
        ),
        "ranking_input": (
            "run_id",
            "symbol",
            "field",
            "raw_value",
            "unit",
            "source",
            "source_as_of",
            "normalization_cohort",
            "cohort_size",
            "transform",
            "direction",
            "normalized_value",
            "component",
            "input_weight",
            "component_weight",
            "weighted_contribution",
            "missing_status",
        ),
    }
    assert {name: ui_projection.DATASETS[name].columns for name in approved} == approved
    projected_columns = {column for columns in approved.values() for column in columns}
    assert "input_fingerprint" not in projected_columns
    assert "content_fingerprint" not in projected_columns
    assert all(
        "LIMIT" in ui_projection._select(name, ui_projection.DATASETS[name]).upper()
        for name in ("ranking_run", "ranking_symbol", "ranking_component", "ranking_input")
    )
    for name in ("ranking_symbol", "ranking_component", "ranking_input"):
        selection = ui_projection._select(name, ui_projection.DATASETS[name]).upper()
        assert selection.index("SOURCE_AS_OF") < selection.rindex("LIMIT")


def test_corrupt_latest_ranking_preserves_last_good(tmp_path):
    source = tmp_path / "source.duckdb"
    output = tmp_path / "ui.duckdb"
    conn = source_db(source)
    ranking.persist(
        conn,
        {
            "methodology_version": ranking.METHODOLOGY,
            "source_as_of": date(2026, 8, 29),
            "survivors": [],
        },
        recorded_at=dt(2026, 8, 29, 4, tzinfo=UTC),
    )
    conn.close()
    ui_projection._publish(source, output)
    before = file_hash(output)
    conn = duckdb.connect(str(source))
    conn.execute("UPDATE ranking_run SET survivor_count=99")
    conn.close()
    with pytest.raises(RuntimeError, match="ranking run failed integrity"):
        ui_projection._publish(source, output)
    assert file_hash(output) == before


def test_queries_are_fixed_and_bounded():
    assert all(
        "SELECT *" not in ui_projection._select(table, dataset).upper()
        for table, dataset in ui_projection.DATASETS.items()
    )
    assert "LIMIT 500" in ui_projection.DATASETS["news_article"].suffix
    assert "INTERVAL 400 DAY" in ui_projection.DATASETS["stock_price"].suffix
    assert ui_projection.publish.__code__.co_argcount == 0
    assert os.fspath(ui_projection.SOURCE_DB).endswith("/data/invest.duckdb")
